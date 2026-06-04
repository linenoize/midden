"""End-to-end test for the phase-1 cluster-review layer.

Builds the synthetic corpus, ingests it, then drives the FastAPI app in-process
(TestClient) through the review actions, asserting against ground truth and the
reversibility invariant. Exit 0 = pass.

Run:  python tools/e2e_review.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midden.ingest import ingest
from midden.store import Store
from midden.server import create_app
from tools.gen_corpus import build


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_e2e_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"

    truth = build(corpus, seed=42, scale=1.0)
    (corpus / "_ground_truth.json").write_text(json.dumps(truth))
    n_truth_groups = len(truth["exact_duplicate_groups"])

    # ingest
    store = Store(db)
    for _ in ingest(corpus, store, label="e2e"):
        pass
    live_groups = store.exact_duplicate_groups()
    store.close()

    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    print(f"ground truth exact-dup groups: {n_truth_groups}")
    print(f"live detected groups:          {len(live_groups)}")
    check("live dup groups == ground truth", len(live_groups) == n_truth_groups)

    # drive the API. The `with` runs the app lifespan, which materializes
    # clusters on startup (bare TestClient(app) would skip it).
    app = create_app(db)
    with TestClient(app) as client:
        ov = client.get("/api/overview").json()
        print(f"overview: {ov['clusters_unresolved']} unresolved clusters, "
              f"{ov['paths_active']} active paths, saveable={ov['bytes_saveable']}")
        check("materialized clusters == dup groups", ov["clusters_unresolved"] == n_truth_groups)
        check("nothing in purgatory yet", ov["paths_purgatory"] == 0)

        clusters = client.get("/api/clusters").json()["clusters"]
        check("API lists all unresolved clusters", len(clusters) == n_truth_groups)
        check("clusters sorted by reclaimable desc",
              all(clusters[i]["reclaimable"] >= clusters[i+1]["reclaimable"]
                  for i in range(len(clusters)-1)))

        # --- action 1: keep first copy of the biggest cluster ---
        c0 = clusters[0]
        detail = client.get(f"/api/clusters/{c0['id']}").json()
        keep_pid = detail["paths"][0]["id"]
        n_active_before = len([p for p in detail["paths"] if p["status"] == "active"])
        r = client.post(f"/api/clusters/{c0['id']}/keep", json={"path_id": keep_pid}).json()
        kept_active = [p for p in r["paths"] if p["status"] == "active"]
        check("keep leaves exactly 1 active path", len(kept_active) == 1)
        check("kept path is the one we chose", kept_active[0]["id"] == keep_pid)
        check("keep marks cluster resolved", r["resolved"] is True)

        ov2 = client.get("/api/overview").json()
        check("purgatory grew by n-1", ov2["paths_purgatory"] == n_active_before - 1)
        check("unresolved count dropped by 1", ov2["clusters_unresolved"] == n_truth_groups - 1)

        # --- action 2: purge_all on the next cluster ---
        c1 = clusters[1]
        d1 = client.get(f"/api/clusters/{c1['id']}").json()
        n1 = len([p for p in d1["paths"] if p["status"] == "active"])
        r1 = client.post(f"/api/clusters/{c1['id']}/purge_all").json()
        check("purge_all leaves 0 active", len([p for p in r1["paths"] if p["status"]=="active"]) == 0)

        ov3 = client.get("/api/overview").json()
        check("purgatory grew by all of cluster 2",
              ov3["paths_purgatory"] == (n_active_before - 1) + n1)

        # --- action 3: undo (reverses purge_all) ---
        u = client.post("/api/undo").json()
        check("undo reports purge_all", u["action"] == "purge_all")
        d1b = client.get(f"/api/clusters/{c1['id']}").json()
        check("undo restored all active paths",
              len([p for p in d1b["paths"] if p["status"]=="active"]) == n1)
        check("undo cleared resolved flag", d1b["resolved"] is False)

        # --- action 4: undo again (reverses the keep) ---
        u2 = client.post("/api/undo").json()
        check("second undo reports resolve_keep", u2["action"] == "resolve_keep")
        ov4 = client.get("/api/overview").json()
        check("fully undone: purgatory empty again", ov4["paths_purgatory"] == 0)
        check("fully undone: all clusters unresolved again",
              ov4["clusters_unresolved"] == n_truth_groups)

        # --- search ---
        sr = client.get("/api/search", params={"q": "manuscript"}).json()["results"]
        check("search finds manuscript paths", len(sr) > 0)

    # --- re-ingest idempotency doesn't double-count clusters ---
    store2 = Store(db, same_thread=False)
    created = store2.materialize_exact_clusters()
    store2.close()
    check("re-materialize creates 0 new clusters", created == 0)

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
