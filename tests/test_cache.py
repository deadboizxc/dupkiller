"""Tests for dupkiller.cache — HashCache CRUD, mtime tolerance, clean_missing."""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dupkiller.cache import MTIME_TOLERANCE, HashCache, _detect_mtime_tolerance
from dupkiller.utils import FileInfo


def _fi(path: str, size: int = 100, mtime: float = 1000.0) -> FileInfo:
    return FileInfo(path=path, size=size, mtime=mtime, inode=1, device=1)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test_cache.db"
    with HashCache(db_path) as c:
        yield c


class TestHashCacheCRUD:
    def test_miss_on_empty(self, db):
        fi = _fi("/nonexistent")
        assert db.get_partial_hash(fi) is None
        assert db.get_full_hash(fi) is None

    def test_store_and_retrieve_partial(self, db, tmp_path):
        p = str(tmp_path / "f.bin")
        Path(p).write_bytes(b"x")
        db.queue_update(p, 1, 1000.0, partial_hash="abcdef")
        db.flush()
        fi = _fi(p, size=1, mtime=1000.0)
        assert db.get_partial_hash(fi) == "abcdef"

    def test_store_and_retrieve_full(self, db, tmp_path):
        p = str(tmp_path / "f2.bin")
        Path(p).write_bytes(b"y")
        db.queue_update(p, 1, 1000.0, full_hash="deadbeef")
        db.flush()
        fi = _fi(p, size=1, mtime=1000.0)
        assert db.get_full_hash(fi) == "deadbeef"

    def test_size_mismatch_gives_miss(self, db, tmp_path):
        p = str(tmp_path / "f3.bin")
        Path(p).write_bytes(b"z")
        db.queue_update(p, 100, 1000.0, partial_hash="aaa")
        db.flush()
        fi = _fi(p, size=999, mtime=1000.0)  # wrong size
        assert db.get_partial_hash(fi) is None

    def test_mtime_within_tolerance_hits(self, db, tmp_path):
        p = str(tmp_path / "f4.bin")
        Path(p).write_bytes(b"w")
        db.queue_update(p, 1, 1000.0, partial_hash="bbb")
        db.flush()
        fi = _fi(p, size=1, mtime=1000.0 + MTIME_TOLERANCE * 0.5)
        assert db.get_partial_hash(fi) == "bbb"

    def test_mtime_beyond_tolerance_misses(self, db, tmp_path):
        p = str(tmp_path / "f5.bin")
        Path(p).write_bytes(b"v")
        db.queue_update(p, 1, 1000.0, partial_hash="ccc")
        db.flush()
        fi = _fi(p, size=1, mtime=1000.0 + MTIME_TOLERANCE * 2 + 1)
        assert db.get_partial_hash(fi) is None

    def test_upsert_preserves_other_hash(self, db, tmp_path):
        p = str(tmp_path / "f6.bin")
        Path(p).write_bytes(b"u")
        db.queue_update(p, 1, 1000.0, partial_hash="ppp")
        db.flush()
        db.queue_update(p, 1, 1000.0, full_hash="fff")
        db.flush()
        fi = _fi(p, size=1, mtime=1000.0)
        assert db.get_partial_hash(fi) == "ppp"
        assert db.get_full_hash(fi) == "fff"

    def test_batch_auto_flush(self, tmp_path):
        db_path = tmp_path / "batch.db"
        from dupkiller.cache import BATCH_SIZE
        with HashCache(db_path) as cache:
            for i in range(BATCH_SIZE + 10):
                cache.queue_update(f"/path/{i}", i, float(i), partial_hash=f"h{i}")
            # auto-flushed at BATCH_SIZE; remaining flushed on close
        with HashCache(db_path) as cache:
            stats = cache.cache_stats()
        assert stats["total_entries"] == BATCH_SIZE + 10


class TestCleanMissingFiles:
    def test_removes_absent_entries(self, db, tmp_path):
        real = tmp_path / "real.bin"
        real.write_bytes(b"r")
        fake = "/this/does/not/exist/at/all.bin"
        db.queue_update(str(real), 1, 1.0, partial_hash="r")
        db.queue_update(fake, 1, 1.0, partial_hash="f")
        db.flush()
        removed = db.clean_missing_files()
        assert removed == 1
        stats = db.cache_stats()
        assert stats["total_entries"] == 1

    def test_no_op_when_all_present(self, db, tmp_path):
        p = tmp_path / "exists.bin"
        p.write_bytes(b"e")
        db.queue_update(str(p), 1, 1.0, partial_hash="e")
        db.flush()
        removed = db.clean_missing_files()
        assert removed == 0


class TestCacheStats:
    def test_stats_structure(self, db, tmp_path):
        p = tmp_path / "s.bin"
        p.write_bytes(b"s")
        db.queue_update(str(p), 1, 1.0, partial_hash="pp", full_hash="ff")
        db.flush()
        s = db.cache_stats()
        assert s["total_entries"] == 1
        assert s["with_partial_hash"] == 1
        assert s["with_full_hash"] == 1


class TestScanResults:
    def test_save_and_retrieve(self, db):
        groups = [
            ("hash1", 1000, [("/a", 1.0), ("/b", 2.0)]),
            ("hash2", 500,  [("/c", 1.0), ("/d", 2.0), ("/e", 3.0)]),
        ]
        scan_id = db.save_scan_results("/root", total_files=100, duplicate_groups=groups)
        assert scan_id > 0

        latest = db.get_latest_scan()
        assert latest is not None
        assert latest["total_files"] == 100
        assert latest["duplicate_groups"] == 2
        assert latest["duplicate_files"] == 5
        assert latest["reclaimable_bytes"] == 1000 * 1 + 500 * 2

    def test_get_duplicate_groups(self, db):
        groups = [("hx", 100, [("/x1", 1.0), ("/x2", 2.0)])]
        scan_id = db.save_scan_results("/r", total_files=5, duplicate_groups=groups)
        result = db.get_duplicate_groups(scan_id)
        assert len(result) == 1
        assert result[0]["hash"] == "hx"
        assert len(result[0]["files"]) == 2

    def test_retains_only_5_scans(self, db):
        for i in range(7):
            db.save_scan_results("/same", total_files=i, duplicate_groups=[])
        scans = db.list_scans(limit=20)
        same = [s for s in scans if s["root_path"] == "/same"]
        assert len(same) == 5

    def test_get_latest_scan_empty(self, tmp_path):
        with HashCache(tmp_path / "empty.db") as cache:
            assert cache.get_latest_scan() is None

    def test_get_scan_by_id_found(self, db):
        scan_id = db.save_scan_results("/r", total_files=10, duplicate_groups=[])
        result = db.get_scan_by_id(scan_id)
        assert result is not None
        assert result["id"] == scan_id

    def test_get_scan_by_id_not_found(self, db):
        assert db.get_scan_by_id(99999) is None

    def test_list_scans_empty(self, tmp_path):
        with HashCache(tmp_path / "e.db") as cache:
            assert cache.list_scans() == []

    def test_iter_duplicate_groups_streaming(self, db):
        groups = [("h1", 100, [("/a", 1.0), ("/b", 2.0)])]
        scan_id = db.save_scan_results("/r", total_files=5, duplicate_groups=groups)
        result = list(db.iter_duplicate_groups(scan_id))
        assert len(result) == 1
        assert result[0]["hash"] == "h1"


class TestMtimeTolerance:
    def test_windows_returns_2_5(self):
        with patch("platform.system", return_value="Windows"):
            assert _detect_mtime_tolerance() == 2.5

    def test_darwin_returns_1_5(self):
        with patch("platform.system", return_value="Darwin"):
            assert _detect_mtime_tolerance() == 1.5

    def test_linux_returns_0_001(self):
        with patch("platform.system", return_value="Linux"):
            assert _detect_mtime_tolerance() == 0.001


class TestFlushError:
    def test_flush_error_logged(self, tmp_path, caplog):
        import logging
        db_path = tmp_path / "ferr.db"
        with HashCache(db_path) as cache:
            cache.queue_update("/p", 1, 1.0, partial_hash="x")
            # sqlite3.Connection.executemany is a C attribute and read-only in
            # CPython — swap out the whole _conn with a mock instead.
            real_conn = cache._conn
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.executemany = MagicMock(side_effect=sqlite3.Error("boom"))
            cache._conn = mock_conn
            with caplog.at_level(logging.ERROR):
                cache._flush_locked()
            cache._conn = real_conn  # restore so __exit__ can close cleanly
        assert "cache flush failed" in caplog.text


class TestCountMissingFiles:
    def test_counts_without_deleting(self, db, tmp_path):
        real = tmp_path / "real.bin"
        real.write_bytes(b"r")
        db.queue_update(str(real), 1, 1.0, partial_hash="r")
        db.queue_update("/nonexistent/abc.bin", 1, 1.0, partial_hash="n")
        db.flush()
        count = db.count_missing_files()
        assert count == 1
        # file_cache still has both entries
        assert db.cache_stats()["total_entries"] == 2
