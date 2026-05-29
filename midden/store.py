"""SQLite access layer for the Midden index.

Single file, WAL mode, single writer convention. Connection per-thread.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

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
    def __init__(self, db_path: Path, same_thread: bool = True):
        # same_thread=False is used by the FastAPI server (sync handlers run in a
        # threadpool); single-user, so we serialize writes with self._lock.
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=same_thread
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent, additive migrations for DBs created before a column existed."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(paths)")}
        if "status" not in cols:
            self.conn.execute(
                "ALTER TABLE paths ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_paths_status ON paths(status)"
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
        return self.conn.execute(
            "SELECT hash, mtime FROM paths WHERE drive_id=? AND path=?",
            (drive_id, rel_path),
        ).fetchone()

    # ---------- queries ----------
    def stats(self) -> dict:
        c = self.conn
        n_files = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_paths = c.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
        n_active = c.execute(
            "SELECT COUNT(*) FROM paths WHERE status='active'"
        ).fetchone()[0]
        n_purgatory = c.execute(
            "SELECT COUNT(*) FROM paths WHERE status='purgatory'"
        ).fetchone()[0]
        n_drives = c.execute("SELECT COUNT(*) FROM drives").fetchone()[0]
        # unique bytes = one copy of each hash that still has an active path
        bytes_unique = c.execute(
            """
            SELECT COALESCE(SUM(size), 0) FROM files
            WHERE hash IN (SELECT DISTINCT hash FROM paths WHERE status='active')
            """
        ).fetchone()[0]
        # total = every active path observation
        bytes_total = c.execute(
            """
            SELECT COALESCE(SUM(f.size), 0)
            FROM paths p JOIN files f ON f.hash=p.hash
            WHERE p.status='active'
            """
        ).fetchone()[0]
        # already reclaimed = purgatoried path observations
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
        """Stats + review-queue counts for the UI Overview."""
        s = self.stats()
        c = self.conn
        s["clusters_total"] = c.execute(
            "SELECT COUNT(*) FROM clusters WHERE kind='exact'"
        ).fetchone()[0]
        s["clusters_unresolved"] = c.execute(
            "SELECT COUNT(*) FROM clusters WHERE kind='exact' AND canonical_hash IS NULL"
        ).fetchone()[0]
        s["clusters_resolved"] = s["clusters_total"] - s["clusters_unresolved"]
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

    # ---------- clusters (materialization) ----------
    def materialize_exact_clusters(self, min_size: int = 1) -> int:
        """Create a cluster (kind='exact') per duplicated hash. Idempotent.

        A hash qualifies if it has >1 active path and size >= min_size. Hashes
        that already have an exact cluster are skipped. Returns # new clusters.
        """
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
            existing = {
                r["hash"] for r in self.conn.execute(
                    """
                    SELECT cm.hash FROM clusters c
                    JOIN cluster_members cm ON cm.cluster_id=c.id
                    WHERE c.kind='exact'
                    """
                )
            }
            now = int(time.time())
            created = 0
            with self.tx():
                for r in rows:
                    h = r["hash"]
                    if h in existing:
                        continue
                    cur = self.conn.execute(
                        "INSERT INTO clusters(kind, label, canonical_hash, created_at) "
                        "VALUES ('exact', NULL, NULL, ?)",
                        (now,),
                    )
                    cid = cur.lastrowid
                    self.conn.execute(
                        "INSERT INTO cluster_members(cluster_id, hash, confidence) VALUES (?, ?, 1.0)",
                        (cid, h),
                    )
                    created += 1
            return created

    def _cluster_hash(self, cluster_id: int) -> Optional[str]:
        row = self.conn.execute(
            "SELECT hash FROM cluster_members WHERE cluster_id=? LIMIT 1",
            (cluster_id,),
        ).fetchone()
        return row["hash"] if row else None

    def list_clusters(self, include_resolved: bool = False, limit: int = 500) -> list[dict]:
        where = "WHERE c.kind='exact'"
        if not include_resolved:
            where += " AND c.canonical_hash IS NULL"
        rows = self.conn.execute(
            f"""
            SELECT c.id, cm.hash, f.size, c.canonical_hash,
                   (SELECT COUNT(*) FROM paths p WHERE p.hash=cm.hash AND p.status='active') AS n_active,
                   (SELECT COUNT(*) FROM paths p WHERE p.hash=cm.hash) AS n_total
            FROM clusters c
            JOIN cluster_members cm ON cm.cluster_id=c.id
            JOIN files f ON f.hash=cm.hash
            {where}
            ORDER BY f.size * (
                (SELECT COUNT(*) FROM paths p WHERE p.hash=cm.hash AND p.status='active') - 1
            ) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "hash": r["hash"],
                "size": r["size"],
                "n_active": r["n_active"],
                "n_total": r["n_total"],
                "resolved": r["canonical_hash"] is not None,
                "reclaimable": r["size"] * max(r["n_active"] - 1, 0),
            }
            for r in rows
        ]

    def get_cluster(self, cluster_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, kind, label, canonical_hash, created_at FROM clusters WHERE id=?",
            (cluster_id,),
        ).fetchone()
        if not row:
            return None
        h = self._cluster_hash(cluster_id)
        size = self.conn.execute(
            "SELECT size, mime FROM files WHERE hash=?", (h,)
        ).fetchone()
        paths = self.conn.execute(
            """
            SELECT id, drive_id, path, mtime, ctime, status
            FROM paths WHERE hash=? ORDER BY status, path
            """,
            (h,),
        ).fetchall()
        return {
            "id": row["id"],
            "kind": row["kind"],
            "hash": h,
            "size": size["size"] if size else 0,
            "mime": size["mime"] if size else None,
            "resolved": row["canonical_hash"] is not None,
            "paths": [dict(p) for p in paths],
        }

    # ---------- review actions (all reversible via decisions) ----------
    def _record_decision(self, action: str, payload: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions(ts, actor, action, payload_json) VALUES (?, 'user', ?, ?)",
            (int(time.time()), action, json.dumps(payload)),
        )
        return cur.lastrowid

    def resolve_keep(self, cluster_id: int, keep_path_id: int) -> dict:
        """Keep one path; send the cluster's other active paths to purgatory."""
        with self._lock:
            h = self._cluster_hash(cluster_id)
            if h is None:
                raise ValueError(f"No such cluster: {cluster_id}")
            actives = self.conn.execute(
                "SELECT id, status FROM paths WHERE hash=? AND status='active'", (h,)
            ).fetchall()
            ids = {r["id"] for r in actives}
            if keep_path_id not in ids:
                raise ValueError(f"path {keep_path_id} is not an active member of cluster {cluster_id}")
            prev_canonical = self.conn.execute(
                "SELECT canonical_hash FROM clusters WHERE id=?", (cluster_id,)
            ).fetchone()["canonical_hash"]
            purged = [{"id": r["id"], "prev_status": "active"} for r in actives if r["id"] != keep_path_id]
            with self.tx():
                for p in purged:
                    self.conn.execute(
                        "UPDATE paths SET status='purgatory' WHERE id=?", (p["id"],)
                    )
                self.conn.execute(
                    "UPDATE clusters SET canonical_hash=? WHERE id=?", (h, cluster_id)
                )
                self._record_decision("resolve_keep", {
                    "cluster_id": cluster_id, "hash": h,
                    "kept_path_id": keep_path_id, "purged": purged,
                    "prev_canonical": prev_canonical,
                })
            return self.get_cluster(cluster_id)

    def purge_all(self, cluster_id: int) -> dict:
        """Send every active path in the cluster to purgatory."""
        with self._lock:
            h = self._cluster_hash(cluster_id)
            if h is None:
                raise ValueError(f"No such cluster: {cluster_id}")
            actives = self.conn.execute(
                "SELECT id FROM paths WHERE hash=? AND status='active'", (h,)
            ).fetchall()
            prev_canonical = self.conn.execute(
                "SELECT canonical_hash FROM clusters WHERE id=?", (cluster_id,)
            ).fetchone()["canonical_hash"]
            purged = [{"id": r["id"], "prev_status": "active"} for r in actives]
            with self.tx():
                for p in purged:
                    self.conn.execute(
                        "UPDATE paths SET status='purgatory' WHERE id=?", (p["id"],)
                    )
                self.conn.execute(
                    "UPDATE clusters SET canonical_hash=? WHERE id=?", (h, cluster_id)
                )
                self._record_decision("purge_all", {
                    "cluster_id": cluster_id, "hash": h,
                    "purged": purged, "prev_canonical": prev_canonical,
                })
            return self.get_cluster(cluster_id)

    def undo_last(self) -> Optional[dict]:
        """Reverse the most recent not-yet-undone decision. Append-only."""
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
            payload = json.loads(row["payload_json"])
            with self.tx():
                if row["action"] in ("resolve_keep", "purge_all"):
                    for p in payload.get("purged", []):
                        self.conn.execute(
                            "UPDATE paths SET status=? WHERE id=?",
                            (p["prev_status"], p["id"]),
                        )
                    self.conn.execute(
                        "UPDATE clusters SET canonical_hash=? WHERE id=?",
                        (payload.get("prev_canonical"), payload["cluster_id"]),
                    )
                self._record_decision("undo", {"target": row["id"], "of_action": row["action"]})
            return {"undone_decision_id": row["id"], "action": row["action"],
                    "cluster_id": payload.get("cluster_id")}

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
        self.conn.close()
