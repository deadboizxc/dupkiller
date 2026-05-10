"""
Hashing primitives: partial pre-filter, full content hash, and bandwidth throttle.

Partial hashing reads a small sample of each file (head + mid + tail, 512 KB each)
to cheaply filter candidates before committing to a full read.  Full hashing uses
adaptive chunk sizes (256 KB – 16 MB) and an optional progress callback for
large-file reporting.

Algorithm selection:
    BLAKE3 is preferred when the ``blake3`` package is installed (~3 GB/s with
    SIMD parallelism).  Falls back silently to ``hashlib.blake2b`` (~600 MB/s).
    Install BLAKE3 with: ``pip install blake3``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from typing import Protocol, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BLAKE3 optional import
# ---------------------------------------------------------------------------

try:
    import blake3 as _blake3_mod
    _HAS_BLAKE3 = True  # pragma: no cover
    logger.debug("BLAKE3 available — using it for hashing")  # pragma: no cover
except ImportError:
    _HAS_BLAKE3 = False
    logger.debug("BLAKE3 not installed — falling back to BLAKE2b")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Threshold above which prehash sampling is used instead of read-from-start.
PREHASH_THRESHOLD: int = 50 * 1024 * 1024   # 50 MB

# Bytes read at each of the three sampling points (head / mid / tail).
SAMPLE_SIZE: int = 512 * 1024               # 512 KB per point = 1.5 MB total I/O

# Fallback partial read for small files (< PREHASH_THRESHOLD).
PARTIAL_READ_BYTES: int = 64 * 1024         # 64 KB

# posix_fadvise availability
_HAS_FADVISE: bool = hasattr(os, "posix_fadvise")

# ---------------------------------------------------------------------------
# Adaptive chunk size
# ---------------------------------------------------------------------------

def _chunk_size_for(file_size: int) -> int:
    """
    Return optimal read-chunk size for *file_size*.

    Larger chunks amortise syscall overhead for big files while keeping
    memory usage bounded.
    """
    if file_size < 1 * 1024 * 1024:           # < 1 MB
        return 256 * 1024                      # 256 KB
    if file_size < 100 * 1024 * 1024:         # < 100 MB
        return 1 * 1024 * 1024                # 1 MB
    if file_size < 1 * 1024 * 1024 * 1024:   # < 1 GB
        return 4 * 1024 * 1024                # 4 MB
    return 16 * 1024 * 1024                   # 16 MB  (≥ 1 GB)


# ---------------------------------------------------------------------------
# Bandwidth throttle
# ---------------------------------------------------------------------------

class ThrottledReader:
    """
    Wraps a sliding-window rate limiter.  Call *throttle(n_bytes)* after
    each read to sleep as needed to stay under *max_bytes_per_sec*.

    Pass ``max_bytes_per_sec=0`` to disable (no-op).
    """

    def __init__(self, max_bytes_per_sec: int = 0) -> None:
        self._max = max_bytes_per_sec
        self._window_start = time.monotonic()
        self._window_bytes = 0

    def throttle(self, n: int) -> None:
        if self._max <= 0:
            return
        self._window_bytes += n
        elapsed = time.monotonic() - self._window_start
        if elapsed <= 0:
            return
        rate = self._window_bytes / elapsed
        if rate > self._max:
            deficit = self._window_bytes / self._max - elapsed
            if deficit > 0:
                time.sleep(deficit)
        # Reset window every second
        if elapsed >= 1.0:
            self._window_start = time.monotonic()
            self._window_bytes = 0


# ---------------------------------------------------------------------------
# Hasher Protocol
# ---------------------------------------------------------------------------

class _Hasher(Protocol):
    """Protocol for hash objects (BLAKE3 or hashlib)."""
    def update(self, data: bytes) -> None: ...
    def hexdigest(self) -> str: ...


# ---------------------------------------------------------------------------
# Internal hasher factory
# ---------------------------------------------------------------------------

def _new_hasher() -> _Hasher:
    """Return the fastest available hash object (BLAKE3 > BLAKE2b)."""
    if _HAS_BLAKE3:  # pragma: no cover
        return cast(_Hasher, _blake3_mod.blake3())  # pragma: no cover
    return hashlib.blake2b()


def _new_small_hasher() -> _Hasher:
    """Hasher for partial/sampling digests (shorter output is fine)."""
    if _HAS_BLAKE3:  # pragma: no cover
        return cast(_Hasher, _blake3_mod.blake3())  # pragma: no cover
    return hashlib.blake2b(digest_size=20)


# ---------------------------------------------------------------------------
# posix_fadvise hint
# ---------------------------------------------------------------------------

def _hint_sequential(fd: int, size: int = 0) -> None:
    """Advise the kernel to read-ahead *fd* sequentially (Linux only)."""
    if _HAS_FADVISE:  # pragma: no cover
        try:  # pragma: no cover
            posix_fadvise = getattr(os, "posix_fadvise", None)  # pragma: no cover
            POSIX_FADV_SEQUENTIAL = getattr(os, "POSIX_FADV_SEQUENTIAL", None)  # pragma: no cover
            if posix_fadvise and POSIX_FADV_SEQUENTIAL:  # pragma: no cover
                posix_fadvise(fd, 0, size, POSIX_FADV_SEQUENTIAL)  # pragma: no cover
        except OSError:  # pragma: no cover
            pass  # pragma: no cover


def _hint_noreuse(fd: int, size: int = 0) -> None:
    """Tell the kernel we won't re-read this data (free page cache sooner)."""
    if _HAS_FADVISE:  # pragma: no cover
        try:  # pragma: no cover
            posix_fadvise = getattr(os, "posix_fadvise", None)  # pragma: no cover
            POSIX_FADV_DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", None)  # pragma: no cover
            if posix_fadvise and POSIX_FADV_DONTNEED:  # pragma: no cover
                posix_fadvise(fd, 0, size, POSIX_FADV_DONTNEED)  # pragma: no cover
        except OSError:  # pragma: no cover
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# Public hashing API
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[int, int], None] | None  # (bytes_done, total_bytes)


def compute_partial_hash(path: str) -> str | None:
    """
    Fast pre-filter hash.

    * Files < PREHASH_THRESHOLD : read first PARTIAL_READ_BYTES (64 KB).
    * Files ≥ PREHASH_THRESHOLD : sample head + mid + tail (3 × 512 KB).

    Returns hex digest or None on I/O error.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None

    h = _new_small_hasher()

    try:
        with open(path, "rb") as fh:
            _hint_sequential(fh.fileno(), min(size, SAMPLE_SIZE * 3))

            if size < PREHASH_THRESHOLD:
                # Small file — just read the beginning
                data = fh.read(PARTIAL_READ_BYTES)
                h.update(data)
            else:
                # 3-point sampling: head + mid + tail, 512 KB each
                h.update(fh.read(SAMPLE_SIZE))                 # head
                mid = max(0, size // 2 - SAMPLE_SIZE // 2)
                fh.seek(mid)
                h.update(fh.read(SAMPLE_SIZE))                 # middle
                tail = max(0, size - SAMPLE_SIZE)
                fh.seek(tail)
                h.update(fh.read(SAMPLE_SIZE))                 # tail

        return h.hexdigest()
    except OSError as exc:
        logger.debug("partial_hash failed %s: %s", path, exc)
        return None


def compute_full_hash(
    path: str,
    on_progress: ProgressCallback = None,
    throttle: ThrottledReader | None = None,
) -> str | None:
    """
    Hash the entire file with adaptive chunk sizes and optional progress reporting.

    Args:
        path: File to hash.
        on_progress: Called as ``on_progress(bytes_done, total_bytes)`` roughly
            every 256 MB to let callers update a progress bar.
        throttle: If provided, bandwidth is limited per ThrottledReader settings.

    Returns:
        Hex digest string, or ``None`` on any I/O error.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        logger.debug("full_hash stat failed %s: %s", path, exc)
        return None

    chunk = _chunk_size_for(size)
    h = _new_hasher()
    PROGRESS_INTERVAL = 256 * 1024 * 1024   # report every 256 MB
    next_report = PROGRESS_INTERVAL
    done = 0

    try:
        with open(path, "rb") as fh:
            _hint_sequential(fh.fileno(), size)
            while True:
                data = fh.read(chunk)
                if not data:
                    break
                h.update(data)
                n = len(data)
                done += n
                if throttle:
                    throttle.throttle(n)
                if on_progress and done >= next_report:  # pragma: no cover
                    on_progress(done, size)
                    next_report = done + PROGRESS_INTERVAL
            _hint_noreuse(fh.fileno(), size)

        if on_progress and size > 0:
            on_progress(done, size)   # final callback

        return h.hexdigest()
    except OSError as exc:
        logger.warning("full_hash failed %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Module-level wrappers — picklable for ProcessPoolExecutor
# ---------------------------------------------------------------------------

def hash_file_partial(path: str) -> tuple[str, str | None]:
    """Return (path, partial_hash).  Safe for ThreadPoolExecutor."""
    return path, compute_partial_hash(path)


def hash_file_full(
    path: str,
    max_bytes_per_sec: int = 0,
) -> tuple[str, str | None]:
    """
    Return (path, full_hash).  Safe for ProcessPoolExecutor (picklable).

    *max_bytes_per_sec* enables bandwidth throttling inside the subprocess.
    """
    throttle = ThrottledReader(max_bytes_per_sec) if max_bytes_per_sec > 0 else None
    return path, compute_full_hash(path, throttle=throttle)


def algo_name() -> str:
    """Return the name of the active hash algorithm."""
    return "blake3" if _HAS_BLAKE3 else "blake2b"
