# Architecture Decisions

These mirror the locked decisions in `CLAUDE.md` ("DO NOT silently revisit"). Surface to the user before changing any.

| Date | Decision | Rationale | Status |
|------|----------|-----------|--------|
| 2026-05-29 | Content-addressed identity (hash, not path) | Paths are observations; the same bytes in 5 places is one file | Locked |
| 2026-05-29 | Read-only ingest (only write: `.midden_drive.json` marker) | Inherited drives must never be mutated | Locked |
| 2026-05-29 | No deletion ever — "delete" = purgatory + hide | Aggressive UX is only safe if everything is reversible | Locked |
| 2026-05-29 | SQLite single-file index, WAL mode | Simple, durable, good until proven otherwise | Locked |
| 2026-05-29 | Cluster-first UX, not folder-tree browser | The unit of attention is a cluster, not a folder | Locked |
| 2026-05-29 | LLM enrichment is async + additive, never a hard dep | Index must work offline / without API keys | Locked |
| 2026-05-29 | `decisions` table is the undo mechanism (row = full reverse) | Enables "send all 30 to purgatory" safely | Locked |
| 2026-05-29 | Windows-first cross-platform; skip junctions/symlinks | User is on Windows; long-path + cycle hazards | Locked |
