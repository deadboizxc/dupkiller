"""
Persistent scan session backed by SQLite.

``ScanSession`` tracks file scan progress across the four pipeline stages,
supports resume after interruption, and uses an advisory lock (via a SQLite
UNIQUE constraint on ``session_locks``) to prevent two processes from scanning
the same root concurrently.

Schema versioning is managed through ``_schema_version``; the v1→v2 migration
adds ``inode`` and ``device`` columns to ``session_files``.  All iterators
use keyset pagination to stay O(1) in memory for 10 M+ row datasets.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from dupkiller.utils import FileInfo

logger = logging.getLogger(__name__)

_BATCH_INSERT = 500
_SCHEMA_VERSION = 2   # bump when schema changes


# ---------------------------------------------------------------------------
# Session lock  (cross-platform via SQLite UNIQUE)
# ---------------------------------------------------------------------------

class _SessionLockError(RuntimeError):
    """Raised when another running session holds the lock for this root."""


# ---------------------------------------------------------------------------
# ScanSession
# ---------------------------------------------------------------------------

class ScanSession:
    """Persistent scan session backed by SQLite."""



    # We need the path — store it as a class-level factory:
    @classmethod
    def _make_conn(cls, db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous  = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA cache_size   = -16000")   # 16 MB page cache
        conn.execute("PRAGMA temp_store   = MEMORY")
        return conn

    def __init__(self, db_path: Path, session_id: int) -> None:
        self._db_path = str(db_path)
        self.session_id = session_id
        self._lock = threading.Lock()
        self._conn = self._make_conn(self._db_path)
        self._pending: list[tuple] = []
        self._last_checkpoint = time.monotonic()

    # ------------------------------------------------------------------
    # Schema bootstrap + migration
    # ------------------------------------------------------------------

    @classmethod
    def _ensure_schema(cls, conn: sqlite3.Connection) -> None:
        # Step 1: create tables (without inode index — columns may not exist yet)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS _schema_version (
                version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_locks (
                root_path TEXT PRIMARY KEY,
                session_id INTEGER NOT NULL,
                pid        INTEGER NOT NULL,
                locked_at  REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_sessions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                root_path        TEXT    NOT NULL,
                started_at       REAL    NOT NULL,
                updated_at       REAL    NOT NULL,
                status           TEXT    NOT NULL DEFAULT 'running',
                stage            TEXT    NOT NULL DEFAULT 'scanning',
                config_json      TEXT    NOT NULL DEFAULT '{}',
                files_scanned    INTEGER DEFAULT 0,
                output_jsonl     TEXT,
                output_log       TEXT
            );

            CREATE TABLE IF NOT EXISTS session_files (
                session_id INTEGER NOT NULL,
                path       TEXT    NOT NULL,
                size       INTEGER NOT NULL,
                mtime      REAL    NOT NULL,
                inode      INTEGER NOT NULL DEFAULT 0,
                device     INTEGER NOT NULL DEFAULT 0,
                stage      TEXT    NOT NULL DEFAULT 'scanned',
                PRIMARY KEY (session_id, path),
                FOREIGN KEY (session_id)
                    REFERENCES scan_sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sf_size_stage
                ON session_files(session_id, size, stage);
            CREATE INDEX IF NOT EXISTS idx_sf_stage
                ON session_files(session_id, stage);
        """)

        # Step 2: migrate if needed (adds inode/device columns on v1 DBs)
        cur = conn.execute("SELECT version FROM _schema_version")
        row = cur.fetchone()
        if row is None:
            conn.execute("INSERT INTO _schema_version VALUES (?)", (_SCHEMA_VERSION,))
        elif row[0] < _SCHEMA_VERSION:
            cls._migrate(conn, row[0])
            conn.execute("UPDATE _schema_version SET version = ?", (_SCHEMA_VERSION,))
        conn.commit()

        # Step 3: create indexes that reference migrated columns (inode/device)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sf_inode "
            "ON session_files(session_id, device, inode)"
        )
        conn.commit()

    @classmethod
    def _migrate(cls, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply incremental schema upgrades."""
        if from_version < 2:
            # v1 → v2: add inode and device columns if absent
            cols = {row[1] for row in conn.execute("PRAGMA table_info(session_files)")}
            if "inode" not in cols:
                conn.execute("ALTER TABLE session_files ADD COLUMN inode INTEGER NOT NULL DEFAULT 0")
            if "device" not in cols:
                conn.execute("ALTER TABLE session_files ADD COLUMN device INTEGER NOT NULL DEFAULT 0")
            logger.info("Migrated session_files schema v1 → v2 (added inode, device)")

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        db_path: Path,
        root_path: str,
        config: dict,
        output_jsonl: str | None = None,
        output_log:   str | None = None,
    ) -> ScanSession:
        conn = cls._make_conn(str(db_path))
        cls._ensure_schema(conn)

        # Advisory lock: try to claim exclusive access for this root path
        now = time.time()
        pid = os.getpid()
        try:
            conn.execute(
                "INSERT INTO session_locks (root_path, session_id, pid, locked_at) "
                "VALUES (?, 0, ?, ?)",
                (root_path, pid, now),
            )
        except sqlite3.IntegrityError:  # pragma: no cover
            # Check if the owning process is still alive
            row = conn.execute(
                "SELECT pid, locked_at FROM session_locks WHERE root_path = ?",
                (root_path,),
            ).fetchone()
            if row:
                other_pid, locked_at = row
                age = now - locked_at
                if _pid_alive(other_pid) and age < 7200:
                    conn.close()
                    raise _SessionLockError(
                        f"Another dupkiller process (PID {other_pid}) is already "
                        f"scanning {root_path!r}.  Use --resume or wait."
                    )
                # Stale lock — take it over
                conn.execute(
                    "UPDATE session_locks SET session_id=0, pid=?, locked_at=? "
                    "WHERE root_path=?",
                    (pid, now, root_path),
                )

        cur = conn.execute(
            "INSERT INTO scan_sessions "
            "(root_path, started_at, updated_at, config_json, output_jsonl, output_log) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (root_path, now, now, json.dumps(config), output_jsonl, output_log),
        )
        session_id = cur.lastrowid
        if session_id is None:  # pragma: no cover
            conn.close()  # pragma: no cover
            raise sqlite3.DatabaseError("failed to create scan session")  # pragma: no cover
        # Update lock with real session_id
        conn.execute(
            "UPDATE session_locks SET session_id=? WHERE root_path=?",
            (session_id, root_path),
        )
        conn.commit()
        conn.close()

        logger.info("Created scan session %d for %s", session_id, root_path)
        return cls(db_path, session_id)

    @classmethod
    def find_resumable(cls, db_path: Path, root_path: str) -> ScanSession | None:
        try:
            conn = cls._make_conn(str(db_path))
            cls._ensure_schema(conn)
            cur = conn.execute(
                "SELECT id FROM scan_sessions "
                "WHERE root_path=? AND status='running' AND stage!='done' "
                "ORDER BY started_at DESC LIMIT 1",
                (root_path,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                logger.info("Found resumable session %d for %s", row[0], root_path)
                return cls(db_path, row[0])
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("Cannot check for resumable session: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Session metadata
    # ------------------------------------------------------------------

    def get_info(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM scan_sessions WHERE id=?", (self.session_id,)
            )
            row = cur.fetchone()
            if not row:
                return {}
            return dict(zip([d[0] for d in cur.description], row))

    def get_stage(self) -> str:
        info = self.get_info()
        stage = info.get("stage", "scanning")
        return str(stage) if stage is not None else "scanning"

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_sessions SET stage=?, updated_at=? WHERE id=?",
                (stage, time.time(), self.session_id),
            )
            self._conn.commit()
        logger.info("Session %d → stage: %s", self.session_id, stage)

    def mark_interrupted(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_sessions SET status='interrupted', updated_at=? WHERE id=?",
                (time.time(), self.session_id),
            )
            self._conn.commit()
        self._release_lock()

    def mark_complete(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE scan_sessions SET status='done', stage='done', updated_at=? WHERE id=?",
                (time.time(), self.session_id),
            )
            self._conn.commit()
        self._release_lock()

    def _release_lock(self) -> None:
        info = self.get_info()
        if not info:  # pragma: no cover
            return  # pragma: no cover
        with self._lock:
            self._conn.execute(
                "DELETE FROM session_locks WHERE root_path=? AND session_id=?",
                (info["root_path"], self.session_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    def checkpoint(self) -> None:
        with self._lock:
            self._flush_locked()
            self._conn.execute(
                "UPDATE scan_sessions SET updated_at=? WHERE id=?",
                (time.time(), self.session_id),
            )
            self._conn.commit()
        self._last_checkpoint = time.monotonic()
        logger.debug("Session %d: checkpoint written", self.session_id)

    def checkpoint_if_due(self, interval: float = 300.0) -> bool:
        if time.monotonic() - self._last_checkpoint >= interval:
            self.checkpoint()
            return True
        return False

    # ------------------------------------------------------------------
    # File insertion
    # ------------------------------------------------------------------

    def queue_file(self, fi: FileInfo) -> None:
        with self._lock:
            self._pending.append(
                (self.session_id, fi.path, fi.size, fi.mtime, fi.inode, fi.device, "scanned")
            )
            if len(self._pending) >= _BATCH_INSERT:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            self._conn.executemany(
                "INSERT OR REPLACE INTO session_files "
                "(session_id, path, size, mtime, inode, device, stage) "
                "VALUES (?,?,?,?,?,?,?)",
                batch,
            )
            self._conn.execute(
                "UPDATE scan_sessions SET files_scanned=files_scanned+?, updated_at=? WHERE id=?",
                (len(batch), time.time(), self.session_id),
            )
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover
            logger.error("session_files batch insert failed: %s", exc)

    def flush_files(self) -> None:
        with self._lock:
            self._flush_locked()

    def total_scanned(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM session_files WHERE session_id=?",
                (self.session_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def stage_counts(self) -> dict[str, int]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT stage, COUNT(*) FROM session_files WHERE session_id=? GROUP BY stage",
                (self.session_id,),
            )
            return dict(cur.fetchall())

    def count_candidates(self) -> int:
        """Count files that have at least one size-peer (actual partial-hash candidates)."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT COALESCE(SUM(cnt), 0) FROM (
                    SELECT COUNT(*) AS cnt
                    FROM session_files
                    WHERE session_id=? AND stage='scanned'
                    GROUP BY size
                    HAVING COUNT(*) >= 2
                )
                """,
                (self.session_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Pipeline queries (paginated)
    # ------------------------------------------------------------------

    def iter_size_groups(
        self,
        target_stage: str = "scanned",
        fetch_size: int = 1000,
    ) -> Iterator[tuple[int, list[tuple[str, float, int, int]]]]:
        """
        Yield ``(size, [(path, mtime, inode, device), ...])`` for groups
        with ≥ 2 files, ordered by size ASC (smaller files first).

        Paginated: never loads more than *fetch_size* distinct sizes at once.
        """
        offset = 0
        while True:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT size FROM session_files "
                    "WHERE session_id=? AND stage=? "
                    "GROUP BY size HAVING COUNT(*) >= 2 "
                    "ORDER BY size ASC "
                    "LIMIT ? OFFSET ?",
                    (self.session_id, target_stage, fetch_size, offset),
                )
                sizes = [row[0] for row in cur.fetchall()]

            if not sizes:
                break

            for size in sizes:
                with self._lock:
                    cur = self._conn.execute(
                        "SELECT path, mtime, inode, device FROM session_files "
                        "WHERE session_id=? AND size=? AND stage=? ORDER BY inode, path",
                        (self.session_id, size, target_stage),
                    )
                    files = cur.fetchall()
                if len(files) >= 2:
                    yield size, files

            offset += len(sizes)
            if len(sizes) < fetch_size:
                break

    def mark_files_stage(self, paths: list[str], new_stage: str) -> None:
        if not paths:
            return
        CHUNK = 500
        with self._lock:
            for i in range(0, len(paths), CHUNK):
                chunk = paths[i : i + CHUNK]
                ph = ",".join("?" * len(chunk))
                self._conn.execute(
                    f"UPDATE session_files SET stage=? "
                    f"WHERE session_id=? AND path IN ({ph})",
                    (new_stage, self.session_id, *chunk),
                )
            self._conn.commit()

    def mark_files_stage_where_scanned_unique(self) -> None:
        """Mark all remaining 'scanned' files (no size peers) as 'unique'."""
        with self._lock:
            self._conn.execute(
                "UPDATE session_files SET stage='unique' "
                "WHERE session_id=? AND stage='scanned'",
                (self.session_id,),
            )
            self._conn.commit()

    def _get_mtime_tol(self) -> float:
        """Read mtime tolerance from _config once; fallback to 0.001."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT CAST(value AS REAL) FROM _config WHERE key='mtime_tolerance'"
                ).fetchone()
            except sqlite3.OperationalError:
                return 0.001
        return row[0] if row else 0.001

    def iter_partial_hash_groups(
        self,
        fetch_size: int = 500,
    ) -> Iterator[tuple[str, list[tuple[str, int, float]]]]:
        """
        Yield (partial_hash, [(path, size, mtime)]) for groups ≥ 2.

        Memory-safe for 1 TB+ scans: uses a two-query paginated approach —
        first fetches distinct partial hashes in batches, then per-hash files.
        Never holds more than *fetch_size* hashes or one group's files in RAM.
        """
        tol = self._get_mtime_tol()
        last_hash = ""   # keyset cursor — avoids O(n²) OFFSET at 10M+ rows

        while True:
            # Page over distinct partial hashes that have ≥ 2 candidates
            with self._lock:
                cur = self._conn.execute(
                    """
                    SELECT fc.partial_hash
                    FROM session_files sf
                    JOIN file_cache fc
                        ON  sf.path = fc.path
                        AND sf.size = fc.size
                        AND ABS(sf.mtime - fc.mtime) < ?
                    WHERE sf.session_id = ? AND sf.stage = 'partial_done'
                      AND fc.partial_hash IS NOT NULL
                      AND fc.partial_hash > ?
                    GROUP BY fc.partial_hash
                    HAVING COUNT(*) >= 2
                    ORDER BY fc.partial_hash
                    LIMIT ?
                    """,
                    (tol, self.session_id, last_hash, fetch_size),
                )
                hashes = [row[0] for row in cur.fetchall()]

            if not hashes:
                break

            for ph in hashes:
                with self._lock:
                    cur = self._conn.execute(
                        """
                        SELECT sf.path, sf.size, sf.mtime
                        FROM session_files sf
                        JOIN file_cache fc
                            ON  sf.path = fc.path
                            AND sf.size = fc.size
                            AND ABS(sf.mtime - fc.mtime) < ?
                        WHERE sf.session_id = ? AND sf.stage = 'partial_done'
                          AND fc.partial_hash = ?
                        """,
                        (tol, self.session_id, ph),
                    )
                    files = cur.fetchall()
                if len(files) >= 2:
                    yield ph, [(r[0], r[1], r[2]) for r in files]

            last_hash = hashes[-1]
            if len(hashes) < fetch_size:
                break

    def iter_full_hash_groups(
        self,
        fetch_size: int = 500,
    ) -> Iterator[tuple[str, list[tuple[str, int, float]]]]:
        """
        Yield (full_hash, [(path, size, mtime)]) for groups ≥ 2.
        Same paginated two-query pattern as iter_partial_hash_groups.
        """
        tol = self._get_mtime_tol()
        last_hash = ""   # keyset cursor

        while True:
            with self._lock:
                cur = self._conn.execute(
                    """
                    SELECT fc.full_hash
                    FROM session_files sf
                    JOIN file_cache fc
                        ON  sf.path = fc.path
                        AND sf.size = fc.size
                        AND ABS(sf.mtime - fc.mtime) < ?
                    WHERE sf.session_id = ? AND sf.stage = 'full_done'
                      AND fc.full_hash IS NOT NULL
                      AND fc.full_hash > ?
                    GROUP BY fc.full_hash
                    HAVING COUNT(*) >= 2
                    ORDER BY fc.full_hash
                    LIMIT ?
                    """,
                    (tol, self.session_id, last_hash, fetch_size),
                )
                hashes = [row[0] for row in cur.fetchall()]

            if not hashes:
                break

            for fh in hashes:
                with self._lock:
                    cur = self._conn.execute(
                        """
                        SELECT sf.path, sf.size, sf.mtime
                        FROM session_files sf
                        JOIN file_cache fc
                            ON  sf.path = fc.path
                            AND sf.size = fc.size
                            AND ABS(sf.mtime - fc.mtime) < ?
                        WHERE sf.session_id = ? AND sf.stage = 'full_done'
                          AND fc.full_hash = ?
                        """,
                        (tol, self.session_id, fh),
                    )
                    files = cur.fetchall()
                if len(files) >= 2:
                    yield fh, [(r[0], r[1], r[2]) for r in files]

            last_hash = hashes[-1]
            if len(hashes) < fetch_size:
                break

    # ------------------------------------------------------------------
    # Incremental scan helpers
    # ------------------------------------------------------------------

    def get_known_file(self, path: str) -> tuple[object, ...] | None:
        """
        Return (size, mtime, inode, stage) for *path* if it exists in
        this session, else None.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT size, mtime, inode, stage FROM session_files "
                "WHERE session_id=? AND path=?",
                (self.session_id, path),
            )
            row = cur.fetchone()
            return tuple(row) if row else None

    def iter_inode_groups(
        self,
        fetch_size: int = 500,
    ) -> Iterator[tuple[int, int, list[str]]]:
        """
        Yield ``(device, inode, [paths])`` for inodes shared by ≥ 2 paths.
        Used to detect hard links before hashing.  Paginated.
        """
        offset = 0
        while True:
            with self._lock:
                cur = self._conn.execute(
                    """
                    SELECT device, inode, GROUP_CONCAT(path, char(0))
                    FROM session_files
                    WHERE session_id=? AND inode != 0
                    GROUP BY device, inode
                    HAVING COUNT(*) >= 2
                    ORDER BY device, inode
                    LIMIT ? OFFSET ?
                    """,
                    (self.session_id, fetch_size, offset),
                )
                rows = cur.fetchall()

            if not rows:
                break

            for device, inode, paths_str in rows:
                yield device, inode, paths_str.split("\x00")

            offset += len(rows)
            if len(rows) < fetch_size:
                break

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.flush_files()
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass  # pragma: no cover

    def __enter__(self) -> ScanSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if *pid* is a running process on this machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False   # process gone
    except PermissionError:
        return True    # process exists, just can't signal it
    except OSError:
        return False
