"""Read-only diagnostics for a live Midden index.

The index stores each drive's *ingest-time* root (e.g. a Linux path like
/home/linenoize). To re-verify files from another machine you must remap that
root to wherever the drive is mounted now: `--root Z:\\`.

Nothing here writes to the DB or the originals. Two modes:

  exact  — re-stat (and optionally re-hash) the members of an exact-dup group to
           prove they really are byte-identical (catches short-read / placeholder
           false positives where stat-size != bytes-actually-read).
  images — dump near-image clustering pathology straight from the signatures
           table (degenerate dHashes, mega-cluster sizes) — no disk I/O.

Usage:
  python tools/diagnose.py --db DB --root Z:\\ exact  --hash 75059ffe [--full]
  python tools/diagnose.py --db DB                    images
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys
from pathlib import Path

# Use the SAME hasher the ingest uses, so a fresh digest is comparable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from midden.ingest import hash_file, HASH_NAME  # noqa: E402


def _connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _remap(root: str | None, drive_root: str | None, rel: str) -> Path:
    """Absolute on-disk path for a stored observation, remapped to --root."""
    if root:
        return Path(root) / rel
    if drive_root:
        return Path(drive_root) / rel
    return Path(rel)


def cmd_exact(con: sqlite3.Connection, root: str | None, hash_prefix: str, full: bool) -> int:
    drives = {r["id"]: r["root_path"] for r in con.execute("SELECT id, root_path FROM drives")}
    frow = con.execute(
        "SELECT hash, size FROM files WHERE hash LIKE ?", (hash_prefix + "%",)
    ).fetchone()
    if not frow:
        print(f"no files row matching {hash_prefix!r}")
        return 1
    stored_hash, stored_size = frow["hash"], frow["size"]
    print(f"stored hash : {stored_hash}")
    print(f"stored size : {stored_size:,}  (hasher={HASH_NAME})")
    paths = con.execute(
        "SELECT drive_id, path FROM paths WHERE hash=? AND status='active' ORDER BY path",
        (stored_hash,),
    ).fetchall()
    print(f"{len(paths)} active path(s):\n")

    sizes: set[int] = set()
    fresh_hashes: set[str] = set()
    for p in paths:
        ap = _remap(root, drives.get(p["drive_id"]), p["path"])
        print(f"  {p['path']}")
        print(f"    -> {ap}")
        try:
            st = os.stat(ap)
            sizes.add(st.st_size)
            tag = "  <-- size != stored!" if st.st_size != stored_size else ""
            print(f"    disk size = {st.st_size:,}{tag}")
            if full:
                fh = hash_file(ap)
                fresh_hashes.add(fh)
                ok = "MATCH" if fh == stored_hash else "DIFFERENT!"
                print(f"    fresh hash = {fh[:32]}…  [{ok}]")
        except FileNotFoundError:
            print("    -> NOT FOUND at this root (check --root mapping)")
        except OSError as e:
            print(f"    -> ERROR: {e!r}")
        print()

    print("=== verdict ===")
    if len(sizes) > 1:
        print(f"  REAL BUG: members have {len(sizes)} distinct disk sizes {sorted(sizes)} "
              f"but share one hash -> short-read/placeholder false positive.")
    elif full and len(fresh_hashes) > 1:
        print(f"  REAL BUG: {len(fresh_hashes)} distinct fresh hashes -> stored grouping is wrong.")
    elif full and fresh_hashes == {stored_hash}:
        print("  CORRECT: every member re-hashes to the stored digest. Genuine identical content.")
    elif len(sizes) == 1:
        print(f"  Sizes all equal ({sizes.pop():,}). Re-run with --full to confirm by re-hashing.")
    return 0


def cmd_images(con: sqlite3.Connection) -> int:
    rows = con.execute(
        """SELECT c.id, COUNT(cm.hash) m FROM clusters c
           JOIN cluster_members cm ON cm.cluster_id=c.id
           WHERE c.kind='near_image' GROUP BY c.id ORDER BY m DESC"""
    ).fetchall()
    print(f"near_image clusters: {len(rows)}")
    if rows:
        print(f"  largest: {rows[0]['m']} members (cluster {rows[0]['id']})")
        print(f"  top sizes: {[r['m'] for r in rows[:8]]}")
    sigs = [r["value"] for r in con.execute(
        "SELECT value FROM signatures WHERE algo='phash_image'")]
    pop = collections.Counter(bin(int(v, 16)).count("1") for v in sigs)
    val = collections.Counter(sigs)
    degenerate = sum(c for k, c in pop.items() if k <= 6 or k >= 58)
    print(f"\nimage signatures: {len(sigs)}  distinct: {len(val)}")
    print(f"degenerate (popcount<=6 or >=58): {degenerate}")
    print("most common dHash values:")
    for v, c in val.most_common(6):
        print(f"  {v}  x{c}  (popcount={bin(int(v,16)).count('1')})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="diagnose")
    ap.add_argument("--db", required=True)
    ap.add_argument("--root", default=None, help="remap drive root to this mount (e.g. Z:\\)")
    sub = ap.add_subparsers(dest="mode", required=True)
    pe = sub.add_parser("exact")
    pe.add_argument("--hash", required=True, help="hash prefix of the exact-dup group")
    pe.add_argument("--full", action="store_true", help="re-hash full content (slow)")
    sub.add_parser("images")
    args = ap.parse_args(argv)

    con = _connect(args.db)
    try:
        if args.mode == "exact":
            return cmd_exact(con, args.root, args.hash, args.full)
        return cmd_images(con)
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
