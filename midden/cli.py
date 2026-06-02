"""Midden CLI.

Usage:
    python -m midden.cli ingest    <ROOT> [--db PATH] [--label NAME] [--quiet] [--prune]
    python -m midden.cli reconcile [--db PATH] [--drive ID] [--dry-run]
    python -m midden.cli stats     [--db PATH]
    python -m midden.cli dups      [--db PATH] [--min-size N] [--json]
    python -m midden.cli cluster   [--db PATH]             # exact + near-dup detection
    python -m midden.cli topics    [--db PATH] [--backend auto|stub|ollama|anthropic] [--ollama-url URL]
    python -m midden.cli serve     [--db PATH] [--host H] [--port N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .ingest import ingest, HASH_NAME
from .store import Store

DEFAULT_DB = Path.home() / ".midden" / "index.sqlite"


def cmd_ingest(args) -> int:
    store = Store(args.db)
    n_hashed = n_skipped = n_err = n_symlink = n_pruned = 0
    bytes_hashed = 0
    last_print = 0.0
    for ev in ingest(Path(args.root), store, label=args.label, prune=args.prune):
        if ev.kind == "started":
            print(f"[ingest] root={ev.path}  hash={HASH_NAME}  db={args.db}"
                  f"{'  (prune on)' if args.prune else ''}")
        elif ev.kind == "hashed":
            n_hashed += 1
            bytes_hashed += ev.size
            now = time.time()
            if not args.quiet and (now - last_print > 0.1):
                print(f"  hashed {n_hashed:>6}  ({bytes_hashed/1e6:.1f} MB)  {ev.path}")
                last_print = now
        elif ev.kind == "skipped":
            n_skipped += 1
        elif ev.kind == "skipped_symlink":
            n_symlink += 1
        elif ev.kind == "pruned":
            n_pruned = ev.count
            print(f"  pruned {n_pruned} vanished path(s) (snapshot in decisions log)")
        elif ev.kind == "error":
            n_err += 1
            print(f"  ERROR {ev.path}: {ev.error}", file=sys.stderr)
        elif ev.kind == "done":
            print(
                f"[done] hashed={n_hashed} skipped={n_skipped} "
                f"symlinks={n_symlink} pruned={n_pruned} errors={n_err} "
                f"bytes_hashed={bytes_hashed/1e6:.1f}MB "
                f"in {ev.elapsed:.1f}s"
            )
    store.close()
    return 0


def cmd_reconcile(args) -> int:
    from . import reconcile
    store = Store(args.db)
    total_missing = total_deleted = total_bytes = 0
    any_drive = False
    for rep in reconcile.reconcile_all(store, drive_id=args.drive, dry_run=args.dry_run):
        any_drive = True
        tag = f"[{rep['drive_id'][:8]}] {rep['label']}"
        if rep["root_unreachable"]:
            print(f"{tag}: root unreachable ({rep['root_path']}) — SKIPPED "
                  f"(drive not mounted? refusing to delete)")
            continue
        total_missing += rep["missing"]
        total_deleted += rep["deleted"]
        total_bytes += rep["bytes_missing"]
        verb = "would remove" if args.dry_run else "removed"
        print(f"{tag}: checked {rep['checked']}, {verb} {rep['missing']} vanished "
              f"({rep['bytes_missing']/1e6:.1f} MB)")
        for s in rep["sample"]:
            print(f"     - {s}")
        if rep["missing"] > len(rep["sample"]):
            print(f"     … and {rep['missing'] - len(rep['sample'])} more")
    if not any_drive:
        print("[reconcile] no drives in index")
    else:
        orphans = store.orphan_file_count()
        mode = "DRY RUN — nothing deleted" if args.dry_run else "done"
        print(f"[reconcile] {mode}: missing={total_missing} deleted={total_deleted} "
              f"({total_bytes/1e6:.1f} MB), orphan file records={orphans}")
    store.close()
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    s = store.stats()
    print(f"drives:           {s['drives']}")
    print(f"unique files:     {s['files_unique']}")
    print(f"total path obs:   {s['paths_total']}")
    print(f"active copies:    {s['paths_active']}")
    print(f"purgatory copies: {s['paths_purgatory']}")
    print(f"unique bytes:     {s['bytes_unique']:>14,}")
    print(f"total bytes:      {s['bytes_total']:>14,}")
    print(f"dedup savings:    {s['bytes_saveable']:>14,}  "
          f"({100*s['bytes_saveable']/max(s['bytes_total'],1):.1f}%)")
    if s['bytes_reclaimed']:
        print(f"reclaimed:        {s['bytes_reclaimed']:>14,}  (in purgatory)")
    store.close()
    return 0


def cmd_dups(args) -> int:
    store = Store(args.db)
    groups = store.exact_duplicate_groups(min_size=args.min_size)
    if args.json:
        print(json.dumps(groups, indent=2))
    else:
        print(f"# {len(groups)} exact-duplicate groups (min_size={args.min_size}):\n")
        for g in groups[: args.limit]:
            print(f"  hash={g['hash'][:16]}…  size={g['size']:,}  copies={g['n_paths']}")
            for p in g["paths"]:
                print(f"     - [{p['drive_id'][:8]}] {p['path']}")
            print()
    store.close()
    return 0


def cmd_cluster(args) -> int:
    from . import near
    store = Store(args.db)
    n_exact = store.materialize_exact_clusters()
    print(f"[cluster] exact-dup clusters: +{n_exact} new")
    r = near.recluster(store)
    print(f"[cluster] text signatures:    +{r['text_signatures']} computed")
    print(f"[cluster] image signatures:   +{r['image_signatures']} computed"
          f"{'  (Pillow not installed)' if not near.phash.PIL_AVAILABLE else ''}")
    print(f"[cluster] doc_version:        +{r['doc_version_clusters']} new")
    print(f"[cluster] near_image:         +{r['near_image_clusters']} new")
    ov = store.overview()
    print(f"[cluster] open queue: {ov['clusters_unresolved']} clusters {dict(ov['open_by_kind'])}")
    store.close()
    return 0


def cmd_topics(args) -> int:
    from . import topics
    store = Store(args.db)
    ollama_url = args.ollama_url or None
    backend = topics.resolve_backend(args.backend, ollama_url=ollama_url)
    if backend == "ollama":
        base = topics.ollama_base(ollama_url)
        ok, models = topics.ollama_available(base_url=ollama_url)
        if not ok:
            print(f"[topics] ollama not reachable at {base} — start it, set "
                  "OLLAMA_HOST / --ollama-url, or use --backend stub", file=sys.stderr)
            store.close()
            return 1
        print(f"[topics] backend=ollama url={base} "
              f"model={args.model or topics.DEFAULT_OLLAMA_MODEL} "
              f"(available: {', '.join(models) or 'none'})")
    elif backend == "anthropic" and not topics.anthropic_available():
        print("[topics] anthropic unavailable — set ANTHROPIC_API_KEY and "
              "`pip install anthropic`, or use --backend stub", file=sys.stderr)
        store.close()
        return 1
    else:
        print(f"[topics] backend={backend}")

    n = 0
    for ev in topics.compute_topics(store, backend=backend, model=args.model or None,
                                    limit=args.limit or None, ollama_url=ollama_url):
        if ev["kind"] == "started":
            print(f"[topics] tagging {ev['total']} untagged doc(s)…")
        elif ev["kind"] == "progress":
            n += 1
            if not args.quiet:
                tail = f" -> {ev['topic']}" if ev["topic"] else " -> (no topic)"
                print(f"  [{ev['i']}/{ev['total']}] {ev['path']}{tail}")
        elif ev["kind"] == "error":
            print(f"  ERROR {ev['path']}: {ev['error']}", file=sys.stderr)
        elif ev["kind"] == "tagged_done":
            print(f"[topics] tagged={ev['tagged']} skipped={ev['skipped']} errors={ev['errors']}")
    created = topics.materialize_topics(store)
    print(f"[topics] topic clusters: +{created} new")
    store.close()
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.db, host=args.host, port=args.port, allow_remote=args.allow_remote)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="midden")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"index database path (default: {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="walk a directory and index it")
    p_ing.add_argument("root", type=Path)
    p_ing.add_argument("--label", help="human label for this drive")
    p_ing.add_argument("--quiet", action="store_true")
    p_ing.add_argument("--prune", action="store_true",
                       help="after a full walk, remove index paths no longer on "
                            "disk (snapshotted to the decisions log for undo)")
    p_ing.set_defaults(func=cmd_ingest)

    p_rec = sub.add_parser("reconcile",
                           help="remove index paths whose files no longer exist on disk")
    p_rec.add_argument("--drive", help="reconcile only this drive id (default: all)")
    p_rec.add_argument("--dry-run", action="store_true",
                       help="report what would be removed without deleting")
    p_rec.set_defaults(func=cmd_reconcile)

    p_st = sub.add_parser("stats", help="print index stats")
    p_st.set_defaults(func=cmd_stats)

    p_dup = sub.add_parser("dups", help="list exact-duplicate groups")
    p_dup.add_argument("--min-size", type=int, default=1)
    p_dup.add_argument("--limit", type=int, default=20)
    p_dup.add_argument("--json", action="store_true")
    p_dup.set_defaults(func=cmd_dups)

    p_cl = sub.add_parser("cluster", help="detect exact + near duplicates")
    p_cl.set_defaults(func=cmd_cluster)

    p_tp = sub.add_parser("topics", help="infer document topics + cluster by project")
    p_tp.add_argument("--backend", choices=["auto", "stub", "ollama", "anthropic"],
                      default="auto", help="inference backend (auto: ollama if up, else stub)")
    p_tp.add_argument("--model", default="", help="model name (backend-specific)")
    p_tp.add_argument("--ollama-url", default="",
                      help="Ollama base URL for remote servers, e.g. "
                           "http://192.168.1.5:11434 (default: $OLLAMA_HOST or localhost)")
    p_tp.add_argument("--limit", type=int, default=0, help="cap docs processed (0 = all)")
    p_tp.add_argument("--quiet", action="store_true")
    p_tp.set_defaults(func=cmd_topics)

    p_srv = sub.add_parser("serve", help="run the cluster-review web UI")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.add_argument("--allow-remote", action="store_true",
                       help="permit binding a non-loopback host (exposes the "
                            "filesystem picker + ingest with no auth — unsafe)")
    p_srv.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
