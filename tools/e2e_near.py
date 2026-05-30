"""End-to-end test for phase-2 near-duplicate detection (doc_version).

Builds the synthetic corpus, ingests, computes text signatures, materializes
doc_version clusters, and asserts:
  - each ground-truth version chain is fully contained in exactly one cluster
    (recall), and
  - no doc_version cluster merges two distinct ground-truth chains, and
  - the multi-hash review actions (keep / purge_all / undo) work via the API.

Run:  python tools/e2e_near.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midden import near
from midden.ingest import ingest
from midden.server import create_app
from midden.store import Store
from tools.gen_corpus import build


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_near_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"
    truth = build(corpus, seed=42, scale=1.0)

    store = Store(db)
    for _ in ingest(corpus, store, label="near-e2e"):
        pass

    def hash_of(rel: str):
        row = store.conn.execute("SELECT hash FROM paths WHERE path=?", (rel,)).fetchone()
        return row["hash"] if row else None

    gt_chains = []
    for c in truth["doc_version_chains"]:
        hs = {hash_of(m) for m in c["members"]}
        hs.discard(None)
        gt_chains.append(hs)

    result = near.recluster(store)
    print(f"recluster: {result}")

    dv = store.list_clusters(include_resolved=True, kinds=("doc_version",), limit=999)
    dv_sets = []
    for c in dv:
        full = store.get_cluster(c["id"])
        dv_sets.append({m["hash"] for m in full["members"]})

    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    print(f"\nground-truth chains: {len(gt_chains)} | doc_version clusters found: {len(dv_sets)}")
    check("text signatures computed for docs", result["text_signatures"] > 0)
    check("at least one doc_version cluster found", len(dv_sets) >= 1)

    for i, chain in enumerate(gt_chains):
        containers = [s for s in dv_sets if chain <= s]
        names = sorted(
            store.conn.execute("SELECT path FROM paths WHERE hash=?", (h,)).fetchone()["path"].split("/")[-1]
            for h in chain
        )
        check(f"chain {i} ({len(chain)} versions) fully in one cluster: {names}",
              len(containers) == 1)

    def merges_two(s):
        return sum(1 for chain in gt_chains if chain & s) >= 2
    check("no doc_version cluster merges two distinct chains",
          not any(merges_two(s) for s in dv_sets))

    check("image signatures handled without crash (0 expected here)",
          result["image_signatures"] == 0)
    check("near_image clusters: 0 (no real images / no ground truth)",
          result["near_image_clusters"] == 0)

    store.close()

    # ---- multi-hash review actions via the API ----
    app = create_app(db)
    client = TestClient(app)
    all_clusters = client.get("/api/clusters").json()["clusters"]
    dvc = [c for c in all_clusters if c["kind"] == "doc_version"]
    check("doc_version clusters exposed via API", len(dvc) >= 1)

    target = max(dvc, key=lambda c: c["n_members"])
    detail = client.get(f"/api/clusters/{target['id']}").json()
    check("target cluster spans multiple hashes", detail["n_members"] >= 2)
    actives = [p for p in detail["paths"] if p["status"] == "active"]
    n_active = len(actives)
    keep_pid = actives[0]["id"]

    r = client.post(f"/api/clusters/{target['id']}/keep", json={"path_id": keep_pid}).json()
    left = [p for p in r["paths"] if p["status"] == "active"]
    check("keep on multi-hash cluster leaves exactly 1 active", len(left) == 1)
    check("kept the chosen path", left[0]["id"] == keep_pid)
    check("multi-hash cluster marked resolved", r["resolved"] is True)

    u = client.post("/api/undo").json()
    check("undo reports resolve_keep", u["action"] == "resolve_keep")
    back = client.get(f"/api/clusters/{target['id']}").json()
    check("undo restored all active versions",
          len([p for p in back["paths"] if p["status"] == "active"]) == n_active)
    check("undo reopened the cluster", back["resolved"] is False)

    store2 = Store(db, same_thread=False)
    again = near.recluster(store2)
    store2.close()
    check("re-recluster creates 0 new doc_version clusters", again["doc_version_clusters"] == 0)
    check("re-recluster computes 0 new signatures", again["text_signatures"] == 0)

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
