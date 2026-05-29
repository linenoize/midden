# Project Contract — Midden

Starter contract derived from the detected stack (Python 3.10 + SQLite, read-only archival tool). Review and customize. Rules here are meant to be enforceable by `build`/`readiness`/`guardian`.

## contract.code
- **No bare `except:`** — catch specific exceptions (the codebase uses `except (PermissionError, OSError)`).
- **Parameterized SQL only** — never f-string/`%`/`+` user or path data into SQL. All current queries use `?` placeholders; keep it that way.
- **Schema isolation** — only `midden/store.py` may define or alter tables (the `SCHEMA` string). No DDL or raw table access in other modules.
- **Type hints** on all public function signatures, including return types.
- `from __future__ import annotations` at the top of every module.

## contract.invariants (see INVARIANTS.md for detail)
- **Ingest is read-only.** The only permitted write to a scanned tree is the `.midden_drive.json` marker (`drives.MARKER`).
- **No destructive ops.** No `os.remove`, `shutil.rmtree`, file truncation, or overwrite of originals anywhere in the ingest/index path. (`gen_corpus.py` may write/delete — it builds the *test* corpus, not user data.)
- **Idempotency.** Re-running any operation must be cheap and safe; unchanged files are not re-hashed.
- **Additive enrichment.** LLM/topic features must degrade gracefully when unavailable; never a hard import-time dependency.

## contract.tests
- Validation is end-to-end against `tools/gen_corpus.py` + `_ground_truth.json`. A change to ingest/clustering must be checked against ground truth, not just "it ran".
- Don't add `pytest` micro-tests for trivial code; do add corpus-based checks for new cluster kinds.

## contract.deps
- New runtime dependencies must be flagged to the user before adding. Pre-approved upcoming: `fastapi`, `uvicorn`, `pillow`, `anthropic`. Anything else → ask.

## contract.platform
- Windows-first. Watch long paths (`\\?\` prefix when >260 chars). Skip junctions/symlinks by default (cycle + semantics hazards).
