# Midden

A read-only archivist for inherited digital chaos.

You've inherited a computer — or a shoebox of old drives — from someone who never
organized anything, and you need to make sense of it without destroying anything.
Midden hashes everything, groups what's related (exact dups, near-dups, version
chains, inferred projects), and lets you make decisions in batches. It never deletes
and never modifies your originals.

A home project. See [`docs/midden_design.md`](docs/midden_design.md) for the design
rationale and [`docs/ROADMAP.md`](docs/ROADMAP.md) for where it's going.

## What it does

- **Read-only ingest** — walks a directory, hashes every file (BLAKE3 if available,
  SHA-256 otherwise), stores results in a SQLite index. Re-runs are idempotent and cheap.
- **Stable drive IDs** — drops a tiny `.midden_drive.json` marker at each drive root, so
  the same drive is recognized even when it shows up at a different mount point. (This
  marker is the *only* thing Midden ever writes to a scanned tree.)
- **Exact duplicates** — finds byte-identical files across drives, ranked by reclaimable space.
- **Near duplicates** — simhash over document text for version chains; perceptual hashing
  (dHash) for visually similar images.
- **Topic inference** — reads the first chunk of each document and assigns a topic tag,
  then clusters files into inferred "projects." Works offline with a deterministic stub,
  or with a local Ollama model, or the Anthropic API.
- **Cluster review UI** — a single web page for working through clusters one at a time:
  pick the canonical copy, send the rest to purgatory, skip, undo. Keyboard-driven.
- **Purgatory, not deletion** — "delete" means hide from default views; every action is
  logged and fully reversible.

## Setup (from a fresh clone)

Requires Python ≥ 3.10. No mandatory third-party dependencies — the ingest core runs on
the standard library alone.

```bash
git clone https://github.com/linenoize/midden.git
cd midden

# (recommended) an isolated environment
python -m venv .venv
# Windows:        .venv\Scripts\activate
# Linux / macOS:  source .venv/bin/activate

# install Midden + the optional extras you want (see below)
pip install -e ".[all]"
```

### Optional extras

Install only what you need; everything degrades gracefully if an extra is missing.

| Extra      | Adds                                    | Install                  |
|------------|-----------------------------------------|--------------------------|
| `fast`     | BLAKE3 hashing (faster than SHA-256)    | `pip install -e ".[fast]"`   |
| `web`      | the cluster-review web UI (FastAPI)     | `pip install -e ".[web]"`    |
| `images`   | perceptual image hashing (Pillow)       | `pip install -e ".[images]"` |
| `llm`      | topic inference via the Anthropic API   | `pip install -e ".[llm]"`    |
| `all`      | everything above                        | `pip install -e ".[all]"`    |

Topic inference also works against a local [Ollama](https://ollama.com) server with no
extra install, or with a deterministic offline stub (`--backend stub`).

## Try it on the synthetic corpus

The repo ships a generator that builds a realistically messy fake drive (~80 files) with
a ground-truth manifest, so you can exercise everything without pointing it at real data.

```bash
# 1. generate a messy synthetic drive
python tools/gen_corpus.py ./midden_test --clean

# 2. ingest it (use a local db so you don't touch the default ~/.midden index)
python -m midden.cli --db ./midden.sqlite ingest ./midden_test --label "synthetic"

# 3. look around
python -m midden.cli --db ./midden.sqlite stats
python -m midden.cli --db ./midden.sqlite dups

# 4. cluster exact + near duplicates
python -m midden.cli --db ./midden.sqlite cluster

# 5. infer topics (offline stub; swap for --backend ollama or anthropic)
python -m midden.cli --db ./midden.sqlite topics --backend stub

# 6. review clusters in the browser (needs the `web` extra)
python -m midden.cli --db ./midden.sqlite serve
# open http://127.0.0.1:8000
```

Re-running ingest hashes nothing the second time — it's idempotent.

## CLI reference

```
python -m midden.cli [--db PATH] <command>

  ingest  <ROOT> [--label NAME] [--quiet]   walk a directory and index it
  stats                                     print index stats
  dups    [--min-size N] [--limit N] [--json]   list exact-duplicate groups
  cluster                                   detect exact + near duplicates
  topics  [--backend auto|stub|ollama|anthropic] [--model M] [--limit N]
  serve   [--host H] [--port N]             run the cluster-review web UI
```

The index defaults to `~/.midden/index.sqlite`; pass `--db` to use another location.

## A note on safety

Midden is designed to ingest *untrusted, inherited* drives, so it's careful:

- It never modifies originals (the single `.midden_drive.json` marker aside).
- It skips junctions and symlinks by default, to avoid following links out of the tree or
  into cycles.
- The web UI binds to loopback only. Binding a non-loopback address requires an explicit
  `--allow-remote` flag, because the UI exposes the filesystem with no authentication —
  don't expose it to a network you don't trust.

## Tests

There's no unit-test suite — verification is end-to-end against the synthetic corpus and
its ground-truth manifest. The runnable suites live in `tools/e2e_*.py`:

```bash
python tools/e2e_ingest_ui.py
python tools/e2e_near.py
python tools/e2e_purgatory.py
python tools/e2e_review.py
python tools/e2e_topics.py
```

## Layout

```
midden/
  midden/
    __init__.py
    store.py       # SQLite schema + access layer (sole owner of the schema)
    drives.py      # stable drive IDs via the .midden_drive.json marker
    ingest.py      # read-only walk + hash + insert; idempotent; yields events
    near.py        # near-duplicate clustering orchestration
    simhash.py     # text simhash fingerprints
    phash.py       # image perceptual (dHash) fingerprints
    topics.py      # document topic inference (stub / ollama / anthropic)
    server.py      # FastAPI server for the review UI
    ui/index.html  # the single-page cluster-review UI
    cli.py         # `python -m midden.cli ...`
  tools/
    gen_corpus.py  # synthetic test-corpus generator + ground truth
    e2e_*.py       # end-to-end test suites
  docs/
    midden_design.md
    ROADMAP.md
```

## License

MIT.
