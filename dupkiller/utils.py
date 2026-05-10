"""
Shared utilities: data types, formatting, shutdown flag, scan counters.
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# File descriptor
# ---------------------------------------------------------------------------


@dataclass
class FileInfo:
    """
    Lightweight per-file record.  __slots__ keeps memory ~56 bytes/instance
    so 10 M files ≈ 560 MB — acceptable without swapping to disk.

    *inode* is 0 on platforms/filesystems that don't expose it (e.g. FAT32).
    *device* is used together with inode to identify hard-linked files.
    """

    __slots__ = ("path", "size", "mtime", "inode", "device")

    path:   str
    size:   int
    mtime:  float
    inode:  int   # st_ino  (0 = unknown)
    device: int   # st_dev  (0 = unknown)

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileInfo):
            return NotImplemented
        return self.path == other.path


# ---------------------------------------------------------------------------
# Skip / error counters (passed through the pipeline, shown in summary)
# ---------------------------------------------------------------------------


@dataclass
class ScanCounters:
    """Mutable counters accumulated during a scan."""

    files_scanned:       int = 0
    dirs_scanned:        int = 0
    skipped_permission:  int = 0   # PermissionError / EACCES
    skipped_symlink:     int = 0   # broken symlinks
    skipped_too_small:   int = 0   # below --min-size
    skipped_too_large:   int = 0   # above --max-size
    skipped_excluded:    int = 0   # matched --exclude pattern
    skipped_cycle:       int = 0   # symlink directory cycle
    hash_errors:         int = 0   # OSError during hashing
    hardlink_groups:     int = 0   # groups identified as hard-links
    hardlink_files:      int = 0   # individual hard-linked files

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def inc(self, field_name: str, n: int = 1) -> None:
        with self._lock:
            setattr(self, field_name, getattr(self, field_name) + n)

    def total_skipped(self) -> int:
        return (
            self.skipped_permission
            + self.skipped_symlink
            + self.skipped_too_small
            + self.skipped_too_large
            + self.skipped_excluded
            + self.skipped_cycle
        )


# ---------------------------------------------------------------------------
# Human-readable formatting
# ---------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(n: int | float) -> str:
    """Return compact, 2-decimal human-readable size string."""
    value = float(n)
    for unit in _UNITS[:-1]:
        if abs(value) < 1024.0:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} {_UNITS[-1]}"


def format_rate(bytes_per_sec: float) -> str:
    return f"{format_bytes(bytes_per_sec)}/s"


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class ShutdownFlag:
    """Thread-safe one-shot flag set by SIGINT/SIGTERM or explicit .set()."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._installed = False

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def install_signal_handlers(self) -> None:
        """Register SIGINT/SIGTERM handlers.  Safe to call multiple times."""
        if self._installed:
            return
        self._installed = True

        def _handler(sig: int, frame: object) -> None:  # noqa: ARG001  # pragma: no cover
            self.set()  # pragma: no cover

        signal.signal(signal.SIGINT,  _handler)
        signal.signal(signal.SIGTERM, _handler)
