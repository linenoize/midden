# Progress Log

## 2026-05-30 — Ingest-from-the-UI: single-launch flow (folder picker + SSE)
- **Why:** the UI was review-only — you still needed two CLI commands (`ingest`, `cluster`) before the browser was useful. Goal: `serve`, then drive everything from the page.
- **Done:** New **Ingest** tab. Native server-side folder picker (`GET /api/dirs` — read-only, lists drive roots → subdirs, skips symlinks/junctions, parent="" at a drive root vs null at the drive list). Live ingest via Server-Sent Events (`GET /api/ingest/stream?path=&label=`): runs `ingest()` on a **dedicated Store/connection** (never shares the request-thread connection), throttles `hashed` frames to ~10/s, and on `done` materializes exact clusters + emits fresh overview. UI streams counters (hashed/skipped/bytes/errors) + current path, then offers Recluster / Go-to-Review / Ingest-another. Recluster button wires the pre-existing `/api/recluster`. EventSource is closed client-side on done/error to defeat auto-reconnect (would re-trigger ingest).
- **Validated:** `tools/e2e_ingest_ui.py` 20 checks — picker browse/echo/400, SSE started→progress→done, exact_clusters==GT(5), overview rides along, recluster recovers doc_version chains, idempotent re-ingest (0 hashed / all skipped / 0 new clusters), picker read-only invariant. Real-socket boot confirmed (84→70 unique, 5 clusters over HTTP; SSE error frame on bad path). All four suites (review/near/purgatory/ingest_ui) green.
- **Note:** folder picker exposes filesystem *listing* over localhost — acceptable for a 127.0.0.1 single-user tool, read-only (no contents, no writes). Revisit if `serve --host` is ever pointed at a non-loopback interface.

## 2026-05-29 — Phase 3 shipped: purgatory browse + targeted restore
- **Done:** Phases 1–2 already had the purgatory *core* (status flips, undo-last). Phase 3 adds the browse/restore UX: `store.list_purgatory()` + `purgatory_summary()` + `restore_path()` (reversible — writes a `restore` decision; `undo_last` extended to reverse it). Targeted restore reopens any resolved cluster the restored hash belongs to (restoring a copy re-creates a dup, so it returns to the queue). New endpoints `GET /api/purgatory`, `POST /api/paths/{id}/restore`. UI gets a 4th **Purgatory** tab: table of purgatoried paths, per-row restore, live count/bytes.
- **Validated:** `tools/e2e_purgatory.py` 16 checks — keep→browse→restore→reopen→undo round-trip, overview consistency, restore-active/restore-unknown both 400. All three suites (review/near/purgatory) green.
- **Note:** "No deletion ever" still holds — purgatory is the terminal state; there is deliberately no empty-purgatory / hard-delete path (CLAUDE.md decision #3). Restore is the only exit.

## 2026-05-29 — Phase 2 shipped: near-duplicate detection
- **Done:** SimHash (text, stdlib `simhash.py`) + dHash (`phash.py`, optional Pillow). `near.py` computes signatures (read-only) and clusters: `doc_version` (directory/stem structural prior + SimHash ≤16, single-linkage) and `near_image` (dHash ≤10). New `signatures` table; `clusters.status` (open|resolved) with migration; clusters now span multiple hashes. CLI `cluster` subcommand; `/api/recluster`. Review UI made kind-aware + multi-hash (was exact-only, would have thrown on `c.hash` for doc_version clusters).
- **Validated:** `tools/e2e_near.py` 19 checks — both GT version chains recovered exactly, no chain-merge, no purely-noise cluster, multi-hash keep/undo reversibility, idempotent re-run. Phase-1 e2e still green. UI contract + JS-balance verified.
- **Process note:** first phase-2 commit (2b98d49) shipped with a failing test (threshold guessed at 12 before the tuning probe ran; real max intra-chain distance is 15). Fixed in eafdde6 by measurement, plus a latent `_VER` regex bug that collapsed enumerated junk files into one stem. Don't commit on red again.
- **Deferred:** near_image has no synthetic ground truth (corpus images are random bytes, not real images) — detection path is exercised for graceful no-op only; needs real-image fixtures to validate dHash. Threshold 16 is tuned to *this* corpus; revisit on real data.

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
