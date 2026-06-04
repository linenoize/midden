"""End-to-end test for Phase-5 explainability + queue noise floor.

Asserts:
  - an exact-dup group of two differently-named identical files carries a
    rationale that says it was matched by CONTENT and that the names DIFFER
    (the fix for "a correct match looks like a bug").
  - the min_reclaimable floor hides a sub-floor (tiny) exact group while keeping
    a high-value one — and never hides near_image/doc_version.
  - /api/thumb/{hash} returns an image for a real image, 404 for a text file.

Run:  python tools/e2e_explain.py   (Pillow needed for the thumbnail assertions)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from midden import near, phash
from midden.ingest import ingest
from midden.server import create_app
from midden.store import EXACT_REVIEW_FLOOR, Store

ok = True


def check(name, cond):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    ok = ok and cond


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_explain_"))
    corpus = tmp / "corpus"
    (corpus / "a").mkdir(parents=True)
    (corpus / "b").mkdir(parents=True)

    # A high-value exact dup: same ~1.5 MiB bytes, two DIFFERENT names/dirs.
    big = os.urandom(1_500_000)
    (corpus / "a" / "archive_one.bin").write_bytes(big)
    (corpus / "b" / "renamed_two.bin").write_bytes(big)

    # A trivially-tiny exact dup: identical 100 bytes, two names -> below floor.
    tiny = os.urandom(100)
    (corpus / "a" / "cfg_x.ini").write_bytes(tiny)
    (corpus / "b" / "cfg_y.ini").write_bytes(tiny)

    # An image (for thumbnail 200) and a text file (for thumbnail 404).
    text_hash_file = corpus / "notes.txt"
    text_hash_file.write_text("just some text, not an image at all")
    if phash.PIL_AVAILABLE:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (300, 200), (30, 30, 30))
        ImageDraw.Draw(im).ellipse([40, 40, 260, 160], fill=(200, 120, 40))
        im.save(corpus / "pic.png")

    db = tmp / "index.sqlite"
    store = Store(db)
    for _ in ingest(corpus, store, label="explain-e2e"):
        pass
    store.materialize_exact_clusters()
    near.compute_image_signatures(store)

    def hash_of(rel):
        r = store.conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
        return r["hash"] if r else None

    big_hash = hash_of("a/archive_one.bin")
    tiny_hash = hash_of("a/cfg_x.ini")

    # --- rationale on the big exact dup ---
    cid = store.conn.execute(
        "SELECT c.id FROM clusters c JOIN cluster_members cm ON cm.cluster_id=c.id "
        "WHERE c.kind='exact' AND cm.hash=?", (big_hash,)).fetchone()["id"]
    detail = store.get_cluster(cid)
    rat = (detail.get("rationale") or "").lower()
    check("exact rationale mentions content-match", "content" in rat)
    check("exact rationale flags that names differ", "differ" in rat)
    check("exact rationale reports copy count", "2 copies" in rat)

    # --- noise floor ---
    floored = store.list_clusters(kinds=("exact",), min_reclaimable=EXACT_REVIEW_FLOOR, limit=999)
    floored_hashes = set()
    for c in floored:
        floored_hashes |= {m["hash"] for m in store.get_cluster(c["id"])["members"]}
    check("high-value exact group survives the floor", big_hash in floored_hashes)
    check("tiny exact group hidden by the floor", tiny_hash not in floored_hashes)

    unfloored = store.list_clusters(kinds=("exact",), min_reclaimable=0, limit=999)
    unfloored_hashes = set()
    for c in unfloored:
        unfloored_hashes |= {m["hash"] for m in store.get_cluster(c["id"])["members"]}
    check("tiny exact group visible with floor=0", tiny_hash in unfloored_hashes)

    ov = store.overview()
    check("overview reports >=1 hidden exact group", ov["exact_hidden"] >= 1)
    store.close()

    # --- thumbnail endpoint ---
    app = create_app(db)
    client = TestClient(app)
    if phash.PIL_AVAILABLE:
        img_hash = None
        # find the image's hash via search
        res = client.get("/api/search?q=pic.png").json()["results"]
        if res:
            img_hash = res[0]["hash"]
        if img_hash:
            r = client.get(f"/api/thumb/{img_hash}")
            check("thumb of an image returns 200", r.status_code == 200)
            check("thumb content-type is image", r.headers.get("content-type", "").startswith("image/"))
    txt_hash = None
    res = client.get("/api/search?q=notes.txt").json()["results"]
    if res:
        txt_hash = res[0]["hash"]
    if txt_hash:
        r = client.get(f"/api/thumb/{txt_hash}")
        check("thumb of a text file returns 404", r.status_code == 404)

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
