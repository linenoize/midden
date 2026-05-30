"""End-to-end test for phase-3 purgatory browse + targeted restore.

Builds the corpus, ingests, clusters, then via the HTTP API:
  - resolve an exact cluster (sends copies to purgatory),
  - browse /api/purgatory (count + bytes + items),
  - restore a specific path -> it returns to active AND its cluster reopens,
  - undo the restore -> path back to purgatory, cluster re-resolved,
  - validation: restoring an active or unknown path is a 400.

Run:  python tools/e2e_purgatory.py
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
    tmp = Path(tempfile.mkdtemp(prefix="midden_purg_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"
    build(corpus, seed=42, scale=1.0)

    store = Store(db)
    for _ in ingest(corpus, store, label="purg-e2e"):
        pass
    store.materialize_exact_clusters()
    near.recluster(store)
    store.close()

    app = create_app(db)
    client = TestClient(app)

    ok = True
    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    # empty to start
    p0 = client.get("/api/purgatory").json()
    check("purgatory empty at start", p0["count"] == 0 and p0["items"] == [])

    # resolve a multi-copy exact cluster
    clusters = client.get("/api/clusters").json()["clusters"]
    exact = [c for c in clusters if c["kind"] == "exact"]
    target = max(exact, key=lambda c: c["n_active"])
    detail = client.get(f"/api/clusters/{target['id']}").json()
    actives = [p for p in detail["paths"] if p["status"] == "active"]
    n_copies = len(actives)
    keep_pid = actives[0]["id"]
    purged_pids = {p["id"] for p in actives[1:]}
    client.post(f"/api/clusters/{target['id']}/keep", json={"path_id": keep_pid})

    # browse purgatory
    p1 = client.get("/api/purgatory").json()
    check("purgatory count == copies-1 after keep", p1["count"] == n_copies - 1)
    check("purgatory bytes > 0", p1["bytes"] > 0)
    purg_ids = {it["id"] for it in p1["items"]}
    check("the purged paths appear in purgatory", purged_pids <= purg_ids)
    check("kept path NOT in purgatory", keep_pid not in purg_ids)

    ov1 = client.get("/api/overview").json()
    check("overview purgatory count matches", ov1["paths_purgatory"] == n_copies - 1)
    check("target cluster resolved (out of queue)",
          target["id"] not in {c["id"] for c in client.get("/api/clusters").json()["clusters"]})

    # restore one purged path -> reopens the cluster (now 2 active again)
    restore_pid = next(iter(purged_pids))
    r = client.post(f"/api/paths/{restore_pid}/restore").json()
    check("restore reports reopened cluster", target["id"] in r["reopened_clusters"])
    p2 = client.get("/api/purgatory").json()
    check("purgatory count dropped by 1 after restore", p2["count"] == n_copies - 2)
    back = client.get(f"/api/clusters/{target['id']}").json()
    check("restored path is active again",
          any(p["id"] == restore_pid and p["status"] == "active" for p in back["paths"]))
    check("cluster reopened after restore", back["resolved"] is False)
    check("cluster back in queue",
          target["id"] in {c["id"] for c in client.get("/api/clusters").json()["clusters"]})

    # undo the restore -> path re-purged, cluster re-resolved
    u = client.post("/api/undo").json()
    check("undo reports restore", u["action"] == "restore")
    p3 = client.get("/api/purgatory").json()
    check("purgatory count back up after undo-restore", p3["count"] == n_copies - 1)
    back2 = client.get(f"/api/clusters/{target['id']}")
    check("cluster re-resolved (404/out of open queue)",
          target["id"] not in {c["id"] for c in client.get("/api/clusters").json()["clusters"]})

    # validation
    bad_active = client.post(f"/api/paths/{keep_pid}/restore")
    check("restoring an active path -> 400", bad_active.status_code == 400)
    bad_missing = client.post("/api/paths/999999/restore")
    check("restoring unknown path -> 400", bad_missing.status_code == 400)

    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
