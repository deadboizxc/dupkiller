"""
Command-line interface for dupkiller.

Cross-platform notes:
    All path arguments are resolved via ``Path.expanduser().resolve()`` before use.
    Output files use UTF-8 encoding with ``errors='replace'``.
    File deletion uses ``Path.unlink()`` (works on all OS).
    HDD auto-detection is Linux-only; on macOS/Windows use ``--hdd`` to force it.
    Environment variable ``DUPKILLER_DB`` overrides the default DB location.

Commands:
    scan    — walk one or more directory trees and identify duplicates
    list    — display results of the last scan
    delete  — remove duplicates from the last scan (read-only by default)
    stats   — summary statistics with optional delta vs an older scan
    export  — write results to JSON / CSV / TXT / JSONL / HTML
    cache   — cache maintenance sub-commands
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import TypedDict

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dupkiller import __version__
from dupkiller.cache import DB_PATH, HashCache
from dupkiller.checkpoint import ScanSession
from dupkiller.dedupe import delete_duplicates
from dupkiller.disk import (
    DiskMonitor,
    is_rotational,
    recommend_cpu_processes,
    recommend_io_threads,
)
from dupkiller.pipeline import OutputHandler, PipelineConfig, run_pipeline
from dupkiller.utils import ScanCounters, ShutdownFlag, format_bytes

console = Console()
err_console = Console(stderr=True)
logger = logging.getLogger(__name__)

_DEFAULT_OUTPUT_DIR = Path.home() / ".dupkiller" / "output"


# ---------------------------------------------------------------------------
# TypedDict structures for scan results
# ---------------------------------------------------------------------------


class DupFileInfo(TypedDict):
    """Duplicate file metadata."""
    path: str
    mtime: float


class DupGroup(TypedDict):
    """Duplicate group with hash, size, and files."""
    hash: str
    size: int
    files: list[DupFileInfo]


class ScanInfo(TypedDict):
    """Scan metadata and statistics."""
    root_path: str
    scan_time: float
    duplicate_groups: int
    reclaimable_bytes: int


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_path(p: str) -> Path:
    """Expand ~ and env vars, resolve to absolute path (cross-platform)."""
    return Path(os.path.expandvars(p)).expanduser().resolve()


# ---------------------------------------------------------------------------
# ionice helper
# ---------------------------------------------------------------------------


def _apply_ionice() -> None:  # pragma: no cover
    """
    Lower I/O priority of the current process to 'idle' class on Linux.
    No-ops silently on macOS/Windows or when permission is denied.

    Strategy: try the `ionice(1)` userspace tool first (most portable).
    Fall back to a direct ioprio_set(2) syscall with architecture-specific
    syscall numbers for common platforms.
    """
    import platform as _platform
    import subprocess

    # --- attempt 1: ionice(1) utility ---
    try:
        subprocess.run(
            ["ionice", "-c", "3", "-p", str(os.getpid())],
            check=True, capture_output=True, timeout=5,
        )
        logger.info("I/O priority set to idle class via ionice(1)")
        return
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        pass  # not installed or failed — try syscall

    # --- attempt 2: direct ioprio_set syscall ---
    # syscall numbers differ per CPU architecture
    _NR_IOPRIO_SET = {
        "x86_64":  251,
        "i686":    289,
        "i386":    289,
        "aarch64":  30,
        "arm":     314,
        "armv7l":  314,
        "ppc64le": 273,
        "s390x":   282,
        "riscv64": 30,
    }.get(_platform.machine())

    if _NR_IOPRIO_SET is None:
        logger.debug("ionice: unknown architecture %s — skipping", _platform.machine())
        return

    try:
        import ctypes
        _IOPRIO_WHO_PROCESS = 1
        _IOPRIO_PRIO_VALUE  = (3 << 13) | 7   # class=IDLE, data=7
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        ret  = libc.syscall(_NR_IOPRIO_SET, _IOPRIO_WHO_PROCESS, 0, _IOPRIO_PRIO_VALUE)
        if ret == 0:
            logger.info("I/O priority set to idle class via ioprio_set(%d)", _NR_IOPRIO_SET)
        else:
            logger.debug("ioprio_set failed (errno %d)", ctypes.get_errno())
    except Exception as exc:
        logger.debug("ionice syscall fallback failed: %s", exc)



# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.option(
    "--db",
    default=str(DB_PATH),
    show_default=True,
    envvar="DUPKILLER_DB",
    help="SQLite cache / results database path.",
)
@click.pass_context
def main(ctx: click.Context, verbose: bool, db: str) -> None:
    """dupkiller — high-performance duplicate file finder."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["db"] = _resolve_path(db)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@main.command()
@click.argument("paths", nargs=-1, required=True, metavar="PATH [PATH …]")
# ── concurrency ──────────────────────────────────────────────────────────
@click.option(
    "--threads", "-t",
    default=None, type=int,
    help="I/O threads for partial hashing  [auto-detected: 2 HDD / 16 SSD].",
)
@click.option(
    "--processes", "-p",
    default=None, type=int,
    help="CPU processes for full hashing  [default: CPU count].",
)
@click.option(
    "--hdd",
    "disk_mode", flag_value="hdd",
    help="Force HDD-safe mode (low concurrency, sequential reads).",
)
@click.option(
    "--ssd",
    "disk_mode", flag_value="ssd",
    default=True,
    help="Force SSD mode (high concurrency).",
)
@click.option(
    "--max-concurrent-large-files", "max_large",
    default=2, show_default=True,
    help="Max simultaneous reads of files > 100 MB.",
)
# ── throttle / I/O priority ───────────────────────────────────────────────
@click.option(
    "--max-throughput", "max_throughput",
    default=None, type=str, metavar="RATE",
    help="Max read throughput, e.g. 50MB or 200KB.",
)
@click.option(
    "--ionice", is_flag=True, default=False,
    help="Run at idle I/O priority (Linux only).",
)
# ── filters ──────────────────────────────────────────────────────────────
@click.option("--min-size", default=1, show_default=True, help="Minimum file size (bytes).")
@click.option("--max-size", default=None, type=int, help="Maximum file size (bytes).")
@click.option("--exclude", "-e", multiple=True, metavar="PATTERN",
              help="Glob pattern to exclude (repeatable).  Matched against name and full path.")
@click.option("--follow-symlinks", is_flag=True, default=False,
              help="Follow symbolic links.")
# ── reliability / long-run ───────────────────────────────────────────────
@click.option(
    "--resume", is_flag=True, default=False,
    help="Resume the last interrupted scan for PATH.",
)
@click.option(
    "--checkpoint-interval", "chk_interval",
    default=300, show_default=True,
    help="Write a DB checkpoint every N seconds.",
)
@click.option(
    "--progress-interval", "prog_interval",
    default=30, show_default=True,
    help="Print a progress line to stderr every N seconds.",
)
# ── output ───────────────────────────────────────────────────────────────
@click.option(
    "--output-dir", "output_dir",
    default=None, type=click.Path(),
    help=f"Directory for scan.log and duplicates.jsonl  [default: {_DEFAULT_OUTPUT_DIR}].",
)
@click.option(
    "--jsonl", "jsonl_path",
    default=None, type=click.Path(),
    help="Explicit path for the JSONL output file.",
)
@click.option(
    "--log-file", "log_file",
    default=None, type=click.Path(),
    help="Explicit path for scan.log.",
)
@click.pass_context
def scan(
    ctx: click.Context,
    paths: tuple,
    threads: int | None,
    processes: int | None,
    disk_mode: str,
    max_large: int,
    max_throughput: str | None,
    ionice: bool,
    min_size: int,
    max_size: int | None,
    exclude: tuple,
    follow_symlinks: bool,
    resume: bool,
    chk_interval: int,
    prog_interval: int,
    output_dir: str | None,
    jsonl_path: str | None,
    log_file: str | None,
) -> None:
    """Scan one or more PATHs for duplicate files."""
    db_path: Path = ctx.obj["db"]

    # Apply idle I/O priority before any disk access
    if ionice:  # pragma: no cover
        _apply_ionice()

    # Resolve all root paths to absolute form
    roots = [str(_resolve_path(p)) for p in paths]
    primary_root = roots[0]  # used for disk detection and session key

    # Parse --max-throughput into bytes/sec
    max_bytes_per_sec = _parse_rate(max_throughput) if max_throughput else 0

    # ── determine concurrency ──────────────────────────────────────────
    if disk_mode == "hdd":  # pragma: no cover
        _threads = threads if threads is not None else 2
        _processes = processes if processes is not None else 2
    else:
        detected_hdd = (is_rotational(primary_root) is True) if disk_mode != "ssd" else False
        if detected_hdd:  # pragma: no cover
            _threads = threads if threads is not None else 2
            _processes = processes if processes is not None else 2
            disk_mode = "hdd"
        else:
            _threads = threads if threads is not None else recommend_io_threads(primary_root)
            _processes = processes if processes is not None else recommend_cpu_processes(primary_root)

    hdd_mode = (disk_mode == "hdd")

    # ── output paths ──────────────────────────────────────────────────
    out_dir = _resolve_path(output_dir) if output_dir else _DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    _jsonl = _resolve_path(jsonl_path) if jsonl_path else out_dir / "duplicates.jsonl"
    _log   = _resolve_path(log_file)   if log_file   else out_dir / "scan.log"

    # ── print header ──────────────────────────────────────────────────
    roots_display = "\n".join(f"  [green]{r}[/green]" for r in roots)
    throughput_display = (
        f"   max-throughput [yellow]{max_throughput}[/yellow]"
        if max_bytes_per_sec else ""
    )
    console.print(
        Panel.fit(
            f"[bold cyan]dupkiller[/bold cyan] v{__version__}  —  scanning\n"
            f"{roots_display}\n"
            f"mode [yellow]{disk_mode}[/yellow]   "
            f"threads [yellow]{_threads}[/yellow]   "
            f"processes [yellow]{_processes}[/yellow]   "
            f"min-size [yellow]{format_bytes(min_size)}[/yellow]"
            f"{throughput_display}\n"
            f"JSONL → [dim]{_jsonl}[/dim]\n"
            f"log  → [dim]{_log}[/dim]",
            title="Configuration",
            border_style="dim",
        )
    )

    shutdown = ShutdownFlag()
    shutdown.install_signal_handlers()
    t_start = time.monotonic()

    disk_mon = DiskMonitor(path=primary_root)
    cfg = PipelineConfig(
        num_threads=_threads,
        num_processes=_processes,
        min_size=min_size,
        max_size=max_size,
        exclude=list(exclude),
        follow_symlinks=follow_symlinks,
        checkpoint_interval=float(chk_interval),
        max_concurrent_large=max_large,
        progress_interval=float(prog_interval),
        disk_monitor=disk_mon,
        max_bytes_per_sec=max_bytes_per_sec,
        hdd_mode=hdd_mode,
    )

    # ── session (new or resume) ────────────────────────────────────────
    counters = ScanCounters()

    with HashCache(db_path) as cache:
        session: ScanSession | None = None

        if resume:  # pragma: no cover
            session = ScanSession.find_resumable(db_path, primary_root)
            if session:
                info = session.get_info()
                console.print(
                    f"[yellow]Resuming session {session.session_id} "
                    f"(stage: {info['stage']}, "
                    f"started: {time.strftime('%Y-%m-%d %H:%M', time.localtime(info['started_at']))})"
                    "[/yellow]"
                )
            else:
                console.print("[dim]No resumable session found — starting fresh.[/dim]")

        if session is None:
            session = ScanSession.create(
                db_path,
                primary_root,
                config=cfg.to_dict(),
                output_jsonl=str(_jsonl),
                output_log=str(_log),
            )

        with OutputHandler(jsonl_path=_jsonl, log_path=_log) as output:
            with session:
                try:
                    total_files, dup_groups = run_pipeline(
                        roots=roots,
                        cache=cache,
                        output=output,
                        session=session,
                        cfg=cfg,
                        shutdown=shutdown,
                        counters=counters,
                    )
                except KeyboardInterrupt:  # pragma: no cover
                    session.mark_interrupted()
                    console.print("\n[yellow]Interrupted — progress saved.[/yellow]")
                    sys.exit(130)
                except Exception as exc:  # pragma: no cover
                    session.mark_interrupted()
                    logger.exception("Pipeline error: %s", exc)
                    console.print(f"\n[red]Error: {exc}[/red]")
                    sys.exit(1)

        # ── also save to scan_results for list/delete/stats/export ──
        # Use a generator so multi-GB JSONL files don't load fully into RAM
        if _jsonl.exists():
            cache.save_scan_results(
                root_path=primary_root,
                total_files=total_files,
                duplicate_groups=_iter_groups_from_jsonl(_jsonl),
            )
        else:  # pragma: no cover
            cache.save_scan_results(
                root_path=primary_root,
                total_files=total_files,
                duplicate_groups=iter([]),
            )

    elapsed = time.monotonic() - t_start

    if shutdown.is_set():  # pragma: no cover
        console.print(f"\n[yellow]Scan interrupted after {elapsed:.1f}s.[/yellow]")
        sys.exit(130)

    # ── summary (read back from DB — totals computed by save_scan_results) ──
    with HashCache(db_path) as cache:
        last = cache.get_latest_scan()
    reclaimable = last.get("reclaimable_bytes", 0) if last else 0
    tbl = Table(title="Scan Results", box=box.ROUNDED)
    tbl.add_column("Metric", style="cyan", no_wrap=True)
    tbl.add_column("Value", style="bold white", justify="right")
    tbl.add_row("Total files scanned", f"{total_files:,}")
    tbl.add_row("Duplicate groups found", f"{dup_groups:,}")
    tbl.add_row("Reclaimable space", f"[red]{format_bytes(reclaimable)}[/red]")
    tbl.add_row("Elapsed", f"{elapsed:.1f} s")
    tbl.add_row("Throughput", f"{total_files/elapsed:.0f} files/s" if elapsed > 0 else "—")
    tbl.add_row("Results written to", str(_jsonl))
    console.print(tbl)

    _print_skip_stats(counters)

    if dup_groups:
        console.print(
            "\n[dim]  dupkiller list    — browse duplicates[/dim]\n"
            "[dim]  dupkiller delete  — remove duplicates[/dim]\n"
            "[dim]  dupkiller export  — save full results[/dim]"
        )


def _print_skip_stats(counters: ScanCounters) -> None:
    """Print per-category skip counters from the scan phase."""
    rows = [
        ("Permission denied", counters.skipped_permission),
        ("Symlinks", counters.skipped_symlink),
        ("Cycle (bind mount)", counters.skipped_cycle),
        ("Excluded by pattern", counters.skipped_excluded),
        ("Below min-size", counters.skipped_too_small),
        ("Above max-size", counters.skipped_too_large),
        ("Hash errors", counters.hash_errors),
        ("Hard-link groups", counters.hardlink_groups),
        ("Hard-linked files", counters.hardlink_files),
    ]
    non_zero = [(label, val) for label, val in rows if val > 0]
    if not non_zero:
        return
    tbl = Table(title="Skip Counters", box=box.SIMPLE)
    tbl.add_column("Category", style="dim")
    tbl.add_column("Count", justify="right")
    for label, val in non_zero:
        tbl.add_row(label, f"{val:,}")
    console.print(tbl)


def _parse_rate(s: str) -> int:
    """Parse a human-readable rate like '50MB', '200KB', '1GB' → bytes/sec."""
    s = s.strip().upper()
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    for suffix, mult in sorted(units.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            try:
                return int(float(s[: -len(suffix)]) * mult)
            except ValueError:
                pass
    try:
        return int(s)
    except ValueError:
        raise click.BadParameter(f"Cannot parse rate: {s!r}")


def _iter_groups_from_jsonl(jsonl_path: Path):
    """Generator: yield (hash, size, [(path, mtime)]) from a JSONL file."""
    try:
        with open(str(jsonl_path), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    h  = obj.get("hash", "")
                    sz = obj.get("size", 0)
                    files = [(d["path"], d.get("mtime", 0.0)) for d in obj.get("duplicates", [])]
                    if h and files:
                        yield h, sz, files
                except (json.JSONDecodeError, KeyError):
                    pass
    except OSError:
        pass


def _load_groups_from_jsonl(
    jsonl_path: Path,
) -> list[tuple[str, int, list[tuple[str, float]]]]:
    """Read JSONL and return list of (hash, size, [(path, mtime)]).
    Use _iter_groups_from_jsonl for large files to avoid OOM."""
    return list(_iter_groups_from_jsonl(jsonl_path))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@main.command("list")
@click.option("--limit", "-n", default=50, show_default=True, help="Max groups to display.")
@click.option("--min-waste", default=0, type=int, help="Min wasted bytes to show a group.")
@click.option("--jsonl", "jsonl_path", default=None, type=click.Path(),
              help="Read directly from a JSONL file instead of the DB.")
@click.pass_context
def list_duplicates(
    ctx: click.Context, limit: int, min_waste: int, jsonl_path: str | None
) -> None:
    """List duplicate files from the last scan."""
    groups: list[DupGroup]
    scan_info: ScanInfo

    if jsonl_path:
        groups_raw = _load_groups_from_jsonl(_resolve_path(jsonl_path))
        groups = [
            {
                "hash": str(h),
                "size": int(sz),
                "files": [{"path": str(p), "mtime": float(m)} for p, m in files]
            }
            for h, sz, files in groups_raw
        ]
        scan_info = {
            "root_path": "(JSONL file)",
            "scan_time": 0.0,
            "duplicate_groups": len(groups),
            "reclaimable_bytes": sum(
                g["size"] * (len(g["files"]) - 1) for g in groups
            ),
        }
    else:
        db_path: Path = ctx.obj["db"]
        with HashCache(db_path) as cache:
            last_scan = cache.get_latest_scan()
            if not last_scan:
                err_console.print(
                    "[red]No scan results.  Run [bold]dupkiller scan <path>[/bold] first.[/red]"
                )
                sys.exit(1)
            groups_raw_db = cache.get_duplicate_groups(last_scan["id"])
            groups = [
                {
                    "hash": str(g["hash"]),
                    "size": int(g["size"]),
                    "files": [
                        {"path": str(f["path"]), "mtime": float(f["mtime"])}
                        for f in g["files"]
                    ]
                }
                for g in groups_raw_db
            ]
            scan_info = {
                "root_path": str(last_scan["root_path"]),
                "scan_time": float(last_scan["scan_time"]),
                "duplicate_groups": int(last_scan["duplicate_groups"]),
                "reclaimable_bytes": int(last_scan["reclaimable_bytes"]),
            }

    if scan_info["scan_time"]:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(scan_info["scan_time"]))
        console.print(f"\n[dim]Last scan: {ts}  root: {scan_info['root_path']}[/dim]")
    console.print(
        f"[dim]{scan_info['duplicate_groups']:,} groups  "
        f"reclaimable: {format_bytes(scan_info['reclaimable_bytes'])}[/dim]\n"
    )

    shown = 0
    for g in groups:
        waste = g["size"] * (len(g["files"]) - 1)
        if waste < min_waste:
            continue
        if shown >= limit:
            console.print("[dim]… more groups hidden (use --limit)[/dim]")
            break
        shown += 1
        console.print(
            f"[bold cyan]Group {shown}[/bold cyan]  "
            f"[white]{format_bytes(g['size'])}[/white] × "
            f"[yellow]{len(g['files'])}[/yellow] copies  "
            f"wastes [red bold]{format_bytes(waste)}[/red bold]"
        )
        for f in g["files"]:
            mts = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["mtime"]))
            console.print(f"  [dim]{mts}[/dim]  {Path(f['path'])}")
        console.print()

    if shown == 0:
        console.print("[green]No duplicates match the current filters.[/green]")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@main.command()
@click.option("--keep",
              type=click.Choice(["newest", "oldest", "first", "shortest", "longest"]),
              default="newest", show_default=True,
              help="Which file to keep in each duplicate group.")
@click.option("--dry-run", is_flag=True, default=False, help="Show plan, do not delete.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.option("--interactive", "-i", is_flag=True, default=False,
              help="Prompt once per group before deleting.")
@click.option("--min-waste", default=0, type=int, help="Only delete groups wasting ≥ N bytes.")
@click.option("--jsonl", "jsonl_path", default=None, type=click.Path(),
              help="Read from JSONL instead of DB.")
@click.pass_context
def delete(
    ctx: click.Context,
    keep: str,
    dry_run: bool,
    yes: bool,
    interactive: bool,
    min_waste: int,
    jsonl_path: str | None,
) -> None:
    """Delete duplicate files from the last scan."""
    db_path: Path = ctx.obj["db"]

    # Resolve scan_id outside any generator to keep sys.exit() away from
    # generator bodies (avoids delayed connection cleanup on early exit).
    _db_scan_id: int | None = None
    if not jsonl_path:
        with HashCache(db_path) as _probe:
            _last = _probe.get_latest_scan()
        if not _last:
            err_console.print("[red]No scan results.  Run scan first.[/red]")
            sys.exit(1)
        _db_scan_id = _last["id"]

    def _group_source():
        """Yield groups one at a time — memory-safe for 1 TB+ scans."""
        if jsonl_path:
            for h, sz, files in _iter_groups_from_jsonl(_resolve_path(jsonl_path)):
                g = {"hash": h, "size": sz,
                     "files": [{"path": p, "mtime": m} for p, m in files]}
                if min_waste == 0 or sz * (len(files) - 1) >= min_waste:
                    yield g
        else:
            with HashCache(db_path) as cache:
                for g in cache.iter_duplicate_groups(_db_scan_id):
                    if min_waste == 0 or g["size"] * (len(g["files"]) - 1) >= min_waste:
                        yield g

    # For dry-run or bulk-confirm we need count first — peek by counting
    if dry_run or (not yes and not interactive):
        # Materialise only to get count; for truly huge data this is the
        # only approach short of a COUNT(*) query. With 1 TB+ use --yes or
        # --interactive to avoid this buffering.
        groups = list(_group_source())
        if not groups:
            console.print("[green]No duplicates to delete.[/green]")
            return
        result = delete_duplicates(
            groups=groups,
            keep=keep,           # type: ignore[arg-type]
            dry_run=dry_run,
            confirm=not yes,
            interactive=interactive,
        )
    else:
        # --yes or --interactive: stream one group at a time — O(1) memory
        from pathlib import Path as _Path

        from dupkiller.dedupe import select_keep_path
        deleted = errors = skipped = freed = 0
        had_any = False

        for group in _group_source():
            had_any = True
            keep_path = select_keep_path(group["files"], keep)  # type: ignore[arg-type]

            if interactive:
                waste = group["size"] * (len(group["files"]) - 1)
                console.print(
                    f"\n[bold cyan]Group[/bold cyan]  "
                    f"[white]{format_bytes(group['size'])}[/white] × "
                    f"{len(group['files'])} copies  waste [red]{format_bytes(waste)}[/red]"
                )
                console.print(f"  [green]keep →[/green] {keep_path}")
                for f in group["files"]:
                    if f["path"] != keep_path:
                        console.print(f"  [red]del  →[/red] {f['path']}")
                from rich.prompt import Confirm
                if not Confirm.ask("  Delete?", default=False):
                    skipped += sum(1 for f in group["files"] if f["path"] != keep_path)
                    continue

            for f in group["files"]:
                if f["path"] == keep_path:
                    continue
                try:
                    _Path(f["path"]).unlink()
                    deleted += 1
                    freed += group["size"]
                except FileNotFoundError:
                    skipped += 1
                except OSError as exc:
                    errors += 1
                    logger.error("delete failed %s: %s", f["path"], exc)

        if not had_any:
            console.print("[green]No duplicates to delete.[/green]")
            return

        console.print(
            f"\n[green]Done.[/green]  Deleted [bold]{deleted:,}[/bold] files, "
            f"freed [bold]{format_bytes(freed)}[/bold]."
        )
        if skipped:
            console.print(f"[dim]Skipped: {skipped:,}[/dim]")
        if errors:
            console.print(f"[red]Errors: {errors:,} (see logs)[/red]")
        result = {"deleted": deleted, "errors": errors, "freed": freed, "skipped": skipped}

    sys.exit(0 if result.get("errors", 0) == 0 else 1)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


@main.command()
@click.option("--compare-with", "compare_id", default=None, type=int,
              help="Scan ID to compare against (show delta).  Use 'dupkiller stats --list' to see IDs.")
@click.option("--list", "list_scans", is_flag=True, default=False,
              help="List available scan IDs instead of showing stats.")
@click.pass_context
def stats(ctx: click.Context, compare_id: int | None, list_scans: bool) -> None:
    """Show statistics from the last scan, optionally comparing with an older scan."""
    db_path: Path = ctx.obj["db"]
    with HashCache(db_path) as cache:
        if list_scans:
            scans = cache.list_scans(limit=20)
            tbl = Table(title="Available Scans", box=box.SIMPLE)
            tbl.add_column("ID", style="cyan", justify="right")
            tbl.add_column("Time")
            tbl.add_column("Root", style="dim")
            tbl.add_column("Files", justify="right")
            tbl.add_column("Dup groups", justify="right")
            tbl.add_column("Reclaimable", justify="right")
            for s in scans:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["scan_time"]))
                tbl.add_row(
                    str(s["id"]), ts, s["root_path"],
                    f"{s['total_files']:,}", f"{s['duplicate_groups']:,}",
                    format_bytes(s["reclaimable_bytes"]),
                )
            console.print(tbl)
            return

        last_scan = cache.get_latest_scan()
        if not last_scan:
            err_console.print("[red]No scan results.  Run scan first.[/red]")
            sys.exit(1)

        groups = cache.get_duplicate_groups(last_scan["id"])

        baseline = None
        if compare_id is not None:
            baseline = cache.get_scan_by_id(compare_id)
            if baseline is None:
                err_console.print(f"[red]Scan ID {compare_id} not found.[/red]")
                sys.exit(1)
            if baseline["id"] == last_scan["id"]:
                err_console.print("[red]--compare-with must reference a different scan, not the current one.[/red]")
                sys.exit(1)
            if baseline["scan_time"] >= last_scan["scan_time"]:  # pragma: no cover
                err_console.print(
                    f"[yellow]Warning: baseline scan {compare_id} is not older than the current scan — "
                    f"delta values may be misleading.[/yellow]"
                )

    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_scan["scan_time"]))
    tbl = Table(title="Duplicate Statistics", box=box.ROUNDED)
    tbl.add_column("Metric", style="cyan", no_wrap=True)
    tbl.add_column("Value", style="bold white", justify="right")
    if baseline:
        tbl.add_column("Baseline", style="dim", justify="right")
        tbl.add_column("Delta", justify="right")

    def _row(label: str, key: str, fmt=str) -> None:
        current_val = last_scan.get(key, 0)
        if baseline:
            base_val = baseline.get(key, 0)
            delta = current_val - base_val
            delta_str = _delta_fmt(delta, key)
            tbl.add_row(label, fmt(current_val), fmt(base_val), delta_str)
        else:
            tbl.add_row(label, fmt(current_val))

    tbl.add_row("Scan root", last_scan["root_path"],
                *([] if not baseline else [baseline["root_path"], ""]))
    tbl.add_row("Scan time", ts,
                *([] if not baseline else [
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(baseline["scan_time"])),
                    "",
                ]))
    _row("Total files scanned", "total_files", lambda v: f"{v:,}")
    _row("Duplicate groups",    "duplicate_groups", lambda v: f"{v:,}")
    _row("Duplicate files",     "duplicate_files",  lambda v: f"{v:,}")
    _row("Reclaimable space",   "reclaimable_bytes", format_bytes)
    console.print(tbl)

    if baseline:
        console.print(
            f"[dim]Comparing scan {last_scan['id']} against baseline scan {compare_id}[/dim]"
        )

    if groups:
        sizes = [g["size"] for g in groups]
        console.print(
            f"\nLargest dup group: [bold]{format_bytes(max(sizes))}[/bold]   "
            f"Smallest: [bold]{format_bytes(min(sizes))}[/bold]"
        )
        top5 = sorted(
            groups, key=lambda g: g["size"] * (len(g["files"]) - 1), reverse=True
        )[:5]
        console.print("\n[bold]Top 5 by wasted space:[/bold]")
        for i, g in enumerate(top5, 1):
            waste = g["size"] * (len(g["files"]) - 1)
            console.print(
                f"  {i}. {format_bytes(g['size'])} × {len(g['files'])} = "
                f"[red]{format_bytes(waste)}[/red] wasted"
            )
            for f in g["files"][:3]:
                console.print(f"     [dim]{Path(f['path'])}[/dim]")
            if len(g["files"]) > 3:
                console.print(f"     [dim]… +{len(g['files'])-3} more[/dim]")


def _delta_fmt(delta: int, key: str) -> str:
    """Format a delta value with colour and sign."""
    if delta == 0:
        return "[dim]—[/dim]"
    sign = "+" if delta > 0 else ""
    val  = format_bytes(abs(delta)) if "bytes" in key else f"{abs(delta):,}"
    colour = "red" if delta > 0 else "green"
    return f"[{colour}]{sign}{'-' if delta < 0 else ''}{val}[/{colour}]"


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--format", "fmt",
    type=click.Choice(["json", "csv", "txt", "jsonl", "html"]),
    default="json", show_default=True,
)
@click.option("--output", "-o", type=click.Path(dir_okay=False), default=None)
@click.option("--jsonl-input", "jsonl_input", default=None, type=click.Path(),
              help="Re-export from an existing JSONL file.")
@click.pass_context
def export(
    ctx: click.Context,
    fmt: str,
    output: str | None,
    jsonl_input: str | None,
) -> None:
    """Export duplicate list from the last scan."""
    db_path: Path = ctx.obj["db"]

    # Resolve group source — generator for DB, generator for JSONL
    if jsonl_input:
        def _group_iter():
            for h, sz, files in _iter_groups_from_jsonl(_resolve_path(jsonl_input)):
                yield {"hash": h, "size": sz,
                       "files": [{"path": p, "mtime": m} for p, m in files]}
        scan_meta = {
            "root_path": str(jsonl_input), "scan_time": 0,
            "total_files": 0, "reclaimable_bytes": 0,
            "duplicate_groups": 0, "duplicate_files": 0,
        }
    else:
        with HashCache(db_path) as cache:
            last_scan = cache.get_latest_scan()
            if not last_scan:
                err_console.print("[red]No scan results.  Run scan first.[/red]")
                sys.exit(1)
            scan_id  = last_scan["id"]
            scan_meta = last_scan

        def _group_iter():
            with HashCache(db_path) as _cache:
                yield from _cache.iter_duplicate_groups(scan_id)

    out_path = _resolve_path(output) if output else None

    # csv / txt / jsonl — fully streaming (O(1) memory)
    if fmt in ("csv", "txt", "jsonl"):
        out_fh = (
            open(str(out_path), "w", encoding="utf-8", errors="replace")
            if out_path else None
        )

        def _write_line(s: str) -> None:
            if out_fh:
                out_fh.write(s + "\n")
            else:
                sys.stdout.buffer.write((s + "\n").encode("utf-8", errors="replace"))

        try:
            if fmt == "csv":
                _write_line("group_id,hash,size_bytes,path,mtime")
            total_groups = 0
            for i, g in enumerate(_group_iter(), 1):
                total_groups += 1
                if fmt == "jsonl":
                    _write_line(json.dumps({
                        "hash": g["hash"], "size": g["size"],
                        "wasted": g["size"] * (len(g["files"]) - 1),
                        "duplicates": g["files"],
                    }, ensure_ascii=False))
                elif fmt == "csv":
                    for f in g["files"]:
                        p = str(Path(f["path"])).replace('"', '""')
                        _write_line(f'{i},{g["hash"]},{g["size"]},"{p}",{f["mtime"]:.3f}')
                else:  # txt
                    waste = g["size"] * (len(g["files"]) - 1)
                    _write_line(
                        f"=== Group {i}  size={format_bytes(g['size'])}  "
                        f"copies={len(g['files'])}  waste={format_bytes(waste)} ==="
                    )
                    for f in g["files"]:
                        mts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f["mtime"]))
                        _write_line(f"  {mts}  {Path(f['path'])}")
                    _write_line("")
        finally:
            if out_fh:
                out_fh.close()

        if out_path:
            console.print(f"[green]Exported {total_groups:,} groups → {out_path}[/green]")
        return

    # json / html — buffer all groups (warn if huge)
    groups = list(_group_iter())
    if len(groups) > 100_000:  # pragma: no cover
        err_console.print(
            f"[yellow]Warning: {len(groups):,} groups loaded into RAM for {fmt} export. "
            "Consider --format jsonl for large datasets.[/yellow]"
        )

    if fmt == "json":
        content = json.dumps({"scan": scan_meta, "groups": groups},
                             indent=2, ensure_ascii=False)
    else:  # html
        content = _render_html(groups, scan_meta)

    if out_path:
        out_path.write_text(content, encoding="utf-8", errors="replace")
        console.print(f"[green]Exported {len(groups):,} groups → {out_path}[/green]")
    else:
        sys.stdout.buffer.write(content.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


def _render_html(groups: list[dict], scan_meta: dict) -> str:
    """Render a self-contained HTML report."""
    ts = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(scan_meta["scan_time"]))
        if scan_meta.get("scan_time")
        else "—"
    )
    reclaimable = sum(g["size"] * (len(g["files"]) - 1) for g in groups)
    rows_html = []
    for i, g in enumerate(groups, 1):
        waste = g["size"] * (len(g["files"]) - 1)
        files_html = "".join(
            f"<li>{_esc(f['path'])} "
            f"<span class='mtime'>{time.strftime('%Y-%m-%d %H:%M', time.localtime(f['mtime']))}</span></li>"
            for f in g["files"]
        )
        rows_html.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{_esc(format_bytes(g['size']))}</td>"
            f"<td>{len(g['files'])}</td>"
            f"<td>{_esc(format_bytes(waste))}</td>"
            f"<td><ul>{files_html}</ul></td>"
            f"</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>dupkiller report</title>
<style>
  body {{ font-family: monospace; font-size: 13px; margin: 1em 2em; }}
  h1 {{ font-size: 1.3em; }}
  .meta {{ color: #666; margin-bottom: 1em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 4px 8px; vertical-align: top; }}
  th {{ background: #f0f0f0; text-align: left; }}
  ul {{ margin: 0; padding-left: 1.2em; }}
  .mtime {{ color: #888; }}
</style>
</head>
<body>
<h1>dupkiller duplicate report</h1>
<div class="meta">
  Root: {_esc(str(scan_meta.get("root_path","")))} &nbsp;|&nbsp;
  Scanned: {ts} &nbsp;|&nbsp;
  Total files: {scan_meta.get("total_files",0):,} &nbsp;|&nbsp;
  Groups: {len(groups):,} &nbsp;|&nbsp;
  Reclaimable: {_esc(format_bytes(reclaimable))}
</div>
<table>
<thead><tr><th>#</th><th>File size</th><th>Copies</th><th>Wasted</th><th>Paths</th></tr></thead>
<tbody>
{"".join(rows_html)}
</tbody>
</table>
</body>
</html>"""


def _esc(s: str) -> str:
    """Minimal HTML escaping."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


@main.group("cache")
@click.pass_context
def cache_group(ctx: click.Context) -> None:
    """Cache maintenance commands."""


@cache_group.command("clean")
@click.option("--batch-size", default=1000, show_default=True,
              help="Paths to check per batch.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show how many entries would be removed without deleting.")
@click.option("--no-vacuum", is_flag=True, default=False,
              help="Skip VACUUM+ANALYZE after cleaning (faster, keeps file size).")
@click.pass_context
def cache_clean(ctx: click.Context, batch_size: int, dry_run: bool, no_vacuum: bool) -> None:
    """Remove cache entries for files that no longer exist on disk."""
    db_path: Path = ctx.obj["db"]

    if dry_run:
        with HashCache(db_path) as cache:
            stale = cache.count_missing_files(batch_size=batch_size)
        console.print(
            f"[yellow][DRY RUN][/yellow] Would remove [bold]{stale:,}[/bold] stale cache entries."
        )
        return

    with HashCache(db_path) as cache:
        removed = cache.clean_missing_files(batch_size=batch_size)
        if removed and not no_vacuum:
            console.print("[dim]Running VACUUM + ANALYZE…[/dim]")
            cache.vacuum()

    if removed:
        console.print(f"[green]Removed {removed:,} stale cache entries.[/green]")
    else:
        console.print("[dim]Cache is clean — no stale entries found.[/dim]")


@cache_group.command("stats")
@click.pass_context
def cache_stats(ctx: click.Context) -> None:
    """Show cache statistics."""
    db_path: Path = ctx.obj["db"]
    with HashCache(db_path) as cache:
        stats_data = cache.cache_stats()
        db_size = Path(cache.get_db_path()).stat().st_size if Path(cache.get_db_path()).exists() else 0

    tbl = Table(title="Cache Statistics", box=box.ROUNDED)
    tbl.add_column("Metric", style="cyan")
    tbl.add_column("Value", justify="right")
    tbl.add_row("Total entries", f"{stats_data['total_entries']:,}")
    tbl.add_row("With partial hash", f"{stats_data['with_partial_hash']:,}")
    tbl.add_row("With full hash", f"{stats_data['with_full_hash']:,}")
    tbl.add_row("Database size", format_bytes(db_size))
    console.print(tbl)
