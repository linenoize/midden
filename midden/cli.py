"""Midden CLI.

Usage:
    python -m midden.cli ingest <ROOT> [--db PATH] [--label NAME] [--quiet]
    python -m midden.cli stats   [--db PATH]
    python -m midden.cli dups    [--db PATH] [--min-size N] [--json]
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


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.db, host=args.host, port=args.port)
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

    p_srv = sub.add_parser("serve", help="run the cluster-review web UI")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
