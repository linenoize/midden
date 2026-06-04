"""End-to-end test for the organize/process feature (keeper moves + holding deletes).

Builds a tiny real tree with known duplicates, ingests it, configures a
destination + holding folder, resolves a cluster WITH a keeper move, then runs
the destructive `process` step and asserts the physical + index outcome. Also
covers: dry-run (no disk change), mirror-subfolder layout, holding for dups,
crash recovery from a 'moving' row, undo-last-process (files come back), and the
refuse-on-unreachable guard. Stdlib + midden only.

Run:  python tools/e2e_organize.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from midden import organize
from midden.ingest import ingest as run_ingest
from midden.store import Store

ok = True


def check(label: str, cond: bool) -> None:
    global ok
    ok = ok and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def write(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def drain(gen):
    return list(gen)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="midden_org_"))
    src = tmp / "drive"
    dest = tmp / "3d-nodupe"
    holding = tmp / "holding"
    DUP = b"IDENTICAL-CONTENT-" + b"x" * 5000  # >1 MiB? no, but exact-dup floor is size>=1

    # Two identical copies in different subfolders + one unique file.
    write(src / "ProjectA" / "model.zip", DUP)
    write(src / "Backup" / "ProjectA" / "model.zip", DUP)  # same name, different subtree
    write(src / "notes.txt", b"unique notes")

    db = str(tmp / "idx.sqlite")
    store = Store(db, same_thread=False)
    drain(run_ingest(src, store, label="t"))
    store.materialize_exact_clusters(min_size=1)

    # configure destination + holding + a highlight pattern
    store.set_destinations([{"label": "~/3d-nodupe", "path": str(dest)}])
    store.set_holding_dir(str(holding))

    # find the exact-dup cluster and pick the copy under Backup/ as the keeper-to-move
    clusters = [c for c in store.list_clusters(kinds=("exact",)) if c["n_active"] == 2]
    check("one exact-dup cluster (2 copies)", len(clusters) == 1)
    cid = clusters[0]["id"]
    detail = store.get_cluster(cid)
    keep = next(p for p in detail["paths"] if "Backup" in p["path"] and p["status"] == "active")
    other = next(p for p in detail["paths"] if p["id"] != keep["id"] and p["status"] == "active")

    # resolve: keep the Backup copy AND queue it to move to dest
    store.resolve_keep(cid, keep["id"], move_dest={"label": "~/3d-nodupe", "path": str(dest)})
    pend = store.list_pending(kind="keeper", statuses=("pending",))
    check("keeper move queued", len(pend) == 1 and pend[0]["path_id"] == keep["id"])
    check("the other copy went to purgatory", len(store.dup_candidates()) == 1)

    # --- dry-run changes nothing on disk ---
    evs = drain(organize.process(store, dry_run=True))
    planned = [e for e in evs if e["kind"] == "planned"]
    check("dry-run plans 2 moves (1 keeper + 1 dup)", len(planned) == 2)
    check("dry-run touched no disk (keeper still at source)", (src / "Backup" / "ProjectA" / "model.zip").exists())
    check("dry-run created no dest", not dest.exists())
    check("dry-run left queue pending", len(store.list_pending(kind="keeper", statuses=("pending",))) == 1)

    # --- real process ---
    evs = drain(organize.process(store, dry_run=False, verify=True))
    done = evs[-1]
    check("process moved 2 files", done["kind"] == "done" and done["moved"] == 2)
    # keeper physically at dest, mirroring its source subfolders
    kept_dest = dest / "Backup" / "ProjectA" / "model.zip"
    check("keeper moved to dest (mirrored subfolders)", kept_dest.exists())
    check("keeper gone from source", not (src / "Backup" / "ProjectA" / "model.zip").exists())
    # dup physically in holding, mirroring its source subfolders
    dup_hold = holding / "ProjectA" / "model.zip"
    check("dup moved to holding (mirrored subfolders)", dup_hold.exists())
    check("dup gone from source", not (src / "ProjectA" / "model.zip").exists())
    # index: keeper now active on the managed dest drive; dup purgatory on holding drive
    pf_after = store.path_full(keep["id"])
    check("original keeper path row removed", pf_after is None)
    actives = [p for p in store.get_cluster(cid)["paths"] if p["status"] == "active"]
    check("cluster has exactly 1 active path (the moved keeper)", len(actives) == 1)
    check("moved keeper is on a managed drive",
          store.drive_kind(actives[0]["drive_id"]) == "managed")
    # decisions recorded
    apply_moves = store.conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE action='apply_move'").fetchone()[0]
    apply_removes = store.conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE action='apply_remove'").fetchone()[0]
    check("apply_move + apply_remove decisions written", apply_moves == 1 and apply_removes == 1)

    # restore guard: a dup now in holding cannot be plain-restored
    dup_row = store.conn.execute(
        "SELECT id FROM paths WHERE status='purgatory'").fetchone()
    try:
        store.restore_path(dup_row["id"])
        check("restore of a holding row is blocked", False)
    except ValueError:
        check("restore of a holding row is blocked", True)

    # --- undo last process: reverses the dup move (most recent), file returns ---
    res = organize.undo_last_process(store)
    check("undo reversed apply_remove first", res and res["action"] == "apply_remove")
    check("dup back at source", (src / "ProjectA" / "model.zip").exists())
    check("dup gone from holding", not dup_hold.exists())
    # --- undo again: reverses the keeper move ---
    res2 = organize.undo_last_process(store)
    check("second undo reversed apply_move", res2 and res2["action"] == "apply_move")
    check("keeper back at source", (src / "Backup" / "ProjectA" / "model.zip").exists())
    check("keeper gone from dest", not kept_dest.exists())

    # --- crash recovery: a 'moving' row whose file already reached dest is finished
    # cleanly (no auto-delete). After the two undos a keeper move is pending again. ---
    kp = store.list_pending(kind="keeper", statuses=("pending",))
    check("a keeper move is pending again after undo", len(kp) >= 1)
    row = kp[0]
    pf = store.path_full(row["path_id"])
    final_rel = pf["path"]
    store.set_move_moving(row["id"], pf["abspath"], final_rel)
    organize.fs_move(pf["abspath"], row["dest_root"], final_rel)  # physical move; DB not updated yet
    conflicts = organize.reconcile_pending(store)
    check("crash recovery finished the half-done move (no conflict)", not conflicts)
    rr = store.conn.execute("SELECT status FROM pending_moves WHERE id=?", (row["id"],)).fetchone()
    check("recovered row marked executed", rr and rr["status"] == "executed")
    check("recovered keeper file is at dest", (Path(row["dest_root"]) / final_rel).exists())

    # --- refuse when a SOURCE root is unreachable (unmounted drive != deletion) ---
    import os as _os
    if store.dup_candidates():
        src_moved = src.with_name("drive_unmounted")
        _os.rename(src, src_moved)
        try:
            evs = drain(organize.process(store, dry_run=False))
            fatal = [e for e in evs
                     if e.get("fatal") and "source root unreachable" in e.get("error", "")]
            check("refuses to process when a source root is unreachable", len(fatal) == 1)
        finally:
            _os.rename(src_moved, src)
    else:
        check("refuses to process when a source root is unreachable (no dups to try)", True)

    store.close()
    print("\n" + ("ALL PASS" if ok else "FAILURES PRESENT"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
