"""
Platform-aware disk type detection and I/O saturation monitoring.

Platform support
----------------
Linux   — /sys/block/ for HDD detection; /proc/diskstats for utilisation
macOS   — diskutil info for HDD detection; psutil for utilisation
Windows — PowerShell Get-PhysicalDisk for HDD detection; psutil for utilisation

psutil is a required dependency (>= 5.9).  On Linux, /proc/diskstats is
preferred for accurate per-device utilisation; psutil is the fallback
for macOS and Windows.
"""

from __future__ import annotations

import functools
import logging
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil as _psutil

logger = logging.getLogger(__name__)

LARGE_FILE_THRESHOLD: int = 100 * 1024 * 1024  # 100 MB
_DEFAULT_IO_SATURATION: float = 0.85

HDD_DEFAULT_THREADS:     int = 2
SSD_DEFAULT_THREADS:     int = 16
UNKNOWN_DEFAULT_THREADS: int = 8


# ---------------------------------------------------------------------------
# Device resolution  (Linux / macOS — cached)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def _run_df(path: str) -> str | None:
    """Return block-device path from ``df``.  Result is cached per path."""
    try:
        result = subprocess.run(
            ["df", path],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().splitlines()
        return lines[1].split()[0] if len(lines) >= 2 else None
    except Exception as exc:
        logger.debug("df failed for %s: %s", path, exc)
        return None


@functools.lru_cache(maxsize=64)
def get_block_device(path: str) -> str | None:
    """
    Resolve the kernel device name (e.g. ``"sda"``) for the filesystem at
    *path* on Linux.  Returns ``None`` on non-Linux or detection failure.

    Result is cached so scanning millions of files costs only one subprocess.
    """
    dev_path = _run_df(path)
    if not dev_path:
        return None
    name = os.path.basename(dev_path)
    name = re.sub(r"p\d+$", "", name)             # nvme0n1p3 → nvme0n1
    name = re.sub(r"(?<=[a-z])\d+$", "", name)    # sda1      → sda
    if Path(f"/sys/block/{name}").exists():
        return name
    return None


# ---------------------------------------------------------------------------
# Rotational detection — Linux
# ---------------------------------------------------------------------------

def _is_rotational_linux(path: str) -> bool | None:
    dev = get_block_device(path)
    if dev is None:
        return None
    rot_file = Path(f"/sys/block/{dev}/queue/rotational")
    try:
        return rot_file.read_text().strip() == "1"
    except OSError as exc:
        logger.debug("rotational check failed %s: %s", dev, exc)
        return None


# ---------------------------------------------------------------------------
# Rotational detection — macOS
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _diskutil_solid_state(dev_path: str) -> bool | None:
    """macOS: run ``diskutil info`` and parse 'Solid State: Yes/No'.  Cached."""
    try:
        result = subprocess.run(
            ["diskutil", "info", dev_path],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if "Solid State" in line:
                return "Yes" in line   # True → SSD (not rotational)
    except Exception as exc:
        logger.debug("diskutil failed for %s: %s", dev_path, exc)
    return None


def _is_rotational_macos(path: str) -> bool | None:
    """macOS: map scan path → device node via df, then query diskutil."""
    dev_path = _run_df(path)
    if dev_path is None:
        return None
    solid = _diskutil_solid_state(dev_path)
    if solid is None:
        return None
    return not solid  # solid=True → SSD → not rotational → False


# ---------------------------------------------------------------------------
# Rotational detection — Windows
# ---------------------------------------------------------------------------

def _windows_media_type(drive_letter: str) -> bool | None:
    """
    Windows: query physical disk media type via PowerShell Get-PhysicalDisk.

    Returns ``True`` for HDD, ``False`` for SSD/SCM, ``None`` if the media
    type cannot be determined.
    """
    letter = drive_letter.upper()
    script = (
        f"$n=(Get-Partition -DriveLetter '{letter}' -ErrorAction Stop).DiskNumber;"
        "(Get-PhysicalDisk | Where-Object {$_.DeviceId -eq $n}).MediaType"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
        media = result.stdout.strip()
        if media == "HDD":
            return True
        if media in ("SSD", "SCM"):
            return False
        return None  # "Unspecified" or empty
    except Exception as exc:
        logger.debug("PowerShell media type query failed: %s", exc)
        return None


def _is_rotational_windows(path: str) -> bool | None:
    """Windows: determine rotational status via the drive letter."""
    drive = Path(path).resolve().drive   # "C:" on Windows, "" on Linux/macOS
    if not drive:
        return None
    return _windows_media_type(drive[0])


# ---------------------------------------------------------------------------
# Public rotational API  (cached)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def is_rotational(path: str) -> bool | None:
    """
    Return ``True`` for HDD, ``False`` for SSD/NVMe, ``None`` if unknown.
    Result is cached per path string.

    Supported platforms: Linux, macOS, Windows.
    """
    plat = platform.system()
    if plat == "Linux":
        return _is_rotational_linux(path)
    if plat == "Darwin":
        return _is_rotational_macos(path)
    if plat == "Windows":
        return _is_rotational_windows(path)
    return None


# ---------------------------------------------------------------------------
# Concurrency recommendations
# ---------------------------------------------------------------------------

def recommend_io_threads(
    path: str,
    hdd: int | None = None,
    ssd: int | None = None,
) -> int:
    rot = is_rotational(path)
    if rot is True:
        n = hdd if hdd is not None else HDD_DEFAULT_THREADS
        logger.info("HDD at %s → %d I/O threads", path, n)
        return n
    if rot is False:
        n = ssd if ssd is not None else SSD_DEFAULT_THREADS
        logger.info("SSD at %s → %d I/O threads", path, n)
        return n
    logger.info("disk type unknown at %s → %d I/O threads", path, UNKNOWN_DEFAULT_THREADS)
    return UNKNOWN_DEFAULT_THREADS


def recommend_cpu_processes(path: str, max_procs: int | None = None) -> int:
    ncpu  = os.cpu_count() or 4
    limit = max_procs or ncpu
    rot   = is_rotational(path)
    if rot is True:
        return min(limit, 2)
    return min(limit, ncpu)


# ---------------------------------------------------------------------------
# I/O utilisation monitor
# ---------------------------------------------------------------------------

@dataclass
class _IOSample:
    ts:    float
    io_ms: int


class DiskMonitor:
    """
    Tracks I/O utilisation across all platforms.

    Linux   — reads /proc/diskstats field 12 (time doing I/Os, ms); accurate
              per-device measurement.
    macOS   — psutil.disk_io_counters(): read_time + write_time sum as proxy.
    Windows — psutil.disk_io_counters(): read_time + write_time sum as proxy.

    On macOS/Windows the metric is an approximation: cumulative per-operation
    I/O time divided by wall-clock time.  It can exceed 1.0 under heavy
    parallel I/O; the result is clamped to [0.0, 1.0].
    """

    _DISKSTATS = Path("/proc/diskstats")

    def __init__(
        self,
        path: str | None = None,
        device: str | None = None,
        saturation_threshold: float = _DEFAULT_IO_SATURATION,
    ) -> None:
        if device:
            self.device: str | None = device
        elif path:
            self.device = get_block_device(path)
        else:
            self.device = None
        self.saturation_threshold = saturation_threshold
        self._prev: _IOSample | None = None

    def _sample_linux(self) -> _IOSample | None:
        """Read io_ms (field 12) from /proc/diskstats for self.device."""
        if not self._DISKSTATS.exists():
            return None
        try:
            with self._DISKSTATS.open() as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 13 and parts[2] == self.device:
                        return _IOSample(ts=time.monotonic(), io_ms=int(parts[12]))
        except OSError:  # pragma: no cover
            pass  # pragma: no cover
        return None  # pragma: no cover

    def _sample_psutil(self) -> _IOSample | None:
        """macOS/Windows: system-wide I/O proxy via psutil disk_io_counters."""
        try:
            counters = _psutil.disk_io_counters(perdisk=False)
            if counters is None:
                return None
            io_ms = counters.read_time + counters.write_time
            return _IOSample(ts=time.monotonic(), io_ms=io_ms)
        except Exception as exc:
            logger.debug("psutil disk_io_counters failed: %s", exc)
            return None

    def _sample(self) -> _IOSample | None:
        plat = platform.system()
        if plat == "Linux":
            if not self.device:
                return None
            return self._sample_linux()
        return self._sample_psutil()

    def utilization(self) -> float:
        """Return I/O utilisation [0.0–1.0] since the last call."""
        cur = self._sample()
        if cur is None:
            return 0.0
        if self._prev is None:
            self._prev = cur
            return 0.0
        elapsed_ms = (cur.ts - self._prev.ts) * 1000.0
        if elapsed_ms < 1.0:
            return 0.0
        delta = cur.io_ms - self._prev.io_ms
        util = max(0.0, min(delta / elapsed_ms, 1.0))
        self._prev = cur
        return util

    def is_saturated(self, threshold: float | None = None) -> bool:
        t = threshold if threshold is not None else self.saturation_threshold
        return self.utilization() >= t
