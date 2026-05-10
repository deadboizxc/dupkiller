"""
Streaming, resumable pipeline that orchestrates the four scan phases.

Phases:
    1. ``scanning``       — walk directory trees and record file metadata.
    2. ``partial_hashing`` — compute fast pre-filter hashes in a thread pool.
    3. ``full_hashing``   — compute full content hashes in a process pool.
    4. emit               — write confirmed duplicate groups to JSONL output.

Hard-linked files (same ``device + inode``) are detected before hashing and
excluded from duplicate counts, since they share the same on-disk data block.
All four phases are checkpointed in SQLite; an interrupted scan resumes from
the exact stage it stopped at without reprocessing already-handled files.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from dupkiller.cache import HashCache
from dupkiller.checkpoint import ScanSession
from dupkiller.disk import DiskMonitor
from dupkiller.grouping import group_by_hash
from dupkiller.hashing import algo_name
from dupkiller.scanner import scan_files
from dupkiller.utils import FileInfo, ScanCounters, ShutdownFlag
from dupkiller.workers import (
    LargeFileSemaphore,
    run_full_hashing,
    run_partial_hashing,
)

logger = logging.getLogger(__name__)
console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Output handler
# ---------------------------------------------------------------------------

class OutputHandler:
    """Thread-safe incremental writer for duplicates.jsonl."""

    def __init__(
        self,
        jsonl_path: Path | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._jsonl_path = jsonl_path
        self._lock = threading.Lock()
        self._emitted: set[str] = set()
        self._groups_written = 0
        self._jsonl_fh: TextIO | None = None
        self._log_fh: logging.FileHandler | None = None

        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(str(log_path), encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logging.getLogger("dupkiller").addHandler(fh)
            self._log_fh = fh
        else:
            self._log_fh = None

        if jsonl_path and jsonl_path.exists():
            self._load_emitted()
        if jsonl_path:
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self._jsonl_fh = open(str(jsonl_path), "a", encoding="utf-8", errors="replace")

    def _load_emitted(self) -> None:
        assert self._jsonl_path
        try:
            with open(str(self._jsonl_path), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        full_h    = record.get("hash")
                        partial_h = record.get("partial_hash")
                        if full_h:
                            self._emitted.add(full_h)
                        if partial_h:
                            self._emitted.add(partial_h)
                    except json.JSONDecodeError:
                        pass
            logger.info("Resume: %d already-output groups found in JSONL", len(self._emitted))
        except OSError as exc:
            logger.warning("Cannot read JSONL for resume: %s", exc)

    def already_emitted(self, group_hash: str) -> bool:
        return group_hash in self._emitted

    def emit_group(
        self,
        group_hash: str,
        file_size: int,
        files: list[tuple[str, float]],
        algo: str = "blake2b",
        partial_hash: str | None = None,
    ) -> None:
        if group_hash in self._emitted:
            return
        record: dict = {
            "hash":       group_hash,
            "hash_algo":  algo,
            "size":       file_size,
            "wasted":     file_size * (len(files) - 1),
            "duplicates": [{"path": str(Path(p)), "mtime": m} for p, m in files],
        }
        if partial_hash:
            record["partial_hash"] = partial_hash
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._emitted.add(group_hash)
            if partial_hash:
                self._emitted.add(partial_hash)
            self._groups_written += 1
            if self._jsonl_fh is not None:
                self._jsonl_fh.write(line + "\n")
                self._jsonl_fh.flush()
            else:
                logger.info("DUPLICATE: %s", line)

    @property
    def groups_written(self) -> int:
        return self._groups_written

    def close(self) -> None:
        with self._lock:
            if self._jsonl_fh:
                try:
                    self._jsonl_fh.close()
                except Exception:  # pragma: no cover
                    pass  # pragma: no cover
            if self._log_fh:
                try:
                    logging.getLogger("dupkiller").removeHandler(self._log_fh)
                    self._log_fh.close()
                except Exception:  # pragma: no cover
                    pass  # pragma: no cover

    def __enter__(self) -> OutputHandler:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Progress reporter (background thread)
# ---------------------------------------------------------------------------

class ProgressReporter:
    def __init__(self, interval: float = 30.0) -> None:
        self._interval  = interval
        self._lock      = threading.Lock()
        self._stats: dict = {}
        self._stop      = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, **kwargs: object) -> None:
        with self._lock:
            self._stats.update(kwargs)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="progress-reporter")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.wait(timeout=self._interval):
            with self._lock:
                stats = dict(self._stats)
            if stats:
                parts = " | ".join(f"{k}={v}" for k, v in stats.items())
                logger.info("[PROGRESS] %s", parts)
                console.print(f"[dim][progress] {parts}[/dim]")


# ---------------------------------------------------------------------------
# ETA helper
# ---------------------------------------------------------------------------

def _eta_str(done: int, total: int, elapsed: float) -> str:
    if done <= 0 or total <= 0 or elapsed <= 0:
        return "ETA:?"
    rate = done / elapsed
    rem  = (total - done) / rate
    if rem < 60:
        return f"ETA:{rem:.0f}s"
    if rem < 3600:
        return f"ETA:{rem/60:.0f}m"
    return f"ETA:{rem/3600:.1f}h"


# ---------------------------------------------------------------------------
# Pipeline config
# ---------------------------------------------------------------------------

class PipelineConfig:
    def __init__(
        self,
        num_threads: int = 8,
        num_processes: int = 4,
        min_size: int = 1,
        max_size: int | None = None,
        exclude: list[str] | None = None,
        follow_symlinks: bool = False,
        checkpoint_interval: float = 300.0,
        max_concurrent_large: int = 2,
        progress_interval: float = 30.0,
        disk_monitor: DiskMonitor | None = None,
        max_bytes_per_sec: int = 0,
        future_timeout: float = 7200.0,
        hdd_mode: bool = False,
    ) -> None:
        self.num_threads          = num_threads
        self.num_processes        = num_processes
        self.min_size             = min_size
        self.max_size             = max_size
        self.exclude              = exclude or []
        self.follow_symlinks      = follow_symlinks
        self.checkpoint_interval  = checkpoint_interval
        self.max_concurrent_large = max_concurrent_large
        self.progress_interval    = progress_interval
        self.disk_monitor         = disk_monitor
        self.max_bytes_per_sec    = max_bytes_per_sec
        self.future_timeout       = future_timeout
        self.hdd_mode             = hdd_mode

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("disk")}


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

def _phase_scan(
    roots: list[str],
    session: ScanSession,
    cfg: PipelineConfig,
    shutdown: ShutdownFlag,
    prog: Progress,
    counters: ScanCounters,
) -> int:
    task = prog.add_task("[green]Scanning…", total=None, rate="")
    t0   = time.monotonic()
    n    = 0
    rep  = ProgressReporter(interval=cfg.progress_interval)
    rep.start()

    try:
        for root in roots:
            for fi in scan_files(
                root,
                min_size=cfg.min_size,
                max_size=cfg.max_size,
                exclude=cfg.exclude,
                follow_symlinks=cfg.follow_symlinks,
                shutdown=shutdown,
                counters=counters,
            ):
                session.queue_file(fi)
                n += 1
                elapsed = time.monotonic() - t0
                rate    = f"{n / elapsed:.0f} files/s" if elapsed else ""
                prog.update(task, advance=1, rate=rate)
                rep.update(stage="scanning", files=f"{n:,}", rate=rate,
                           skipped=f"{counters.total_skipped():,}")
                session.checkpoint_if_due(cfg.checkpoint_interval)

        session.flush_files()
        session.checkpoint()
    finally:
        rep.stop()

    prog.update(task, total=n, completed=n,
                description=f"[green]Scanned {n:,} files", rate="")
    return n


def _phase_partial_hash(
    session: ScanSession,
    cache: HashCache,
    cfg: PipelineConfig,
    shutdown: ShutdownFlag,
    prog: Progress,
    large_sem: LargeFileSemaphore,
    counters: ScanCounters,
) -> None:
    # Use candidate count (files with size peers), not total scanned, for accurate ETA
    total = session.count_candidates()
    task  = prog.add_task("[yellow]Partial hash…", total=max(total, 1), completed=0, rate="")
    t0    = time.monotonic()
    done  = 0
    rep   = ProgressReporter(interval=cfg.progress_interval)
    rep.start()

    try:
        for size, files_in_group in session.iter_size_groups(target_stage="scanned"):
            if shutdown.is_set():  # pragma: no cover
                break  # pragma: no cover
            fis = [
                FileInfo(path=p, size=size, mtime=m, inode=ino, device=dev)
                for p, m, ino, dev in files_in_group
            ]
            results = run_partial_hashing(
                fis, cache, cfg.num_threads, shutdown,
                disk_monitor=cfg.disk_monitor,
                large_sem=large_sem,
                hdd_mode=cfg.hdd_mode,
            )
            hashed = [p for p, h, _, _ in results if h]
            session.mark_files_stage(hashed, "partial_done")

            failed = {fi.path for fi in fis} - set(hashed)
            if failed:
                session.mark_files_stage(list(failed), "unique")
                counters.inc("hash_errors", len(failed))

            done += len(fis)
            elapsed = time.monotonic() - t0
            rate = f"{done / elapsed:.0f} files/s" if elapsed else ""
            prog.update(task, advance=len(fis), rate=rate)
            rep.update(stage="partial_hashing", done=f"{done:,}",
                       candidates=f"{total:,}", rate=rate,
                       eta=_eta_str(done, total, elapsed))
            session.checkpoint_if_due(cfg.checkpoint_interval)

        session.mark_files_stage_where_scanned_unique()
        cache.flush()
        session.checkpoint()
    finally:
        rep.stop()


def _phase_full_hash(
    session: ScanSession,
    cache: HashCache,
    output: OutputHandler,
    cfg: PipelineConfig,
    shutdown: ShutdownFlag,
    prog: Progress,
    large_sem: LargeFileSemaphore,
) -> int:
    counts = session.stage_counts()
    total  = counts.get("partial_done", 0)
    task   = prog.add_task("[red]Full hash…", total=max(total, 1), completed=0, rate="")
    t0     = time.monotonic()
    done   = 0
    groups_found = 0
    bytes_done   = 0
    rep = ProgressReporter(interval=cfg.progress_interval)
    rep.start()

    _algo = algo_name()

    try:
        for partial_hash, group_files in session.iter_partial_hash_groups():
            if shutdown.is_set():  # pragma: no cover
                break  # pragma: no cover
            if output.already_emitted(partial_hash):
                n = len(group_files)
                done += n
                prog.update(task, advance=n, rate="")
                continue

            candidates = [(p, s, m) for p, s, m in group_files]
            results = run_full_hashing(
                candidates, cache, cfg.num_processes, shutdown,
                disk_monitor=cfg.disk_monitor,
                large_sem=large_sem,
                max_bytes_per_sec=cfg.max_bytes_per_sec,
                future_timeout=cfg.future_timeout,
                hdd_mode=cfg.hdd_mode,
            )

            full_paths = [p for p, h in results if h]
            session.mark_files_stage(full_paths, "full_done")

            full_groups = group_by_hash(results)
            for fh, dup_paths in full_groups.items():
                if len(dup_paths) < 2:  # pragma: no cover
                    continue  # pragma: no cover
                file_size = next((s for p, s, _ in group_files if p == dup_paths[0]), 0)
                files_meta = [
                    (p, next((mm for pp, _, mm in group_files if pp == p), 0.0))
                    for p in dup_paths
                ]
                output.emit_group(fh, file_size, files_meta, algo=_algo,
                                  partial_hash=partial_hash)
                groups_found += 1

            n = len(group_files)
            done += n
            bytes_done += sum(s for _, s, _ in group_files)
            elapsed = time.monotonic() - t0
            rate = f"{done / elapsed:.0f} files/s" if elapsed else ""
            mb_rate = f"{bytes_done / elapsed / 1_048_576:.1f} MB/s" if elapsed else ""
            prog.update(task, advance=n, rate=rate)
            rep.update(stage="full_hashing", done=f"{done:,}",
                       groups=f"{groups_found:,}", rate=rate,
                       throughput=mb_rate, eta=_eta_str(done, total, elapsed))
            session.checkpoint_if_due(cfg.checkpoint_interval)

        cache.flush()
        session.checkpoint()
    finally:
        rep.stop()

    return groups_found


# ---------------------------------------------------------------------------
# Hard-link detection
# ---------------------------------------------------------------------------

def _detect_and_report_hardlinks(
    session: ScanSession,
    output: OutputHandler,
    counters: ScanCounters,
) -> None:
    """
    Identify files that are hard-linked (same device+inode) and report them
    as already-deduplicated.  Marks them as 'unique' so they skip hashing.
    """
    for device, inode, paths in session.iter_inode_groups():
        if len(paths) < 2:  # pragma: no cover
            continue  # pragma: no cover
        # Hard-linked files are the same inode — not duplicates in the
        # "wasted space" sense.  Mark them to skip hashing.
        session.mark_files_stage(paths, "unique")
        counters.inc("hardlink_groups")
        counters.inc("hardlink_files", len(paths))
        logger.debug("hardlink group dev=%d ino=%d: %s", device, inode, paths[:3])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(
    roots: list[str],
    cache: HashCache,
    output: OutputHandler,
    session: ScanSession,
    cfg: PipelineConfig,
    shutdown: ShutdownFlag | None = None,
    counters: ScanCounters | None = None,
) -> tuple[int, int]:
    """
    Execute the full (or resumed) pipeline.

    Args:
        roots: One or more root directories to scan.
        cache: Hash cache shared across all pipeline phases.
        output: Handles JSONL writing and resume state.
        session: Persistent scan session for checkpointing and resume.
        cfg: Tuning parameters (threads, processes, throttle, etc.).
        shutdown: When set, the pipeline stops at the next safe point.
        counters: Accumulated skip/error statistics for the summary.

    Returns:
        Tuple of ``(total_files, duplicate_groups_found)``.
    """
    if shutdown is None:
        shutdown = ShutdownFlag()
    if counters is None:
        counters = ScanCounters()

    large_sem = LargeFileSemaphore(max_concurrent=cfg.max_concurrent_large)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description:<32}"),
        BarColumn(bar_width=28),
        MofNCompleteColumn(),
        TextColumn("[cyan]{task.fields[rate]:>16}"),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as prog:

        stage = session.get_stage()
        logger.info("Pipeline start — stage: %s  algo: %s", stage, algo_name())

        # Phase 1: scan
        if stage == "scanning":
            total_files = _phase_scan(roots, session, cfg, shutdown, prog, counters)
            if shutdown.is_set():  # pragma: no cover
                session.mark_interrupted()
                return total_files, 0

            # Detect hard links before hashing to avoid counting them as duplicates
            _detect_and_report_hardlinks(session, output, counters)

            session.set_stage("partial_hashing")
            stage = "partial_hashing"
            sc = session.stage_counts()
            console.print(
                f"  [dim]Scan done: {session.total_scanned():,} files  "
                f"hardlink groups: {counters.hardlink_groups:,}[/dim]"
            )
        else:
            total_files = session.total_scanned()
            console.print(f"  [dim]Resuming — {total_files:,} files already scanned[/dim]")

        # Phase 2: partial hash
        if stage == "partial_hashing":
            _phase_partial_hash(session, cache, cfg, shutdown, prog, large_sem, counters)
            if shutdown.is_set():  # pragma: no cover
                session.mark_interrupted()
                return total_files, 0
            session.set_stage("full_hashing")
            stage = "full_hashing"
            sc = session.stage_counts()
            console.print(
                f"  [dim]Partial hash: {sc.get('partial_done', 0):,} candidates  "
                f"unique: {sc.get('unique', 0):,}[/dim]"
            )
        elif stage == "full_hashing":
            console.print("  [dim]Resuming from full_hashing[/dim]")

        # Phase 3: full hash
        groups = 0
        if stage == "full_hashing":
            groups = _phase_full_hash(session, cache, output, cfg, shutdown, prog, large_sem)
            if shutdown.is_set():  # pragma: no cover
                session.mark_interrupted()
                return total_files, groups
            session.mark_complete()

    return total_files, groups
