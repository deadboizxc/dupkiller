"""Tests for dupkiller.checkpoint — ScanSession lifecycle, queries, migration."""
import os
from unittest.mock import patch

import pytest

from dupkiller.checkpoint import ScanSession, _pid_alive
from dupkiller.utils import FileInfo


def _fi(path: str, size: int = 100, mtime: float = 1.0,
        inode: int = 1, device: int = 1) -> FileInfo:
    return FileInfo(path=path, size=size, mtime=mtime, inode=inode, device=device)


@pytest.fixture
def session(tmp_path):
    db = tmp_path / "test.db"
    s = ScanSession.create(db, "/test/root", config={})
    with s:
        yield s


class TestPidAlive:
    def test_self_alive(self):
        assert _pid_alive(os.getpid()) is True

    def test_negative_pid(self):
        assert _pid_alive(-1) is False

    def test_zero_pid(self):
        assert _pid_alive(0) is False

    def test_dead_pid(self):
        # PID 999999 is virtually guaranteed to not exist
        assert _pid_alive(999999) is False


class TestScanSessionCreate:
    def test_creates_session(self, tmp_path):
        db = tmp_path / "c.db"
        s = ScanSession.create(db, "/root", config={"k": "v"})
        with s:
            assert s.session_id > 0
            info = s.get_info()
            assert info["root_path"] == "/root"
            assert info["stage"] == "scanning"
            assert info["status"] == "running"

    def test_find_resumable_none(self, tmp_path):
        db = tmp_path / "r.db"
        result = ScanSession.find_resumable(db, "/root")
        assert result is None

    def test_find_resumable_after_create(self, tmp_path):
        db = tmp_path / "r2.db"
        s = ScanSession.create(db, "/root2", config={})
        s.close()
        found = ScanSession.find_resumable(db, "/root2")
        assert found is not None
        assert found.session_id == s.session_id
        found.close()

    def test_mark_complete_not_resumable(self, tmp_path):
        db = tmp_path / "done.db"
        s = ScanSession.create(db, "/done", config={})
        s.mark_complete()
        found = ScanSession.find_resumable(db, "/done")
        assert found is None


class TestFileInsertion:
    def test_queue_and_flush(self, session):
        session.queue_file(_fi("/a"))
        session.queue_file(_fi("/b"))
        session.flush_files()
        assert session.total_scanned() == 2

    def test_rescan_replaces_record(self, session):
        """INSERT OR REPLACE: rescanning same path updates the record."""
        session.queue_file(_fi("/a", size=100))
        session.flush_files()
        session.queue_file(_fi("/a", size=200))  # same path, new size
        session.flush_files()
        assert session.total_scanned() == 1  # one row, not two

    def test_stage_counts(self, session):
        session.queue_file(_fi("/a"))
        session.queue_file(_fi("/b"))
        session.flush_files()
        counts = session.stage_counts()
        assert counts.get("scanned", 0) == 2

    def test_mark_files_stage(self, session):
        session.queue_file(_fi("/a"))
        session.queue_file(_fi("/b"))
        session.flush_files()
        session.mark_files_stage(["/a"], "partial_done")
        counts = session.stage_counts()
        assert counts["partial_done"] == 1
        assert counts["scanned"] == 1

    def test_mark_scanned_unique(self, session):
        session.queue_file(_fi("/a"))
        session.queue_file(_fi("/b"))
        session.flush_files()
        session.mark_files_stage_where_scanned_unique()
        counts = session.stage_counts()
        assert counts.get("unique", 0) == 2
        assert counts.get("scanned", 0) == 0


class TestIterSizeGroups:
    def test_yields_groups_with_min_2(self, session):
        session.queue_file(_fi("/a", size=100))
        session.queue_file(_fi("/b", size=100))
        session.queue_file(_fi("/c", size=200))  # singleton
        session.flush_files()
        groups = list(session.iter_size_groups())
        assert len(groups) == 1
        size, files = groups[0]
        assert size == 100
        assert len(files) == 2

    def test_pagination(self, session):
        for i in range(50):
            session.queue_file(_fi(f"/{i}a", size=i * 10 + 10))
            session.queue_file(_fi(f"/{i}b", size=i * 10 + 10))
        session.flush_files()
        groups = list(session.iter_size_groups(fetch_size=10))
        assert len(groups) == 50


class TestCountCandidates:
    def test_only_counts_size_peers(self, session):
        session.queue_file(_fi("/a", size=100))
        session.queue_file(_fi("/b", size=100))
        session.queue_file(_fi("/c", size=200))  # singleton — not a candidate
        session.flush_files()
        assert session.count_candidates() == 2


class TestIterInodeGroups:
    def test_detects_hardlinks(self, session):
        session.queue_file(_fi("/link1", size=100, inode=42, device=7))
        session.queue_file(_fi("/link2", size=100, inode=42, device=7))
        session.queue_file(_fi("/other", size=100, inode=99, device=7))
        session.flush_files()
        groups = list(session.iter_inode_groups())
        assert len(groups) == 1
        dev, ino, paths = groups[0]
        assert ino == 42
        assert set(paths) == {"/link1", "/link2"}

    def test_ignores_inode_zero(self, session):
        session.queue_file(_fi("/a", inode=0, device=0))
        session.queue_file(_fi("/b", inode=0, device=0))
        session.flush_files()
        groups = list(session.iter_inode_groups())
        assert len(groups) == 0


class TestCheckpointIfDue:
    def test_not_due_initially(self, session):
        was_checkpointed = session.checkpoint_if_due(interval=9999.0)
        assert was_checkpointed is False

    def test_due_after_interval(self, session):
        session._last_checkpoint -= 400  # fake elapsed time
        was_checkpointed = session.checkpoint_if_due(interval=300.0)
        assert was_checkpointed is True


class TestSessionLifecycle:
    def test_set_stage(self, tmp_path):
        db = tmp_path / "s.db"
        s = ScanSession.create(db, "/r", config={})
        with s:
            s.set_stage("partial_hashing")
            assert s.get_stage() == "partial_hashing"

    def test_mark_interrupted(self, tmp_path):
        db = tmp_path / "i.db"
        s = ScanSession.create(db, "/r", config={})
        s.mark_interrupted()
        # session should no longer be resumable
        found = ScanSession.find_resumable(db, "/r")
        assert found is None

    def test_get_known_file_exists(self, session):
        session.queue_file(_fi("/known", size=50))
        session.flush_files()
        row = session.get_known_file("/known")
        assert row is not None
        assert row[0] == 50  # size

    def test_get_known_file_missing(self, session):
        assert session.get_known_file("/no_such_file") is None

    def test_context_manager(self, tmp_path):
        db = tmp_path / "ctx.db"
        with ScanSession.create(db, "/r", config={}) as s:
            s.queue_file(_fi("/f"))
            s.flush_files()
            assert s.total_scanned() == 1

    def test_get_info_returns_empty_for_unknown_id(self, tmp_path):
        """get_info returns {} when the session row has been deleted."""
        import sqlite3
        db = tmp_path / "gi.db"
        s = ScanSession.create(db, "/r", config={})
        with s:
            # Delete the session row directly to trigger the empty-row path
            conn = sqlite3.connect(str(db))
            conn.execute("DELETE FROM scan_sessions WHERE id=?", (s.session_id,))
            conn.commit()
            conn.close()
            info = s.get_info()
            assert info == {}

    def test_mark_files_stage_empty_paths(self, session):
        """mark_files_stage with empty list is a no-op."""
        session.queue_file(_fi("/a"))
        session.flush_files()
        session.mark_files_stage([], "partial_done")  # should not raise
        counts = session.stage_counts()
        assert counts.get("scanned", 0) == 1

    def test_auto_flush_on_batch_limit(self, tmp_path):
        """queue_file auto-flushes when pending count reaches _BATCH_INSERT (500)."""
        from dupkiller.checkpoint import _BATCH_INSERT
        db = tmp_path / "ab.db"
        s = ScanSession.create(db, "/r", config={})
        with s:
            for i in range(_BATCH_INSERT + 1):
                s.queue_file(_fi(f"/{i}", size=100, inode=i + 1))
            # At _BATCH_INSERT + 1 items, auto-flush was triggered at least once
            # so total_scanned should reflect some committed rows
            s.flush_files()
            assert s.total_scanned() == _BATCH_INSERT + 1


class TestGetMtimeTol:
    def test_fallback_when_no_config_table(self, tmp_path):
        """_get_mtime_tol falls back to 0.001 when _config table does not exist."""
        db = tmp_path / "nomtime.db"
        s = ScanSession.create(db, "/r", config={})
        with s:
            # No HashCache on this DB → _config table doesn't exist → OperationalError
            tol = s._get_mtime_tol()
        assert tol == 0.001

    def test_fallback_when_no_row(self, tmp_path):
        """_get_mtime_tol falls back to 0.001 when _config has no mtime_tolerance row."""
        db = tmp_path / "nocfg.db"
        s = ScanSession.create(db, "/r", config={})
        with s:
            # Create _config table but insert no mtime_tolerance row
            s._conn.execute(
                "CREATE TABLE IF NOT EXISTS _config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            s._conn.commit()
            tol = s._get_mtime_tol()
        assert tol == 0.001


class TestPidAliveEdgeCases:
    def test_permission_error(self):
        """PermissionError from os.kill means process exists → True."""
        with patch("dupkiller.checkpoint.os.kill", side_effect=PermissionError):
            from dupkiller.checkpoint import _pid_alive
            assert _pid_alive(12345) is True

    def test_oserror(self):
        """Generic OSError from os.kill → False."""
        with patch("dupkiller.checkpoint.os.kill", side_effect=OSError("other")):
            from dupkiller.checkpoint import _pid_alive
            assert _pid_alive(12345) is False


class TestIterHashGroups:
    def _populate(self, session, cache, partial_hash="ph1", full_hash="fh1"):
        """Add two files with same partial/full hash via cache."""
        for path in ["/x/a.bin", "/x/b.bin"]:
            session.queue_file(_fi(path, size=100))
            cache.queue_update(path, 100, 1.0,
                               partial_hash=partial_hash, full_hash=full_hash)
        session.flush_files()
        cache.flush()
        session.mark_files_stage(["/x/a.bin", "/x/b.bin"], "partial_done")

    def test_iter_partial_hash_groups(self, tmp_path):
        db = tmp_path / "p.db"
        from dupkiller.cache import HashCache
        with HashCache(db) as cache:
            s = ScanSession.create(db, "/r", config={})
            with s:
                self._populate(s, cache)
                groups = list(s.iter_partial_hash_groups())
        assert len(groups) == 1
        ph, files = groups[0]
        assert ph == "ph1"
        assert len(files) == 2

    def test_iter_full_hash_groups(self, tmp_path):
        db = tmp_path / "f.db"
        from dupkiller.cache import HashCache
        with HashCache(db) as cache:
            s = ScanSession.create(db, "/r", config={})
            with s:
                self._populate(s, cache)
                session_mark = s.mark_files_stage
                session_mark(["/x/a.bin", "/x/b.bin"], "full_done")
                groups = list(s.iter_full_hash_groups())
        assert len(groups) == 1

    def test_iter_partial_empty(self, tmp_path):
        # iter_partial_hash_groups JOINs file_cache, so HashCache must open the
        # same DB first to create that table.
        db = tmp_path / "pe.db"
        from dupkiller.cache import HashCache
        with HashCache(db):
            pass
        s = ScanSession.create(db, "/r", config={})
        with s:
            groups = list(s.iter_partial_hash_groups())
        assert groups == []

    def test_iter_full_empty(self, tmp_path):
        db = tmp_path / "fe.db"
        from dupkiller.cache import HashCache
        with HashCache(db):
            pass
        s = ScanSession.create(db, "/r", config={})
        with s:
            groups = list(s.iter_full_hash_groups())
        assert groups == []


class TestSchemaMigration:
    def test_v1_migrates_to_v2(self, tmp_path):
        """Simulate a v1 DB (no inode/device) and open it with current code."""
        import sqlite3
        db = tmp_path / "v1.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE _schema_version (version INTEGER NOT NULL);
            INSERT INTO _schema_version VALUES (1);
            CREATE TABLE session_locks (
                root_path TEXT PRIMARY KEY,
                session_id INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                locked_at REAL NOT NULL
            );
            CREATE TABLE scan_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                root_path TEXT NOT NULL,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                stage TEXT NOT NULL DEFAULT 'scanning',
                config_json TEXT NOT NULL DEFAULT '{}',
                files_scanned INTEGER DEFAULT 0,
                output_jsonl TEXT,
                output_log TEXT
            );
            CREATE TABLE session_files (
                session_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                stage TEXT NOT NULL DEFAULT 'scanned',
                PRIMARY KEY (session_id, path)
            );
        """)
        conn.commit()
        conn.close()

        # Opening via ScanSession should migrate automatically
        s = ScanSession.create(db, "/migrated", config={})
        s.queue_file(_fi("/migrated/file.txt", inode=5, device=3))
        s.flush_files()
        with s:
            assert s.total_scanned() == 1
