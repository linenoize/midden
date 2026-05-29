# Progress Log

## 2026-05-29 — Onboard / current state
- **Done (v0.1):** Ingest core complete and validated. BLAKE3+SHA-256 hashing, SQLite index, stable drive IDs, idempotent re-ingest, exact-duplicate detection. CLI: `ingest`, `stats`, `dups`. Synthetic corpus generator with ground truth.
- **Validated:** 84-file synthetic corpus → 70 unique, 5 exact-dup groups (matches `_ground_truth.json`). (Note: README says ~80 files / 5 groups; CLAUDE.md says 84 → 70.)
- **Next (priority order):** 1) Cluster review UI (FastAPI + single static page, keyboard-driven review queue) → 2) Near-duplicate detection (simhash for docs, perceptual hash for images) → 3) Purgatory layer → 4) LLM topic inference → 5) Export curated view.

## Open items surfaced during onboard
- ~~`midden_design.md` missing~~ — **resolved 2026-05-29**: recovered from session outputs and copied to repo root; README's "one level up" reference corrected.
- No dependency manifest. About-to-add deps per CLAUDE.md: `fastapi`, `uvicorn`, `pillow`, `anthropic`. Consider a `pyproject.toml` before the UI phase.
- Schema already anticipates future phases (`status`, `clusters.kind` ∈ {exact, near_image, doc_version, topic}, `tags`, `decisions`) — DB groundwork for phases 2–4 is laid.
