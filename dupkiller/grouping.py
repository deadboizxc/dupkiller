"""
Grouping primitives.

group_by_size  — first pass: cheapest possible pre-filter (O(n) dict insert)
group_by_hash  — second/third pass: only called on size-collision candidates
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from dupkiller.utils import FileInfo


def group_by_size(files: list[FileInfo]) -> dict[int, list[FileInfo]]:
    """Return a dict mapping *size* → list of :class:`FileInfo` for that size.

    Only groups with ≥ 2 members are returned — solo-size files cannot have
    duplicates and are discarded early.
    """
    buckets: dict[int, list[FileInfo]] = defaultdict(list)
    for fi in files:
        buckets[fi.size].append(fi)
    return {sz: group for sz, group in buckets.items() if len(group) >= 2}


def group_by_hash(
    path_hash_pairs: Sequence[tuple[str, str | None]],
) -> dict[str, list[str]]:
    """Return a dict mapping *hash* → list of paths sharing that hash.

    Pairs with a None hash value are silently dropped.  Only groups with ≥ 2
    paths are returned.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for path, digest in path_hash_pairs:
        if digest is not None:
            buckets[digest].append(path)
    return {h: paths for h, paths in buckets.items() if len(paths) >= 2}
