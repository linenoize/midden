# Midden

Read-only archivist for inherited digital chaos.

See `midden_design.md` for the design rationale.

## What works today (v0.1)

- **Ingest:** walks a directory, hashes every file (BLAKE3 if available, SHA-256 fallback), stores results in SQLite. Read-only. Idempotent re-runs.
- **Stable drive IDs:** writes a `.midden_drive.json` marker at the root of each drive. Same drive plugged in at a different mount point is still recognized.
- **Exact-duplicate detection:** finds and groups byte-identical files across drives, ranked by reclaimable bytes.
- **Synthetic corpus generator:** produces a realistically messy fake drive (~80 files) for development.

## Install

```bash
pip install blake3   # optional but recommended; SHA-256 used otherwise
```

No other deps for the ingest core. Future layers will add Pillow (perceptual image hash) and the Anthropic SDK (topic inference).

## Try it

```bash
# 1. Generate a messy synthetic drive
python tools/gen_corpus.py /tmp/midden_test --clean

# 2. Ingest it (writes index to ~/.midden/index.sqlite by default)
python -m midden.cli --db /tmp/midden.sqlite ingest /tmp/midden_test --label "synthetic"

# 3. See what's there
python -m midden.cli --db /tmp/midden.sqlite stats
python -m midden.cli --db /tmp/midden.sqlite dups

# 4. Re-run ingest — should hash nothing (idempotent)
python -m midden.cli --db /tmp/midden.sqlite ingest /tmp/midden_test --quiet
```

Validated against ground-truth on the synthetic corpus: 5 exact-dup groups expected, 5 detected, all memberships correct.

## Layout

```
midden/
  midden/
    __init__.py
    store.py     # SQLite schema + access layer
    drives.py    # stable drive IDs via .midden_drive.json marker
    ingest.py    # walk + hash + insert (read-only, idempotent)
    cli.py       # `python -m midden.cli ...`
  tools/
    gen_corpus.py  # synthetic test-corpus generator
  README.md
```

## Coming next

- **Cluster review UI** (FastAPI + a single static page): surface exact-dup and version-chain clusters; mark canonical / send to purgatory in batches, keyboard-driven.
- **Near-duplicate detection:** simhash over extracted document text; perceptual hash for images.
- **LLM topic inference:** read documents in batches, group by inferred project.
- **Purgatory & undo:** virtual delete, decisions audit log, full undo.

## Windows note

Tested under Linux for the synthetic-corpus run. For real-drive ingestion on Windows, the long-path (`\\?\`) prefix may need to be applied for paths > 260 chars depending on system config. Junctions and symlinks are skipped by default to avoid cycles.
