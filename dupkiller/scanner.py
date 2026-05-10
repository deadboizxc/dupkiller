"""
Generator-based recursive file scanner.

Walks a directory tree with ``os.scandir``, applying size filters and exclude
patterns.  Uses an explicit DFS stack (no recursion limit) and tracks visited
directory ``(device, inode)`` pairs to detect and skip symlink cycles.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Iterator

from dupkiller.utils import FileInfo, ScanCounters, ShutdownFlag

logger = logging.getLogger(__name__)


def _compile_excludes(patterns: list[str]) -> tuple[list[str], list[str]]:
    """
    Split *patterns* into:
    - *path_patterns*  — contain '/' or start with '/' → matched against full path
    - *name_patterns*  — everything else → matched against entry.name only
    """
    path_patterns: list[str] = []
    name_patterns: list[str] = []
    for p in patterns:
        if os.sep in p or (os.altsep and os.altsep in p) or p.startswith("/"):
            path_patterns.append(p)
        else:
            name_patterns.append(p)
    return name_patterns, path_patterns


def _is_excluded(
    name: str,
    full_path: str,
    name_patterns: list[str],
    path_patterns: list[str],
) -> bool:
    for pat in name_patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    for pat in path_patterns:
        if fnmatch.fnmatch(full_path, pat):
            return True
    return False


def scan_files(
    root: str,
    min_size: int = 1,
    max_size: int | None = None,
    exclude: list[str] | None = None,
    follow_symlinks: bool = False,
    shutdown: ShutdownFlag | None = None,
    counters: ScanCounters | None = None,
) -> Iterator[FileInfo]:
    """
    Recursively walk *root* with ``os.scandir`` and yield a :class:`FileInfo`
    for every regular file that passes the filters.

    Uses an explicit DFS stack (no recursion limit) and tracks visited
    directory inodes to detect and skip symlink cycles.
    """
    _counters = counters or ScanCounters()
    name_pats, path_pats = _compile_excludes(exclude or [])

    # Visited directory (dev, ino) pairs — guards against symlink cycles
    visited_dirs: set[tuple[int, int]] = set()

    stack: list[str] = [os.path.abspath(root)]

    while stack:
        if shutdown and shutdown.is_set():
            logger.info("scanner: shutdown received")
            return

        current = stack.pop()

        # Cycle detection on the directory itself
        try:
            dir_st = os.stat(current)
            dir_key = (dir_st.st_dev, dir_st.st_ino)
            if dir_key in visited_dirs:
                logger.warning("symlink cycle detected, skipping: %s", current)
                _counters.inc("skipped_cycle")
                continue
            visited_dirs.add(dir_key)
        except OSError as exc:
            logger.warning("cannot stat dir %s: %s", current, exc)
            _counters.inc("skipped_permission")
            continue

        try:
            with os.scandir(current) as it:
                for entry in it:
                    if shutdown and shutdown.is_set():  # pragma: no cover
                        return  # pragma: no cover

                    if _is_excluded(entry.name, entry.path, name_pats, path_pats):
                        _counters.inc("skipped_excluded")
                        continue

                    try:
                        is_dir  = entry.is_dir(follow_symlinks=follow_symlinks)
                        is_file = entry.is_file(follow_symlinks=follow_symlinks)
                    except OSError as exc:
                        logger.debug("is_dir/is_file failed %s: %s", entry.path, exc)
                        # Broken symlink
                        if entry.is_symlink():
                            _counters.inc("skipped_symlink")
                        else:
                            _counters.inc("skipped_permission")
                        continue

                    if is_dir:
                        stack.append(entry.path)
                        _counters.inc("dirs_scanned")
                        continue

                    if not is_file:
                        # Device, socket, broken symlink not following…
                        if entry.is_symlink():
                            _counters.inc("skipped_symlink")
                        continue

                    # --- stat the file ---
                    try:
                        st = entry.stat(follow_symlinks=follow_symlinks)
                    except OSError as exc:
                        logger.debug("stat failed %s: %s", entry.path, exc)
                        _counters.inc("skipped_permission")
                        continue

                    size = st.st_size

                    if size < min_size:
                        _counters.inc("skipped_too_small")
                        continue
                    if max_size is not None and size > max_size:
                        _counters.inc("skipped_too_large")
                        continue

                    _counters.inc("files_scanned")
                    yield FileInfo(
                        path=entry.path,
                        size=size,
                        mtime=st.st_mtime,
                        inode=st.st_ino,   # used for inode-ordered reads and hard-link detection
                        device=st.st_dev,  # combined with inode to identify hard-linked files
                    )

        except PermissionError as exc:
            logger.warning("permission denied: %s (%s)", current, exc)
            _counters.inc("skipped_permission")
        except OSError as exc:
            logger.warning("cannot scan %s: %s", current, exc)
            _counters.inc("skipped_permission")
