"""End-to-end test for the ingest-from-the-UI layer (folder picker + SSE ingest).

Drives the FastAPI app in-process (TestClient) the way the browser does:
  - GET /api/dirs to browse the filesystem (drive list -> into the corpus)
  - GET /api/ingest/stream to ingest with live SSE progress
  - POST /api/recluster to find near-dups
then asserts the index was populated, exact clusters materialized to match
ground truth, idempotent re-ingest adds nothing, and the picker is read-only.
Exit 0 = pass.

Run:  python tools/e2e_ingest_ui.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from midden.server import create_app
from tools.gen_corpus import build


def drain_stream(client: TestClient, path: str, label: str = "") -> list[dict]:
    """Consume an SSE ingest stream, returning the decoded event dicts in order."""
    events = []
    with client.stream("GET", "/api/ingest/stream",
                        params={"path": path, "label": label}) as r:
        assert r.status_code == 200, r.status_code
        for line in r.iter_lines():
            if line and line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_e2e_ui_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"

    truth = build(corpus, seed=42, scale=1.0)
    n_truth_groups = len(truth["exact_duplicate_groups"])

    app = create_app(db)
    client = TestClient(app)

    ok = True
    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # ---- folder picker ----
    roots = client.get("/api/dirs").json()
    check("dirs root: parent is null (top level)", roots["parent"] is None)
    check("dirs root: lists at least one drive/root", len(roots["dirs"]) >= 1)

    # browse into the corpus's parent, confirm the corpus dir is listed
    listing = client.get("/api/dirs", params={"path": str(corpus.parent)}).json()
    names = {d["name"] for d in listing["dirs"]}
    check("dirs: corpus folder visible in its parent", corpus.name in names)
    check("dirs: parent link present below drive root", listing["parent"] is not None)

    inside = client.get("/api/dirs", params={"path": str(corpus)}).json()
    check("dirs: selected path echoes back", inside["path"] == str(corpus))
    check("dirs: bad path -> 400", client.get("/api/dirs", params={"path": str(corpus / "nope")}).status_code == 400)

    # ---- ingest via SSE ----
    evs = drain_stream(client, str(corpus), label="ui")
    kinds = [e["kind"] for e in evs]
    done = evs[-1]
    check("ingest: emitted a 'started' event", "started" in kinds)
    check("ingest: emitted progress frames", "progress" in kinds)
    check("ingest: final event is 'done'", done["kind"] == "done")
    check("ingest: hashed > 0 files", done["hashed"] > 0)
    check("ingest: materialized exact clusters == ground truth",
          done["exact_clusters"] == n_truth_groups)
    check("ingest: overview rides along on done", done["overview"]["files_unique"] > 0)

    ov = client.get("/api/overview").json()
    check("overview: unresolved == exact groups (pre-recluster)",
          ov["clusters_unresolved"] == n_truth_groups)
    check("overview: drives == 1", ov["drives"] == 1)

    # ---- recluster (near-dups) from the same flow ----
    rc = client.post("/api/recluster").json()
    check("recluster: recovered doc_version chains", rc["doc_version_clusters"] >= 1)
    check("recluster: queue grew past the exact-only count",
          rc["clusters_unresolved"] > n_truth_groups)

    # ---- idempotent re-ingest ----
    evs2 = drain_stream(client, str(corpus), label="ui")
    done2 = evs2[-1]
    check("re-ingest: nothing new hashed", done2["hashed"] == 0)
    check("re-ingest: everything skipped-unchanged", done2["skipped"] > 0)
    check("re-ingest: no new exact clusters", done2["exact_clusters"] == 0)

    # ---- read-only invariant: picker never mutates the tree ----
    before = client.get("/api/overview").json()
    client.get("/api/dirs", params={"path": str(corpus)})
    after = client.get("/api/overview").json()
    check("picker is read-only (paths unchanged)",
          before["paths_active"] == after["paths_active"])

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
