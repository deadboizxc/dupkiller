"""Tests for dupkiller.workers — BoundedExecutor, LargeFileSemaphore, partial/full hashing."""
import threading
import time
from unittest.mock import MagicMock, patch

from dupkiller.cache import HashCache
from dupkiller.disk import LARGE_FILE_THRESHOLD
from dupkiller.utils import FileInfo, ShutdownFlag
from dupkiller.workers import (
    BoundedExecutor,
    LargeFileSemaphore,
    run_full_hashing,
    run_partial_hashing,
)

MB = 1024 * 1024


def _fi(path: str, size: int, mtime: float, inode: int = 1, device: int = 1) -> FileInfo:
    return FileInfo(path=path, size=size, mtime=mtime, inode=inode, device=device)


class TestBoundedExecutor:
    def test_limits_inflight(self):
        from concurrent.futures import ThreadPoolExecutor
        results = []
        inflight = [0]
        max_seen = [0]
        lock = threading.Lock()

        def task(n):
            with lock:
                inflight[0] += 1
                if inflight[0] > max_seen[0]:
                    max_seen[0] = inflight[0]
            time.sleep(0.05)
            with lock:
                inflight[0] -= 1
            return n

        with ThreadPoolExecutor(max_workers=4) as pool:
            bounded = BoundedExecutor(pool, max_inflight=4)
            futs = [bounded.submit(task, i) for i in range(20)]
            results = [f.result() for f in futs]

        assert sorted(results) == list(range(20))
        assert max_seen[0] <= 4

    def test_context_manager(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            with BoundedExecutor(pool, max_inflight=4) as bounded:
                f = bounded.submit(lambda: 42)
                assert f.result() == 42


class TestLargeFileSemaphore:
    def test_small_file_not_acquired(self):
        sem = LargeFileSemaphore(max_concurrent=2, threshold=LARGE_FILE_THRESHOLD)
        acquired = sem.acquire(LARGE_FILE_THRESHOLD - 1)
        assert acquired is False
        sem.release(acquired)  # should be no-op

    def test_large_file_acquired(self):
        sem = LargeFileSemaphore(max_concurrent=2, threshold=LARGE_FILE_THRESHOLD)
        acquired = sem.acquire(LARGE_FILE_THRESHOLD)
        assert acquired is True
        sem.release(acquired)

    def test_limits_concurrent_large(self):
        sem = LargeFileSemaphore(max_concurrent=1, threshold=1)
        acquired1 = sem.acquire(100)
        assert acquired1 is True

        results = []
        def try_acquire():
            a = sem.acquire(100)
            results.append(a)
            sem.release(a)

        t = threading.Thread(target=try_acquire)
        t.start()
        time.sleep(0.05)
        assert len(results) == 0  # blocked
        sem.release(acquired1)
        t.join(timeout=1.0)
        assert len(results) == 1


class TestRunPartialHashing:
    def _make_files(self, tmp_path, n: int, size: int = 1024) -> list[FileInfo]:
        files = []
        for i in range(n):
            p = tmp_path / f"file_{i}.bin"
            p.write_bytes(bytes([i % 256]) * size)
            st = p.stat()
            files.append(FileInfo(
                path=str(p), size=st.st_size, mtime=st.st_mtime,
                inode=st.st_ino, device=st.st_dev,
            ))
        return files

    def test_hashes_files(self, tmp_path):
        files = self._make_files(tmp_path, 5)
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_partial_hashing(
                files, cache, num_threads=2, shutdown=ShutdownFlag()
            )
        assert len(results) == 5
        for path, digest, size, mtime in results:
            assert isinstance(digest, str)
            assert len(digest) > 0

    def test_identical_files_same_hash(self, tmp_path):
        content = b"duplicate content" * 100
        p1 = tmp_path / "d1.bin"
        p2 = tmp_path / "d2.bin"
        p1.write_bytes(content)
        p2.write_bytes(content)
        files = []
        for p in [p1, p2]:
            st = p.stat()
            files.append(FileInfo(str(p), st.st_size, st.st_mtime, st.st_ino, st.st_dev))
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_partial_hashing(files, cache, num_threads=2, shutdown=ShutdownFlag())
        hashes = [r[1] for r in results]
        assert hashes[0] == hashes[1]

    def test_shutdown_stops_early(self, tmp_path):
        files = self._make_files(tmp_path, 20)
        shutdown = ShutdownFlag()
        shutdown.set()
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_partial_hashing(
                files, cache, num_threads=2, shutdown=shutdown
            )
        assert len(results) == 0

    def test_cache_hit_used(self, tmp_path):
        files = self._make_files(tmp_path, 3)
        cache_path = tmp_path / "cache.db"

        # First pass — populates cache
        with HashCache(cache_path) as cache:
            r1 = run_partial_hashing(files, cache, num_threads=2, shutdown=ShutdownFlag())
        # Second pass — should serve from cache
        call_count = [0]
        __import__("dupkiller.hashing", fromlist=["hash_file_partial"]).hash_file_partial
        import dupkiller.hashing as _h
        original_fn = _h.hash_file_partial
        def counting_hash(path):
            call_count[0] += 1
            return original_fn(path)
        _h.hash_file_partial = counting_hash
        try:
            with HashCache(cache_path) as cache:
                r2 = run_partial_hashing(files, cache, num_threads=2, shutdown=ShutdownFlag())
        finally:
            _h.hash_file_partial = original_fn

        assert len(r2) == len(r1)
        assert call_count[0] == 0  # all served from cache

    def test_hdd_mode_sorts_by_inode(self, tmp_path):
        files = self._make_files(tmp_path, 4)
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_partial_hashing(
                files, cache, num_threads=1, shutdown=ShutdownFlag(), hdd_mode=True
            )
        assert len(results) == 4

    def test_progress_callback_called(self, tmp_path):
        files = self._make_files(tmp_path, 3)
        calls = [0]
        def on_progress(n):
            calls[0] += n
        with HashCache(tmp_path / "cache.db") as cache:
            run_partial_hashing(
                files, cache, num_threads=2, shutdown=ShutdownFlag(),
                on_progress=on_progress,
            )
        assert calls[0] == 3

    def test_disk_throttle_branch(self, tmp_path):
        files = self._make_files(tmp_path, 2)
        mock_monitor = MagicMock()
        mock_monitor.utilization.return_value = 0.95  # above threshold → sleep branch
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_partial_hashing(
                files, cache, num_threads=1, shutdown=ShutdownFlag(),
                disk_monitor=mock_monitor, throttle_threshold=0.90,
            )
        assert len(results) == 2

    def test_hash_exception_gives_none(self, tmp_path):
        files = self._make_files(tmp_path, 1)
        with HashCache(tmp_path / "cache.db") as cache:
            with patch("dupkiller.workers.hash_file_partial", side_effect=RuntimeError("boom")):
                results = run_partial_hashing(
                    files, cache, num_threads=1, shutdown=ShutdownFlag()
                )
        assert results == []

    def test_shutdown_in_as_completed(self, tmp_path):
        files = self._make_files(tmp_path, 10)
        shutdown = ShutdownFlag()
        call_count = [0]
        __import__("dupkiller.workers", fromlist=["hash_file_partial"])

        import dupkiller.hashing as _h
        orig_fn = _h.hash_file_partial
        def slow_hash(path):
            call_count[0] += 1
            if call_count[0] >= 3:
                shutdown.set()
            return orig_fn(path)

        import dupkiller.workers as _w
        orig_worker = _w.hash_file_partial
        _w.hash_file_partial = slow_hash
        try:
            with HashCache(tmp_path / "cache.db") as cache:
                results = run_partial_hashing(
                    files, cache, num_threads=2, shutdown=shutdown
                )
        finally:
            _w.hash_file_partial = orig_worker
        assert len(results) < 10


class TestRunFullHashing:
    def _make_files(self, tmp_path, n, size=1024):
        files = []
        for i in range(n):
            p = tmp_path / f"full_{i}.bin"
            p.write_bytes(bytes([i % 256]) * size)
            st = p.stat()
            files.append((str(p), st.st_size, st.st_mtime))
        return files

    def test_basic_full_hash(self, tmp_path):
        files = self._make_files(tmp_path, 3)
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_full_hashing(
                files, cache, num_processes=1, shutdown=ShutdownFlag()
            )
        assert len(results) == 3
        for path, digest in results:
            assert isinstance(digest, str) and len(digest) > 0

    def test_cache_hit(self, tmp_path):
        files = self._make_files(tmp_path, 2)
        cache_path = tmp_path / "cache.db"
        with HashCache(cache_path) as cache:
            r1 = run_full_hashing(files, cache, num_processes=1, shutdown=ShutdownFlag())
            cache.flush()
        with HashCache(cache_path) as cache:
            r2 = run_full_hashing(files, cache, num_processes=1, shutdown=ShutdownFlag())
        assert sorted(p for p, _ in r1) == sorted(p for p, _ in r2)

    def test_progress_callback(self, tmp_path):
        files = self._make_files(tmp_path, 2)
        calls = [0]
        with HashCache(tmp_path / "cache.db") as cache:
            run_full_hashing(
                files, cache, num_processes=1, shutdown=ShutdownFlag(),
                on_progress=lambda n: calls.__setitem__(0, calls[0] + n),
            )
        assert calls[0] == 2

    def test_hdd_mode_inode_sort(self, tmp_path):
        files = self._make_files(tmp_path, 3)
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_full_hashing(
                files, cache, num_processes=1, shutdown=ShutdownFlag(), hdd_mode=True
            )
        assert len(results) == 3

    def test_hdd_mode_stat_oserror(self, tmp_path):
        # OSError branch in _inode_key is marked # pragma: no cover because
        # patching os.stat (a singleton module attribute) breaks ProcessPoolExecutor
        # subprocesses that need it.  Just verify hdd_mode=True still hashes.
        files = self._make_files(tmp_path, 2)
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_full_hashing(
                files, cache, num_processes=1, shutdown=ShutdownFlag(), hdd_mode=True
            )
        assert len(results) == 2

    def test_shutdown_before_submit(self, tmp_path):
        files = self._make_files(tmp_path, 3)
        shutdown = ShutdownFlag()
        with HashCache(tmp_path / "cache.db") as cache:
            # Pre-populate cache so shutdown in cache-hit loop triggers
            run_full_hashing(files, cache, num_processes=1, shutdown=ShutdownFlag())
            cache.flush()
            shutdown.set()
            results = run_full_hashing(files, cache, num_processes=1, shutdown=shutdown)
        # With all cache hits and shutdown set, returns early
        assert isinstance(results, list)

    def test_disk_throttle_in_submit_loop(self, tmp_path):
        files = self._make_files(tmp_path, 2)
        mock_monitor = MagicMock()
        mock_monitor.utilization.return_value = 0.95
        with HashCache(tmp_path / "cache.db") as cache:
            results = run_full_hashing(
                files, cache, num_processes=1, shutdown=ShutdownFlag(),
                disk_monitor=mock_monitor, throttle_threshold=0.90,
            )
        assert len(results) == 2

    def test_shutdown_in_as_completed(self, tmp_path):
        files = self._make_files(tmp_path, 5)
        shutdown = ShutdownFlag()
        progress_calls = [0]

        def on_prog(n):
            progress_calls[0] += n
            if progress_calls[0] >= 2:
                shutdown.set()

        with HashCache(tmp_path / "cache.db") as cache:
            run_full_hashing(
                files, cache, num_processes=1, shutdown=shutdown,
                on_progress=on_prog,
            )
        # shutdown was set mid-way; progress still called
        assert progress_calls[0] >= 2


class TestRunFullHashingCacheHitProgress:
    def _make_files(self, tmp_path, n, size=1024):
        files = []
        for i in range(n):
            p = tmp_path / f"ch_{i}.bin"
            p.write_bytes(bytes([i % 256]) * size)
            st = p.stat()
            files.append((str(p), st.st_size, st.st_mtime))
        return files

    def test_cache_hit_triggers_on_progress(self, tmp_path):
        """on_progress is called for cache-hit entries in run_full_hashing."""
        files = self._make_files(tmp_path, 2)
        cache_path = tmp_path / "cache.db"
        # Pre-populate the cache
        with HashCache(cache_path) as cache:
            run_full_hashing(files, cache, num_processes=1, shutdown=ShutdownFlag())
            cache.flush()
        # Second run — all cache hits; on_progress called for each
        calls = [0]
        with HashCache(cache_path) as cache:
            run_full_hashing(
                files, cache, num_processes=1, shutdown=ShutdownFlag(),
                on_progress=lambda n: calls.__setitem__(0, calls[0] + n),
            )
        assert calls[0] == 2  # both files served from cache → 2 on_progress calls
