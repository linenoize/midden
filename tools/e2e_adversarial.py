"""End-to-end regression test over the adversarial synthetic corpus (Phase 6).

Builds gen_corpus with --adversarial, ingests, clusters, and asserts the
invariants that real data violated — driven by the generated ground truth:

  - blank/uniform images never appear in any near_image cluster,
  - a genuine near-dup chain (same image, different JPEG quality) clusters,
  - an unrelated wide image is NOT pulled into that chain,
  - no near_image cluster exceeds MAX_NEAR_CLUSTER (no mega-blob),
  - a renamed-identical archive is one exact-dup group whose rationale flags
    that the filenames differ (matched by content).

Run:  python tools/e2e_adversarial.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midden import near
from midden.ingest import ingest
from midden.store import Store
from tools.gen_corpus import build

ok = True


def check(name, cond):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    ok = ok and cond


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_adv_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"
    truth = build(corpus, seed=123, scale=1.0, adversarial=True)

    store = Store(db)
    for _ in ingest(corpus, store, label="adversarial-e2e"):
        pass
    store.materialize_exact_clusters()
    res = near.recluster(store)
    print(f"  recluster: near_image+{res['near_image_clusters']} "
          f"dropped_degenerate={res['dropped_degenerate']} "
          f"dropped_oversize={res['dropped_oversize']}")

    def hash_of(rel):
        r = store.conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
        return r["hash"] if r else None

    # ---- renamed-identical archive: one exact group, names differ ----
    ri = truth["renamed_identical"]
    check("renamed-identical ground truth present", len(ri) == 2)
    h_a, h_b = hash_of(ri[0]), hash_of(ri[1])
    check("renamed-identical files share one content hash", h_a is not None and h_a == h_b)
    cidrow = store.conn.execute(
        "SELECT c.id FROM clusters c JOIN cluster_members cm ON cm.cluster_id=c.id "
        "WHERE c.kind='exact' AND cm.hash=?", (h_a,)).fetchone()
    check("renamed-identical forms an exact cluster", cidrow is not None)
    if cidrow:
        rat = (store.get_cluster(cidrow["id"]).get("rationale") or "").lower()
        check("exact rationale flags names differ / content match",
              "differ" in rat and "content" in rat)

    # ---- image invariants ----
    if not truth.get("pil_available"):
        print("  (Pillow absent — image invariants skipped)")
    else:
        near_clusters = store.list_clusters(include_resolved=True,
                                            kinds=("near_image",), limit=9999)
        member_sets = [{m["hash"] for m in store.get_cluster(c["id"])["members"]}
                       for c in near_clusters]

        blank_hashes = {hash_of(p) for p in truth["blank_images"]}
        blank_hashes.discard(None)
        chain_hashes = {hash_of(p) for p in truth["near_image_chain"]["members"]}
        chain_hashes.discard(None)
        wide_hash = hash_of(truth["near_image_chain"]["unrelated"])

        check("blank images were dropped as degenerate",
              res["dropped_degenerate"] >= len(blank_hashes) and len(blank_hashes) >= 20)
        check("no blank image in any near_image cluster",
              not any(m & blank_hashes for m in member_sets))
        check("no near_image cluster exceeds MAX_NEAR_CLUSTER",
              all(len(m) <= near.MAX_NEAR_CLUSTER for m in member_sets))

        chain_clusters = [m for m in member_sets if m & chain_hashes]
        check("near-dup chain clusters together (>=2 members)",
              chain_clusters and max(len(m & chain_hashes) for m in chain_clusters) >= 2)
        check("unrelated wide image is not in the chain cluster",
              not any(wide_hash in m for m in chain_clusters))

    store.close()
    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
