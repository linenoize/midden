# Midden — Context for Claude Code

You're picking up an in-progress MVP. Read this file first; it captures the decisions and conventions so you don't re-derive them.

## What this is

A read-only archivist for inherited digital chaos. The framing problem: you've inherited a computer (or a stack of drives) from someone who left things disorganized, and you need to make sense of it without destroying anything. See `midden_design.md` for the full design rationale.

## Current state (v0.1)

The **ingest core** is complete and validated against a synthetic corpus:
- BLAKE3 hashing (SHA-256 fallback), SQLite index, stable drive IDs, idempotent re-ingest.
- Exact-duplicate detection works.
- CLI: `python -m midden.cli {ingest,stats,dups}`.
- Synthetic corpus generator at `tools/gen_corpus.py` — use this for dev.

Verified: 84-file synthetic corpus → 70 unique files, 5 dup groups detected (matches ground truth in `_ground_truth.json`).

## Design decisions — DO NOT silently revisit these

These were decided up front. If you want to change one, surface it to the user first.

1. **Content-addressed identity.** Files are identified by hash, not path. Paths are observations.
2. **Read-only ingest.** Never modify originals. Only exception: `.midden_drive.json` marker file at each drive's root (one tiny write per drive, once).
3. **No deletion, ever.** "Delete" = move to purgatory + hide from default views. Infinite retention default.
4. **SQLite as the index.** Single file, WAL mode. Good until proven otherwise.
5. **Cluster-first UX, not folder-first.** The unit of human attention is a cluster (exact dups, version chains, inferred projects). Don't build a folder tree browser as the primary view.
6. **LLM enrichment is async + additive.** Index works without it. Don't make it a hard dependency.
7. **Decisions table is the undo mechanism.** Every user action writes a row sufficient to fully reverse. This is what lets the UI be aggressive ("send all 30 to purgatory") — every decision is undoable.
8. **Windows-first cross-platform.** The user is on Windows. Watch for long paths (`\\?\` prefix when needed), skip junctions/symlinks by default.

## Conventions

- **No new dependencies without flagging.** Current deps: `blake3` (optional). About to add: `fastapi`, `uvicorn`, `pillow`, `anthropic`. Anything beyond that, ask first.
- **Module boundaries** as laid out in `midden_design.md` — one file per concern. Keep `store.py` the only thing that touches the DB schema.
- **Yield events from long-running operations** (ingest does this). Lets the CLI and the future web UI both render progress without coupling.
- **Idempotency everywhere.** Re-running anything should be cheap and safe.
- **Tests via the synthetic corpus.** Don't bother with unit tests for trivial code; do bother with end-to-end runs against `gen_corpus.py` output + ground-truth JSON.

## What's next (priority order)

These were planned in this order:

1. **Cluster review UI** (the actual UX bet)
   - FastAPI server + single static HTML page.
   - Three views: Overview, Review queue, Search.
   - Review queue is the product: one cluster at a time, keyboard-driven (J/K nav, 1-9 to pick canonical, X skip, P send-to-purgatory).
   - Writes to `decisions` table for full undo.

2. **Near-duplicate detection**
   - Simhash over extracted document text (cheap, catches version chains).
   - Perceptual hash (`imagehash` lib + Pillow) for images.
   - New cluster `kind` values: `near_image`, `doc_version`.
   - Open question deferred from design: simhash vs embeddings for documents. Start with simhash, see how it performs on the synthetic corpus.

3. **Purgatory layer**
   - Status flips on `files` table; default queries exclude `status='purgatory'`.
   - Restore from decisions log.

4. **LLM topic inference** (the "wow")
   - Async batch job. Read first N KB of each doc, call Claude to assign topic tags.
   - Cluster by topic tag → "files that look like one project."
   - New cluster `kind`: `topic`.

5. **Export curated view**
   - Build a clean target tree (symlinks on Linux/Mac, junctions or copies on Windows) from canonicals + project clusters. Originals untouched.

## Things explicitly out of scope (don't add them without asking)

- Backup / sync (use restic, Syncthing).
- Cloud account ingestion (later, not v0.x).
- Folder-tree drag-and-drop reorganization UI.
- Automatic deletion of anything, ever.

## How to run

```bash
# generate test corpus
python tools/gen_corpus.py /tmp/midden_test --clean

# ingest
python -m midden.cli --db /tmp/midden.sqlite ingest /tmp/midden_test --label "synthetic"

# inspect
python -m midden.cli --db /tmp/midden.sqlite stats
python -m midden.cli --db /tmp/midden.sqlite dups
```

## File map

```
midden/
  midden_design.md     # full design rationale — read this if context is missing
  README.md            # user-facing intro
  CLAUDE.md            # this file
  midden/
    __init__.py
    store.py           # SQLite schema + access (the ONLY thing that touches schema)
    drives.py          # stable drive IDs via .midden_drive.json marker
    ingest.py          # walk + hash + insert; read-only, idempotent; yields events
    cli.py             # argparse-based CLI
  tools/
    gen_corpus.py      # synthetic test-corpus generator with ground_truth.json
```

The user's preferences: concise/direct, no sycophancy, push back when you have a reason. Treat them as a developer — they want substantive technical disagreement, not agreeable execution.

<!-- @Topia-invariants-pointer:start -->
## Invariants

Cross-file rules and danger zones live in `.topia/INVARIANTS.md` (consumed by logic-guardian). Highest-risk zones:
- `midden/store.py` — sole owner of the SQLite schema and the `files.status` / `clusters.kind` / `tags.source` vocabularies.
- `midden/ingest.py` — the read-only boundary; the only sanctioned write to a scanned tree is the `.midden_drive.json` marker.
- `midden/drives.py` — `MARKER` name is drive identity; changing it orphans every prior ingest.
<!-- @Topia-invariants-pointer:end -->

<!-- @Topia-context-pointer:start -->
## Context

Persisted session state lives in `.topia/` (added by `/topia onboard`, 2026-05-29):
- `conventions.md` — detected code style/idioms · `decisions.md` — the locked architecture decisions · `progress.md` — current state + next steps
- `contract.md` — enforceable rules · `INVARIANTS.md` — danger zones · `DEVELOPER-GUIDE.md` — human onboarding · `session-log.md`, `instincts.md`
<!-- @Topia-context-pointer:end -->

