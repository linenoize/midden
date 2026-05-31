"""Midden CLI.

Usage:
    python -m midden.cli ingest  <ROOT> [--db PATH] [--label NAME] [--quiet]
    python -m midden.cli stats   [--db PATH]
    python -m midden.cli dups    [--db PATH] [--min-size N] [--json]
    python -m midden.cli cluster [--db PATH]               # exact + near-dup detection
    python -m midden.cli topics  [--db PATH] [--backend auto|stub|ollama|anthropic]
    python -m midden.cli serve   [--db PATH] [--host H] [--port N]
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
    n_hashed = n_skipped = n_err = n_symlink = 0
    bytes_hashed = 0
    last_print = 0.0
    for ev in ingest(Path(args.root), store, label=args.label):
        if ev.kind == "started":
            print(f"[ingest] root={ev.path}  hash={HASH_NAME}  db={args.db}")
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
        elif ev.kind == "error":
            n_err += 1
            print(f"  ERROR {ev.path}: {ev.error}", file=sys.stderr)
        elif ev.kind == "done":
            print(
                f"[done] hashed={n_hashed} skipped={n_skipped} "
                f"symlinks={n_symlink} errors={n_err} "
                f"bytes_hashed={bytes_hashed/1e6:.1f}MB "
                f"in {ev.elapsed:.1f}s"
            )
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
    backend = topics.resolve_backend(args.backend)
    if backend == "ollama":
        ok, models = topics.ollama_available()
        if not ok:
            print("[topics] ollama not reachable at localhost:11434 — start it, or "
                  "use --backend stub", file=sys.stderr)
            store.close()
            return 1
        print(f"[topics] backend=ollama model={args.model or topics.DEFAULT_OLLAMA_MODEL} "
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
                                    limit=args.limit or None):
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
    serve(args.db, host=args.host, port=args.port)
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
    p_ing.set_defaults(func=cmd_ingest)

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
    p_tp.add_argument("--limit", type=int, default=0, help="cap docs processed (0 = all)")
    p_tp.add_argument("--quiet", action="store_true")
    p_tp.set_defaults(func=cmd_topics)

    p_srv = sub.add_parser("serve", help="run the cluster-review web UI")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
