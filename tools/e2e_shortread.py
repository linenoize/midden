"""End-to-end test for the Phase-2 hash-integrity guard.

Asserts:
  - hash_file(expected_size=correct) returns a digest; wrong expected_size raises
    ShortReadError; an empty file with expected_size=0 hashes fine (the 869 legit
    zero-byte files must not regress).
  - the ingest walk, when a file reads short, emits a `short_read` event, counts
    it, and does NOT index the file (a bogus partial hash is worse than omission).

A real placeholder/sparse short read isn't portably fabricable (normal
filesystems zero-fill a hole, so read() returns the full logical size). The
ingest-level case therefore forces a short read by monkeypatching hash_file —
this exercises our wiring (event + skip + stat), which is the contract under test.

Run:  python tools/e2e_shortread.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midden import ingest as ingest_mod
from midden.ingest import hash_file, ingest, ShortReadError
from midden.store import Store

ok = True


def check(name, cond):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    ok = ok and cond


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_shortread_"))

    # ---- unit: hash_file guard ----
    f = tmp / "data.bin"
    f.write_bytes(b"x" * 5000)
    digest = hash_file(f, expected_size=5000)
    check("correct expected_size returns a digest", isinstance(digest, str) and len(digest) > 0)
    check("hash_file(None) still works (back-compat)", hash_file(f) == digest)

    raised = False
    try:
        hash_file(f, expected_size=9999)
    except ShortReadError as e:
        raised = True
        check("ShortReadError carries expected/got", e.expected == 9999 and e.got == 5000)
    check("under-size mismatch raises ShortReadError", raised)

    raised = False
    try:
        hash_file(f, expected_size=1)
    except ShortReadError:
        raised = True
    check("over-read (file larger than expected) raises", raised)

    empty = tmp / "empty.bin"
    empty.write_bytes(b"")
    check("empty file with expected_size=0 hashes (no false short_read)",
          isinstance(hash_file(empty, expected_size=0), str))

    check("ShortReadError is an OSError subclass", issubclass(ShortReadError, OSError))

    # ---- ingest wiring: a short read is reported + skipped, not indexed ----
    corpus = tmp / "corpus"
    corpus.mkdir()
    good = corpus / "good.txt"
    good.write_text("hello world")
    bad = corpus / "bad.bin"
    bad.write_bytes(b"y" * 4096)

    real_hash_file = ingest_mod.hash_file

    def fake_hash_file(path, chunk_size=4 * 1024 * 1024, expected_size=None):
        if Path(path).name == "bad.bin":
            raise ShortReadError(path, expected_size or 0, 7)  # simulate a short read
        return real_hash_file(path, chunk_size=chunk_size, expected_size=expected_size)

    ingest_mod.hash_file = fake_hash_file
    try:
        db = tmp / "index.sqlite"
        store = Store(db)
        kinds = []
        short_paths = []
        for ev in ingest(corpus, store, label="shortread-e2e"):
            kinds.append(ev.kind)
            if ev.kind == "short_read":
                short_paths.append(ev.path)
        check("exactly one short_read event emitted", kinds.count("short_read") == 1)
        check("short_read event names the offending file",
              short_paths == ["bad.bin"])
        check("the good file was hashed", kinds.count("hashed") == 1)

        indexed = {r["path"] for r in store.conn.execute("SELECT path FROM paths")}
        check("short-read file is NOT indexed", "bad.bin" not in indexed)
        check("good file IS indexed", "good.txt" in indexed)
        store.close()
    finally:
        ingest_mod.hash_file = real_hash_file

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
