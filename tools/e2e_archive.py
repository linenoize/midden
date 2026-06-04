"""End-to-end test for read-only archive inspection.

Asserts:
  - is_archive() recognizes zip/7z/rar/tar.gz and rejects non-archives,
  - a .zip is listed via the stdlib (names, sizes, totals, dirs),
  - the 7z -slt parser yields correct entries (deterministic, sample blob),
  - if a 7z CLI exists, a real generated .7z lists via the 7z backend,
  - GET /api/archive/{path_id} returns the listing for an indexed archive,
    404 for an unknown path, 400 for a non-archive.

Run:  python tools/e2e_archive.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from midden import archives
from midden.ingest import ingest
from midden.server import create_app
from midden.store import Store

ok = True


def check(name, cond):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    ok = ok and cond


SLT_SAMPLE = """
7-Zip 23.01 (x64)

Listing archive: x.7z

--
Path = x.7z
Type = 7z
Physical Size = 4096

----------
Path = data
Folder = +
Size = 0
Attributes = D....

Path = data/a.bin
Folder = -
Size = 1000
Encrypted = -

Path = readme.txt
Folder = -
Size = 5
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_arch_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)

    # ---- is_archive detection ----
    check("is_archive .zip", archives.is_archive("pack.zip"))
    check("is_archive .RAR (case)", archives.is_archive("PACK.RAR"))
    check("is_archive .7z", archives.is_archive("x.7z"))
    check("is_archive .tar.gz compound", archives.is_archive("bundle.tar.gz"))
    check("not archive .txt", not archives.is_archive("notes.txt"))

    # ---- 7z -slt parser (deterministic) ----
    entries = archives._parse_7z_slt(SLT_SAMPLE)
    by = {e["name"]: e for e in entries}
    check("slt parser finds 3 entries", len(entries) == 3)
    check("slt parser marks data/ as dir", by.get("data", {}).get("is_dir") is True)
    check("slt parser reads data/a.bin size", by.get("data/a.bin", {}).get("size") == 1000)
    check("slt parser reads readme.txt size", by.get("readme.txt", {}).get("size") == 5)

    # ---- real .zip via stdlib ----
    zpath = corpus / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("readme.txt", "hello")          # 5 bytes
        z.writestr("data/a.bin", b"\x00" * 1000)    # 1000 bytes
        z.writestr("data/b.bin", b"\x01" * 2000)    # 2000 bytes
    r = archives.list_archive(zpath)
    names = {e["name"] for e in r["entries"]}
    check("zip listed ok via stdlib", r["ok"] and r["backend"] == "zipfile")
    check("zip contains readme.txt + data/a.bin", "readme.txt" in names and "data/a.bin" in names)
    check("zip total uncompressed == 3005", r["total_size"] == 3005)
    check("zip not flagged encrypted", r["encrypted"] is False)

    # ---- real .7z via CLI, if available ----
    seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if seven:
        src = tmp / "src"
        (src / "sub").mkdir(parents=True)
        (src / "readme.txt").write_text("hello")
        (src / "sub" / "a.bin").write_bytes(b"\x00" * 1234)
        szpath = corpus / "made.7z"
        subprocess.run([seven, "a", "-y", str(szpath), "."], cwd=src,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        r7 = archives.list_archive(szpath)
        n7 = {e["name"].replace("\\", "/"): e for e in r7["entries"]}
        check("7z listed ok via 7z backend", r7["ok"] and r7["backend"] == "7z")
        check("7z contains readme.txt", "readme.txt" in n7)
        check("7z reports sub/a.bin size 1234",
              n7.get("sub/a.bin", {}).get("size") == 1234)
    else:
        print("  (no 7z CLI — real .7z listing skipped)")

    # ---- endpoint ----
    (corpus / "notes.txt").write_text("not an archive")
    db = tmp / "index.sqlite"
    store = Store(db)
    for _ in ingest(corpus, store, label="archive-e2e"):
        pass

    def path_id_of(rel):
        row = store.conn.execute("SELECT id FROM paths WHERE path=?", (rel,)).fetchone()
        return row["id"] if row else None

    zip_pid = path_id_of("bundle.zip")
    txt_pid = path_id_of("notes.txt")
    store.close()

    client = TestClient(create_app(db))
    res = client.get(f"/api/archive/{zip_pid}")
    check("endpoint lists the zip (200)", res.status_code == 200)
    if res.status_code == 200:
        body = res.json()
        check("endpoint payload ok with entries", body["ok"] and len(body["entries"]) >= 3)
    check("endpoint 400 on a non-archive", client.get(f"/api/archive/{txt_pid}").status_code == 400)
    check("endpoint 404 on unknown path", client.get("/api/archive/99999999").status_code == 404)

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
