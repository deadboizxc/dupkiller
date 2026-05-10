"""
SQLite-backed hash cache and scan results storage.

HashCache stores per-file ``(partial_hash, full_hash)`` records keyed by path.
All writes are batched and committed in WAL mode for throughput.  Queries use
keyset pagination to remain O(1) in memory for arbitrarily large datasets.

Mtime comparison uses a platform-aware tolerance to handle filesystem resolution
differences (ext4: 1 ms, HFS+: 1 s, NTFS: 100 ms, FAT32: 2 s).  The tolerance
is stored in the ``_config`` table so that ``ScanSession`` JOIN queries agree.
"""

from __future__ import annotations

import logging
import os
import platform
import sqlite3
import threading
import time
from pathlib import Path

from dupkiller.utils import FileInfo

logger = logging.getLogger(__name__)

DB_PATH: Path = Path.home() / ".dupkiller" / "cache.db"
BATCH_SIZE: int = 500


# ---------------------------------------------------------------------------
# Platform mtime tolerance
# ---------------------------------------------------------------------------

def _detect_mtime_tolerance() -> float:
    """
    Return the mtime comparison tolerance in seconds for the current OS.

    Filesystem resolution:
      ext4 / XFS / APFS : 1 ns  → use 1 ms tolerance
      HFS+               : 1 s   → use 1.5 s tolerance
      NTFS               : 100 ns → use 100 ms tolerance
      FAT32/exFAT        : 2 s   → use 2.5 s tolerance

    We use a conservative value per OS, not per filesystem, to keep it simple.
    """
    sys = platform.system()
    if sys == "Windows":
        return 2.5    # FAT32 worst case
    if sys == "Darwin":
        return 1.5    # HFS+ worst case; APFS is nanoseconds but same binary
    return 0.001      # Linux: ext4/xfs/btrfs all have nanosecond precision

MTIME_TOLERANCE: float = _detect_mtime_tolerance()


# ---------------------------------------------------------------------------
# HashCache
# ---------------------------------------------------------------------------

class HashCache:
    """Thread-safe SQLite cache for file hashes and scan results."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._pending: list[tuple] = []
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript("""
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous  = NORMAL;
                PRAGMA foreign_keys = ON;
                PRAGMA cache_size   = -32000;
                PRAGMA temp_store   = MEMORY;

                CREATE TABLE IF NOT EXISTS _config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_cache (
                    path         TEXT    PRIMARY KEY,
                    size         INTEGER NOT NULL,
                    mtime        REAL    NOT NULL,
                    partial_hash TEXT,
                    full_hash    TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_fc_partial
                    ON file_cache(partial_hash)
                    WHERE partial_hash IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_fc_full
                    ON file_cache(full_hash)
                    WHERE full_hash IS NOT NULL;

                CREATE TABLE IF NOT EXISTS scan_results (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_time        REAL    NOT NULL,
                    root_path        TEXT    NOT NULL,
                    total_files      INTEGER DEFAULT 0,
                    duplicate_groups INTEGER DEFAULT 0,
                    duplicate_files  INTEGER DEFAULT 0,
                    reclaimable_bytes INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS duplicate_groups (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id   INTEGER NOT NULL,
                    group_hash TEXT   NOT NULL,
                    file_size  INTEGER NOT NULL,
                    FOREIGN KEY (scan_id) REFERENCES scan_results(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS duplicate_files (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    path     TEXT    NOT NULL,
                    mtime    REAL    NOT NULL,
                    FOREIGN KEY (group_id) REFERENCES duplicate_groups(id)
                        ON DELETE CASCADE
                );

                -- Fix: index on group_id was missing in v1
                CREATE INDEX IF NOT EXISTS idx_df_group_id
                    ON duplicate_files(group_id);
            """)
            # Store mtime tolerance so checkpoint.py JOIN queries can read it
            self._conn.execute(
                "INSERT OR IGNORE INTO _config (key, value) VALUES ('mtime_tolerance', ?)",
                (str(MTIME_TOLERANCE),),
            )

    def get_db_path(self) -> Path:
        return Path(self._path)

    # ------------------------------------------------------------------
    # Cache reads
    # ------------------------------------------------------------------

    def get_cached_hashes(
        self, fi: FileInfo
    ) -> tuple[str | None, str | None]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT partial_hash, full_hash, mtime, size "
                "FROM file_cache WHERE path=?",
                (fi.path,),
            )
            row = cur.fetchone()

        if row is None:
            return None, None

        cached_partial, cached_full, cached_mtime, cached_size = row
        if cached_size == fi.size and abs(cached_mtime - fi.mtime) <= MTIME_TOLERANCE:
            return cached_partial, cached_full
        return None, None

    def get_partial_hash(self, fi: FileInfo) -> str | None:
        return self.get_cached_hashes(fi)[0]

    def get_full_hash(self, fi: FileInfo) -> str | None:
        return self.get_cached_hashes(fi)[1]

    # ------------------------------------------------------------------
    # Cache writes (batched)
    # ------------------------------------------------------------------

    def queue_update(
        self,
        path: str,
        size: int,
        mtime: float,
        partial_hash: str | None = None,
        full_hash: str | None = None,
    ) -> None:
        with self._lock:
            self._pending.append((path, size, mtime, partial_hash, full_hash))
            if len(self._pending) >= BATCH_SIZE:
                self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT INTO file_cache (path, size, mtime, partial_hash, full_hash)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size         = excluded.size,
                        mtime        = excluded.mtime,
                        partial_hash = COALESCE(excluded.partial_hash, partial_hash),
                        full_hash    = COALESCE(excluded.full_hash,    full_hash)
                    """,
                    batch,
                )
        except sqlite3.Error as exc:
            logger.error("cache flush failed: %s", exc)

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    # ------------------------------------------------------------------
    # Cache maintenance
    # ------------------------------------------------------------------

    def count_missing_files(self, batch_size: int = 1000) -> int:
        """Count cache entries whose file no longer exists on disk (dry-run helper)."""
        stale = 0
        last_path = ""
        while True:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT path FROM file_cache WHERE path > ? ORDER BY path LIMIT ?",
                    (last_path, batch_size),
                )
                paths = [row[0] for row in cur.fetchall()]
            if not paths:
                break
            stale += sum(1 for p in paths if not os.path.lexists(p))
            last_path = paths[-1]
            if len(paths) < batch_size:
                break
        return stale

    def clean_missing_files(self, batch_size: int = 1000) -> int:
        """
        Remove cache entries whose file no longer exists on disk.

        Processes paths in batches to avoid loading millions of rows.
        Returns the number of entries removed.
        After cleaning, runs VACUUM + ANALYZE to reclaim space and refresh
        query planner statistics.
        """
        removed = 0
        last_path = ""   # keyset cursor — avoids O(n²) OFFSET on large caches

        while True:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT path FROM file_cache WHERE path > ? ORDER BY path LIMIT ?",
                    (last_path, batch_size),
                )
                paths = [row[0] for row in cur.fetchall()]

            if not paths:
                break

            missing = [p for p in paths if not os.path.lexists(p)]
            if missing:
                with self._lock:
                    ph = ",".join("?" * len(missing))
                    with self._conn:
                        self._conn.execute(
                            f"DELETE FROM file_cache WHERE path IN ({ph})", missing
                        )
                removed += len(missing)
                logger.info("Removed %d stale cache entries (batch after %r)", len(missing), last_path)

            last_path = paths[-1]
            if len(paths) < batch_size:
                break

        if removed:
            self.vacuum()

        return removed

    def vacuum(self) -> None:
        """
        Reclaim disk space after bulk deletes and refresh query planner stats.

        VACUUM rewrites the DB file (may take seconds on large caches).
        ANALYZE updates table/index statistics so SQLite picks optimal plans.
        Should be called after clean_missing_files() or any bulk delete.
        """
        logger.info("Running VACUUM on cache DB...")
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.execute("ANALYZE")
        logger.info("VACUUM + ANALYZE complete")

    def cache_stats(self) -> dict:
        """Return basic cache statistics."""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM file_cache").fetchone()[0]
            with_partial = self._conn.execute(
                "SELECT COUNT(*) FROM file_cache WHERE partial_hash IS NOT NULL"
            ).fetchone()[0]
            with_full = self._conn.execute(
                "SELECT COUNT(*) FROM file_cache WHERE full_hash IS NOT NULL"
            ).fetchone()[0]
        return {
            "total_entries": total,
            "with_partial_hash": with_partial,
            "with_full_hash": with_full,
        }

    # ------------------------------------------------------------------
    # Scan results
    # ------------------------------------------------------------------

    def save_scan_results(
        self,
        root_path: str,
        total_files: int,
        duplicate_groups,   # Iterable[tuple[hash, size, [(path, mtime)]]]
    ) -> int:
        """
        Persist scan results.  *duplicate_groups* may be a list OR a generator
        so callers can stream from JSONL without loading everything into RAM.
        Totals (reclaimable, dup_files, group_count) are computed on the fly.
        """
        reclaimable  = 0
        dup_files    = 0
        group_count  = 0

        with self._lock:
            with self._conn:
                # Insert placeholder row first; update counters after iteration
                cur = self._conn.execute(
                    "INSERT INTO scan_results "
                    "(scan_time, root_path, total_files, duplicate_groups, "
                    " duplicate_files, reclaimable_bytes) "
                    "VALUES (?, ?, ?, 0, 0, 0)",
                    (time.time(), root_path, total_files),
                )
                scan_id = cur.lastrowid
                if scan_id is None:  # pragma: no cover
                    raise sqlite3.DatabaseError("failed to insert scan_results row")  # pragma: no cover

                for group_hash, file_size, files in duplicate_groups:
                    cur2 = self._conn.execute(
                        "INSERT INTO duplicate_groups (scan_id, group_hash, file_size) "
                        "VALUES (?, ?, ?)",
                        (scan_id, group_hash, file_size),
                    )
                    group_id = cur2.lastrowid
                    self._conn.executemany(
                        "INSERT INTO duplicate_files (group_id, path, mtime) VALUES (?,?,?)",
                        [(group_id, p, m) for p, m in files],
                    )
                    n = len(files)
                    reclaimable += file_size * (n - 1)
                    dup_files   += n
                    group_count += 1

                # Patch the row with actual totals
                self._conn.execute(
                    "UPDATE scan_results SET duplicate_groups=?, duplicate_files=?, "
                    "reclaimable_bytes=? WHERE id=?",
                    (group_count, dup_files, reclaimable, scan_id),
                )

                # Keep only the 5 most recent scans per root
                self._conn.execute(
                    """
                    DELETE FROM scan_results
                    WHERE root_path=? AND id NOT IN (
                        SELECT id FROM scan_results
                        WHERE root_path=? ORDER BY scan_time DESC LIMIT 5
                    )
                    """,
                    (root_path, root_path),
                )
            return scan_id

    def get_latest_scan(self) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM scan_results ORDER BY scan_time DESC LIMIT 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in cur.description], row))

    def get_scan_by_id(self, scan_id: int) -> dict | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM scan_results WHERE id=?", (scan_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(zip([d[0] for d in cur.description], row))

    def list_scans(self, limit: int = 20) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM scan_results ORDER BY scan_time DESC LIMIT ?", (limit,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def iter_duplicate_groups(
        self, scan_id: int, fetch_size: int = 200
    ):
        """
        Streaming version of get_duplicate_groups — yields one group dict at
        a time.  Use this for large scans (1 TB+) where loading all groups
        at once would exhaust RAM.

        Yields dicts with keys: id, hash, size, files (list of {path, mtime}).
        """
        offset = 0
        while True:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT id, group_hash, file_size FROM duplicate_groups "
                    "WHERE scan_id=? ORDER BY id LIMIT ? OFFSET ?",
                    (scan_id, fetch_size, offset),
                )
                group_rows = cur.fetchall()

            if not group_rows:
                break

            for group_id, group_hash, file_size in group_rows:
                with self._lock:
                    cur = self._conn.execute(
                        "SELECT path, mtime FROM duplicate_files "
                        "WHERE group_id=? ORDER BY mtime DESC",
                        (group_id,),
                    )
                    files = [{"path": r[0], "mtime": r[1]} for r in cur.fetchall()]
                yield {
                    "id": group_id,
                    "hash": group_hash,
                    "size": file_size,
                    "files": files,
                }

            offset += len(group_rows)
            if len(group_rows) < fetch_size:
                break

    def get_duplicate_groups(self, scan_id: int) -> list[dict]:
        """
        Return all duplicate groups for *scan_id*.
        Uses a single JOIN query instead of N per-group queries (O(1) vs O(n)).
        """
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT dg.id, dg.group_hash, dg.file_size, df.path, df.mtime
                FROM duplicate_groups dg
                JOIN duplicate_files df ON df.group_id = dg.id
                WHERE dg.scan_id = ?
                ORDER BY dg.id, df.mtime DESC
                """,
                (scan_id,),
            )
            rows = cur.fetchall()

        groups: dict[int, dict] = {}
        for group_id, group_hash, file_size, path, mtime in rows:
            if group_id not in groups:
                groups[group_id] = {
                    "id": group_id,
                    "hash": group_hash,
                    "size": file_size,
                    "files": [],
                }
            groups[group_id]["files"].append({"path": path, "mtime": mtime})

        return list(groups.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.flush()
        self._conn.close()

    def __enter__(self) -> HashCache:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
