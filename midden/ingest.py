"""Walk a directory, hash every file, store results.

Design:
- Read-only by construction (we never open files for write).
- Idempotent: re-running over the same tree is cheap.
  - If (drive_id, path) already exists AND (size, mtime) match, we skip hashing.
- BLAKE3 if available; falls back to SHA-256 silently.
- Skip symlinks/junctions by default (cycles + unclear semantics on inherited drives).
- Yields events the caller can render as progress.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .store import Store
from .drives import DriveInfo, get_or_create, MARKER

# --- hashing ---
try:
    import blake3 as _blake3
    HASH_NAME = "blake3"

    def _new_hasher():
        return _blake3.blake3()
except ImportError:
    HASH_NAME = "sha256"

    def _new_hasher():
        return hashlib.sha256()


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400  # Windows junctions / mount points


def _under_any(p: Path, roots: list[Path]) -> bool:
    """True if `p` resolves at or below any root in `roots`."""
    if not roots:
        return False
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            rp.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def is_reparse_point(p: Path) -> bool:
    """True if `p` is a symlink or (Windows) a junction / reparse point.

    `Path.is_symlink()` returns False for Windows directory junctions, so
    `os.walk(followlinks=False)` would still descend into them. We additionally
    check the reparse-point attribute (design D7: never traverse these). Uses
    `os.lstat` so we inspect the link/junction itself, not its target.
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    # st_file_attributes exists only on Windows; 0 elsewhere -> False.
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


# How many files to buffer before committing one transaction. Amortizes
# per-transaction overhead across a batch — the lever that matters at millions
# of files. Hashing happens OUTSIDE the transaction, so a larger batch doesn't
# lengthen lock hold proportionally.
BATCH_N = 512


class ShortReadError(OSError):
    """Bytes actually read from a file differ from the size stat() reported.

    A short (or over-) read means partial / placeholder / sparse / racy content.
    Hashing it would produce a digest of something other than the file's real
    bytes, which could false-group it as an "exact duplicate". Callers skip and
    report rather than index a bogus hash. Subclasses OSError so a caller that
    only cares about "couldn't process this file" still catches it — but ingest
    handles it explicitly to label it `short_read`.
    """

    def __init__(self, path, expected: int, got: int):
        self.path = str(path)
        self.expected = expected
        self.got = got
        super().__init__(
            f"short read: {path} expected {expected} bytes, read {got}"
        )


def hash_file(
    path: Path,
    chunk_size: int = 4 * 1024 * 1024,
    expected_size: Optional[int] = None,
) -> str:
    """Stream-hash `path`. If `expected_size` is given and the bytes actually
    read differ from it, raise ShortReadError (the read content is not the whole
    file). `expected_size=None` disables the check (back-compat for diagnostics
    and tests that hash an arbitrary path)."""
    h = _new_hasher()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    if expected_size is not None and total != expected_size:
        raise ShortReadError(path, expected_size, total)
    return h.hexdigest()


# --- events for the CLI/UI to render ---
@dataclass
class IngestEvent:
    kind: str            # 'started'|'hashed'|'skipped'|'skipped_symlink'|'short_read'|'error'|'pruned'|'done'
    path: Optional[str] = None
    hash: Optional[str] = None
    size: int = 0
    error: Optional[str] = None
    elapsed: float = 0.0
    count: int = 0       # 'pruned': number of vanished paths removed


@dataclass
class IngestStats:
    files_seen: int = 0
    files_hashed: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_symlink: int = 0
    files_short_read: int = 0
    files_pruned: int = 0
    bytes_hashed: int = 0
    errors: int = 0


def ingest(
    root: Path,
    store: Store,
    label: Optional[str] = None,
    follow_symlinks: bool = False,
    skip_names: tuple[str, ...] = (MARKER,),
    prune: bool = False,
) -> Iterator[IngestEvent]:
    """Walk `root`, ingest into `store`. Yields events. Idempotent.

    Resume: re-running is cheap. Files whose (mtime, size) match an existing
    record are not re-hashed, so an interrupted run picks up where it left off
    just by being re-invoked — the already-hashed files fall through the skip
    path. Writes are batched (BATCH_N per transaction).

    prune: after a FULLY completed walk, hard-delete active paths that were not
    re-observed this run (i.e. gone from disk). Off by default and only safe on
    a complete pass — never enable it for a partial/aborted scan, or unscanned
    files would be deleted from the index. The standalone `reconcile` command is
    the safer route (it stat-checks rather than trusting walk completeness).
    """
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    drive = get_or_create(root, label=label)
    store.upsert_drive(drive.id, drive.label, drive.root_path)

    # Managed roots (organize destinations + holding folder) are NEVER walked —
    # they may live inside this scanned drive, and re-discovering relocated files
    # there would rebuild the exact-dup groups the user just resolved.
    managed_roots = [Path(r).resolve() for r in store.managed_drive_roots()]

    stats = IngestStats()
    started = time.time()
    run_started = int(started)  # epoch boundary for --prune (observed_at < this)
    yield IngestEvent(kind="started", path=str(root))

    # Batched-write buffers; flushed every BATCH_N files and once at the end.
    file_buf: list[tuple] = []
    path_buf: list[tuple] = []

    def flush() -> None:
        if file_buf or path_buf:
            store.flush_writes(file_buf, path_buf)
            file_buf.clear()
            path_buf.clear()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        if not follow_symlinks:
            # Prune directory symlinks AND Windows junctions/reparse points in
            # place so os.walk never descends into them. os.walk(followlinks=
            # False) already skips true symlinked dirs, but junctions are NOT
            # symlinks on Windows, so it would otherwise follow them (cycles +
            # double-ingest of the same content under two paths).
            kept = []
            for d in dirnames:
                dpath = Path(dirpath) / d
                if is_reparse_point(dpath):
                    stats.files_skipped_symlink += 1
                    yield IngestEvent(kind="skipped_symlink", path=str(dpath))
                elif _under_any(dpath, managed_roots):
                    continue  # managed dest/holding subtree — never ingest
                else:
                    kept.append(d)
            dirnames[:] = kept
        # If the scan root itself is a managed folder, skip its files too (the
        # dirname pruning above only protects descendants, not the current dir).
        if _under_any(Path(dirpath), managed_roots):
            continue
        for name in filenames:
            if name in skip_names:
                continue
            stats.files_seen += 1
            full = Path(dirpath) / name
            try:
                # symlinks / reparse points: skip unless explicitly enabled
                if not follow_symlinks and is_reparse_point(full):
                    stats.files_skipped_symlink += 1
                    yield IngestEvent(kind="skipped_symlink", path=str(full))
                    continue

                st = full.stat()
                size = st.st_size
                mtime = int(st.st_mtime)
                ctime = int(st.st_ctime)
                now = int(time.time())
                rel = str(full.relative_to(root)).replace("\\", "/")

                # idempotency / resume: skip if (drive_id, path) exists and both
                # mtime AND size are unchanged. get_path joins files, so size is
                # already in hand — no second query on this hot path. We still
                # re-touch observed_at (buffered) so --prune knows it's present.
                existing = store.get_path(drive.id, rel)
                if (
                    existing is not None
                    and existing["mtime"] == mtime
                    and existing["size"] == size
                ):
                    path_buf.append(
                        (existing["hash"], drive.id, rel, mtime, ctime, now)
                    )
                    stats.files_skipped_unchanged += 1
                    yield IngestEvent(
                        kind="skipped", path=rel, hash=existing["hash"], size=size
                    )
                    if len(path_buf) >= BATCH_N:
                        flush()
                    continue

                # Guard: hash the file and verify we read exactly `size` bytes.
                # A short/over read (placeholder, sparse, truncated, or a file
                # being written) yields a hash of partial content that could
                # false-group as an exact duplicate — skip + report instead of
                # indexing a bogus digest. ShortReadError subclasses OSError, so
                # this inner handler MUST precede the outer OSError catch.
                try:
                    h = hash_file(full, expected_size=size)
                except ShortReadError as e:
                    stats.files_short_read += 1
                    yield IngestEvent(
                        kind="short_read", path=rel, size=size, error=str(e)
                    )
                    continue
                mime, _ = mimetypes.guess_type(full.name)
                file_buf.append((h, size, mime, now))
                path_buf.append((h, drive.id, rel, mtime, ctime, now))

                stats.files_hashed += 1
                stats.bytes_hashed += size
                yield IngestEvent(kind="hashed", path=rel, hash=h, size=size)

                if len(path_buf) >= BATCH_N:
                    flush()

            except (PermissionError, OSError) as e:
                stats.errors += 1
                yield IngestEvent(kind="error", path=str(full), error=str(e))

    flush()  # commit the tail batch before pruning / finishing

    # --prune: remove paths not re-observed this run. Guarded to a non-empty
    # completed walk so an empty/unreadable scan can't wipe the drive's index.
    if prune and stats.files_seen > 0:
        stale = store.stale_path_rows(drive.id, run_started)
        if stale:
            n = store.delete_paths(
                drive.id, [dict(r) for r in stale], reason="ingest_prune"
            )
            stats.files_pruned = n
            yield IngestEvent(kind="pruned", count=n)

    elapsed = time.time() - started
    yield IngestEvent(kind="done", elapsed=elapsed, size=stats.bytes_hashed)
