"""
Deduplication strategy and safe file deletion.

Safety contract:
    Default is always read-only (``dry_run=True``).
    Deletion requires either ``--yes`` (confirm=False) or an interactive prompt.
    Files already removed between scan and delete are treated as warnings,
    not errors — this is a normal race condition in any deduplicator.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, TypedDict

from rich.console import Console
from rich.prompt import Confirm

from dupkiller.utils import format_bytes

logger = logging.getLogger(__name__)
console = Console()

KeepStrategy = Literal["newest", "oldest", "first", "shortest", "longest"]


class DupFile(TypedDict):
    """Single duplicate file metadata."""
    path: str
    mtime: float


def select_keep_path(files: list[DupFile], strategy: KeepStrategy) -> str:
    """
    Return the path that should be *kept* based on *strategy*.

    Tie-breaking:
        For ``"newest"`` / ``"oldest"``: when two files share the exact same
        mtime, the lexicographically smaller path wins — deterministic and
        OS-independent behaviour.

    Args:
        files: List of file dicts with ``"path"`` and ``"mtime"`` keys.
        strategy: One of ``newest``, ``oldest``, ``first``, ``shortest``, ``longest``.

    Returns:
        The path string of the file to keep.
    """
    if strategy == "newest":
        return max(files, key=lambda f: (f["mtime"], -len(f["path"]), f["path"]))["path"]
    if strategy == "oldest":
        return min(files, key=lambda f: (f["mtime"], len(f["path"]), f["path"]))["path"]
    if strategy == "shortest":
        # fewest path components, then lex order as tiebreak
        return min(files, key=lambda f: (len(Path(f["path"]).parts), f["path"]))["path"]
    if strategy == "longest":
        return max(files, key=lambda f: (len(Path(f["path"]).parts), f["path"]))["path"]
    # "first" → lexicographically smallest path
    return min(files, key=lambda f: f["path"])["path"]


def delete_duplicates(
    groups: list[dict],
    keep: KeepStrategy = "newest",
    dry_run: bool = True,
    confirm: bool = True,
    interactive: bool = False,
) -> dict:
    """
    Delete one copy from each duplicate group, keeping the file selected by
    *keep* strategy.

    Parameters
    ----------
    groups:
        Duplicate groups as returned by :func:`pipeline.run_pipeline`.
    keep:
        Which file in each group to preserve.
    dry_run:
        When True, print what *would* be deleted but do nothing.
    confirm:
        When True (and not dry_run, and not interactive), ask once before
        deleting everything.
    interactive:
        When True, show each group individually and ask whether to delete it.
        Overrides *confirm* (no bulk prompt is shown).

    Returns
    -------
    dict with keys: ``deleted``, ``errors``, ``freed``, ``skipped``
    """
    # Build deletion plan grouped so interactive mode can skip groups
    plan: list[tuple[str, int, str]] = []   # (path, size, keep_path)

    for group in groups:
        keep_path = select_keep_path(group["files"], keep)
        for f in group["files"]:
            if f["path"] != keep_path:
                plan.append((f["path"], group["size"], keep_path))

    if not plan:
        console.print("[green]Nothing to delete — no duplicates found.[/green]")
        return {"deleted": 0, "errors": 0, "freed": 0, "skipped": 0}

    reclaimable = sum(sz for _, sz, _ in plan)
    console.print(
        f"\n[bold]Files selected for deletion:[/bold] [red]{len(plan):,}[/red]"
    )
    console.print(
        f"[bold]Space to reclaim:[/bold]           [red]{format_bytes(reclaimable)}[/red]"
    )

    if dry_run:
        console.print("\n[yellow bold][DRY RUN] — no files will be touched.[/yellow bold]")
        preview = plan[:25]
        for path, _, keep_path in preview:
            console.print(
                f"  [dim]would delete →[/dim] {path}\n"
                f"  [dim]        keep ↑[/dim] {keep_path}"
            )
        if len(plan) > len(preview):
            console.print(f"  [dim]… and {len(plan) - len(preview):,} more[/dim]")
        return {
            "deleted": 0,
            "errors": 0,
            "freed": 0,
            "skipped": 0,
            "would_delete": len(plan),
        }

    # Interactive mode: prompt per group individually
    if interactive:
        return _delete_interactive(groups, keep, plan)

    # Bulk confirm
    if confirm:
        if not Confirm.ask(
            f"\n[bold red]Permanently delete {len(plan):,} files?[/bold red]",
            default=False,
        ):
            console.print("[yellow]Aborted.[/yellow]")
            return {"deleted": 0, "errors": 0, "freed": 0, "skipped": 0}

    return _do_delete(plan)


def _delete_interactive(
    groups: list[dict],
    keep: KeepStrategy,
    _plan: list,   # ignored — rebuilt per group below
) -> dict:
    """Confirm and delete files one group at a time."""
    deleted = errors = skipped = freed = 0

    for i, group in enumerate(groups, 1):
        keep_path = select_keep_path(group["files"], keep)
        to_del = [f for f in group["files"] if f["path"] != keep_path]
        if not to_del:
            continue

        waste = group["size"] * len(to_del)
        console.print(
            f"\n[bold cyan]Group {i}/{len(groups)}[/bold cyan]  "
            f"[white]{format_bytes(group['size'])}[/white] × "
            f"{len(group['files'])} copies  "
            f"waste [red]{format_bytes(waste)}[/red]"
        )
        console.print(f"  [green]keep →[/green] {keep_path}")
        for f in to_del:
            console.print(f"  [red]del  →[/red] {f['path']}")

        if not Confirm.ask("  Delete these files?", default=False):
            console.print("  [dim]Skipped.[/dim]")
            skipped += len(to_del)
            continue

        for f in to_del:
            path = f["path"]
            try:
                Path(path).unlink()
                deleted += 1
                freed += group["size"]
                logger.info("deleted: %s", path)
            except FileNotFoundError:
                skipped += 1
                logger.warning("already absent (skipped): %s", path)
            except OSError as exc:
                errors += 1
                logger.error("delete failed %s: %s", path, exc)

    console.print(
        f"\n[green]Done.[/green]  Deleted [bold]{deleted:,}[/bold] files, "
        f"freed [bold]{format_bytes(freed)}[/bold]."
    )
    if skipped:
        console.print(f"[dim]Skipped: {skipped:,}[/dim]")
    if errors:
        console.print(f"[red]Errors: {errors:,} (see logs)[/red]")

    return {"deleted": deleted, "errors": errors, "freed": freed, "skipped": skipped}


def _do_delete(plan: list[tuple[str, int, str]]) -> dict:
    """Execute bulk deletion of (path, size, keep_path) triples."""
    deleted = errors = skipped = freed = 0

    for path, size, _ in plan:
        try:
            Path(path).unlink()
            deleted += 1
            freed += size
            logger.info("deleted: %s", path)
        except FileNotFoundError:
            skipped += 1
            logger.warning("already absent (skipped): %s", path)
        except OSError as exc:
            errors += 1
            logger.error("delete failed %s: %s", path, exc)

    console.print(
        f"\n[green]Done.[/green]  Deleted [bold]{deleted:,}[/bold] files, "
        f"freed [bold]{format_bytes(freed)}[/bold]."
    )
    if skipped:
        console.print(f"[dim]Skipped (already absent): {skipped:,}[/dim]")
    if errors:
        console.print(f"[red]Errors: {errors:,} (see logs)[/red]")

    return {"deleted": deleted, "errors": errors, "freed": freed, "skipped": skipped}
