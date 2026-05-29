# Conventions — Midden

Detected from source on 2026-05-29. These describe how the code is actually written, not aspirations.

## Language & runtime
- **Python 3.10** (uses `X | Y` union syntax, `list[dict]` builtins generics, `from __future__ import annotations` at the top of every module).
- No packaging manifest (`pyproject.toml` / `setup.py` / `requirements.txt` absent). Run as a module: `python -m midden.cli`.
- Sole optional dependency: `blake3` (graceful fallback to stdlib `hashlib.sha256`).

## Naming
- Files & modules: `snake_case`, one concern per file (`store.py`, `drives.py`, `ingest.py`, `cli.py`).
- Functions: `snake_case` (`upsert_file`, `exact_duplicate_groups`, `get_or_create`).
- Classes: `PascalCase` (`Store`, `DriveInfo`, `IngestEvent`, `IngestStats`).
- Module-level constants: `UPPER_SNAKE` (`SCHEMA`, `MARKER`, `HASH_NAME`, `DEFAULT_DB`, `LOREM`).
- Private trailing-underscore to dodge builtins: `hash_`.

## Imports
- `from __future__ import annotations` first, then stdlib, then intra-package relative imports (`from .store import Store`).
- Named imports only; no barrel/`__init__` re-exports (`__init__.py` holds just `__version__`).

## Idioms
- **Dataclasses** for value/event types (`DriveInfo`, `IngestEvent`, `IngestStats`) — not dicts, not namedtuples.
- **Generators yield events** from long-running work; `ingest()` yields `IngestEvent`, and the CLI renders them. This is the decoupling seam between core and any UI. Preserve it.
- **Idempotency** is load-bearing: re-running ingest skips unchanged `(drive_id, path)` by `mtime`+`size` before hashing.
- **SQL via upserts** (`INSERT ... ON CONFLICT ... DO UPDATE`), parameterized everywhere (no string interpolation into SQL).
- Type hints on all public function signatures, including return types.

## Error handling
- Ingest is defensive at the file boundary: `except (PermissionError, OSError)` per file → emits an `error` event and continues; one bad file never aborts a walk.
- `Store.tx()` is an explicit `BEGIN`/`COMMIT`/`ROLLBACK` context manager; exceptions roll back and re-raise.
- No bare `except:` anywhere — keep it that way.

## Persistence
- **`store.py` is the only module that touches the schema.** Schema lives in the `SCHEMA` string. Do not write SQL DDL or raw table access elsewhere.
- SQLite, WAL mode, `synchronous=NORMAL`, `foreign_keys=ON`, `isolation_level=None` (autocommit + manual `tx()`).
- Timestamps are `int(time.time())` (epoch seconds), not ISO strings.

## CLI
- `argparse` with subparsers (`ingest`, `stats`, `dups`); each sets `func` via `set_defaults`.
- `--db` is a top-level arg; default `~/.midden/index.sqlite`.

## Testing
- No unit-test framework. Validation is **end-to-end against the synthetic corpus**: `tools/gen_corpus.py` emits `_ground_truth.json`; ingest results are checked against it. Add coverage the same way, not via `pytest` micro-tests for trivial code.
