# Roadmap

Where Midden is headed, roughly in priority order. Nothing here is a promise — it's
a home project and the plan shifts as the synthetic-corpus runs teach me things.

See [`midden_design.md`](midden_design.md) for the rationale behind these.

## Done

- **Ingest core** — read-only walk + hash (BLAKE3, SHA-256 fallback), SQLite index,
  stable drive IDs, idempotent re-ingest.
- **Exact-duplicate detection** — byte-identical files grouped across drives.
- **Near-duplicate detection** — simhash over extracted document text (`doc_version`
  clusters) and dHash perceptual hashing for images (`near_image` clusters).
- **Cluster review UI** — FastAPI + a single static page. Overview, review queue, and
  search. Keyboard-driven; writes to the `decisions` table.
- **Purgatory & undo** — virtual delete (status flip on `files`); default views exclude
  it; every action is reversible from the `decisions` log.
- **Topic inference** — read the first chunk of each document and assign a topic tag
  (Ollama / Anthropic / deterministic stub backends), then cluster by inferred project.

## Next

- **Export a curated view** — build a clean target tree from canonicals + project
  clusters (symlinks on Linux/Mac, junctions or copies on Windows). Originals untouched.
- **Real-drive hardening on Windows** — long-path (`\\?\`) handling for paths > 260 chars,
  more testing against actual messy drives rather than only the synthetic corpus.
- **Smarter version chains** — embeddings as an alternative to simhash for document
  similarity, measured against the synthetic corpus before committing.

## Explicitly out of scope

These belong to other tools; Midden won't grow into them without a deliberate decision:

- Backup / sync (use restic, Syncthing).
- Cloud-account ingestion.
- A folder-tree drag-and-drop reorganization UI.
- Automatic deletion of anything, ever.
