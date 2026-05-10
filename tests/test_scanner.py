"""Tests for dupkiller.scanner — scan_files, exclude patterns, cycle guard."""
import os
from pathlib import Path
from unittest.mock import patch

from dupkiller.scanner import _is_excluded, scan_files
from dupkiller.utils import FileInfo, ScanCounters, ShutdownFlag


def _tree(root: Path, files: dict[str, bytes]) -> None:
    """Create a tree of files under root. keys are relative paths."""
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class TestScanFiles:
    def test_basic_scan(self, tmp_path):
        _tree(tmp_path, {
            "a.txt": b"hello",
            "b.txt": b"world",
            "sub/c.txt": b"deep",
        })
        results = list(scan_files(str(tmp_path)))
        paths = {fi.path for fi in results}
        assert str(tmp_path / "a.txt") in paths
        assert str(tmp_path / "b.txt") in paths
        assert str(tmp_path / "sub/c.txt") in paths

    def test_returns_fileinfo(self, tmp_path):
        (tmp_path / "f.bin").write_bytes(b"12345")
        results = list(scan_files(str(tmp_path)))
        assert len(results) == 1
        fi = results[0]
        assert isinstance(fi, FileInfo)
        assert fi.size == 5
        assert fi.inode > 0
        assert fi.device > 0

    def test_min_size_filter(self, tmp_path):
        _tree(tmp_path, {
            "small.txt": b"x",
            "big.txt": b"x" * 100,
        })
        results = list(scan_files(str(tmp_path), min_size=50))
        assert len(results) == 1
        assert results[0].size == 100

    def test_max_size_filter(self, tmp_path):
        _tree(tmp_path, {
            "small.txt": b"x",
            "big.txt": b"x" * 100,
        })
        results = list(scan_files(str(tmp_path), max_size=50))
        assert len(results) == 1
        assert results[0].size == 1

    def test_exclude_by_name(self, tmp_path):
        _tree(tmp_path, {
            "keep.txt": b"keep",
            "skip.log": b"skip",
        })
        results = list(scan_files(str(tmp_path), exclude=["*.log"]))
        assert all(not fi.path.endswith(".log") for fi in results)
        assert len(results) == 1

    def test_exclude_by_dirname(self, tmp_path):
        _tree(tmp_path, {
            "ok.txt": b"ok",
            ".git/config": b"git",
        })
        results = list(scan_files(str(tmp_path), exclude=[".git"]))
        paths = [fi.path for fi in results]
        assert all(".git" not in p for p in paths)

    def test_shutdown_respected(self, tmp_path):
        for i in range(50):
            (tmp_path / f"f{i}.txt").write_bytes(b"x")
        shutdown = ShutdownFlag()
        shutdown.set()
        results = list(scan_files(str(tmp_path), shutdown=shutdown))
        assert len(results) == 0

    def test_counters_populated(self, tmp_path):
        _tree(tmp_path, {
            "big.bin": b"x" * 200,
            "tiny.txt": b"x",
        })
        counters = ScanCounters()
        results = list(scan_files(str(tmp_path), min_size=100, counters=counters))
        assert len(results) == 1
        assert counters.skipped_too_small == 1

    def test_symlink_not_followed_by_default(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_bytes(b"real")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        results = list(scan_files(str(tmp_path), follow_symlinks=False))
        # symlinks should not appear as regular files
        paths = [fi.path for fi in results]
        assert str(link) not in paths
        assert str(target) in paths

    def test_exclude_by_path_pattern(self, tmp_path):
        """Patterns with '/' are matched against full path."""
        _tree(tmp_path, {
            "keep.txt": b"keep",
            "sub/skip.txt": b"skip",
        })
        pat = "*/sub/*"
        results = list(scan_files(str(tmp_path), exclude=[pat]))
        paths = [fi.path for fi in results]
        assert all("sub" not in p for p in paths)

    def test_oserror_on_dir_stat_skipped(self, tmp_path):
        (tmp_path / "f.txt").write_bytes(b"x")
        counters = ScanCounters()
        original_stat = os.stat

        def bad_stat(path, **kw):
            if str(path) == str(tmp_path):
                raise PermissionError("no access")
            return original_stat(path, **kw)

        with patch("os.stat", side_effect=bad_stat):
            list(scan_files(str(tmp_path), counters=counters))
        assert counters.skipped_permission >= 1

    def test_permission_error_on_scandir(self, tmp_path):
        sub = tmp_path / "restricted"
        sub.mkdir()
        (sub / "f.txt").write_bytes(b"x")
        counters = ScanCounters()
        original_scandir = os.scandir

        def bad_scandir(path):
            if str(path) == str(sub):
                raise PermissionError("denied")
            return original_scandir(path)

        with patch("os.scandir", side_effect=bad_scandir):
            list(scan_files(str(tmp_path), counters=counters))
        assert counters.skipped_permission >= 1

    def test_oserror_on_scandir(self, tmp_path):
        sub = tmp_path / "broken"
        sub.mkdir()
        counters = ScanCounters()
        original_scandir = os.scandir

        def bad_scandir(path):
            if str(path) == str(sub):
                raise OSError("I/O error")
            return original_scandir(path)

        with patch("os.scandir", side_effect=bad_scandir):
            list(scan_files(str(tmp_path), counters=counters))
        assert counters.skipped_permission >= 1

    def test_stat_failed_on_file(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_bytes(b"x")
        counters = ScanCounters()


        class FakeEntry:
            name = "f.txt"
            path = str(f)
            def is_dir(self, **kw): return False
            def is_file(self, **kw): return True
            def is_symlink(self): return False
            def stat(self, **kw): raise PermissionError("no stat")

        class FakeScanDir:
            def __enter__(self): return iter([FakeEntry()])
            def __exit__(self, *a): pass

        original_stat = os.stat
        call_count = [0]
        def selective_stat(p, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return original_stat(p, **kw)  # dir stat ok
            return original_stat(p, **kw)

        with patch("os.scandir", return_value=FakeScanDir()):
            list(scan_files(str(tmp_path), counters=counters))
        assert counters.skipped_permission >= 1

    def test_is_excluded_path_pattern(self):
        assert _is_excluded("file.txt", "/a/b/file.txt", [], ["*/b/*"]) is True
        assert _is_excluded("file.txt", "/x/y/file.txt", [], ["*/b/*"]) is False

    def test_max_size_counter(self, tmp_path):
        (tmp_path / "big.bin").write_bytes(b"x" * 1000)
        counters = ScanCounters()
        list(scan_files(str(tmp_path), max_size=100, counters=counters))
        assert counters.skipped_too_large == 1

    def test_cycle_guard(self, tmp_path):
        """A symlink loop must not cause infinite recursion."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "f.txt").write_bytes(b"hello")
        loop = sub / "loop"
        loop.symlink_to(tmp_path)  # points back to parent
        # follow_symlinks=True would recurse; cycle guard must stop it
        results = list(scan_files(str(tmp_path), follow_symlinks=True))
        assert len(results) < 1000  # didn't explode

    def test_shutdown_during_scan(self, tmp_path):
        """scan_files returns early when shutdown flag is set."""
        from dupkiller.utils import ShutdownFlag
        for i in range(5):
            (tmp_path / f"f{i}.bin").write_bytes(b"x" * 100)
        shutdown = ShutdownFlag()
        shutdown.set()
        results = list(scan_files(str(tmp_path), shutdown=shutdown))
        assert results == []

    def test_is_dir_is_file_oserror_regular(self, tmp_path):
        """OSError from is_dir() on a non-symlink entry → skipped_permission."""
        (tmp_path / "file.bin").write_bytes(b"x" * 100)
        counters = ScanCounters()

        class BadEntry:
            name = "file.bin"
            path = str(tmp_path / "file.bin")
            def is_symlink(self): return False
            def is_dir(self, **kw): raise OSError("fail")
            def is_file(self, **kw): raise OSError("fail")
            def stat(self, **kw):
                return os.stat(self.path)

        class FakeScanDir:
            def __enter__(self): return iter([BadEntry()])
            def __exit__(self, *a): pass

        with patch("os.scandir", return_value=FakeScanDir()):
            list(scan_files(str(tmp_path), counters=counters))
        assert counters.skipped_permission >= 1

    def test_is_dir_is_file_oserror_symlink(self, tmp_path):
        """OSError from is_dir() on a symlink entry → skipped_symlink."""
        (tmp_path / "link").write_bytes(b"x" * 100)
        counters = ScanCounters()

        class SymlinkEntry:
            name = "link"
            path = str(tmp_path / "link")
            def is_symlink(self): return True
            def is_dir(self, **kw): raise OSError("broken symlink")
            def is_file(self, **kw): raise OSError("broken symlink")
            def stat(self, **kw):
                return os.stat(self.path)

        class FakeScanDir:
            def __enter__(self): return iter([SymlinkEntry()])
            def __exit__(self, *a): pass

        with patch("os.scandir", return_value=FakeScanDir()):
            list(scan_files(str(tmp_path), counters=counters))
        assert counters.skipped_symlink >= 1
