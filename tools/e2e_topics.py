"""End-to-end test for phase-4 topic inference (tags + topic clusters).

Drives the FastAPI app in-process (TestClient) through the topic-inference flow
the browser uses — GET /api/topics/stream with the deterministic `stub` backend
(no model required, so this runs offline in CI) — then asserts:
  - the right docs are tagged and headerless noise is left alone,
  - topic clusters match the corpus's ground-truth projects,
  - topic clusters are EXCLUDED from the dedup review queue (the load-bearing
    design rule — topic groups are not keep/purge decisions),
  - the pass is idempotent.
Exit 0 = pass.

Run:  python tools/e2e_topics.py
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


def drain(client: TestClient, params: dict) -> list[dict]:
    events = []
    with client.stream("GET", "/api/topics/stream", params=params) as r:
        assert r.status_code == 200, r.status_code
        for line in r.iter_lines():
            if line and line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_e2e_topics_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"

    build(corpus, seed=42, scale=1.0)

    # ingest first (topic inference needs an index)
    store = Store(db)
    for _ in ingest(corpus, store, label="e2e"):
        pass
    store.close()

    app = create_app(db)
    client = TestClient(app)

    ok = True
    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # ---- health: stub always available ----
    health = client.get("/api/topics/health").json()
    check("health: stub backend available", health["stub"]["available"] is True)
    check("health: reports a default backend", bool(health["default"]))

    # ---- inference via SSE (stub backend) ----
    evs = drain(client, {"backend": "stub"})
    kinds = [e["kind"] for e in evs]
    done = evs[-1]
    check("stream: started + progress + done", "started" in kinds and "progress" in kinds and done["kind"] == "done")
    check("stream: tagged the 8 titled docs", done["tagged"] == 8)
    check("stream: skipped the 30 headerless noise files", done["skipped"] == 30)
    check("stream: 0 errors", done["errors"] == 0)
    check("stream: materialized 2 topic clusters", done["topic_clusters"] == 2)

    # ---- topic clusters match ground-truth projects ----
    topic_clusters = client.get("/api/clusters", params={"kinds": "topic"}).json()["clusters"]
    by_label = {c["label"]: c for c in topic_clusters}
    check("topics: Kitchen Renovation cluster exists", "Kitchen Renovation Notes" in by_label)
    check("topics: Long Migration cluster exists", "The Long Migration" in by_label)
    if "Kitchen Renovation Notes" in by_label:
        check("topics: kitchen has 5 members (notes + 4 versions)",
              by_label["Kitchen Renovation Notes"]["n_members"] == 5)
    if "The Long Migration" in by_label:
        check("topics: manuscript has 3 members (v1/v2/v3)",
              by_label["The Long Migration"]["n_members"] == 3)

    # spot-check member paths of the kitchen cluster
    if "Kitchen Renovation Notes" in by_label:
        cid = by_label["Kitchen Renovation Notes"]["id"]
        full = client.get(f"/api/clusters/{cid}").json()
        paths = {p["path"] for p in full["paths"]}
        check("topics: kitchen cluster includes the canonical notes.md",
              any(p.endswith("kitchen_reno/notes.md") for p in paths))
        check("topics: kitchen cluster spans both folders (Desktop + Projects)",
              any("Desktop" in p for p in paths) and any("Projects" in p for p in paths))

    # ---- the load-bearing rule: topic clusters are NOT in the dedup queue ----
    dedup = client.get("/api/clusters", params={"kinds": "exact,doc_version,near_image"}).json()["clusters"]
    check("queue: no topic clusters leak into the dedup review queue",
          all(c["kind"] != "topic" for c in dedup))
    ov = client.get("/api/overview").json()
    check("overview: topic_clusters counted separately", ov["topic_clusters"] == 2)
    check("overview: dedup queue count excludes topics",
          ov["clusters_dedup_open"] == sum(1 for c in dedup))

    # ---- noise stays untagged ----
    full_misc = [c for c in topic_clusters if "misc" in c["label"].lower() or "file" in c["label"].lower()]
    check("topics: no cluster formed from the headerless Misc noise", not full_misc)

    # ---- idempotent re-run ----
    done2 = drain(client, {"backend": "stub"})[-1]
    check("re-run: nothing newly tagged", done2["tagged"] == 0)
    check("re-run: no new topic clusters", done2["topic_clusters"] == 0)

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
