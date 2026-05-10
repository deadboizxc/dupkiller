"""Tests for dupkiller.utils — FileInfo, ScanCounters, format_bytes, ShutdownFlag."""
import threading

import pytest

from dupkiller.utils import FileInfo, ScanCounters, ShutdownFlag, format_bytes


class TestFileInfo:
    def _make(self, path="a", size=100, mtime=1.0, inode=1, device=1):
        return FileInfo(path=path, size=size, mtime=mtime, inode=inode, device=device)

    def test_slots(self):
        fi = self._make()
        assert fi.path == "a"
        assert fi.size == 100
        assert fi.inode == 1
        assert fi.device == 1

    def test_hash_by_path(self):
        fi1 = FileInfo(path="x", size=1, mtime=1.0, inode=10, device=5)
        fi2 = FileInfo(path="x", size=999, mtime=9.0, inode=99, device=99)
        assert hash(fi1) == hash(fi2)

    def test_eq_by_path(self):
        fi1 = FileInfo(path="y", size=1, mtime=1.0, inode=1, device=1)
        fi2 = FileInfo(path="y", size=2, mtime=2.0, inode=2, device=2)
        assert fi1 == fi2

    def test_usable_in_set(self):
        fi1 = FileInfo(path="a", size=1, mtime=1.0, inode=1, device=1)
        fi2 = FileInfo(path="a", size=2, mtime=2.0, inode=2, device=2)
        fi3 = FileInfo(path="b", size=1, mtime=1.0, inode=1, device=1)
        s = {fi1, fi2, fi3}
        assert len(s) == 2


class TestScanCounters:
    def test_inc_single(self):
        c = ScanCounters()
        c.inc("skipped_permission")
        assert c.skipped_permission == 1

    def test_inc_by_n(self):
        c = ScanCounters()
        c.inc("hash_errors", 5)
        assert c.hash_errors == 5

    def test_total_skipped(self):
        c = ScanCounters()
        c.inc("skipped_permission", 2)
        c.inc("skipped_symlink", 3)
        c.inc("skipped_too_small", 1)
        assert c.total_skipped() == 6
        # hash_errors not in total_skipped
        c.inc("hash_errors", 10)
        assert c.total_skipped() == 6

    def test_thread_safe_inc(self):
        c = ScanCounters()
        def bump():
            for _ in range(1000):
                c.inc("skipped_excluded")
        threads = [threading.Thread(target=bump) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert c.skipped_excluded == 10_000

    def test_inc_unknown_field_raises(self):
        c = ScanCounters()
        with pytest.raises((AttributeError, TypeError)):
            c.inc("nonexistent_field")


class TestFileInfoEq:
    def test_eq_non_fileinfo_returns_not_implemented(self):
        fi = FileInfo(path="a", size=1, mtime=1.0, inode=1, device=1)
        assert fi.__eq__("not a fileinfo") is NotImplemented

    def test_eq_non_fileinfo_via_operator(self):
        fi = FileInfo(path="a", size=1, mtime=1.0, inode=1, device=1)
        # != with unrelated type should not raise
        assert fi != 42


class TestFormatBytes:
    @pytest.mark.parametrize("n,expected", [
        (0, "0.00 B"),
        (999, "999.00 B"),
        (1024, "1.00 KB"),
        (1024 ** 2, "1.00 MB"),
        (1024 ** 3, "1.00 GB"),
        (1536, "1.50 KB"),
        (1_500_000, "1.43 MB"),
        (1024 ** 5, "1.00 PB"),
    ])
    def test_format(self, n, expected):
        assert format_bytes(n) == expected


class TestFormatRate:
    def test_format_rate(self):
        from dupkiller.utils import format_rate
        assert format_rate(1024 * 1024) == "1.00 MB/s"


class TestShutdownFlag:
    def test_default_not_set(self):
        f = ShutdownFlag()
        assert not f.is_set()

    def test_set(self):
        f = ShutdownFlag()
        f.set()
        assert f.is_set()

    def test_install_signal_handlers_idempotent(self):
        f = ShutdownFlag()
        f.install_signal_handlers()
        f.install_signal_handlers()  # should not raise
