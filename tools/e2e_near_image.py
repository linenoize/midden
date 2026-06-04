"""End-to-end test for the Phase-3 near-image clustering fix.

Builds a temp tree of generated images, ingests, computes perceptual signatures,
materializes near_image clusters, and asserts the failure modes that produced the
9,581-member blob on real data are gone:

  - blank / near-uniform images (degenerate dHash) are EXCLUDED, never clustered.
  - a genuine near-dup chain of same-shape images DOES cluster together.
  - an image of a very different aspect ratio is NOT pulled into that cluster.
  - no cluster exceeds MAX_NEAR_CLUSTER.
  - re-running is idempotent (0 new clusters).

Run:  python tools/e2e_near_image.py   (requires Pillow)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midden import near, phash
from midden.ingest import ingest
from midden.store import Store

ok = True


def check(name, cond):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    ok = ok and cond


def main() -> int:
    if not phash.PIL_AVAILABLE:
        print("Pillow not installed — skipping (vacuous pass).")
        return 0
    from PIL import Image, ImageDraw

    tmp = Path(tempfile.mkdtemp(prefix="midden_nearimg_"))
    corpus = tmp / "corpus"
    corpus.mkdir()

    # 30 blank / solid-color squares -> degenerate dHash (must be excluded).
    for i in range(30):
        shade = (i * 8) % 256
        Image.new("RGB", (256, 256), (shade, shade, shade)).save(corpus / f"blank_{i:02d}.png")

    # A near-dup chain: ONE structured image re-saved at different JPEG qualities.
    # This is the canonical near-dup case — byte-distinct files (distinct content
    # hashes), same square shape, near-identical dHash (Hamming ≤ a few bits).
    base = Image.new("RGB", (256, 256), (20, 20, 20))
    d = ImageDraw.Draw(base)
    d.rectangle([40, 40, 200, 200], fill=(220, 180, 60))
    d.ellipse([80, 80, 160, 160], fill=(40, 80, 200))
    d.line([0, 0, 256, 256], fill=(255, 255, 255), width=5)
    for q in (95, 85, 75, 60):
        base.save(corpus / f"chain_q{q}.jpg", quality=q)
    chain_names = [f"chain_q{q}.jpg" for q in (95, 85, 75, 60)]

    # A structured WIDE image (different aspect) — must NOT join the square chain.
    wide = Image.new("RGB", (640, 200), (10, 10, 10))
    dw = ImageDraw.Draw(wide)
    dw.rectangle([20, 20, 300, 180], fill=(200, 60, 60))
    dw.ellipse([350, 30, 600, 170], fill=(60, 200, 120))
    wide.save(corpus / "wide_unrelated.jpg")

    db = tmp / "index.sqlite"
    store = Store(db)
    for _ in ingest(corpus, store, label="nearimg-e2e"):
        pass

    n_sig = near.compute_image_signatures(store)
    res = near.materialize_near_images(store)
    print(f"  signatures={n_sig}  result={res}")

    clusters = store.list_clusters(include_resolved=True, kinds=("near_image",), limit=999)
    members = []
    for c in clusters:
        full = store.get_cluster(c["id"])
        members.append({m["hash"] for m in full["members"]})

    def hash_of(rel):
        r = store.conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
        return r["hash"] if r else None

    chain_hashes = {hash_of(n) for n in chain_names}
    chain_hashes.discard(None)
    blank_hashes = {hash_of(f"blank_{i:02d}.png") for i in range(30)}
    blank_hashes.discard(None)
    wide_hash = hash_of("wide_unrelated.jpg")

    check("signatures computed for all images", n_sig >= 34)
    check("at least 30 degenerate (blank) dHashes dropped", res["dropped_degenerate"] >= 30)
    check("no cluster exceeds MAX_NEAR_CLUSTER",
          all(len(m) <= near.MAX_NEAR_CLUSTER for m in members))

    blank_in_any = any(m & blank_hashes for m in members)
    check("no blank image appears in any near_image cluster", not blank_in_any)

    chain_clusters = [m for m in members if m & chain_hashes]
    check("the near-dup chain forms (at least 2 chain images cluster)",
          len(chain_clusters) >= 1 and max((len(m & chain_hashes) for m in chain_clusters), default=0) >= 2)
    check("wide image of different aspect is NOT in a chain cluster",
          not any(wide_hash in m for m in chain_clusters))

    # idempotency
    again = near.materialize_near_images(store)
    check("re-materialize creates 0 new clusters", again["created"] == 0)

    store.close()
    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
