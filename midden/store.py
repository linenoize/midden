"""SQLite access layer for the Midden index.

Single file, WAL mode, single writer convention. Connection per-thread (the web
server passes same_thread=False and serializes writes via self._lock).

This is the ONLY module that touches the schema.
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
                     kinds: Optional[tuple[str, ...]] = None, limit: int = 500) -> list[dict]:
        params: list = []
        where = ["1=1"]
        if not include_resolved:
            where.append("c.status='open'")
        if kinds:
            where.append("c.kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
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
            HAVING n_active > 0
            ORDER BY (active_bytes - max_active) DESC, active_bytes DESC
            LIMIT ?
            """,
            (*params, limit),
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
        return {
            "id": row["id"], "kind": row["kind"], "label": row["label"],
            "resolved": row["status"] != "open",
            "n_members": len(members),
            "n_active": len(actives),
            "reclaimable": reclaimable,
            "members": members,
            "paths": flat_paths,
        }

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

    def resolve_keep(self, cluster_id: int, keep_path_id: int) -> dict:
        """Keep one path; send every other active path in the cluster to purgatory."""
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
            with self.tx():
                for p in purged:
                    self.conn.execute("UPDATE paths SET status='purgatory' WHERE id=?", (p["id"],))
                self.conn.execute(
                    "UPDATE clusters SET canonical_hash=?, status='resolved' WHERE id=?",
                    (keep_hash, cluster_id),
                )
                self._record_decision("resolve_keep", {
                    "cluster_id": cluster_id, "kept_path_id": keep_path_id,
                    "purged": purged,
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
                            "UPDATE paths SET status=? WHERE id=?", (p["prev_status"], p["id"]))
                    self.conn.execute(
                        "UPDATE clusters SET canonical_hash=?, status=? WHERE id=?",
                        (payload.get("prev_canonical"), payload.get("prev_status", "open"),
                         payload["cluster_id"]),
                    )
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
                "SELECT id, hash, status FROM paths WHERE id=?", (path_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"No such path: {path_id}")
            if row["status"] != "purgatory":
                raise ValueError(f"path {path_id} is not in purgatory")
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
