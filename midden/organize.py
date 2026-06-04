"""Execute queued file relocations — the ONLY module that moves user files.

This is the read-WRITE filesystem boundary, isolated the way `ingest.py` is the
read-only boundary. The review UI only ever *queues* relocations (index rows in
`pending_moves`); nothing on disk changes until `process()` runs here.

Two kinds of relocation, both queued during review and committed together:
- keeper: a file the user chose to keep is moved to a destination folder.
- dup:    a purgatory duplicate is moved into the holding folder (the chosen
          "delete" semantics — reversible trash, not unlink).

Safety model (the filesystem is the source of truth; the index follows):
- Refuse the whole batch up-front if any involved root — every source drive root
  AND each destination/holding root — is unreachable (an unmounted drive is not a
  deletion). Mirrors reconcile's guard.
- Each move is a persisted two-phase op: the planned source+dest are committed as
  status='moving' BEFORE the OS move, then status='executed' + the index update
  commit AFTER. A crash leaves a recoverable 'moving' row; `reconcile_pending`
  probes the disk to finish or roll back — it never auto-deletes.
- Every source file is re-hashed and compared to the index before being moved
  (skip on mismatch / short read), unless --no-verify (which still checks size+mtime).
- Moves MIRROR the source subfolders under the target and NEVER overwrite
  (auto-suffix " (2)"). Windows long-path prefixing and junction skipping are
  handled the same way ingest/reconcile do.

Invariant note (CLAUDE.md #2/#3): this is a sanctioned-destructive exception —
snapshot-before (decisions), stat-gated, refuse-on-unreachable, fully reversible
via `undo_last_process` while the originals remain in holding/destination.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterator, Optional

from .ingest import HASH_NAME, ShortReadError, hash_file, is_reparse_point
from .reconcile import _exists, _fs_path
from .store import Store


def _ensure_parent(fs_dst: str) -> None:
    os.makedirs(os.path.dirname(fs_dst), exist_ok=True)


def _noncolliding_rel(dest_root: str, rel: str) -> str:
    """Return a rel path under dest_root that does NOT yet exist on disk, suffixing
    ' (2)', ' (3)', ... on the stem if needed. Mirrors the source subfolders."""
    candidate = rel
    n = 2
    p = Path(rel)
    stem, suffix, parent = p.stem, p.suffix, p.parent
    while _exists(_fs_path(dest_root, candidate)):
        candidate = str(parent / f"{stem} ({n}){suffix}").replace("\\", "/")
        n += 1
    return candidate


def fs_move(src_abs: str, dest_root: str, dest_rel: str, *, verify_hash: Optional[str] = None) -> None:
    """Physically move src_abs -> dest_root/dest_rel. dest_rel must already be
    collision-free (resolved by the caller). Long-path safe; never overwrites;
    falls back to copy+verify+unlink across volumes. Refuses reparse-point sources.
    """
    src = _fs_path(os.path.dirname(src_abs), os.path.basename(src_abs))
    if is_reparse_point(Path(src_abs)):
        raise OSError(f"refusing to move a symlink/junction: {src_abs}")
    dst = _fs_path(dest_root, dest_rel)
    _ensure_parent(dst)
    if _exists(dst):  # caller resolved collisions; a leftover here is a partial crash
        raise FileExistsError(f"destination already exists: {dst}")
    try:
        os.rename(src, dst)  # atomic on the same volume
    except OSError:
        # cross-volume (or rename refused): copy, verify, then unlink the source.
        shutil.copy2(src, dst)
        if verify_hash is not None:
            got = hash_file(Path(dst))
            if got != verify_hash:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                raise OSError(f"copy verification failed for {src_abs} -> {dst}")
        os.remove(src)


def _verify_source(store: Store, item: dict, verify: bool) -> Optional[str]:
    """Return None if the source is safe to move, else a human reason to skip."""
    src_abs = item["abspath"]
    if not _exists(_fs_path(os.path.dirname(src_abs), os.path.basename(src_abs))):
        return "source file is gone"
    if is_reparse_point(Path(src_abs)):
        return "source is a symlink/junction"
    if not verify:
        # cheap guard: still confirm the file hasn't changed since ingest
        try:
            st = Path(src_abs).stat()
        except OSError as e:
            return f"cannot stat source: {e}"
        if int(st.st_mtime) != (item["mtime"] or 0) or st.st_size != (item["size"] or st.st_size):
            return "source changed since ingest (size/mtime differ)"
        return None
    try:
        got = hash_file(Path(src_abs), expected_size=item["size"])
    except ShortReadError:
        return "source is being written / placeholder (short read)"
    except OSError as e:
        return f"cannot read source: {e}"
    if got != item["hash"]:
        return f"content changed since ingest ({HASH_NAME} mismatch)"
    return None


def _unreachable_source(items: list[dict]) -> Optional[str]:
    """First SOURCE drive root that is unreachable, else None. An unmounted source
    is an unplugged drive, not a deletion — we refuse rather than touch anything.
    Destination/holding roots are NOT checked here: a missing target just needs
    creating (handled separately), and refusing on it would block first use."""
    for r in {it["src_root"] for it in items if it.get("src_root")}:
        if not _exists(_fs_path(r, "")):
            return r
    return None


def _ensure_target_roots(items: list[dict], holding: Optional[str]) -> Optional[str]:
    """Create each destination/holding root if absent. Returns an error string if
    a root can't be created (e.g. its volume isn't mounted), else None."""
    roots = {it["dest_root"] for it in items}
    if holding:
        roots.add(holding)
    for r in roots:
        if not r:
            continue
        try:
            os.makedirs(_fs_path(r, ""), exist_ok=True)
        except OSError as e:
            return f"cannot create target root {r}: {e} (is the drive mounted?)"
    return None


def reconcile_pending(store: Store) -> list[dict]:
    """Resolve any rows stuck in 'moving' (a crash mid-process) by probing disk.

    Never auto-deletes. dst-only -> finish the DB update; src-only -> roll back to
    pending; both/neither -> leave flagged (returned as conflicts).
    """
    conflicts = []
    for row in store.moving_rows():
        src_abs = row["src_abspath"]
        dest_root, final_rel = row["dest_root"], row["final_path"]
        src_there = bool(src_abs) and _exists(_fs_path(os.path.dirname(src_abs), os.path.basename(src_abs)))
        dst_there = bool(final_rel) and _exists(_fs_path(dest_root, final_rel))
        if dst_there and not src_there:
            # move completed before the crash — finish the index side
            pf = store.path_full(row["path_id"])
            mtime = pf["mtime"] if pf else 0
            ctime = pf["ctime"] if pf else 0
            new_status = "active" if row["kind"] == "keeper" else "purgatory"
            action = "apply_move" if row["kind"] == "keeper" else "apply_remove"
            dest_drive = store.ensure_managed_drive(dest_root, row["dest_label"])
            if pf:  # source row may already be gone if commit partially ran
                store.commit_relocation(row["id"], row["path_id"], dest_drive,
                                        final_rel, mtime, ctime, new_status, action)
        elif src_there and not dst_there:
            store.revert_move_to_pending(row["id"])
        else:
            conflicts.append({"pending_id": row["id"], "src": src_abs,
                              "dst": f"{dest_root}/{final_rel}",
                              "reason": "both or neither present — manual check needed"})
    return conflicts


def process(store: Store, *, dry_run: bool = False, verify: bool = True) -> Iterator[dict]:
    """Execute the queued keeper + dup relocations. Yields progress events."""
    holding = store.get_holding_dir()

    # crash recovery before planning new work
    if not dry_run:
        for c in reconcile_pending(store):
            yield {"kind": "conflict", **c}

    keepers = store.list_pending(kind="keeper", statuses=("pending",))
    for k in keepers:
        k["abspath"] = (str(Path(k["src_root"]) / k["rel"]) if k["src_root"] else k["rel"])
    dups = store.dup_candidates()
    for d in dups:
        d["abspath"] = (str(Path(d["src_root"]) / d["rel"]) if d["src_root"] else d["rel"])
        d["dest_root"] = holding
        d["dest_label"] = "holding"

    if dups and not holding:
        yield {"kind": "error", "fatal": True,
               "error": "holding folder is not set — configure it in Settings before processing"}
        return

    work = ([{**k, "kind": "keeper"} for k in keepers]
            + [{**d, "kind": "dup"} for d in dups])
    if not work:
        yield {"kind": "done", "moved": 0, "skipped": 0, "keepers": 0, "dups": 0, "dry_run": dry_run}
        return

    bad = _unreachable_source(work)
    if bad:
        yield {"kind": "error", "fatal": True,
               "error": f"source root unreachable, refusing to process: {bad} "
                        f"(an unmounted drive is not a deletion)"}
        return

    if not dry_run:
        err = _ensure_target_roots(work, holding)
        if err:
            yield {"kind": "error", "fatal": True, "error": err}
            return

    yield {"kind": "started", "total": len(work), "keepers": len(keepers),
           "dups": len(dups), "dry_run": dry_run, "verify": verify, "holding": holding}

    moved = skipped = 0
    for it in work:
        # full row (hash, mtime, ctime, status) — dup_candidates is lean
        pf = store.path_full(it["path_id"])
        if pf is None:
            skipped += 1
            yield {"kind": "skipped", "path": it.get("rel"), "reason": "path row vanished"}
            continue
        it.setdefault("hash", pf["hash"])
        it["mtime"], it["ctime"] = pf["mtime"], pf["ctime"]
        it["abspath"] = pf["abspath"]

        reason = _verify_source(store, it, verify)
        if reason:
            skipped += 1
            yield {"kind": "conflict", "path": it["rel"], "reason": reason}
            continue

        dest_rel = _noncolliding_rel(it["dest_root"], it["rel"])
        if dry_run:
            yield {"kind": "planned", "kind2": it["kind"], "path": it["rel"],
                   "dest_root": it["dest_root"], "dest_rel": dest_rel,
                   "label": it.get("dest_label"), "size": it.get("size")}
            continue

        # --- real move: two-phase, FS is source of truth ---
        if it["kind"] == "dup":
            with store._lock, store.tx():
                pending_id = store.enqueue_move(it["path_id"], it["hash"], "dup",
                                                "holding", it["dest_root"])
        else:
            pending_id = it["id"]

        new_status = "active" if it["kind"] == "keeper" else "purgatory"
        action = "apply_move" if it["kind"] == "keeper" else "apply_remove"
        dest_drive = store.ensure_managed_drive(it["dest_root"], it.get("dest_label"))
        try:
            store.set_move_moving(pending_id, it["abspath"], dest_rel)
            fs_move(it["abspath"], it["dest_root"], dest_rel,
                    verify_hash=it["hash"] if verify else None)
            store.commit_relocation(pending_id, it["path_id"], dest_drive,
                                    dest_rel, it["mtime"], it["ctime"], new_status, action)
        except Exception as e:  # noqa: BLE001 — surface, leave row in 'moving' for recovery
            skipped += 1
            yield {"kind": "error", "path": it["rel"], "error": str(e)}
            continue
        moved += 1
        yield {"kind": "moved", "kind2": it["kind"], "path": it["rel"],
               "dest_root": it["dest_root"], "dest_rel": dest_rel, "size": it.get("size")}

    yield {"kind": "done", "moved": moved, "skipped": skipped,
           "keepers": len(keepers), "dups": len(dups), "dry_run": dry_run}


def undo_last_process(store: Store) -> Optional[dict]:
    """Reverse the most recent executed relocation: move the file back to its
    original location, then undo the index side. The holding/destination copy is
    what makes this possible. Returns a summary, or None if nothing to undo."""
    d = store.last_executed_relocation()
    if d is None:
        return None
    import json
    payload = json.loads(d["payload_json"])
    src = payload["src"]
    drive = store.get_drive(src["drive_id"])
    orig_abs = str(Path(drive["root_path"]) / src["path"]) if drive and drive["root_path"] else src["path"]
    cur = store.path_full(payload["new_path_id"])
    if cur is None:
        raise ValueError("destination row vanished; cannot undo this relocation")
    # move the file back to where it came from (never overwrite)
    orig_parent = os.path.dirname(orig_abs)
    orig_rel = os.path.basename(orig_abs)
    back_rel = _noncolliding_rel(orig_parent, orig_rel)
    fs_move(cur["abspath"], orig_parent, back_rel, verify_hash=src["hash"])
    result = store.reverse_relocation(d["id"])
    result["restored_to"] = str(Path(orig_parent) / back_rel)
    return result
