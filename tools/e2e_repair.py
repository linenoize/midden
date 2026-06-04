"""End-to-end test for the Phase-4 repair primitive (reset_clusters).

Asserts that rebuilding the derived near_image view:
  - removes ALL near_image clusters + their members,
  - leaves exact and doc_version clusters untouched,
  - writes a reversible `decisions` audit row,
  - is a no-op on a second call (idempotent).

Run:  python tools/e2e_repair.py
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


def count(store, sql, *p):
    return store.conn.execute(sql, p).fetchone()[0]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_repair_"))
    corpus = tmp / "corpus"
    corpus.mkdir(parents=True)
    db = tmp / "index.sqlite"
    build(corpus, seed=7, scale=1.0)

    store = Store(db)
    for _ in ingest(corpus, store, label="repair-e2e"):
        pass
    store.materialize_exact_clusters()
    near.recluster(store)

    # Inject a synthetic near_image cluster (the algorithm finds none on docs).
    some_hashes = [r["hash"] for r in store.conn.execute(
        "SELECT DISTINCT hash FROM paths LIMIT 3")]
    store.create_cluster("near_image", some_hashes, label="similar images")

    n_exact_before = count(store, "SELECT COUNT(*) FROM clusters WHERE kind='exact'")
    n_doc_before = count(store, "SELECT COUNT(*) FROM clusters WHERE kind='doc_version'")
    n_near_before = count(store, "SELECT COUNT(*) FROM clusters WHERE kind='near_image'")
    n_dec_before = count(store, "SELECT COUNT(*) FROM decisions")
    check("setup: a near_image cluster exists", n_near_before >= 1)
    check("setup: exact clusters exist", n_exact_before >= 1)

    removed = store.reset_clusters("near_image")
    check("reset removed the near_image cluster(s)", removed == n_near_before)
    check("no near_image clusters remain",
          count(store, "SELECT COUNT(*) FROM clusters WHERE kind='near_image'") == 0)
    check("no orphaned near_image members remain",
          count(store, "SELECT COUNT(*) FROM cluster_members cm "
                       "LEFT JOIN clusters c ON c.id=cm.cluster_id WHERE c.id IS NULL") == 0)
    check("exact clusters untouched",
          count(store, "SELECT COUNT(*) FROM clusters WHERE kind='exact'") == n_exact_before)
    check("doc_version clusters untouched",
          count(store, "SELECT COUNT(*) FROM clusters WHERE kind='doc_version'") == n_doc_before)
    check("a decisions audit row was written",
          count(store, "SELECT COUNT(*) FROM decisions") == n_dec_before + 1)
    check("audit row records the reset",
          count(store, "SELECT COUNT(*) FROM decisions WHERE action='reset_clusters'") >= 1)

    check("second reset is a no-op", store.reset_clusters("near_image") == 0)

    # files / paths are never touched by a reset
    check("paths preserved", count(store, "SELECT COUNT(*) FROM paths") > 0)
    check("files preserved", count(store, "SELECT COUNT(*) FROM files") > 0)

    store.close()
    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
