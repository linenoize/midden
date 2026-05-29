# INVARIANTS — Midden

Rules that span files or encode load-bearing intent — the kind of thing a linter won't catch but a single careless edit can break. Consumed by `logic-guardian`.

> Edits above `## Auto-detected (new)` are preserved across re-runs.

## Danger Zones
- `midden/store.py` — **owns the schema.** The `SCHEMA` string and every table/column definition live here and only here. Column comments encode the state vocabularies:
  - `files.status ∈ {active, purgatory, canonical}` — default `active`. Purgatory phase depends on this.
  - `clusters.kind ∈ {exact, near_image, doc_version, topic}` — phases 1–4 each add a value, not a new mechanism.
  - `tags.source ∈ {rule, llm, user}`; `decisions.actor ∈ {user, auto}`.
  - Changing these strings is a cross-cutting change — grep for the literal before editing.
- `midden/ingest.py` — **the read-only boundary.** Any new file I/O here must be read-only. The only sanctioned write to a scanned tree is the marker (next item).
- `midden/drives.py` — drive identity. `MARKER = ".midden_drive.json"` is the *sole* file Midden writes into a user drive, and only when absent. Changing the marker name orphans every previously-ingested drive's identity.

## Critical Invariants
1. **Read-only ingest.** No `open(..., 'w'/'a'/'x')`, `os.remove`, `os.rename`, `shutil.move/rmtree`, or truncation on scanned content. Marker write in `drives.get_or_create` is the one exception.
2. **`skip_names` must include `MARKER`.** `ingest()` defaults `skip_names=(MARKER,)` so Midden never indexes its own marker. Keep MARKER in that set.
3. **Idempotency gate before hashing.** The `existing is not None and existing["mtime"] == mtime` + size check in `ingest()` is what makes re-runs cheap. Don't hash unconditionally.
4. **`(drive_id, path)` uniqueness.** `paths` has `UNIQUE(drive_id, path)`; all writes go through `upsert_path`'s `ON CONFLICT`. Don't add a plain `INSERT` path-row writer.
5. **Hash is identity.** `files.hash` is PK; everything references it. Two files with the same hash are the same file. Don't introduce path-keyed identity.
6. **Transaction discipline.** Multi-statement writes use `with store.tx():` (BEGIN/COMMIT/ROLLBACK). Don't interleave bare writes inside a `tx()` block's intent.

## State Machine Rules
- **File lifecycle:** `active` → `canonical` or `active` → `purgatory`. "Delete" means flip to `purgatory` and exclude from default views — never `DELETE FROM`. Restore = reverse via a `decisions` row.
- **Undo:** every user action must write a `decisions` row whose `payload_json` is sufficient to fully reverse it. A mutation with no corresponding reversible decision row is a bug.

## Cross-File Consistency
- `HASH_NAME` / `_new_hasher()` in `ingest.py` select blake3-or-sha256 at import. Stored hashes are not algorithm-tagged — **do not mix algorithms in one index.** If blake3 availability changes between ingests of the same DB, exact-dup detection silently breaks.
- The `clusters.kind` and `files.status` vocabularies in `store.py` are the contract the future UI and detectors code against. Add values in lockstep with the code that produces and consumes them.

## Auto-detected (new)
_No automated scan this run (node + invariants scanner unavailable). Rules above are hand-derived from source._
