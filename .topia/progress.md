# Progress Log

## 2026-05-29 — Phase 1 shipped: cluster-review UI + purgatory + undo
- **Done:** Materialize exact-dup clusters → `clusters`/`cluster_members`. FastAPI JSON API + single-page keyboard-driven review UI (J/K/1-9/P/X/U), Overview, Search. Thin purgatory slice (`paths.status`, additive migration). `resolve_keep`/`purge_all`/`undo_last` all write reversible `decisions` rows. CLI `serve` subcommand; `stats` shows active/purgatory/reclaimed. `pyproject.toml` added.
- **Validated:** `tools/e2e_review.py` — 22 checks pass incl. full purge→undo→restore reversibility. Live HTTP smoke test passed. Commit `6c9d87d`.
- **Next:** Phase 2 (near-dup: simhash docs + perceptual-hash images). Deferred polish: real Search (LIKE only today; FTS later), purgatory-browsing view in UI, paginate >500 clusters.

## 2026-05-29 — Onboard / current state
- **Done (v0.1):** Ingest core complete and validated. BLAKE3+SHA-256 hashing, SQLite index, stable drive IDs, idempotent re-ingest, exact-duplicate detection. CLI: `ingest`, `stats`, `dups`. Synthetic corpus generator with ground truth.
- **Validated:** 84-file synthetic corpus → 70 unique, 5 exact-dup groups (matches `_ground_truth.json`). (Note: README says ~80 files / 5 groups; CLAUDE.md says 84 → 70.)
- **Next (priority order):** 1) Cluster review UI (FastAPI + single static page, keyboard-driven review queue) → 2) Near-duplicate detection (simhash for docs, perceptual hash for images) → 3) Purgatory layer → 4) LLM topic inference → 5) Export curated view.

## Open items surfaced during onboard
- ~~`midden_design.md` missing~~ — **resolved 2026-05-29**: recovered from session outputs and copied to repo root; README's "one level up" reference corrected.
- No dependency manifest. About-to-add deps per CLAUDE.md: `fastapi`, `uvicorn`, `pillow`, `anthropic`. Consider a `pyproject.toml` before the UI phase.
- Schema already anticipates future phases (`status`, `clusters.kind` ∈ {exact, near_image, doc_version, topic}, `tags`, `decisions`) — DB groundwork for phases 2–4 is laid.
