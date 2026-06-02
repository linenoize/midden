"""Reconcile the index against the live filesystem.

The index records every path it has ever observed. When files are moved or
deleted on disk, their `paths` rows linger as `status='active'`, which inflates
duplicate groups and makes `stats` report files that no longer exist. This
module finds those vanished observations and hard-deletes them.

Two layers of safety (see invariant #3 — "no deletion, ever" — and #7 — the
decisions table is the undo mechanism):
- Deletion is gated behind an actual `os.lstat` existence check, not "I didn't
  see it this run". A drive that wasn't mounted reports zero existing paths, so
  we refuse to wipe a drive whose root is itself missing (that's a mount
  failure, not a deletion).
- Every delete is snapshotted into `decisions` before the rows go (handled by
  store.delete_paths), so a bad sweep is reversible.

Filesystem access lives here, not in store.py, which stays DB-only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

from .store import Store


def _fs_path(root_path: str, rel: str) -> str:
    """Absolute on-disk path for a (drive root, rel) pair.

    On Windows, prefix with \\\\?\\ so paths over MAX_PATH (260) still stat
    correctly (CLAUDE.md #8 / long-path handling).
    """
    joined = os.path.join(root_path, rel.replace("/", os.sep))
    if os.name == "nt":
        ap = os.path.abspath(joined)
        if not ap.startswith("\\\\?\\"):
            ap = "\\\\?\\" + ap
        return ap
    return joined


def _exists(path: str) -> bool:
    # lstat, not exists(): a broken symlink/dangling junction still "exists" as
    # an entry we recorded; we only treat a truly-absent entry as gone.
    try:
        os.lstat(path)
        return True
    except OSError:
        return False


def reconcile_drive(
    store: Store,
    drive_id: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Find + (optionally) delete vanished active paths for one drive.

    Returns a report dict. Refuses to delete anything if the drive root itself
    is unreachable — that's an unmounted drive, not deleted files.
    """
    drive = store.get_drive(drive_id)
    if drive is None:
        raise ValueError(f"unknown drive: {drive_id}")
    root = drive["root_path"]
    rows = store.active_paths_for_drive(drive_id)

    report = {
        "drive_id": drive_id,
        "label": drive["label"],
        "root_path": root,
        "checked": len(rows),
        "missing": 0,
        "bytes_missing": 0,
        "deleted": 0,
        "dry_run": dry_run,
        "root_unreachable": False,
        "sample": [],
    }

    # Guard: if the drive root is gone, treat the whole drive as unmounted and
    # do NOT delete (otherwise an unplugged USB drive wipes its entire index).
    if not _exists(_fs_path(root, "")):
        report["root_unreachable"] = True
        return report

    missing = []
    for r in rows:
        if not _exists(_fs_path(root, r["path"])):
            missing.append(r)
            report["bytes_missing"] += r["size"] or 0
            if len(report["sample"]) < 20:
                report["sample"].append(r["path"])
    report["missing"] = len(missing)

    if missing and not dry_run:
        report["deleted"] = store.delete_paths(
            drive_id, [dict(m) for m in missing], reason="reconcile_delete"
        )
    return report


def reconcile_all(
    store: Store,
    *,
    drive_id: Optional[str] = None,
    dry_run: bool = False,
) -> Iterator[dict]:
    """Reconcile one drive (if drive_id given) or every known drive."""
    if drive_id is not None:
        yield reconcile_drive(store, drive_id, dry_run=dry_run)
        return
    for d in store.list_drives():
        yield reconcile_drive(store, d["id"], dry_run=dry_run)
