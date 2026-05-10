"""
Parallel hashing workers for the partial (I/O) and full (CPU) hashing stages.

BoundedExecutor wraps any *concurrent.futures* executor with a semaphore that
caps the number of in-flight futures, preventing unbounded queue growth and OOM
on large candidate sets.  LargeFileSemaphore further limits concurrent reads of
big files to avoid I/O saturation on constrained hardware.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import (
    Executor,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from types import TracebackType
from typing import Any

from dupkiller.cache import HashCache
from dupkiller.disk import LARGE_FILE_THRESHOLD, DiskMonitor
from dupkiller.hashing import hash_file_full, hash_file_partial
from dupkiller.utils import FileInfo, ShutdownFlag

logger = logging.getLogger(__name__)

_MAX_FD: int = 64
_DEFAULT_FUTURE_TIMEOUT: float = 7200.0   # 2 h max per file

ProgressCallback = Callable[[int], None] | None


# ---------------------------------------------------------------------------
# BoundedExecutor
# ---------------------------------------------------------------------------

class BoundedExecutor:
    """
    Wraps a *concurrent.futures* executor and limits the number of futures
    that can be pending simultaneously via a semaphore.

    Without this, submitting 1M tasks fills the internal queue and exhausts
    memory before any result is consumed.
    """

    def __init__(self, executor: Executor, max_inflight: int) -> None:
        self._exec = executor
        self._sem  = threading.Semaphore(max_inflight)

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        self._sem.acquire()
        fut: Future[Any] = self._exec.submit(fn, *args, **kwargs)
        fut.add_done_callback(lambda _: self._sem.release())
        return fut

    def __enter__(self) -> BoundedExecutor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: TracebackType | None,
    ) -> object:
        return self._exec.__exit__(exc_type, exc_val, tb)


# ---------------------------------------------------------------------------
# LargeFileSemaphore — controls concurrent reads of big files
# ---------------------------------------------------------------------------

class LargeFileSemaphore:
    """
    Caps the number of concurrently open large files (> *threshold* bytes).
    Small files bypass it unconditionally.
    """

    def __init__(
        self,
        max_concurrent: int = 2,
        threshold: int = LARGE_FILE_THRESHOLD,
    ) -> None:
        self._sem       = threading.Semaphore(max_concurrent)
        self._threshold = threshold

    def acquire(self, size: int) -> bool:
        """Acquire a slot iff size ≥ threshold.  Returns True if acquired."""
        if size >= self._threshold:
            self._sem.acquire()
            return True
        return False

    def release(self, acquired: bool) -> None:
        """Release iff *acquired* is True (i.e. acquire() returned True)."""
        if acquired:
            self._sem.release()


# ---------------------------------------------------------------------------
# Stage A — partial hashing (ThreadPoolExecutor)
# ---------------------------------------------------------------------------

def run_partial_hashing(
    candidates: list[FileInfo],
    cache: HashCache,
    num_threads: int,
    shutdown: ShutdownFlag,
    on_progress: ProgressCallback = None,
    disk_monitor: DiskMonitor | None = None,
    throttle_threshold: float = 0.90,
    large_sem: LargeFileSemaphore | None = None,
    hdd_mode: bool = False,
) -> list[tuple[str, str, int, float]]:
    """
    Hash the first bytes of each candidate using a thread pool.

    Sort order:
        ``hdd_mode=True``  → sort by inode for minimal disk seeking.
        ``hdd_mode=False`` → sort by path (lexicographic, optimal for SSDs).

    Args:
        candidates: Files to hash.
        cache: Hash cache for read-through lookups.
        num_threads: Thread pool size.
        shutdown: Checked before each task; stops submission when set.
        on_progress: Called with ``1`` after each file completes.
        disk_monitor: Optional I/O saturation monitor for adaptive throttling.
        throttle_threshold: Utilisation fraction above which reads are slowed.
        large_sem: Semaphore limiting concurrent reads of large files.
        hdd_mode: When True, sort candidates by inode before submission.

    Returns:
        List of ``(path, partial_hash, size, mtime)`` tuples for all files
        that were hashed successfully.
    """
    if hdd_mode:
        ordered = sorted(candidates, key=lambda fi: (fi.device, fi.inode))
    else:
        ordered = sorted(candidates, key=lambda fi: fi.path)

    results: list[tuple[str, str, int, float]] = []
    fd_sem  = threading.Semaphore(_MAX_FD)
    _lsem   = large_sem or LargeFileSemaphore()

    def _work(fi: FileInfo) -> tuple[str, str, int, float] | None:
        if shutdown.is_set():
            return None

        # Throttle when disk is saturated
        if disk_monitor is not None:
            util = disk_monitor.utilization()
            if util >= throttle_threshold:
                sleep = min(0.5, 0.05 + (util - throttle_threshold) * 1.0)
                time.sleep(sleep)

        # Cache hit
        cached = cache.get_partial_hash(fi)
        if cached is not None:
            return (fi.path, cached, fi.size, fi.mtime)

        fd_sem.acquire()
        acquired = _lsem.acquire(fi.size)
        try:
            _, digest = hash_file_partial(fi.path)
        except Exception as exc:
            logger.debug("partial hash exception %s: %s", fi.path, exc)
            digest = None
        finally:
            _lsem.release(acquired)
            fd_sem.release()

        if digest is None:
            return None
        cache.queue_update(fi.path, fi.size, fi.mtime, partial_hash=digest)
        return (fi.path, digest, fi.size, fi.mtime)

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        future_map = {pool.submit(_work, fi): fi for fi in ordered}
        for fut in as_completed(future_map):
            if shutdown.is_set():
                break
            fi = future_map[fut]
            try:
                res = fut.result()
                if res is not None:
                    results.append(res)
            except Exception as exc:  # pragma: no cover
                logger.debug("partial hash error %s: %s", fi.path, exc)
            finally:
                if on_progress:
                    on_progress(1)

    return results


# ---------------------------------------------------------------------------
# Stage B — full hashing (ProcessPoolExecutor)
# ---------------------------------------------------------------------------

def run_full_hashing(
    candidates: list[tuple[str, int, float]],
    cache: HashCache,
    num_processes: int,
    shutdown: ShutdownFlag,
    on_progress: ProgressCallback = None,
    disk_monitor: DiskMonitor | None = None,
    throttle_threshold: float = 0.90,
    large_sem: LargeFileSemaphore | None = None,
    max_bytes_per_sec: int = 0,
    future_timeout: float = _DEFAULT_FUTURE_TIMEOUT,
    hdd_mode: bool = False,
) -> list[tuple[str, str]]:
    """
    Hash entire files in a process pool, bypassing the GIL.

    BoundedExecutor limits in-flight futures to ``max_workers × 2`` to
    prevent queue unbounded growth.  In HDD mode candidates are sorted by
    inode before submission for sequential disk locality.

    Args:
        candidates: ``(path, size, mtime)`` triples to hash.
        cache: Hash cache for read-through lookups.
        num_processes: Process pool size.
        shutdown: Checked before each submission; stops on set.
        on_progress: Called with ``1`` after each file completes.
        disk_monitor: Optional I/O saturation monitor for adaptive throttling.
        throttle_threshold: Utilisation fraction above which reads are slowed.
        large_sem: Semaphore limiting concurrent reads of large files.
        max_bytes_per_sec: Bandwidth limit forwarded into each subprocess.
        future_timeout: Maximum seconds to wait for a single future (default 2 h).
        hdd_mode: When True, sort candidates by inode before submission.

    Returns:
        List of ``(path, full_hash)`` tuples for all files hashed successfully.
    """
    # Separate cache hits from misses
    results: list[tuple[str, str]] = []
    to_hash: list[tuple[str, int, float]] = []
    info_map: dict[str, tuple[int, float]] = {}

    for path, size, mtime in candidates:
        if shutdown.is_set():
            break
        fi = FileInfo(path=path, size=size, mtime=mtime, inode=0, device=0)
        info_map[path] = (size, mtime)
        cached = cache.get_full_hash(fi)
        if cached is not None:
            results.append((path, cached))
            if on_progress:
                on_progress(1)
        else:
            to_hash.append((path, size, mtime))

    if not to_hash or shutdown.is_set():
        return results

    if hdd_mode:
        # stat() to get inode — worth the overhead to minimise HDD seeking
        def _inode_key(t: tuple) -> tuple:
            try:
                st = os.stat(t[0])
                return (st.st_dev, st.st_ino)
            except OSError:  # pragma: no cover
                return (0, 0)  # pragma: no cover
        to_hash.sort(key=_inode_key)
    else:
        to_hash.sort(key=lambda t: t[0])

    _lsem = large_sem or LargeFileSemaphore()

    with ProcessPoolExecutor(max_workers=num_processes) as pool:
        bounded = BoundedExecutor(pool, max_inflight=num_processes * 2)
        pending: dict[Future, tuple[str, int, float, bool]] = {}

        for path, size, mtime in to_hash:
            if shutdown.is_set():  # pragma: no cover
                break  # pragma: no cover

            if disk_monitor is not None:
                util = disk_monitor.utilization()
                if util >= throttle_threshold:
                    sleep = min(0.5, 0.05 + (util - throttle_threshold) * 1.0)
                    time.sleep(sleep)

            acquired = _lsem.acquire(size)
            fut = bounded.submit(hash_file_full, path, max_bytes_per_sec)
            pending[fut] = (path, size, mtime, acquired)

        for fut in as_completed(pending):
            path, size, mtime, acquired = pending[fut]
            _lsem.release(acquired)
            if shutdown.is_set():
                if on_progress:
                    on_progress(1)
                continue
            try:
                _, digest = fut.result(timeout=future_timeout)
                if digest is not None:
                    cache.queue_update(path, size, mtime, full_hash=digest)
                    results.append((path, digest))
            except TimeoutError:  # pragma: no cover
                logger.warning("full hash timed out (%gs) for %s — skipping", future_timeout, path)
            except Exception as exc:  # pragma: no cover
                logger.debug("full hash error %s: %s", path, exc)
            finally:
                if on_progress:
                    on_progress(1)

    return results
