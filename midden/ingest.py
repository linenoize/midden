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


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = _new_hasher()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# --- events for the CLI/UI to render ---
@dataclass
class IngestEvent:
    kind: str            # 'started' | 'hashed' | 'skipped' | 'skipped_symlink' | 'error' | 'done'
    path: Optional[str] = None
    hash: Optional[str] = None
    size: int = 0
    error: Optional[str] = None
    elapsed: float = 0.0


@dataclass
class IngestStats:
    files_seen: int = 0
    files_hashed: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_symlink: int = 0
    bytes_hashed: int = 0
    errors: int = 0


def ingest(
    root: Path,
    store: Store,
    label: Optional[str] = None,
    follow_symlinks: bool = False,
    skip_names: tuple[str, ...] = (MARKER,),
) -> Iterator[IngestEvent]:
    """Walk `root`, ingest into `store`. Yields events. Idempotent."""
    root = Path(root).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    drive = get_or_create(root, label=label)
    store.upsert_drive(drive.id, drive.label, drive.root_path)

    stats = IngestStats()
    started = time.time()
    yield IngestEvent(kind="started", path=str(root))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        for name in filenames:
            if name in skip_names:
                continue
            stats.files_seen += 1
            full = Path(dirpath) / name
            try:
                # symlinks: skip unless explicitly enabled
                if full.is_symlink() and not follow_symlinks:
                    stats.files_skipped_symlink += 1
                    yield IngestEvent(kind="skipped_symlink", path=str(full))
                    continue

                st = full.stat()
                size = st.st_size
                mtime = int(st.st_mtime)
                ctime = int(st.st_ctime)
                rel = str(full.relative_to(root)).replace("\\", "/")

                # idempotency: skip if (drive_id, path) exists and mtime+size unchanged
                # (we still re-touch observed_at via upsert_path so we know it's still there)
                existing = store.get_path(drive.id, rel)
                if existing is not None and existing["mtime"] == mtime:
                    # confirm size matches a record we already hashed
                    row = store.conn.execute(
                        "SELECT size FROM files WHERE hash=?",
                        (existing["hash"],),
                    ).fetchone()
                    if row and row["size"] == size:
                        # still mark the path as observed-just-now
                        store.upsert_path(existing["hash"], drive.id, rel, mtime, ctime)
                        stats.files_skipped_unchanged += 1
                        yield IngestEvent(
                            kind="skipped",
                            path=rel,
                            hash=existing["hash"],
                            size=size,
                        )
                        continue

                h = hash_file(full)
                mime, _ = mimetypes.guess_type(full.name)
                with store.tx():
                    store.upsert_file(h, size, mime)
                    store.upsert_path(h, drive.id, rel, mtime, ctime)

                stats.files_hashed += 1
                stats.bytes_hashed += size
                yield IngestEvent(kind="hashed", path=rel, hash=h, size=size)

            except (PermissionError, OSError) as e:
                stats.errors += 1
                yield IngestEvent(kind="error", path=str(full), error=str(e))

    elapsed = time.time() - started
    yield IngestEvent(kind="done", elapsed=elapsed, size=stats.bytes_hashed)
