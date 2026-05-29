# Developer Guide: Midden

## What This Does
Midden is a **read-only archivist for inherited digital chaos** — point it at a disorganized drive (e.g. one you inherited) and it builds a searchable, content-addressed index so you can make sense of the mess without ever modifying or deleting the originals. v0.1 indexes files and finds exact duplicates; later phases add near-dup detection, a cluster-review UI, and LLM topic inference.

## Quick Setup
No build step, no packaging manifest yet — it runs straight from source as a module.

```bash
# (recommended) create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell on Windows
# source .venv/bin/activate          # macOS/Linux

# optional but recommended hashing speedup (falls back to SHA-256 if absent)
pip install blake3

# 1. generate a messy synthetic test drive
python tools/gen_corpus.py ./_corpus --clean

# 2. ingest it
python -m midden.cli --db ./midden.sqlite ingest ./_corpus --label "synthetic"

# 3. inspect
python -m midden.cli --db ./midden.sqlite stats
python -m midden.cli --db ./midden.sqlite dups

# 4. re-run ingest — should hash nothing (idempotency check)
python -m midden.cli --db ./midden.sqlite ingest ./_corpus --quiet
```

> The `--db` default is `~/.midden/index.sqlite`. The README's examples use `/tmp/...` (Linux); on Windows use a local path like `./midden.sqlite` as above.

## Key Files
- `midden/store.py` — SQLite schema + all DB access. **The only file that touches the schema.** Start here to understand the data model.
- `midden/ingest.py` — walks a tree, hashes files, upserts into the store; read-only and idempotent; yields progress events.
- `midden/drives.py` — stable per-drive IDs via the `.midden_drive.json` marker (survives remount at a different drive letter).
- `midden/cli.py` — `argparse` entry point: `ingest`, `stats`, `dups`.
- `midden/__init__.py` — package version only.
- `tools/gen_corpus.py` — generates a realistic messy drive + `_ground_truth.json` for end-to-end validation.
- `CLAUDE.md` — the design decisions and conventions. Read it before changing anything.

## How to Contribute
1. Read `CLAUDE.md` first — several decisions are explicitly locked (read-only, no deletion, content-addressed identity, cluster-first UX). Surface before revisiting.
2. Make changes.
3. Validate end-to-end: regenerate the corpus and confirm ingest/dups still match `_ground_truth.json`. There is no unit-test suite by design.
4. Flag any new dependency before adding it.

## Common Issues
- **`ModuleNotFoundError: midden`** → run from the repo root and use the module form: `python -m midden.cli ...` (there's no installed package).
- **Hashes differ between runs of the same index** → blake3 was available for one ingest and not another. Keep `blake3` installed (or uninstalled) consistently for a given DB; the stored hashes aren't algorithm-tagged.
- **Long-path errors on Windows** → paths over 260 chars may need the `\\?\` prefix depending on system config; junctions/symlinks are skipped by default.
- **`See midden_design.md`** → that file is currently missing from the repo despite being referenced; ask the maintainer for it.
