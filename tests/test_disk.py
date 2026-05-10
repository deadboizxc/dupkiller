"""Tests for dupkiller.disk — DiskMonitor, is_rotational, platform detection."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from dupkiller.disk import (
    LARGE_FILE_THRESHOLD,
    DiskMonitor,
    _IOSample,
    _is_rotational_linux,
    _is_rotational_macos,
    _is_rotational_windows,
    _windows_media_type,
    is_rotational,
    recommend_cpu_processes,
    recommend_io_threads,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_all_caches():
    """Clear every lru_cache in the disk module after each test."""
    from dupkiller.disk import (
        _diskutil_solid_state,
        _run_df,
        get_block_device,
        is_rotational,
    )
    _run_df.cache_clear()
    get_block_device.cache_clear()
    _diskutil_solid_state.cache_clear()
    is_rotational.cache_clear()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestLargeFileThreshold:
    def test_value(self):
        assert LARGE_FILE_THRESHOLD == 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# _run_df failures
# ---------------------------------------------------------------------------

class TestRunDfFailures:
    def setup_method(self):
        from dupkiller.disk import _run_df
        _run_df.cache_clear()

    def teardown_method(self):
        from dupkiller.disk import _run_df
        _run_df.cache_clear()

    def test_returncode_nonzero(self):
        from dupkiller.disk import _run_df
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("dupkiller.disk.subprocess.run", return_value=mock_result):
            result = _run_df("/nonexistent_rc1_path")
        assert result is None

    def test_exception_in_df(self):
        from dupkiller.disk import _run_df
        with patch("dupkiller.disk.subprocess.run", side_effect=Exception("no df")):
            result = _run_df("/nonexistent_exc_path")
        assert result is None


# ---------------------------------------------------------------------------
# get_block_device
# ---------------------------------------------------------------------------

class TestGetBlockDeviceInSysBlock:
    def test_device_found(self):
        from dupkiller.disk import _run_df, get_block_device
        _run_df.cache_clear()
        get_block_device.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Filesystem\n/dev/sda1 /mnt\n"
        with patch("dupkiller.disk.subprocess.run", return_value=mock_result), \
             patch("dupkiller.disk.Path") as MockPath:
            MockPath.return_value.exists.return_value = True
            result = get_block_device("/some/mount/path")
        _run_df.cache_clear()
        get_block_device.cache_clear()
        assert result == "sda"


class TestGetBlockDeviceNotInSysBlock:
    def test_device_not_found(self):
        from dupkiller.disk import _run_df, get_block_device
        _run_df.cache_clear()
        get_block_device.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Filesystem\n/dev/nonexistent_zz9z99 /mnt\n"
        with patch("dupkiller.disk.subprocess.run", return_value=mock_result):
            result = get_block_device("/nonexistent_path_blk")
        _run_df.cache_clear()
        get_block_device.cache_clear()
        assert result is None


# ---------------------------------------------------------------------------
# Linux rotational detection
# ---------------------------------------------------------------------------

class TestIsRotationalLinux:
    def test_returns_true_for_hdd(self):
        with patch("dupkiller.disk.get_block_device", return_value="sda"), \
             patch("dupkiller.disk.Path.read_text", return_value="1\n"):
            assert _is_rotational_linux("/mnt/hdd") is True

    def test_returns_false_for_ssd(self):
        with patch("dupkiller.disk.get_block_device", return_value="nvme0n1"), \
             patch("dupkiller.disk.Path.read_text", return_value="0\n"):
            assert _is_rotational_linux("/mnt/ssd") is False

    def test_returns_none_when_no_device(self):
        with patch("dupkiller.disk.get_block_device", return_value=None):
            assert _is_rotational_linux("/mnt/unknown") is None

    def test_oserror_returns_none(self):
        with patch("dupkiller.disk.get_block_device", return_value="sda"), \
             patch("dupkiller.disk.Path.read_text", side_effect=OSError("no read")):
            assert _is_rotational_linux("/mnt/test") is None


# ---------------------------------------------------------------------------
# macOS rotational detection
# ---------------------------------------------------------------------------

class TestIsRotationalMacOS:
    def setup_method(self):
        from dupkiller.disk import _diskutil_solid_state, _run_df
        _diskutil_solid_state.cache_clear()
        _run_df.cache_clear()

    def teardown_method(self):
        from dupkiller.disk import _diskutil_solid_state, _run_df
        _diskutil_solid_state.cache_clear()
        _run_df.cache_clear()

    def _mock_diskutil(self, solid_state_line: str) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = f"   Device Node:        /dev/disk0\n   Solid State:        {solid_state_line}\n"
        return m

    def test_ssd_returns_false(self):
        with patch("dupkiller.disk._run_df", return_value="/dev/disk0s1"), \
             patch("dupkiller.disk.subprocess.run", return_value=self._mock_diskutil("Yes")):
            assert _is_rotational_macos("/Volumes/HD") is False

    def test_hdd_returns_true(self):
        with patch("dupkiller.disk._run_df", return_value="/dev/disk2"), \
             patch("dupkiller.disk.subprocess.run", return_value=self._mock_diskutil("No")):
            assert _is_rotational_macos("/Volumes/Data") is True

    def test_run_df_none_returns_none(self):
        with patch("dupkiller.disk._run_df", return_value=None):
            assert _is_rotational_macos("/Volumes/Unknown") is None

    def test_diskutil_no_solid_state_line_returns_none(self):
        m = MagicMock()
        m.returncode = 0
        m.stdout = "   Device Node:        /dev/disk0\n"
        with patch("dupkiller.disk._run_df", return_value="/dev/disk0"), \
             patch("dupkiller.disk.subprocess.run", return_value=m):
            assert _is_rotational_macos("/Volumes/NoInfo") is None

    def test_diskutil_exception_returns_none(self):
        with patch("dupkiller.disk._run_df", return_value="/dev/disk0"), \
             patch("dupkiller.disk.subprocess.run", side_effect=OSError("no diskutil")):
            assert _is_rotational_macos("/Volumes/Broken") is None


# ---------------------------------------------------------------------------
# Windows rotational detection
# ---------------------------------------------------------------------------

class TestWindowsMediaType:
    def test_hdd(self):
        m = MagicMock()
        m.stdout = "HDD\n"
        with patch("dupkiller.disk.subprocess.run", return_value=m):
            assert _windows_media_type("C") is True

    def test_ssd(self):
        m = MagicMock()
        m.stdout = "SSD\n"
        with patch("dupkiller.disk.subprocess.run", return_value=m):
            assert _windows_media_type("D") is False

    def test_scm(self):
        m = MagicMock()
        m.stdout = "SCM\n"
        with patch("dupkiller.disk.subprocess.run", return_value=m):
            assert _windows_media_type("E") is False

    def test_unspecified_returns_none(self):
        m = MagicMock()
        m.stdout = "Unspecified\n"
        with patch("dupkiller.disk.subprocess.run", return_value=m):
            assert _windows_media_type("F") is None

    def test_exception_returns_none(self):
        with patch("dupkiller.disk.subprocess.run", side_effect=OSError("no ps")):
            assert _windows_media_type("C") is None


class TestIsRotationalWindows:
    def test_hdd_path(self):
        with patch("dupkiller.disk._windows_media_type", return_value=True):
            # Simulate a Windows-style absolute path with drive letter
            with patch("dupkiller.disk.Path") as MockPath:
                MockPath.return_value.resolve.return_value.drive = "C:"
                result = _is_rotational_windows("C:\\Users")
        assert result is True

    def test_ssd_path(self):
        with patch("dupkiller.disk._windows_media_type", return_value=False):
            with patch("dupkiller.disk.Path") as MockPath:
                MockPath.return_value.resolve.return_value.drive = "D:"
                result = _is_rotational_windows("D:\\Data")
        assert result is False

    def test_no_drive_letter_returns_none(self):
        with patch("dupkiller.disk.Path") as MockPath:
            MockPath.return_value.resolve.return_value.drive = ""
            result = _is_rotational_windows("relative\\path")
        assert result is None

    def test_media_type_none_returns_none(self):
        with patch("dupkiller.disk._windows_media_type", return_value=None):
            with patch("dupkiller.disk.Path") as MockPath:
                MockPath.return_value.resolve.return_value.drive = "C:"
                result = _is_rotational_windows("C:\\foo")
        assert result is None


# ---------------------------------------------------------------------------
# is_rotational — platform dispatch
# ---------------------------------------------------------------------------

class TestIsRotational:
    def setup_method(self):
        is_rotational.cache_clear()

    def teardown_method(self):
        is_rotational.cache_clear()

    def test_linux_dispatches_correctly(self):
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch("dupkiller.disk._is_rotational_linux", return_value=True):
            assert is_rotational("/mnt/hdd") is True

    def test_darwin_dispatches_correctly(self):
        with patch("dupkiller.disk.platform.system", return_value="Darwin"), \
             patch("dupkiller.disk._is_rotational_macos", return_value=False):
            is_rotational.cache_clear()
            assert is_rotational("/Volumes/SSD") is False

    def test_windows_dispatches_correctly(self):
        with patch("dupkiller.disk.platform.system", return_value="Windows"), \
             patch("dupkiller.disk._is_rotational_windows", return_value=True):
            is_rotational.cache_clear()
            assert is_rotational("C:\\Users") is True

    def test_unknown_platform_returns_none(self):
        with patch("dupkiller.disk.platform.system", return_value="FreeBSD"):
            is_rotational.cache_clear()
            assert is_rotational("/tmp") is None

    def test_linux_returns_none_when_df_fails(self):
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch("dupkiller.disk._run_df", return_value=None):
            is_rotational.cache_clear()
            assert is_rotational("/nonexistent") is None


# ---------------------------------------------------------------------------
# Concurrency recommendations
# ---------------------------------------------------------------------------

class TestRecommendIoThreads:
    def test_hdd_returns_low(self):
        with patch("dupkiller.disk.is_rotational", return_value=True):
            assert recommend_io_threads("/mnt/hdd") == 2

    def test_ssd_returns_high(self):
        with patch("dupkiller.disk.is_rotational", return_value=False):
            assert recommend_io_threads("/mnt/ssd") == 16

    def test_unknown_returns_default(self):
        with patch("dupkiller.disk.is_rotational", return_value=None):
            assert recommend_io_threads("/mnt/unknown") == 8

    def test_custom_hdd_value(self):
        with patch("dupkiller.disk.is_rotational", return_value=True):
            assert recommend_io_threads("/mnt/hdd", hdd=4) == 4


class TestRecommendCpuProcesses:
    def test_hdd_caps_at_2(self):
        with patch("dupkiller.disk.is_rotational", return_value=True):
            assert recommend_cpu_processes("/mnt/hdd") <= 2

    def test_ssd_uses_cpu_count(self):
        with patch("dupkiller.disk.is_rotational", return_value=False), \
             patch("os.cpu_count", return_value=8):
            assert recommend_cpu_processes("/mnt/ssd") == 8

    def test_respects_max_procs(self):
        with patch("dupkiller.disk.is_rotational", return_value=False), \
             patch("os.cpu_count", return_value=16):
            assert recommend_cpu_processes("/mnt/ssd", max_procs=4) == 4


# ---------------------------------------------------------------------------
# DiskMonitor — Linux path
# ---------------------------------------------------------------------------

class TestDiskMonitorLinux:
    def test_returns_zero_when_no_device(self):
        mon = DiskMonitor(device=None)
        with patch("dupkiller.disk.platform.system", return_value="Linux"):
            assert mon.utilization() == 0.0

    def test_returns_zero_without_proc_diskstats(self, tmp_path):
        mon = DiskMonitor(device="sda")
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch.object(DiskMonitor, "_DISKSTATS", tmp_path / "no_diskstats"):
            assert mon.utilization() == 0.0

    def test_returns_zero_on_first_call(self, tmp_path):
        diskstats = tmp_path / "diskstats"
        diskstats.write_text("  8  0 sda 100 0 200 50 0 0 0 0 0 100 100 0 0 0 0\n")
        mon = DiskMonitor(device="sda")
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch.object(DiskMonitor, "_DISKSTATS", diskstats):
            assert mon.utilization() == 0.0

    def test_calculates_utilisation(self, tmp_path):
        diskstats = tmp_path / "diskstats"

        def _write(io_ms: int) -> None:
            diskstats.write_text(
                f"  8  0 sda 100 0 200 50 0 0 0 0 0 {io_ms} 100 0 0 0 0\n"
            )

        _write(0)
        mon = DiskMonitor(device="sda")
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch.object(DiskMonitor, "_DISKSTATS", diskstats):
            mon.utilization()
            time.sleep(0.05)
            _write(50)
            util = mon.utilization()
        assert 0.0 <= util <= 1.0

    def test_is_saturated_false(self):
        mon = DiskMonitor(device=None)
        with patch("dupkiller.disk.platform.system", return_value="Linux"):
            assert mon.is_saturated() is False

    def test_is_saturated_custom_threshold(self):
        mon = DiskMonitor(device=None, saturation_threshold=0.0)
        with patch("dupkiller.disk.platform.system", return_value="Linux"):
            assert mon.is_saturated() is True

    def test_from_path(self):
        with patch("dupkiller.disk.get_block_device", return_value="sda"):
            mon = DiskMonitor(path="/mnt/data")
        assert mon.device == "sda"

    def test_sample_linux_oserror(self, tmp_path):
        diskstats = tmp_path / "diskstats"
        diskstats.write_text("  8  0 sda 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15\n")
        mon = DiskMonitor(device="sda")
        with patch("dupkiller.disk.Path.open", side_effect=OSError("no read")):
            result = mon._sample_linux()
        assert result is None

    def test_elapsed_less_than_1ms_returns_zero(self, tmp_path):
        diskstats = tmp_path / "diskstats"
        diskstats.write_text("  8  0 sda 1 2 3 4 5 6 7 8 9 50 11 12 13 14 15\n")
        mon = DiskMonitor(device="sda")
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch.object(DiskMonitor, "_DISKSTATS", diskstats):
            mon._prev = _IOSample(ts=time.monotonic(), io_ms=50)
            assert mon.utilization() == 0.0

    def test_sample_dispatches_linux(self, tmp_path):
        diskstats = tmp_path / "diskstats"
        diskstats.write_text("  8  0 sda 1 2 3 4 5 6 7 8 9 42 11 12 13 14 15\n")
        mon = DiskMonitor(device="sda")
        with patch("dupkiller.disk.platform.system", return_value="Linux"), \
             patch.object(DiskMonitor, "_DISKSTATS", diskstats):
            sample = mon._sample()
        assert sample is not None
        assert sample.io_ms == 42


# ---------------------------------------------------------------------------
# DiskMonitor — psutil path (macOS / Windows)
# ---------------------------------------------------------------------------

class TestDiskMonitorPsutil:
    def _mock_counters(self, read_time: int, write_time: int) -> MagicMock:
        m = MagicMock()
        m.read_time  = read_time
        m.write_time = write_time
        return m

    def test_sample_psutil_returns_sample(self):
        mock_counters = self._mock_counters(100, 200)
        with patch("dupkiller.disk._psutil") as mp:
            mp.disk_io_counters.return_value = mock_counters
            mon = DiskMonitor(device=None)
            sample = mon._sample_psutil()
        assert sample is not None
        assert sample.io_ms == 300

    def test_sample_psutil_none_counters(self):
        with patch("dupkiller.disk._psutil") as mp:
            mp.disk_io_counters.return_value = None
            mon = DiskMonitor(device=None)
            assert mon._sample_psutil() is None

    def test_sample_psutil_exception(self):
        with patch("dupkiller.disk._psutil") as mp:
            mp.disk_io_counters.side_effect = Exception("psutil error")
            mon = DiskMonitor(device=None)
            assert mon._sample_psutil() is None

    def test_sample_dispatches_to_psutil_on_macos(self):
        mock_counters = self._mock_counters(50, 50)
        with patch("dupkiller.disk.platform.system", return_value="Darwin"), \
             patch("dupkiller.disk._psutil") as mp:
            mp.disk_io_counters.return_value = mock_counters
            mon = DiskMonitor(device=None)
            sample = mon._sample()
        assert sample is not None
        assert sample.io_ms == 100

    def test_sample_dispatches_to_psutil_on_windows(self):
        mock_counters = self._mock_counters(10, 20)
        with patch("dupkiller.disk.platform.system", return_value="Windows"), \
             patch("dupkiller.disk._psutil") as mp:
            mp.disk_io_counters.return_value = mock_counters
            mon = DiskMonitor(device=None)
            sample = mon._sample()
        assert sample is not None
        assert sample.io_ms == 30

    def test_utilization_full_roundtrip_macos(self):
        seq = [
            self._mock_counters(0,   0),
            self._mock_counters(500, 500),
        ]
        call_count = 0

        def side_effect(**_kwargs):
            nonlocal call_count
            result = seq[call_count]
            call_count += 1
            return result

        with patch("dupkiller.disk.platform.system", return_value="Darwin"), \
             patch("dupkiller.disk._psutil") as mp, \
             patch("dupkiller.disk.time") as mt:
            mp.disk_io_counters.side_effect = side_effect
            mt.monotonic.side_effect = [0.0, 1.0]
            mon = DiskMonitor(device=None)
            mon.utilization()   # prime _prev
            util = mon.utilization()
        assert 0.0 <= util <= 1.0
