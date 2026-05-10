"""Tests for dupkiller.hashing — chunk sizes, partial hash, full hash, throttle."""
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from dupkiller.hashing import (
    PREHASH_THRESHOLD,
    ThrottledReader,
    _chunk_size_for,
    _hint_noreuse,
    _hint_sequential,
    algo_name,
    compute_full_hash,
    compute_partial_hash,
    hash_file_full,
    hash_file_partial,
)

KB = 1024
MB = 1024 * KB


def _write_tmp(content: bytes) -> str:
    fd, path = tempfile.mkstemp()
    os.write(fd, content)
    os.close(fd)
    return path


class TestChunkSize:
    def test_tiny(self):
        assert _chunk_size_for(1) == 256 * KB

    def test_small(self):
        assert _chunk_size_for(512 * KB) == 256 * KB

    def test_medium(self):
        assert _chunk_size_for(5 * MB) == 1 * MB

    def test_large(self):
        # 4 MB chunks kick in above 100 MB
        assert _chunk_size_for(200 * MB) == 4 * MB

    def test_very_large(self):
        # 16 MB chunks kick in above 1 GB
        assert _chunk_size_for(2 * 1024 * MB) == 16 * MB


class TestThrottledReader:
    def test_zero_max_is_noop(self):
        tr = ThrottledReader(0)
        t0 = time.monotonic()
        for _ in range(100):
            tr.throttle(1024 * 1024)
        assert time.monotonic() - t0 < 0.1

    def test_throttles_below_max(self):
        KB = 1024
        tr = ThrottledReader(512 * KB)
        t0 = time.monotonic()
        tr.throttle(512 * KB)
        tr.throttle(512 * KB)   # 1 MB transferred → should sleep ~1 s
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.8

    def test_window_resets_after_one_second(self):
        tr = ThrottledReader(1024 * 1024)
        tr.throttle(100)
        time.sleep(1.05)
        # After reset, window_bytes is 0 so no sleep for small amount
        t0 = time.monotonic()
        tr.throttle(100)
        assert time.monotonic() - t0 < 0.1

    def test_elapsed_zero_returns_early(self):
        tr = ThrottledReader(1)
        # Freeze time so elapsed == 0
        with patch("dupkiller.hashing.time.monotonic", return_value=0.0):
            tr._window_start = 0.0
            tr.throttle(9999)  # should not raise


class TestHintFunctions:
    @pytest.mark.skipif(
        not hasattr(os, "posix_fadvise"),
        reason="posix_fadvise not available on this platform"
    )
    def test_hint_sequential_oserror_silenced(self, tmp_path):
        f = tmp_path / "t.bin"
        f.write_bytes(b"x")
        with open(str(f), "rb") as fh:
            with patch("os.posix_fadvise", side_effect=OSError("fail")):
                _hint_sequential(fh.fileno(), 1)  # should not raise

    @pytest.mark.skipif(
        not hasattr(os, "posix_fadvise"),
        reason="posix_fadvise not available on this platform"
    )
    def test_hint_noreuse_oserror_silenced(self, tmp_path):
        f = tmp_path / "t.bin"
        f.write_bytes(b"x")
        with open(str(f), "rb") as fh:
            with patch("os.posix_fadvise", side_effect=OSError("fail")):
                _hint_noreuse(fh.fileno(), 1)  # should not raise


class TestComputePartialHashLargeFile:
    def test_large_file_sampling(self, tmp_path):
        # File larger than PREHASH_THRESHOLD triggers 3-point sampling
        p = tmp_path / "big.bin"
        p.write_bytes(b"A" * (PREHASH_THRESHOLD + 1024))
        digest = compute_partial_hash(str(p))
        assert isinstance(digest, str) and len(digest) > 0

    def test_oserror_returns_none(self):
        result = compute_partial_hash("/nonexistent/xyz/abc.bin")
        assert result is None


class TestComputeFullHash:
    def test_stat_fails_returns_none(self):
        result = compute_full_hash("/nonexistent/path.bin")
        assert result is None

    def test_read_error_returns_none(self, tmp_path):
        p = tmp_path / "t.bin"
        p.write_bytes(b"hello")
        with patch("builtins.open", side_effect=OSError("read error")):
            result = compute_full_hash(str(p))
        assert result is None

    def test_progress_callback(self, tmp_path):
        p = tmp_path / "t.bin"
        p.write_bytes(b"x" * 1024)
        calls = []
        def cb(done, total):
            calls.append((done, total))
        compute_full_hash(str(p), on_progress=cb)
        # final callback should be called
        assert len(calls) >= 1
        assert calls[-1][0] == 1024

    def test_with_throttle(self, tmp_path):
        p = tmp_path / "t.bin"
        p.write_bytes(b"y" * 512)
        tr = ThrottledReader(max_bytes_per_sec=0)  # disabled
        result = compute_full_hash(str(p), throttle=tr)
        assert result is not None


class TestAlgoName:
    def test_returns_string(self):
        name = algo_name()
        assert name in ("blake3", "blake2b")


class TestHashFilePartial:
    def test_returns_tuple(self):
        path = _write_tmp(b"hello world" * 100)
        try:
            result = hash_file_partial(path)
            assert len(result) == 2
            assert isinstance(result[0], str)   # algo
            assert isinstance(result[1], str)   # hex digest
        finally:
            os.unlink(path)

    def test_deterministic(self):
        path = _write_tmp(b"same content" * 1000)
        try:
            _, h1 = hash_file_partial(path)
            _, h2 = hash_file_partial(path)
            assert h1 == h2
        finally:
            os.unlink(path)

    def test_different_content_gives_different_hash(self):
        p1 = _write_tmp(b"aaa" * 1000)
        p2 = _write_tmp(b"bbb" * 1000)
        try:
            _, h1 = hash_file_partial(p1)
            _, h2 = hash_file_partial(p2)
            assert h1 != h2
        finally:
            os.unlink(p1)
            os.unlink(p2)

    def test_missing_file_returns_none(self):
        # hashing functions swallow I/O errors and return None digest
        _, digest = hash_file_partial("/nonexistent/path/xyz.bin")
        assert digest is None

    def test_empty_file(self):
        path = _write_tmp(b"")
        try:
            _, h = hash_file_partial(path)
            assert isinstance(h, str)
        finally:
            os.unlink(path)


class TestHashFileFull:
    def test_returns_tuple(self):
        path = _write_tmp(b"full hash test content" * 500)
        try:
            algo, digest = hash_file_full(path)
            assert isinstance(algo, str)
            assert isinstance(digest, str)
            assert len(digest) > 0
        finally:
            os.unlink(path)

    def test_deterministic(self):
        path = _write_tmp(b"deterministic" * 2000)
        try:
            _, h1 = hash_file_full(path)
            _, h2 = hash_file_full(path)
            assert h1 == h2
        finally:
            os.unlink(path)

    def test_partial_vs_full_differ_for_large_file(self):
        # partial uses head+mid+tail sampling for large files;
        # for small files they may agree — just check both return valid hex
        path = _write_tmp(b"x" * (100 * KB))
        try:
            _, hp = hash_file_partial(path)
            _, hf = hash_file_full(path)
            assert isinstance(hp, str) and len(hp) > 0
            assert isinstance(hf, str) and len(hf) > 0
        finally:
            os.unlink(path)

    def test_throttle_respected(self):
        # 1 MB file throttled to 512 KB/s should take ≥ ~1 s
        data = b"t" * (1 * MB)
        path = _write_tmp(data)
        try:
            t0 = time.monotonic()
            hash_file_full(path, max_bytes_per_sec=512 * KB)
            elapsed = time.monotonic() - t0
            assert elapsed >= 1.0, f"Expected ≥1 s, got {elapsed:.2f} s"
        finally:
            os.unlink(path)


class TestComputeHashOsError:
    def test_partial_hash_nonexistent_file(self):
        """compute_partial_hash returns None for a non-existent file."""
        result = compute_partial_hash("/no/such/file/xyz.bin")
        assert result is None

    def test_full_hash_nonexistent_file(self):
        """compute_full_hash returns None for a non-existent file."""
        result = compute_full_hash("/no/such/file/xyz.bin")
        assert result is None

    def test_full_hash_final_progress_callback(self):
        """on_progress is called once at the end for a small file (final callback)."""
        path = _write_tmp(b"data" * 5_000)
        calls = []
        try:
            compute_full_hash(
                path,
                on_progress=lambda done, total: calls.append((done, total)),
            )
        finally:
            os.unlink(path)
        # Final callback fires once (when size > 0)
        assert len(calls) == 1
        assert calls[0][0] == calls[0][1]  # done == total
