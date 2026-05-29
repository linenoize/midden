"""SQLite access layer for the Midden index.

Single file, WAL mode, single writer convention. Connection per-thread.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  hash                 TEXT PRIMARY KEY,
  size                 INTEGER NOT NULL,
  mime                 TEXT,
  first_seen_at        INTEGER NOT NULL,
  status               TEXT NOT NULL DEFAULT 'active',  -- active | purgatory | canonical
  inferred_created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS drives (
  id                TEXT PRIMARY KEY,
  label             TEXT,
  root_path         TEXT,
  last_ingested_at  INTEGER
);

CREATE TABLE IF NOT EXISTS paths (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  hash         TEXT NOT NULL REFERENCES files(hash),
  drive_id     TEXT NOT NULL REFERENCES drives(id),
  path         TEXT NOT NULL,
  mtime        INTEGER,
  ctime        INTEGER,
  observed_at  INTEGER NOT NULL,
  UNIQUE(drive_id, path)
);
CREATE INDEX IF NOT EXISTS idx_paths_hash ON paths(hash);
CREATE INDEX IF NOT EXISTS idx_paths_drive ON paths(drive_id);

CREATE TABLE IF NOT EXISTS clusters (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT NOT NULL,            -- exact | near_image | doc_version | topic
  label           TEXT,
  canonical_hash  TEXT REFERENCES files(hash),
  created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_members (
  cluster_id  INTEGER NOT NULL REFERENCES clusters(id),
  hash        TEXT NOT NULL REFERENCES files(hash),
  confidence  REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (cluster_id, hash)
);
CREATE INDEX IF NOT EXISTS idx_cluster_members_hash ON cluster_members(hash);

CREATE TABLE IF NOT EXISTS tags (
  hash        TEXT NOT NULL REFERENCES files(hash),
  key         TEXT NOT NULL,
  value       TEXT NOT NULL,
  source      TEXT NOT NULL,  -- rule | llm | user
  confidence  REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (hash, key, value)
);

CREATE TABLE IF NOT EXISTS decisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            INTEGER NOT NULL,
  actor         TEXT NOT NULL,    -- user | auto
  action        TEXT NOT NULL,    -- mark_canonical | send_to_purgatory | tag | undo | ...
  payload_json  TEXT NOT NULL     -- enough to fully reverse
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---------- drives ----------
    def upsert_drive(self, drive_id: str, label: str, root_path: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO drives(id, label, root_path, last_ingested_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              label=excluded.label,
              root_path=excluded.root_path,
              last_ingested_at=excluded.last_ingested_at
            """,
            (drive_id, label, root_path, now),
        )

    # ---------- files & paths ----------
    def upsert_file(self, hash_: str, size: int, mime: Optional[str]) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO files(hash, size, mime, first_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hash) DO UPDATE SET
              mime=COALESCE(excluded.mime, files.mime),
              size=excluded.size
            """,
            (hash_, size, mime, now),
        )

    def upsert_path(
        self,
        hash_: str,
        drive_id: str,
        rel_path: str,
        mtime: int,
        ctime: int,
    ) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO paths(hash, drive_id, path, mtime, ctime, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(drive_id, path) DO UPDATE SET
              hash=excluded.hash,
              mtime=excluded.mtime,
              ctime=excluded.ctime,
              observed_at=excluded.observed_at
            """,
            (hash_, drive_id, rel_path, mtime, ctime, now),
        )

    def get_path(self, drive_id: str, rel_path: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT hash, mtime FROM paths WHERE drive_id=? AND path=?",
            (drive_id, rel_path),
        ).fetchone()

    # ---------- queries ----------
    def stats(self) -> dict:
        c = self.conn
        n_files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_paths = c.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
        n_drives = c.execute("SELECT COUNT(*) FROM drives").fetchone()[0]
        bytes_unique = c.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()[0]
        bytes_total = c.execute(
            "SELECT COALESCE(SUM(f.size), 0) FROM paths p JOIN files f ON f.hash=p.hash"
        ).fetchone()[0]
        return {
            "files_unique": n_files,
            "paths_total": n_paths,
            "drives": n_drives,
            "bytes_unique": bytes_unique,
            "bytes_total": bytes_total,
            "bytes_saveable": bytes_total - bytes_unique,
        }

    def exact_duplicate_groups(self, min_size: int = 1) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT f.hash, f.size, COUNT(p.id) AS n_paths
            FROM files f
            JOIN paths p ON p.hash = f.hash
            WHERE f.size >= ?
            GROUP BY f.hash
            HAVING n_paths > 1
            ORDER BY f.size * (n_paths - 1) DESC
            """,
            (min_size,),
        ).fetchall()
        groups = []
        for r in rows:
            paths = self.conn.execute(
                "SELECT drive_id, path FROM paths WHERE hash=? ORDER BY path",
                (r["hash"],),
            ).fetchall()
            groups.append({
                "hash": r["hash"],
                "size": r["size"],
                "n_paths": r["n_paths"],
                "paths": [dict(p) for p in paths],
            })
        return groups

    def close(self) -> None:
        self.conn.close()
