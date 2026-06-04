"""SQLite access layer for the Midden index.

Single file, WAL mode, single writer convention. Connection per-thread (the web
server passes same_thread=False and serializes writes via self._lock).

This is the ONLY module that touches the schema.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Exact-dup groups reclaiming less than this are hidden from the default review
# queue (still reachable with min_reclaimable=0). Thousands of sub-KB identical
# configs/icons are technically correct dups but bury the high-value ones.
EXACT_REVIEW_FLOOR = 1_048_576  # 1 MiB

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
  last_ingested_at  INTEGER,
  kind              TEXT NOT NULL DEFAULT 'scanned'  -- scanned | managed
);

CREATE TABLE IF NOT EXISTS paths (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  hash         TEXT NOT NULL REFERENCES files(hash),
  drive_id     TEXT NOT NULL REFERENCES drives(id),
  path         TEXT NOT NULL,
  mtime        INTEGER,
  ctime        INTEGER,
  observed_at  INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',  -- active | purgatory
  UNIQUE(drive_id, path)
);
CREATE INDEX IF NOT EXISTS idx_paths_hash ON paths(hash);
CREATE INDEX IF NOT EXISTS idx_paths_drive ON paths(drive_id);
CREATE INDEX IF NOT EXISTS idx_paths_status ON paths(status);

CREATE TABLE IF NOT EXISTS clusters (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT NOT NULL,            -- exact | near_image | doc_version | topic
  label           TEXT,
  canonical_hash  TEXT REFERENCES files(hash),
  created_at      INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open'  -- open | resolved
);

CREATE TABLE IF NOT EXISTS cluster_members (
  cluster_id  INTEGER NOT NULL REFERENCES clusters(id),
  hash        TEXT NOT NULL REFERENCES files(hash),
  confidence  REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (cluster_id, hash)
);
CREATE INDEX IF NOT EXISTS idx_cluster_members_hash ON cluster_members(hash);

CREATE TABLE IF NOT EXISTS signatures (
  hash   TEXT NOT NULL REFERENCES files(hash),
  algo   TEXT NOT NULL,   -- simhash_text | phash_image
  value  TEXT NOT NULL,   -- hex
  PRIMARY KEY (hash, algo)
);
CREATE INDEX IF NOT EXISTS idx_signatures_algo ON signatures(algo);

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

-- Small key/value config travelling with the index (JSON values). Holds the
-- organize destinations, the holding folder, and review highlight patterns.
CREATE TABLE IF NOT EXISTS settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

-- Queue of physical file relocations to be executed by organize.py during the
-- `process` step. Each row is a two-phase op: 'pending' -> 'moving' (source/dest
-- resolved + committed BEFORE the OS move) -> 'executed' (after the move + index
-- update). The 'moving' state is what makes a crash recoverable. kind='keeper'
-- (a kept file going to a destination) or 'dup' (a purgatory file going to the
-- holding folder).
CREATE TABLE IF NOT EXISTS pending_moves (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  path_id      INTEGER NOT NULL,                    -- live paths.id (no FK: rows are
                                                    -- relocated/deleted during execute;
                                                    -- full reversal data is in decisions)
  hash         TEXT NOT NULL,
  kind         TEXT NOT NULL,                       -- keeper | dup
  dest_label   TEXT,
  dest_root    TEXT NOT NULL,                       -- absolute destination/holding root
  src_abspath  TEXT,                                -- resolved at 'moving' time (crash recovery)
  final_path   TEXT,                                -- actual mirrored rel path under dest_root after move
  enqueued_at  INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending'      -- pending | moving | executed | canceled
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_moves(status);
CREATE INDEX IF NOT EXISTS idx_pending_path ON pending_moves(path_id);
"""


class Store:
    def __init__(self, db_path: Path, same_thread: bool = True):
        # The FastAPI server (same_thread=False) runs sync handlers in a
        # threadpool. A single sqlite3.Connection is NOT safe for concurrent use
        # across threads, so each thread gets its OWN connection via a
        # threading.local (see the `conn` property). WAL mode lets those
        # connections read concurrently with a single writer; we still serialize
        # *writers* with self._lock so two threads never collide on the write.
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._same_thread = same_thread
        self._local = threading.local()
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=self._same_thread
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")  # wait out a concurrent writer
        conn.row_factory = sqlite3.Row
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self._connect()
        return c

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent, additive migrations for DBs created before a column existed."""
        path_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(paths)")}
        if "status" not in path_cols:
            self.conn.execute(
                "ALTER TABLE paths ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paths_status ON paths(status)"
            )
        cluster_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(clusters)")}
        if "status" not in cluster_cols:
            self.conn.execute(
                "ALTER TABLE clusters ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
            )
        # Image dimensions on the perceptual-signature row. Used as an aspect-ratio
        # structural prior in near-image clustering (dHash discards aspect). Nullable
        # — pre-existing image signatures backfill on the next signature recompute.
        sig_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(signatures)")}
        if "w" not in sig_cols:
            self.conn.execute("ALTER TABLE signatures ADD COLUMN w INTEGER")
        if "h" not in sig_cols:
            self.conn.execute("ALTER TABLE signatures ADD COLUMN h INTEGER")
        # Managed drives (organize destinations / holding folder) vs scanned ones.
        drive_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(drives)")}
        if "kind" not in drive_cols:
            self.conn.execute(
                "ALTER TABLE drives ADD COLUMN kind TEXT NOT NULL DEFAULT 'scanned'"
            )

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
        # Join files so the idempotency check (hash + mtime + size) needs ONE
        # query per already-seen file, not two. This is the hot path on a
        # resume/re-ingest of a large tree.
        return self.conn.execute(
            """
            SELECT p.hash AS hash, p.mtime AS mtime, f.size AS size
            FROM paths p JOIN files f ON f.hash = p.hash
            WHERE p.drive_id=? AND p.path=?
            """,
            (drive_id, rel_path),
        ).fetchone()

    def flush_writes(
        self,
        file_rows: list[tuple],
        path_rows: list[tuple],
    ) -> None:
        """Apply a batch of file + path upserts in a SINGLE transaction.

        Batching amortizes per-transaction overhead across many files — the
        difference that matters when ingesting millions of files. Files are
        written before paths so the paths FK (paths.hash -> files.hash) is
        satisfied within the transaction. Lock is held only for the write, not
        for hashing (the caller hashes outside this call).

        file_rows: (hash, size, mime, now)
        path_rows: (hash, drive_id, path, mtime, ctime, now)
        """
        if not file_rows and not path_rows:
            return
        with self._lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                if file_rows:
                    c.executemany(
                        """
                        INSERT INTO files(hash, size, mime, first_seen_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(hash) DO UPDATE SET
                          mime=COALESCE(excluded.mime, files.mime),
                          size=excluded.size
                        """,
                        file_rows,
                    )
                if path_rows:
                    c.executemany(
                        """
                        INSERT INTO paths(hash, drive_id, path, mtime, ctime, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(drive_id, path) DO UPDATE SET
                          hash=excluded.hash,
                          mtime=excluded.mtime,
                          ctime=excluded.ctime,
                          observed_at=excluded.observed_at
                        """,
                        path_rows,
                    )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ---------- drives & reconciliation ----------
    def list_drives(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, label, root_path, last_ingested_at, kind FROM drives ORDER BY label"
        ).fetchall()

    def get_drive(self, drive_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, label, root_path, last_ingested_at, kind FROM drives WHERE id=?",
            (drive_id,),
        ).fetchone()

    def active_paths_for_drive(self, drive_id: str) -> list[sqlite3.Row]:
        """Active path observations for a drive, with size (for reconcile)."""
        return self.conn.execute(
            """
            SELECT p.id AS id, p.hash AS hash, p.path AS path,
                   p.mtime AS mtime, p.ctime AS ctime, f.size AS size
            FROM paths p JOIN files f ON f.hash = p.hash
            WHERE p.drive_id=? AND p.status='active'
            """,
            (drive_id,),
        ).fetchall()

    def stale_path_rows(self, drive_id: str, before_ts: int) -> list[sqlite3.Row]:
        """Active paths for a drive NOT re-observed since `before_ts`.

        Used by `ingest --prune` after a fully-completed walk: any active path
        whose observed_at predates this run's start was not seen this pass, so
        the file is gone from disk.
        """
        return self.conn.execute(
            """
            SELECT p.id AS id, p.hash AS hash, p.path AS path,
                   p.mtime AS mtime, p.ctime AS ctime, f.size AS size
            FROM paths p JOIN files f ON f.hash = p.hash
            WHERE p.drive_id=? AND p.status='active' AND p.observed_at < ?
            """,
            (drive_id, before_ts),
        ).fetchall()

    def delete_paths(
        self,
        drive_id: str,
        rows: list[dict],
        *,
        actor: str = "auto",
        reason: str = "reconcile_delete",
    ) -> int:
        """Hard-delete path rows, snapshotting them to `decisions` first.

        The deletion is real (rows leave the `paths` table — no phantom dups),
        but the decisions snapshot makes it reversible (invariant #7): the
        payload carries enough to re-INSERT every deleted observation. One
        transaction so the audit row and the deletes commit together.
        """
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        payload = json.dumps({
            "drive_id": drive_id,
            "reason": reason,
            "paths": [
                {"path": r["path"], "hash": r["hash"],
                 "mtime": r["mtime"], "ctime": r["ctime"]}
                for r in rows
            ],
        })
        now = int(time.time())
        with self._lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                c.execute(
                    "INSERT INTO decisions(ts, actor, action, payload_json) "
                    "VALUES (?, ?, ?, ?)",
                    (now, actor, reason, payload),
                )
                c.executemany(
                    "DELETE FROM paths WHERE id=?", [(i,) for i in ids]
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        return len(ids)

    def orphan_file_count(self) -> int:
        """Files with no remaining active path (informational for reconcile)."""
        return self.conn.execute(
            """
            SELECT COUNT(*) FROM files f
            WHERE NOT EXISTS (
                SELECT 1 FROM paths p WHERE p.hash=f.hash AND p.status='active'
            )
            """
        ).fetchone()[0]

    # ---------- stats ----------
    def stats(self) -> dict:
        c = self.conn
        n_files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_paths = c.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
        n_active = c.execute("SELECT COUNT(*) FROM paths WHERE status='active'").fetchone()[0]
        n_purgatory = c.execute("SELECT COUNT(*) FROM paths WHERE status='purgatory'").fetchone()[0]
        n_drives = c.execute("SELECT COUNT(*) FROM drives").fetchone()[0]
        bytes_unique = c.execute(
            """
            SELECT COALESCE(SUM(size), 0) FROM files
            WHERE hash IN (SELECT DISTINCT hash FROM paths WHERE status='active')
            """
        ).fetchone()[0]
        bytes_total = c.execute(
            """
            SELECT COALESCE(SUM(f.size), 0)
            FROM paths p JOIN files f ON f.hash=p.hash
            WHERE p.status='active'
            """
        ).fetchone()[0]
        bytes_reclaimed = c.execute(
            """
            SELECT COALESCE(SUM(f.size), 0)
            FROM paths p JOIN files f ON f.hash=p.hash
            WHERE p.status='purgatory'
            """
        ).fetchone()[0]
        return {
            "files_unique": n_files,
            "paths_total": n_paths,
            "paths_active": n_active,
            "paths_purgatory": n_purgatory,
            "drives": n_drives,
            "bytes_unique": bytes_unique,
            "bytes_total": bytes_total,
            "bytes_saveable": bytes_total - bytes_unique,
            "bytes_reclaimed": bytes_reclaimed,
        }

    def overview(self) -> dict:
        """Stats + review-queue counts (all cluster kinds) for the UI Overview."""
        s = self.stats()
        c = self.conn
        s["clusters_total"] = c.execute("SELECT COUNT(*) FROM clusters").fetchone()[0]
        s["clusters_unresolved"] = c.execute(
            "SELECT COUNT(*) FROM clusters WHERE status='open'"
        ).fetchone()[0]
        s["clusters_resolved"] = s["clusters_total"] - s["clusters_unresolved"]
        by_kind = {}
        for r in c.execute(
            "SELECT kind, COUNT(*) n FROM clusters WHERE status='open' GROUP BY kind"
        ):
            by_kind[r["kind"]] = r["n"]
        s["open_by_kind"] = by_kind
        # The dedup review queue is exact/doc_version/near_image only — topic
        # clusters are non-destructive groupings shown in the Projects view, so
        # they must not inflate the "to review" count or they'd never clear.
        dedup_kinds = ("exact", "doc_version", "near_image")
        s["clusters_dedup_open"] = sum(v for k, v in by_kind.items() if k in dedup_kinds)
        s["topic_clusters"] = c.execute(
            "SELECT COUNT(*) FROM clusters WHERE kind='topic'"
        ).fetchone()[0]
        # How many open exact groups the default queue hides as low-value (so the
        # UI can say "N trivially-small duplicate groups hidden").
        s["exact_review_floor"] = EXACT_REVIEW_FLOOR
        s["exact_hidden"] = c.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT c.id,
                COALESCE(SUM(CASE WHEN p.status='active' THEN f.size END),0)
                - COALESCE(MAX(CASE WHEN p.status='active' THEN f.size END),0) AS recl
              FROM clusters c
              JOIN cluster_members cm ON cm.cluster_id=c.id
              JOIN files f ON f.hash=cm.hash
              LEFT JOIN paths p ON p.hash=cm.hash
              WHERE c.kind='exact' AND c.status='open'
              GROUP BY c.id
              HAVING COUNT(CASE WHEN p.status='active' THEN 1 END) > 0 AND recl < ?
            )
            """,
            (EXACT_REVIEW_FLOOR,),
        ).fetchone()[0]
        return s

    def exact_duplicate_groups(self, min_size: int = 1) -> list[dict]:
        """Live exact-dup groups over *active* paths (purgatory excluded)."""
        rows = self.conn.execute(
            """
            SELECT f.hash, f.size, COUNT(p.id) AS n_paths
            FROM files f
            JOIN paths p ON p.hash = f.hash AND p.status='active'
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
                "SELECT drive_id, path FROM paths WHERE hash=? AND status='active' ORDER BY path",
                (r["hash"],),
            ).fetchall()
            groups.append({
                "hash": r["hash"],
                "size": r["size"],
                "n_paths": r["n_paths"],
                "paths": [dict(p) for p in paths],
            })
        return groups

    # ---------- signatures (near-dup fingerprints) ----------
    def upsert_signature(self, hash_: str, algo: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO signatures(hash, algo, value) VALUES (?, ?, ?)
                ON CONFLICT(hash, algo) DO UPDATE SET value=excluded.value
                """,
                (hash_, algo, value),
            )

    def get_signatures(self, algo: str, active_only: bool = True) -> dict[str, str]:
        q = "SELECT hash, value FROM signatures WHERE algo=?"
        if active_only:
            q += " AND hash IN (SELECT DISTINCT hash FROM paths WHERE status='active')"
        return {r["hash"]: r["value"] for r in self.conn.execute(q, (algo,))}

    def upsert_image_dims(self, hash_: str, w: int, h: int) -> None:
        """Attach (width, height) to an existing phash_image signature row."""
        with self._lock:
            self.conn.execute(
                "UPDATE signatures SET w=?, h=? WHERE hash=? AND algo='phash_image'",
                (w, h, hash_),
            )

    def get_image_dims(self, active_only: bool = True) -> dict[str, tuple]:
        """hash -> (w, h) for image signatures. (None, None) where not yet known."""
        q = "SELECT hash, w, h FROM signatures WHERE algo='phash_image'"
        if active_only:
            q += " AND hash IN (SELECT DISTINCT hash FROM paths WHERE status='active')"
        return {r["hash"]: (r["w"], r["h"]) for r in self.conn.execute(q)}

    def image_hashes_missing_dims(self) -> set:
        """Image signatures lacking w/h (need a backfill recompute)."""
        return {r["hash"] for r in self.conn.execute(
            "SELECT hash FROM signatures WHERE algo='phash_image' AND (w IS NULL OR h IS NULL)")}

    def path_abspath(self, path_id: int) -> Optional[str]:
        """Absolute on-disk path for a specific path row (drive root + rel),
        resolved strictly from the DB. Used to inspect a concrete file location."""
        r = self.conn.execute(
            """
            SELECT d.root_path AS root_path, p.path AS path
            FROM paths p JOIN drives d ON d.id=p.drive_id
            WHERE p.id=?
            """,
            (path_id,),
        ).fetchone()
        if not r:
            return None
        return str(Path(r["root_path"]) / r["path"]) if r["root_path"] else r["path"]

    def thumb_source(self, hash_: str) -> Optional[str]:
        """Absolute path of ONE active location for `hash_`, for thumbnailing.
        Resolved strictly from the DB (never a client-supplied path)."""
        r = self.conn.execute(
            """
            SELECT d.root_path AS root_path, p.path AS path, f.mime AS mime
            FROM files f
            JOIN paths p ON p.hash=f.hash AND p.status='active'
            JOIN drives d ON d.id=p.drive_id
            WHERE f.hash=?
            LIMIT 1
            """,
            (hash_,),
        ).fetchone()
        if not r:
            return None
        return str(Path(r["root_path"]) / r["path"]) if r["root_path"] else r["path"]

    # ---------- tags (rule | llm | user) ----------
    def upsert_tag(self, hash_: str, key: str, value: str,
                   source: str, confidence: float = 1.0) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO tags(hash, key, value, source, confidence)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(hash, key, value) DO UPDATE SET
                  source=excluded.source, confidence=excluded.confidence
                """,
                (hash_, key, value, source, confidence),
            )

    def tags_for(self, hash_: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT key, value, source, confidence FROM tags WHERE hash=?", (hash_,))]

    def hashes_with_tag(self, key: str, source: Optional[str] = None) -> set[str]:
        q, params = "SELECT DISTINCT hash FROM tags WHERE key=?", [key]
        if source:
            q += " AND source=?"
            params.append(source)
        return {r["hash"] for r in self.conn.execute(q, params)}

    def topic_groups(self, min_size: int = 2) -> list[dict]:
        """Active hashes grouped by their `topic_slug` tag, groups of >= min_size.

        Each group carries a representative human label (the `topic` tag). This is
        the basis for kind='topic' clusters — non-redundant "looks like one project"
        groupings, distinct from the dedup cluster kinds.
        """
        rows = self.conn.execute(
            """
            SELECT ts.value AS slug, ts.hash AS hash,
                   (SELECT value FROM tags WHERE hash=ts.hash AND key='topic' LIMIT 1) AS label
            FROM tags ts
            WHERE ts.key='topic_slug'
              AND ts.hash IN (SELECT DISTINCT hash FROM paths WHERE status='active')
            """
        ).fetchall()
        by: dict[str, dict] = {}
        for r in rows:
            g = by.setdefault(r["slug"], {"slug": r["slug"], "label": r["label"] or r["slug"], "hashes": set()})
            g["hashes"].add(r["hash"])
        return [
            {"slug": g["slug"], "label": g["label"], "hashes": sorted(g["hashes"])}
            for g in by.values() if len(g["hashes"]) >= min_size
        ]

    def active_files(self) -> list[dict]:
        """One active filesystem location per active hash (for reading content)."""
        rows = self.conn.execute(
            """
            SELECT f.hash AS hash, f.mime AS mime, f.size AS size,
                   d.root_path AS root_path, p.path AS path
            FROM files f
            JOIN paths p ON p.hash=f.hash AND p.status='active'
            JOIN drives d ON d.id=p.drive_id
            GROUP BY f.hash
            """
        ).fetchall()
        out = []
        for r in rows:
            abspath = str(Path(r["root_path"]) / r["path"]) if r["root_path"] else r["path"]
            out.append({
                "hash": r["hash"], "mime": r["mime"], "size": r["size"],
                "abspath": abspath, "rel": r["path"],
            })
        return out

    def hashes_with_signature(self, algo: str) -> set[str]:
        return {r["hash"] for r in self.conn.execute(
            "SELECT hash FROM signatures WHERE algo=?", (algo,))}

    def active_paths(self) -> list[dict]:
        """Every active path observation (NOT collapsed by hash).

        Near-dup candidate bucketing needs all of a hash's locations — a file
        copied into two folders should be a clustering candidate in both.
        """
        rows = self.conn.execute(
            "SELECT hash, path FROM paths WHERE status='active'"
        ).fetchall()
        return [{"hash": r["hash"], "rel": r["path"]} for r in rows]

    # ---------- clusters ----------
    def materialize_exact_clusters(self, min_size: int = 1) -> int:
        """Create a cluster (kind='exact') per duplicated hash. Idempotent."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT f.hash
                FROM files f
                JOIN paths p ON p.hash=f.hash AND p.status='active'
                WHERE f.size >= ?
                GROUP BY f.hash
                HAVING COUNT(p.id) > 1
                """,
                (min_size,),
            ).fetchall()
            existing = self.clustered_hashes("exact")
            now = int(time.time())
            created = 0
            with self.tx():
                for r in rows:
                    h = r["hash"]
                    if h in existing:
                        continue
                    cur = self.conn.execute(
                        "INSERT INTO clusters(kind, label, canonical_hash, created_at, status) "
                        "VALUES ('exact', NULL, NULL, ?, 'open')",
                        (now,),
                    )
                    self.conn.execute(
                        "INSERT INTO cluster_members(cluster_id, hash, confidence) VALUES (?, ?, 1.0)",
                        (cur.lastrowid, h),
                    )
                    created += 1
            return created

    def create_cluster(self, kind: str, member_hashes: list[str],
                       label: Optional[str] = None, confidence: float = 1.0) -> int:
        now = int(time.time())
        with self._lock, self.tx():
            cur = self.conn.execute(
                "INSERT INTO clusters(kind, label, canonical_hash, created_at, status) "
                "VALUES (?, ?, NULL, ?, 'open')",
                (kind, label, now),
            )
            cid = cur.lastrowid
            for h in member_hashes:
                self.conn.execute(
                    "INSERT OR IGNORE INTO cluster_members(cluster_id, hash, confidence) "
                    "VALUES (?, ?, ?)",
                    (cid, h, confidence),
                )
        return cid

    def reset_clusters(self, kind: str, actor: str = "auto") -> int:
        """Delete all clusters of `kind` and their members, in one transaction.

        For DERIVED cluster kinds only (near_image / doc_version): these are
        recomputable groupings, not user data. An audit row is written to
        `decisions` (invariant #7 spirit) recording how much was removed. Callers
        must NOT use this on 'exact' (it encodes review state). Returns #clusters
        removed.
        """
        with self._lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM clusters WHERE kind=?", (kind,))]
            if not ids:
                return 0
            now = int(time.time())
            with self.tx():
                self.conn.execute(
                    "DELETE FROM cluster_members WHERE cluster_id IN "
                    "(SELECT id FROM clusters WHERE kind=?)", (kind,))
                self.conn.execute("DELETE FROM clusters WHERE kind=?", (kind,))
                self.conn.execute(
                    "INSERT INTO decisions(ts, actor, action, payload_json) VALUES (?, ?, ?, ?)",
                    (now, actor, "reset_clusters",
                     json.dumps({"kind": kind, "removed_clusters": len(ids)})),
                )
            return len(ids)

    def clustered_hashes(self, kind: str) -> set[str]:
        return {r["hash"] for r in self.conn.execute(
            """
            SELECT cm.hash FROM clusters c
            JOIN cluster_members cm ON cm.cluster_id=c.id
            WHERE c.kind=?
            """, (kind,))}

    def _member_hashes(self, cluster_id: int) -> list[str]:
        return [r["hash"] for r in self.conn.execute(
            "SELECT hash FROM cluster_members WHERE cluster_id=?", (cluster_id,))]

    def list_clusters(self, include_resolved: bool = False,
                     kinds: Optional[tuple[str, ...]] = None, limit: int = 500,
                     min_reclaimable: int = 0) -> list[dict]:
        params: list = []
        where = ["1=1"]
        if not include_resolved:
            where.append("c.status='open'")
        if kinds:
            where.append("c.kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        # min_reclaimable hides trivially-tiny EXACT groups (e.g. thousands of
        # sub-KB identical configs) that bury the high-value duplicates. Applied
        # ONLY to exact clusters — near_image/doc_version aren't size-driven and
        # must stay visible regardless of reclaimable bytes. 0 = show everything.
        having = ["n_active > 0"]
        if min_reclaimable > 0:
            having.append("(c.kind != 'exact' OR (active_bytes - max_active) >= ?)")
        rows = self.conn.execute(
            f"""
            SELECT c.id AS id, c.kind AS kind, c.label AS label, c.status AS status,
                   COUNT(CASE WHEN p.status='active' THEN 1 END) AS n_active,
                   COUNT(p.id) AS n_total,
                   COUNT(DISTINCT cm.hash) AS n_members,
                   COALESCE(SUM(CASE WHEN p.status='active' THEN f.size END), 0) AS active_bytes,
                   COALESCE(MAX(CASE WHEN p.status='active' THEN f.size END), 0) AS max_active
            FROM clusters c
            JOIN cluster_members cm ON cm.cluster_id=c.id
            JOIN files f ON f.hash=cm.hash
            LEFT JOIN paths p ON p.hash=cm.hash
            WHERE {" AND ".join(where)}
            GROUP BY c.id
            HAVING {" AND ".join(having)}
            ORDER BY (active_bytes - max_active) DESC, active_bytes DESC
            LIMIT ?
            """,
            (*params, *([min_reclaimable] if min_reclaimable > 0 else []), limit),
        ).fetchall()
        return [
            {
                "id": r["id"], "kind": r["kind"], "label": r["label"],
                "n_active": r["n_active"], "n_total": r["n_total"],
                "n_members": r["n_members"],
                "resolved": r["status"] != "open",
                "reclaimable": r["active_bytes"] - r["max_active"],
            }
            for r in rows
        ]

    def get_cluster(self, cluster_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, kind, label, canonical_hash, status FROM clusters WHERE id=?",
            (cluster_id,),
        ).fetchone()
        if not row:
            return None
        members = []
        flat_paths = []
        for h in self._member_hashes(cluster_id):
            f = self.conn.execute("SELECT size, mime FROM files WHERE hash=?", (h,)).fetchone()
            ps = self.conn.execute(
                "SELECT id, drive_id, path, mtime, status FROM paths WHERE hash=? ORDER BY status, path",
                (h,),
            ).fetchall()
            paths = [dict(p) for p in ps]
            members.append({
                "hash": h,
                "size": f["size"] if f else 0,
                "mime": f["mime"] if f else None,
                "paths": paths,
            })
            for p in paths:
                flat_paths.append({**p, "hash": h, "size": f["size"] if f else 0})
        actives = [p for p in flat_paths if p["status"] == "active"]
        reclaimable = sum(p["size"] for p in actives) - max((p["size"] for p in actives), default=0)
        rationale = self._cluster_rationale(row["kind"], members, actives)
        return {
            "id": row["id"], "kind": row["kind"], "label": row["label"],
            "resolved": row["status"] != "open",
            "n_members": len(members),
            "n_active": len(actives),
            "reclaimable": reclaimable,
            "rationale": rationale,
            "members": members,
            "paths": flat_paths,
        }

    def _cluster_rationale(self, kind: str, members: list, actives: list) -> str:
        """Human-readable 'why these match' — the trust signal. For exact dups
        (matched by content, not name) and near images (perceptual distance) a
        correct match otherwise reads as a bug. Mutates image members in place to
        attach per-member distance + dimensions."""
        if kind == "exact":
            from .ingest import HASH_NAME  # function-level: avoids import cycle
            h0 = members[0]["hash"] if members else ""
            sz = members[0]["size"] if members else 0
            names = {Path(p["path"]).name for p in actives}
            note = ("filenames differ — matched by content"
                    if len(names) > 1 else "same filename in multiple locations")
            return (f"identical content — {HASH_NAME} {h0[:12]}…, "
                    f"{sz:,} bytes, {len(actives)} copies; {note}")
        if kind == "near_image":
            from . import phash
            from .near import NEAR_IMAGE_THRESHOLD
            hs = [m["hash"] for m in members]
            if not hs:
                return "perceptual near-match"
            ph = ",".join("?" * len(hs))
            sigs = {r["hash"]: r["value"] for r in self.conn.execute(
                f"SELECT hash, value FROM signatures WHERE algo='phash_image' "
                f"AND hash IN ({ph})", hs)}
            dims = {r["hash"]: (r["w"], r["h"]) for r in self.conn.execute(
                f"SELECT hash, w, h FROM signatures WHERE algo='phash_image' "
                f"AND hash IN ({ph})", hs)}
            ref = phash.from_hex(sigs[hs[0]]) if hs[0] in sigs else None
            for m in members:
                w, h = dims.get(m["hash"], (None, None))
                m["w"], m["h"] = w, h
                sv = sigs.get(m["hash"])
                m["distance"] = (phash.hamming(ref, phash.from_hex(sv))
                                 if (ref is not None and sv) else None)
            return (f"perceptual near-match — dHash Hamming ≤ {NEAR_IMAGE_THRESHOLD}, "
                    f"same aspect ratio (not byte-identical; sizes/format may differ)")
        if kind == "doc_version":
            return "text near-duplicate — similar SimHash + shared folder/filename stem"
        if kind == "topic":
            return "grouped by inferred topic — not duplicates"
        return ""

    # ---------- review actions (all reversible via decisions) ----------
    def _record_decision(self, action: str, payload: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions(ts, actor, action, payload_json) VALUES (?, 'user', ?, ?)",
            (int(time.time()), action, json.dumps(payload)),
        )
        return cur.lastrowid

    def _active_member_paths(self, cluster_id: int) -> list[sqlite3.Row]:
        hs = self._member_hashes(cluster_id)
        if not hs:
            return []
        ph = ",".join("?" * len(hs))
        return self.conn.execute(
            f"SELECT id, hash, status FROM paths WHERE hash IN ({ph}) AND status='active'",
            hs,
        ).fetchall()

    def _cluster_state(self, cluster_id: int) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT canonical_hash, status FROM clusters WHERE id=?", (cluster_id,)
        ).fetchone()

    def resolve_keep(self, cluster_id: int, keep_path_id: int,
                     move_dest: Optional[dict] = None) -> dict:
        """Keep one path; send every other active path in the cluster to purgatory.

        If `move_dest` ({label, path}) is given, the kept file is ALSO queued (a
        pending_moves row) to be physically relocated to that destination during
        the `process` step. Queuing is index-only and fully reversible by undo —
        no file is touched here.
        """
        with self._lock:
            actives = self._active_member_paths(cluster_id)
            if not actives:
                raise ValueError(f"Cluster {cluster_id} has no active paths")
            by_id = {r["id"]: r for r in actives}
            if keep_path_id not in by_id:
                raise ValueError(f"path {keep_path_id} is not an active member of cluster {cluster_id}")
            keep_hash = by_id[keep_path_id]["hash"]
            prev = self._cluster_state(cluster_id)
            purged = [{"id": r["id"], "prev_status": "active"}
                      for r in actives if r["id"] != keep_path_id]
            move_id = None
            with self.tx():
                for p in purged:
                    self.conn.execute("UPDATE paths SET status='purgatory' WHERE id=?", (p["id"],))
                self.conn.execute(
                    "UPDATE clusters SET canonical_hash=?, status='resolved' WHERE id=?",
                    (keep_hash, cluster_id),
                )
                if move_dest and move_dest.get("path"):
                    move_id = self.enqueue_move(
                        keep_path_id, keep_hash, "keeper",
                        move_dest.get("label"), move_dest["path"])
                self._record_decision("resolve_keep", {
                    "cluster_id": cluster_id, "kept_path_id": keep_path_id,
                    "purged": purged, "pending_move_id": move_id,
                    "prev_canonical": prev["canonical_hash"], "prev_status": prev["status"],
                })
            return self.get_cluster(cluster_id)

    def purge_all(self, cluster_id: int) -> dict:
        """Send every active path in the cluster to purgatory."""
        with self._lock:
            actives = self._active_member_paths(cluster_id)
            if not actives:
                raise ValueError(f"Cluster {cluster_id} has no active paths")
            prev = self._cluster_state(cluster_id)
            purged = [{"id": r["id"], "prev_status": "active"} for r in actives]
            with self.tx():
                for p in purged:
                    self.conn.execute("UPDATE paths SET status='purgatory' WHERE id=?", (p["id"],))
                self.conn.execute(
                    "UPDATE clusters SET status='resolved' WHERE id=?", (cluster_id,)
                )
                self._record_decision("purge_all", {
                    "cluster_id": cluster_id, "purged": purged,
                    "prev_canonical": prev["canonical_hash"], "prev_status": prev["status"],
                })
            return self.get_cluster(cluster_id)

    # Decisions whose reversal moves files on disk — never reversed by the fast
    # index-only `u` keystroke (undo_last); handled by organize.undo_last_process.
    _DISK_OP_ACTIONS = ("apply_move", "apply_remove")

    def undo_last(self) -> Optional[dict]:
        """Reverse the most recent not-yet-undone *index-only* decision.

        If the most recent decision is a physical relocation (apply_move /
        apply_remove), this refuses and returns a {needs_process_undo} signal —
        reversing a committed move touches the disk and must go through
        organize.undo_last_process, not a keystroke. Append-only.
        """
        with self._lock:
            row = self.conn.execute(
                """
                SELECT id, action, payload_json FROM decisions
                WHERE action != 'undo'
                  AND id NOT IN (
                    SELECT CAST(json_extract(payload_json, '$.target') AS INTEGER)
                    FROM decisions WHERE action='undo'
                  )
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            if row["action"] in self._DISK_OP_ACTIONS:
                return {"needs_process_undo": True, "decision_id": row["id"],
                        "action": row["action"]}
            payload = json.loads(row["payload_json"])
            with self.tx():
                if row["action"] in ("resolve_keep", "purge_all"):
                    for p in payload.get("purged", []):
                        self.conn.execute(
                            "UPDATE paths SET status=? WHERE id=?", (p["prev_status"], p["id"]))
                    self.conn.execute(
                        "UPDATE clusters SET canonical_hash=?, status=? WHERE id=?",
                        (payload.get("prev_canonical"), payload.get("prev_status", "open"),
                         payload["cluster_id"]),
                    )
                    # un-queue a keeper move that was enqueued with this keep
                    pmid = payload.get("pending_move_id")
                    if pmid:
                        self.conn.execute(
                            "UPDATE pending_moves SET status='canceled' "
                            "WHERE id=? AND status='pending'", (pmid,))
                elif row["action"] == "restore":
                    # reverse a targeted restore: re-purge the path, re-resolve clusters
                    self.conn.execute(
                        "UPDATE paths SET status='purgatory' WHERE id=?", (payload["path_id"],))
                    for r in payload.get("reopened", []):
                        self.conn.execute(
                            "UPDATE clusters SET status=?, canonical_hash=? WHERE id=?",
                            (r["prev_status"], r["prev_canonical"], r["cluster_id"]))
                self._record_decision("undo", {"target": row["id"], "of_action": row["action"]})
            return {"undone_decision_id": row["id"], "action": row["action"],
                    "cluster_id": payload.get("cluster_id")}

    # ---------- purgatory (browse + targeted restore) ----------
    def purgatory_summary(self) -> dict:
        c = self.conn
        n = c.execute("SELECT COUNT(*) FROM paths WHERE status='purgatory'").fetchone()[0]
        b = c.execute(
            "SELECT COALESCE(SUM(f.size),0) FROM paths p JOIN files f ON f.hash=p.hash "
            "WHERE p.status='purgatory'"
        ).fetchone()[0]
        return {"count": n, "bytes": b}

    def list_purgatory(self, limit: int = 1000) -> list[dict]:
        """All paths currently in purgatory, heaviest first."""
        rows = self.conn.execute(
            """
            SELECT p.id, p.drive_id, p.path, p.hash, f.size, f.mime
            FROM paths p JOIN files f ON f.hash=p.hash
            WHERE p.status='purgatory'
            ORDER BY f.size DESC, p.path
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def restore_path(self, path_id: int) -> dict:
        """Flip a single purgatory path back to active (reversible).

        If restoring re-creates ambiguity — a resolved cluster containing this
        hash now has more than one active path again — that cluster is reopened
        so it returns to the review queue. The decision row captures enough to
        fully undo (re-purge the path, re-resolve the clusters).
        """
        with self._lock:
            row = self.conn.execute(
                """
                SELECT p.id, p.hash, p.status, d.kind AS drive_kind
                FROM paths p JOIN drives d ON d.id=p.drive_id WHERE p.id=?
                """, (path_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No such path: {path_id}")
            if row["status"] != "purgatory":
                raise ValueError(f"path {path_id} is not in purgatory")
            if row["drive_kind"] == "managed":
                # File already physically relocated to the holding folder — a plain
                # index flip would point 'active' at the holding copy, not the
                # original. Reversing that is a disk op: use "undo last process".
                raise ValueError(
                    f"path {path_id} was moved to the holding folder; use "
                    f"'undo last process' or restore it from holding manually")
            h = row["hash"]
            reopened = []
            for cr in self.conn.execute(
                """
                SELECT c.id, c.status, c.canonical_hash
                FROM clusters c JOIN cluster_members cm ON cm.cluster_id=c.id
                WHERE cm.hash=? AND c.status='resolved'
                """,
                (h,),
            ).fetchall():
                reopened.append({
                    "cluster_id": cr["id"],
                    "prev_status": cr["status"],
                    "prev_canonical": cr["canonical_hash"],
                })
            with self.tx():
                self.conn.execute("UPDATE paths SET status='active' WHERE id=?", (path_id,))
                for r in reopened:
                    self.conn.execute(
                        "UPDATE clusters SET status='open', canonical_hash=NULL WHERE id=?",
                        (r["cluster_id"],),
                    )
                self._record_decision("restore", {
                    "path_id": path_id, "prev_status": "purgatory", "reopened": reopened,
                })
            return {"restored_path_id": path_id,
                    "reopened_clusters": [r["cluster_id"] for r in reopened]}

    # ---------- settings (JSON key/value config travelling with the index) ----------
    def get_setting(self, key: str, default=None):
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_destinations(self) -> list[dict]:
        """List of {label, path} the user can move keepers to (max 5, enforced by UI)."""
        return self.get_setting("destinations", []) or []

    def set_destinations(self, dests: list[dict]) -> None:
        self.set_setting("destinations", dests)

    def get_highlight_patterns(self) -> list[str]:
        return self.get_setting("highlight_patterns", []) or []

    def set_highlight_patterns(self, patterns: list[str]) -> None:
        self.set_setting("highlight_patterns", patterns)

    def get_holding_dir(self) -> Optional[str]:
        return self.get_setting("holding_dir", None)

    def set_holding_dir(self, path: Optional[str]) -> None:
        self.set_setting("holding_dir", path)

    # ---------- managed drives (organize destinations / holding folder) ----------
    # A managed drive is a synthetic `drives` row whose root is a user destination
    # or the holding folder. Unlike scanned drives it carries NO .midden_drive.json
    # marker (we never write into the user's target folders) and is NEVER walked by
    # ingest or touched by reconcile — that's what lets destinations live inside an
    # already-scanned disk without re-discovering relocated files (design: managed
    # subtrees are skipped).
    @staticmethod
    def managed_drive_id(root_path: str) -> str:
        """Deterministic id for a managed root, so re-ensuring the same folder is
        idempotent. Derived from the normalized absolute path (no marker file)."""
        import hashlib as _h
        norm = os.path.normcase(os.path.abspath(root_path))
        return "managed:" + _h.sha256(norm.encode("utf-8")).hexdigest()[:16]

    def ensure_managed_drive(self, root_path: str, label: Optional[str] = None) -> str:
        """Insert (idempotently) a managed drive row for `root_path`; return its id."""
        abspath = os.path.abspath(root_path)
        did = self.managed_drive_id(abspath)
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                "INSERT INTO drives(id, label, root_path, last_ingested_at, kind) "
                "VALUES (?, ?, ?, ?, 'managed') "
                "ON CONFLICT(id) DO UPDATE SET root_path=excluded.root_path, "
                "label=COALESCE(excluded.label, drives.label)",
                (did, label or Path(abspath).name, abspath, now),
            )
        return did

    def managed_drive_roots(self) -> list[str]:
        """Absolute roots of all managed drives (for ingest/reconcile skip checks)."""
        return [r["root_path"] for r in self.conn.execute(
            "SELECT root_path FROM drives WHERE kind='managed' AND root_path IS NOT NULL")]

    def drive_kind(self, drive_id: str) -> Optional[str]:
        r = self.conn.execute("SELECT kind FROM drives WHERE id=?", (drive_id,)).fetchone()
        return r["kind"] if r else None

    # ---------- pending-move queue (executed by organize.py) ----------
    def path_full(self, path_id: int) -> Optional[dict]:
        """Full row for a path id with its drive root + kind + absolute path."""
        r = self.conn.execute(
            """
            SELECT p.id, p.hash, p.drive_id, p.path, p.mtime, p.ctime, p.status,
                   d.root_path AS root_path, d.kind AS drive_kind
            FROM paths p JOIN drives d ON d.id=p.drive_id
            WHERE p.id=?
            """,
            (path_id,),
        ).fetchone()
        if not r:
            return None
        d = dict(r)
        d["abspath"] = (str(Path(r["root_path"]) / r["path"]) if r["root_path"] else r["path"])
        return d

    def enqueue_move(self, path_id: int, hash_: str, kind: str,
                     dest_label: Optional[str], dest_root: str) -> int:
        """Queue a relocation. Caller holds the write context when batching."""
        cur = self.conn.execute(
            "INSERT INTO pending_moves(path_id, hash, kind, dest_label, dest_root, "
            "enqueued_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (path_id, hash_, kind, dest_label, dest_root, int(time.time())),
        )
        return cur.lastrowid

    def pending_for_path(self, path_id: int, statuses=("pending", "moving", "executed")) -> list[dict]:
        ph = ",".join("?" * len(statuses))
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM pending_moves WHERE path_id=? AND status IN ({ph})",
            (path_id, *statuses))]

    def list_pending(self, kind: Optional[str] = None,
                     statuses=("pending",)) -> list[dict]:
        """Pending-move rows joined with path/drive info, for the Process view."""
        ph = ",".join("?" * len(statuses))
        q = (f"SELECT pm.*, p.path AS rel, p.status AS path_status, f.size AS size, "
             f"d.root_path AS src_root, d.label AS src_drive_label "
             f"FROM pending_moves pm "
             f"JOIN paths p ON p.id=pm.path_id "
             f"JOIN files f ON f.hash=pm.hash "
             f"JOIN drives d ON d.id=p.drive_id "
             f"WHERE pm.status IN ({ph})")
        params: list = list(statuses)
        if kind:
            q += " AND pm.kind=?"
            params.append(kind)
        q += " ORDER BY f.size DESC"
        return [dict(r) for r in self.conn.execute(q, params)]

    def cancel_pending_move(self, pending_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE pending_moves SET status='canceled' WHERE id=? AND status='pending'",
                (pending_id,))

    def set_move_moving(self, pending_id: int, src_abspath: str, final_rel: str) -> None:
        """Persist resolved source + planned dest rel, flip to 'moving' BEFORE the
        OS move. Storing the exact planned dest makes a crash recoverable: recovery
        probes src_abspath and dest_root/final_path on disk to decide what happened."""
        with self._lock, self.tx():
            self.conn.execute(
                "UPDATE pending_moves SET status='moving', src_abspath=?, final_path=? WHERE id=?",
                (src_abspath, final_rel, pending_id))

    def moving_rows(self) -> list[dict]:
        """Rows stuck mid-move (crash recovery probes disk for these)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM pending_moves WHERE status='moving'")]

    def revert_move_to_pending(self, pending_id: int) -> None:
        with self._lock, self.tx():
            self.conn.execute(
                "UPDATE pending_moves SET status='pending', src_abspath=NULL WHERE id=?",
                (pending_id,))

    def dup_candidates(self) -> list[dict]:
        """Purgatory paths still on SCANNED drives (i.e. not yet moved to holding).
        These are the 'to remove' work list for the process step."""
        rows = self.conn.execute(
            """
            SELECT p.id AS path_id, p.hash AS hash, p.path AS rel, f.size AS size,
                   d.root_path AS src_root, d.label AS src_drive_label
            FROM paths p JOIN files f ON f.hash=p.hash
            JOIN drives d ON d.id=p.drive_id
            WHERE p.status='purgatory' AND d.kind='scanned'
            ORDER BY f.size DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def commit_relocation(self, pending_id: int, src_path_id: int,
                          dest_drive_id: str, dest_rel: str, mtime: int, ctime: int,
                          new_status: str, decision_action: str) -> int:
        """DB side of a completed physical move (organize.py calls this AFTER the
        OS move succeeds). Inserts the destination path row, deletes the source
        row, marks the pending row executed, and snapshots a reversible decision.
        Pure-DB: no filesystem access here (store stays the DB-only boundary)."""
        with self._lock:
            src = self.conn.execute(
                "SELECT hash, drive_id, path, mtime, ctime, status FROM paths WHERE id=?",
                (src_path_id,)).fetchone()
            if src is None:
                raise ValueError(f"source path {src_path_id} vanished from index")
            now = int(time.time())
            with self.tx():
                cur = self.conn.execute(
                    "INSERT INTO paths(hash, drive_id, path, mtime, ctime, observed_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (src["hash"], dest_drive_id, dest_rel, mtime, ctime, now, new_status))
                new_path_id = cur.lastrowid
                self.conn.execute("DELETE FROM paths WHERE id=?", (src_path_id,))
                # point the executed queue row at the live destination row
                self.conn.execute(
                    "UPDATE pending_moves SET status='executed', final_path=?, path_id=? WHERE id=?",
                    (dest_rel, new_path_id, pending_id))
                self._record_decision(decision_action, {
                    "pending_id": pending_id,
                    "new_path_id": new_path_id, "new_status": new_status,
                    "src": {"hash": src["hash"], "drive_id": src["drive_id"],
                            "path": src["path"], "mtime": src["mtime"],
                            "ctime": src["ctime"], "status": src["status"]},
                    "dest": {"drive_id": dest_drive_id, "path": dest_rel},
                })
            return new_path_id

    def reverse_relocation(self, decision_id: int) -> dict:
        """DB side of undoing an executed move (organize.py moves the file back
        first, then calls this). Re-inserts the original source row, deletes the
        destination row, flips the pending row back to 'pending'. Records an undo."""
        with self._lock:
            d = self.conn.execute(
                "SELECT action, payload_json FROM decisions WHERE id=?", (decision_id,)).fetchone()
            if d is None or d["action"] not in ("apply_move", "apply_remove"):
                raise ValueError(f"decision {decision_id} is not a reversible relocation")
            p = json.loads(d["payload_json"])
            src, now = p["src"], int(time.time())
            with self.tx():
                self.conn.execute("DELETE FROM paths WHERE id=?", (p["new_path_id"],))
                cur = self.conn.execute(
                    "INSERT INTO paths(hash, drive_id, path, mtime, ctime, observed_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (src["hash"], src["drive_id"], src["path"], src["mtime"],
                     src["ctime"], now, src["status"]))
                # re-point the queue row at the re-inserted source so a later
                # re-process targets the live row, and clear the in-flight state
                self.conn.execute(
                    "UPDATE pending_moves SET status='pending', src_abspath=NULL, "
                    "final_path=NULL, path_id=? WHERE id=?", (cur.lastrowid, p["pending_id"]))
                self._record_decision("undo", {"target": decision_id, "of_action": d["action"]})
            return {"reversed_decision_id": decision_id, "action": d["action"],
                    "src": src, "dest": p["dest"]}

    def last_executed_relocation(self) -> Optional[dict]:
        """Most recent not-yet-undone apply_move/apply_remove decision (for
        'undo last process')."""
        row = self.conn.execute(
            """
            SELECT id, action, payload_json FROM decisions
            WHERE action IN ('apply_move','apply_remove')
              AND id NOT IN (
                SELECT CAST(json_extract(payload_json,'$.target') AS INTEGER)
                FROM decisions WHERE action='undo')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    # ---------- search ----------
    def search(self, q: str, include_purgatory: bool = False, limit: int = 200) -> list[dict]:
        status_clause = "" if include_purgatory else "AND p.status='active'"
        rows = self.conn.execute(
            f"""
            SELECT p.id, p.drive_id, p.path, p.status, f.hash, f.size, f.mime
            FROM paths p JOIN files f ON f.hash=p.hash
            WHERE p.path LIKE ? {status_clause}
            ORDER BY f.size DESC
            LIMIT ?
            """,
            (f"%{q}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        # Closes the calling thread's connection. Other threads' connections are
        # released when the process exits (server connections are process-lived).
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
