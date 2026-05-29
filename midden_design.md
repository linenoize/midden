# Midden — Design Doc (v0.1)

**Working name:** Midden (archaeological term for an ancient refuse heap — the pile a later generation sifts through to reconstruct how people lived). Captures the framing better than "organizer."

**One line:** A read-only archivist for inherited digital chaos. Hash everything, cluster the related, infer projects, decide in batches, never delete.

---

> **As-built note (v0.1, 2026-05-29).** This is the original *plan*. Where the shipped code differs, the code is authoritative:
> - **CLI uses stdlib `argparse`, not `typer`** (§Module boundaries) — keeps the ingest core dependency-free.
> - **Exact-dup grouping lives in `store.py`** (`exact_duplicate_groups`), not a separate `cluster_exact.py`. The `cluster_near.py` / `cluster_versions.py` / `enrich_*.py` / `purgatory.py` / `server.py` / `ui/` modules are **not built yet** — still planned (see CLAUDE.md "What's next").
> - **Drive identity** is implemented in `drives.py` via the `.midden_drive.json` marker, as designed (D1/D7).
> - The **data model below matches the implemented schema** (`status`, `clusters.kind`, `tags.source`, `decisions`).

---

## Goals

- Make sense of a corpus you didn't create and don't understand.
- Survive incremental discovery (new drive shows up later, merge cleanly).
- Make destructive decisions cheap to reverse and easy to defer.

## Non-goals

- Not a backup tool. Not a sync tool. Not Marie Kondo.
- Not a folder-tree builder. Hierarchy is an output, not the unit of work.
- No automatic deletion. Ever.

---

## Core decisions

**D1. Content-addressed identity.** A file's identity is `BLAKE3(content)`, not its path. Every path is just an *observation* of where a hash was seen. Solves dedup across drives, makes incremental ingest trivial, and lets us reorganize without losing track.

- *Why BLAKE3 over SHA-256:* ~5-10x faster on modern CPUs, no security tradeoff for this use case. Falls back to SHA-256 if BLAKE3 unavailable.

**D2. Read-only ingest, virtual organization.** Originals are never touched. The "organized" view is a layer over the index (manifests, symlinks, or a separate exported tree). Chaos drives stay byte-identical until the user explicitly exports a curated copy.

**D3. No deletion — purgatory only.** "Delete" means "move to purgatory and hide from default views." Purgatory has infinite retention by default. This is the single most important UX choice: it eliminates the fear that paralyzes triage.

**D4. SQLite as the index.** Single-file, zero-setup, fast enough for millions of files. Good enough until proven otherwise.

**D5. Cluster-first, not file-first.** The unit of human attention is a cluster (exact dupes, near-dupes, version chains, inferred projects). Folder-walking is explicitly *not* the workflow.

**D6. LLM enrichment is async and additive.** Topic inference runs as a background job over batches. Index works without it. If you never run inference, you still have a working dedup tool.

**D7. Cross-platform but Windows-first.** Joseph is on Windows. Test long-path support (`\\?\` prefix), case-insensitive paths, junction points. Don't follow symlinks/junctions by default (cycles).

---

## Data model (SQLite)

```
files
  hash TEXT PRIMARY KEY        -- BLAKE3 hex
  size INTEGER
  mime TEXT
  first_seen_at INTEGER         -- unix ts of first ingest
  status TEXT                   -- 'active' | 'purgatory' | 'canonical'
  inferred_created_at INTEGER   -- nullable; from EXIF/metadata/content

paths
  id INTEGER PRIMARY KEY
  hash TEXT REFERENCES files(hash)
  drive_id TEXT                 -- stable id for source drive/mount
  path TEXT                     -- path relative to drive root
  mtime INTEGER
  ctime INTEGER
  observed_at INTEGER           -- when we last saw it here
  UNIQUE(drive_id, path)

drives
  id TEXT PRIMARY KEY           -- generated, persisted in drive root
  label TEXT                    -- human name ("Dad's old WD")
  root_path TEXT                -- where it was mounted last
  last_ingested_at INTEGER

clusters
  id INTEGER PRIMARY KEY
  kind TEXT                     -- 'exact' | 'near_image' | 'doc_version' | 'topic'
  label TEXT                    -- nullable, human or LLM-generated
  canonical_hash TEXT           -- nullable until user picks

cluster_members
  cluster_id INTEGER
  hash TEXT
  confidence REAL               -- 0..1
  PRIMARY KEY(cluster_id, hash)

tags
  hash TEXT
  key TEXT                      -- 'topic', 'mime_class', 'likely_kind'
  value TEXT
  source TEXT                   -- 'rule' | 'llm' | 'user'
  confidence REAL
  PRIMARY KEY(hash, key, value)

decisions
  id INTEGER PRIMARY KEY
  ts INTEGER
  actor TEXT                    -- 'user' | 'auto'
  action TEXT                   -- 'mark_canonical' | 'send_to_purgatory' | 'tag' | 'undo'
  payload_json TEXT             -- enough to fully reverse the action
```

The `decisions` table is the audit log. Every UI action writes a row. Undo replays in reverse.

---

## Module boundaries

```
midden/
  ingest.py          # walk + hash + insert; idempotent
  drives.py          # drive id generation, mount detection
  cluster_exact.py   # hash-based groups
  cluster_near.py    # perceptual hash for images; fuzzy for docs
  cluster_versions.py # filename + content similarity for doc version chains
  enrich_meta.py     # EXIF, docx/pdf metadata, content date extraction
  enrich_llm.py      # async topic inference via Claude API
  store.py           # SQLite access layer
  purgatory.py       # virtual delete + restore
  server.py          # FastAPI app, serves UI + JSON API
  ui/                # static HTML/JS — single page, no framework
  cli.py             # typer-based CLI for ingest / status / export
```

## UX sketch (the part that matters)

The UI is one page with three modes:

1. **Overview** — drives, total files, dedup savings estimate, queue counts ("142 exact-dup clusters waiting," "38 likely-project groups waiting").
2. **Review** — opens a queue. One cluster at a time. Shows all members with thumbnails/previews, inferred metadata, paths across drives. Three buttons: *Pick canonical*, *All to purgatory*, *Skip*. Keyboard-driven (J/K to navigate, 1-9 to pick canonical, X to skip).
3. **Search** — full-text and metadata search across the whole index. Includes purgatory if you toggle it on.

The review queue is the product. Everything else is plumbing.

---

## Open questions

- **Near-dup for documents:** is simhash over extracted text good enough, or do we need embeddings? Embeddings are better but add a dependency. *Default: simhash for v1, embeddings as opt-in.*
- **What counts as a "project"?** Heuristic threshold for topic clustering. *Default: ≥5 files with strong topic similarity, then let LLM name the group.*
- **Purgatory retention:** infinite by default, but do we offer a "really delete" after N months? *Default: never auto-delete; user can manually empty purgatory if they want.*
- **Symlinks/junctions:** skip entirely or follow with cycle detection? *Default: skip, log them as "encountered but not traversed."*
