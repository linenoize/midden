# Audit Report: Midden

- **Date:** 2026-05-31
- **Verdict:** **WARNING** (composite grades *Good*, but one HIGH security finding should not wait for "next sprint")
- **Overall Health:** 7.6/10 (weighted composite 75.6 → Good)
- **Total Findings:** 13 (CRITICAL: 0, HIGH: 1, MEDIUM: 4, LOW: 6, INFO: 2)
- **Framework Checks Applied:** Python / FastAPI

## Project Profile

- **Language:** Python ≥3.10 (stdlib-first; `from __future__ import annotations` throughout)
- **Type:** Local, single-user CLI + localhost web tool (read-only file archivist)
- **Stack:** argparse CLI · FastAPI + uvicorn (optional `web` extra) · SQLite (WAL) · vanilla single-page HTML/JS UI
- **Optional deps:** `blake3` (fast hash), `pillow` (perceptual hash), `anthropic` (cloud topic inference)
- **Build:** hatchling · **Tests:** 5 runnable e2e suites against a synthetic corpus (no pytest/CI) — **all 5 green this run**
- **Threat model (their own framing):** ingests *inherited, untrusted* drives. This is load-bearing for the security findings below.

### Health Score
| Dimension      | Score    | Notes |
|----------------|:--------:|-------|
| Security       |  6/10    | Stored XSS via filenames in UI; non-loopback exposure + CSRF gaps. Reversible state + localhost cap the blast. |
| Code Quality   |  8/10    | Clean, documented, idempotent. One real shared-connection concurrency hazard. |
| Architecture   |  8/10    | Excellent boundaries (`store.py` sole schema owner). Windows-junction gap contradicts a stated decision. |
| Dependencies   |  9/10    | Near-zero required deps. Only gap: unpinned *optional* extras, no lockfile. |
| Performance    |  8/10    | Chunked hashing, throttled SSE, bucketed near-dup. `near_image` is all-pairs O(n²). |
| Infrastructure |  6/10    | No CI — "don't commit on red" is unenforced. print()-based logging (fine for a CLI). |
| Documentation  |  9/10    | Design doc + as-built notes + invariants + progress log. Missing LICENSE file. |
| Nexus Analytics|  7/10    | Tool metrics present; skill/chain metrics empty (cursor sessions). Advisory only. |
| **Overall**    | **7.6/10** | **Good, with one HIGH to clear promptly.** |

### Phase Breakdown
| Phase | Issues |
|-------|--------|
| Dependencies | 1 |
| Security | 3 |
| Code Quality | 2 |
| Architecture | 2 |
| Performance | 1 |
| Infrastructure | 2 |
| Documentation | 2 |
| Nexus Analytics | 0 |

### Composite Score
- **Formula:** (Sec×0.25)+(CQ×0.20)+(Arch×0.15)+(Deps×0.15)+(Perf×0.10)+(Infra×0.08)+(Docs×0.07)
- = (6×.25)+(8×.20)+(8×.15)+(9×.15)+(8×.10)+(6×.08)+(9×.07) = **7.56/10 → 75.6 → Good**
- Verdict downgraded **Good → WARNING** by judgment: a HIGH stored-XSS in a tool whose entire value proposition is *safety* warrants a near-term fix, not a next-sprint deferral.

---

## Findings

### HIGH

**H1 — Stored DOM XSS via filenames in the review, search, and purgatory views.**
`midden/ui/index.html` interpolates ingested file paths **raw** into `innerHTML`:
- `index.html:426` — review row: `<div class="path">${p.path}</div>`
- `index.html:560` — search result: `<td class="path">${x.path}</td>`
- `index.html:578` — purgatory row: `<td class="path">${x.path}</td>`

The codebase *has* the fix and uses it elsewhere — the folder picker (`:314`) and Projects view (`:503`,`:517`) wrap the same kind of data in `esc()` (defined at `:300`). These three sites are the omission, not a policy.

Why it matters here specifically: Midden's stated job is to ingest **inherited, untrusted drives**. A file named
`<img src=x onerror="fetch('/api/clusters/1/purge_all',{method:'POST'})">.txt`
becomes executable script the moment that cluster/search hit/purgatory row renders. The same origin serves **unauthenticated mutation endpoints** (`/api/clusters/{id}/purge_all`, `/api/undo`, `/api/paths/{id}/restore`, `/api/ingest/stream`), so the injected script can drive the tool. Capped below CRITICAL only because it's loopback single-user and every action is reversible (purgatory, not deletion) — no true data loss.
**Fix:** wrap all three with `esc(...)`, exactly as `:314`/`:517` already do.

### MEDIUM

**M1 — Non-loopback bind turns the folder picker + ingest trigger into remote filesystem exposure.**
`server.py:260` `serve(host=...)` and `cli.py:189` accept `--host`, with no guard or warning when it's not `127.0.0.1`. If pointed at `0.0.0.0`, `/api/dirs` (`server.py:110`) becomes a remote directory-lister and `/api/ingest/stream?path=` (`server.py:146`) lets any caller trigger a walk/hash of **any server path** — both unauthenticated. The `dirs` comment acknowledges the loopback assumption, but nothing enforces it.
**Fix:** refuse (or loudly warn + require an explicit `--insecure` flag) when `host` is non-loopback; minimally, gate `/api/dirs` and `/api/ingest/stream` behind that.

**M2 — No CSRF protection on bodyless mutation POSTs.**
`/api/clusters/{id}/purge_all`, `/api/undo`, `/api/paths/{id}/restore` are simple POSTs with no body/custom header, so any web page the user visits while the server runs can fire them cross-origin (no preflight). Reversible, so low-damage, but combined with H1/M1 it widens the attack surface. **Fix:** require a custom header (e.g. `X-Requested-With`) or same-origin check on mutations.

**M3 — Windows directory junctions are not skipped, contradicting design decision D7 / CLAUDE.md #8.**
`ingest.py:97` relies on `Path.is_symlink()`, which returns **False** for Windows directory junctions; `os.walk(followlinks=False)` (`:89`) still descends into them on Windows. The design explicitly says "skip junctions by default (cycles)" and the tool is *Windows-first*. On a real inherited Windows drive this risks cycle traversal and double-ingest of the same content under two paths. **Fix:** detect reparse points on Windows (e.g. `os.stat().st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`) and skip+log them, matching the symlink path.

**M4 — Shared SQLite connection: reads bypass the write lock.**
`store.py` serializes *writers* with `self._lock`, but read methods (`overview`, `list_clusters`, `get_cluster`, `search`, `stats`) take no lock and run on the same `sqlite3.Connection` shared across FastAPI's threadpool (`same_thread=False`, `server.py:38`). A read interleaving a writer's `BEGIN/COMMIT` on one connection can raise or read mid-transaction state. Single-user localhost makes it low-probability, not impossible. **Fix:** either guard reads with the same lock, or give each request its own connection / use a connection pool.

### LOW

- **L1 — Unpinned optional dependencies, no lockfile.** `pyproject.toml:15-25` lists `fastapi/uvicorn/pillow/anthropic` with no version bounds. Required deps are empty (excellent), but the optional extras can drift. Add lower bounds or a lockfile for the `web`/`all` extras.
- **L2 — Long-path (`\\?\`) support is a stated design goal (CLAUDE.md #8) that isn't implemented.** Paths >260 chars raise `OSError`, caught per-file at `ingest.py:139` as an `error` event → file silently skipped. Graceful, but the intended capability is absent; on a deep inherited Windows tree this drops files quietly.
- **L3 — `near_image` clustering is all-pairs O(n²).** `near.py:174` is documented as acceptable for "small image sets," but an inherited drive with thousands of photos is the expected input. Add LSH/banding or bucket by hash prefix before it bites.
- **L4 — No CSRF/origin defense is also why `/api/recluster` and `/api/topics/stream` are reachable cross-site** (see M2). Listed separately only as a reminder these two also mutate/read heavily.
- **L5 — No `LICENSE` file.** `pyproject.toml:11` declares MIT but the repo has no `LICENSE`. Add one.
- **L6 — No `CHANGELOG.md`.** The `.topia/progress.md` log is excellent but isn't a user-facing changelog for a tool shipping a console-script.

### INFO

- **I1 — `files.inferred_created_at`** (`store.py:25`) is defined but never written — the `enrich_meta` (EXIF/metadata) phase isn't built yet. Expected; noting so it isn't mistaken for a populated field.
- **I2 — Signatures/hashes are not algorithm-tagged.** Already captured in `.topia/INVARIANTS.md` (blake3↔sha256 mixing, simhash `k` mixing). Not re-litigating — flagging that the invariant doc correctly owns this risk and it remains unmitigated by code (only by discipline).

---

## Nexus Analytics
Data source: `.topia/metrics/` (18 sessions, platform=cursor).

| Signal | Value |
|--------|-------|
| Top tools by invocation | Bash (482), Read (216), Edit (106), PowerShell (94), Write (70), Grep (68) |
| Top tool by est. I/O tokens | **Write ~190.7k** (heavy file generation), Bash ~55.6k, Edit ~44.5k |
| Skill metrics | `skills.json` empty `{}`, `chains.jsonl` empty — cursor sessions logged `skill_invocations: 0` |
| Session pressure | reached `red` in a 174-tool-call / 15-min session (~141k est. I/O) |
| Baseline | none (`baseline.json` absent) → no savings delta |

**Read:** the heavy Write-token total tracks the phase-by-phase build cadence. The empty skill/chain data means nexus routing can't be assessed from cursor runs — not a project defect, just no signal. Advisory; contributes 0 to the score.

---

## Top Priority Actions
1. **H1** — `esc()` the three raw `${...path}` sites (`index.html:426`, `:560`, `:578`). One-line each; the helper already exists at `:300`. Untrusted-input rendering in a safety tool. *Do this first.*
2. **M3** — Skip Windows junctions in `ingest.py` (reparse-point check). Directly contradicts a locked design decision on the primary platform; risks cycle/double-ingest on real drives.
3. **M1 + M2** — Gate non-loopback `--host` and add a same-origin/header check on mutation POSTs. Closes the "if you ever expose it" cliff and the cross-site POST vector.
4. **M4** — Per-request connection or lock reads. Cheapest correct fix: one `Store` per request, or `with self._lock` around the read queries.
5. **Infra** — Add a minimal CI (`.github/workflows`) that runs the 5 e2e suites. The "don't commit on red" note in `progress.md` (Phase 2 shipped red once) is currently honor-system.

## Positive Findings
- **Near-zero dependency surface.** Core ingest/index/dedup runs on pure stdlib; everything heavier is an opt-in extra. Rare discipline.
- **Reversibility is real, not aspirational.** Every mutation writes a `decisions` row sufficient to fully reverse it; `undo_last`/`restore_path` round-trips are e2e-tested. The "no deletion ever" decision is enforced by the schema, not just docs.
- **SQL is fully parameterized** — no injection vectors found across `store.py`, including the dynamic `IN (...)` and `LIKE` builders.
- **Architecture boundaries hold.** `store.py` is genuinely the sole schema owner; `server.py` carries no business logic; signature *computation* is correctly kept out of server boot. The invariants doc matches the code.
- **Documentation is well above norm** for a project this age — design doc with explicit as-built divergence notes, invariants, conventions, and a detailed decision log.
- **Privacy decision respected in code:** the cloud (`anthropic`) topic backend is opt-in, lazily imported, and never the `auto` default; local-only paths are the default.

## Follow-up Timeline
- WARNING → re-audit in ~1 month, **after H1 is fixed** (sooner if `--host` ships before the picker/ingest endpoints are gated).

Report saved to: AUDIT-REPORT.md
