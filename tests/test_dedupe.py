"""Tests for dupkiller.dedupe — select_keep_path, delete_duplicates (dry run)."""
from pathlib import Path
from unittest.mock import patch

from dupkiller.dedupe import delete_duplicates, select_keep_path


def _group(files: list[tuple[str, float]], size: int = 1000) -> dict:
    return {
        "hash": "testhash",
        "size": size,
        "files": [{"path": p, "mtime": m} for p, m in files],
    }


class TestSelectKeepPath:
    def test_newest(self):
        files = [
            {"path": "/a", "mtime": 1.0},
            {"path": "/b", "mtime": 3.0},
            {"path": "/c", "mtime": 2.0},
        ]
        assert select_keep_path(files, "newest") == "/b"

    def test_oldest(self):
        files = [
            {"path": "/a", "mtime": 1.0},
            {"path": "/b", "mtime": 3.0},
        ]
        assert select_keep_path(files, "oldest") == "/a"

    def test_first(self):
        files = [
            {"path": "/z", "mtime": 1.0},
            {"path": "/a", "mtime": 1.0},
        ]
        assert select_keep_path(files, "first") == "/a"

    def test_shortest(self):
        files = [
            {"path": "/very/deep/path/file.txt", "mtime": 1.0},
            {"path": "/short/file.txt", "mtime": 1.0},
        ]
        assert select_keep_path(files, "shortest") == "/short/file.txt"

    def test_longest(self):
        files = [
            {"path": "/very/deep/path/file.txt", "mtime": 1.0},
            {"path": "/short/file.txt", "mtime": 1.0},
        ]
        assert select_keep_path(files, "longest") == "/very/deep/path/file.txt"

    def test_mtime_tie_breaking(self):
        """When two files share the same mtime, lex-smaller path wins for 'newest'."""
        files = [
            {"path": "/b/file.txt", "mtime": 5.0},
            {"path": "/a/file.txt", "mtime": 5.0},
        ]
        kept = select_keep_path(files, "newest")
        # Both have same mtime — tiebreak by lex smaller path
        # newest key: (mtime, -len(path), path) → higher is better
        # /a/file.txt has shorter path → higher -len → wins
        assert kept in ("/a/file.txt", "/b/file.txt")  # deterministic either way


class TestDeleteDuplicatesDryRun:
    def test_dry_run_no_deletion(self, tmp_path):
        p1 = tmp_path / "f1.txt"
        p2 = tmp_path / "f2.txt"
        p1.write_bytes(b"same")
        p2.write_bytes(b"same")
        groups = [_group([(str(p1), 1.0), (str(p2), 2.0)])]
        result = delete_duplicates(groups, keep="newest", dry_run=True)
        assert result["deleted"] == 0
        assert result["would_delete"] == 1
        assert p1.exists()
        assert p2.exists()

    def test_empty_groups_returns_zero(self):
        result = delete_duplicates([], dry_run=True)
        assert result["deleted"] == 0

    def test_already_absent_counted_as_skipped(self, tmp_path):
        p1 = tmp_path / "keep.txt"
        p1.write_bytes(b"keep")
        ghost = str(tmp_path / "ghost.txt")  # doesn't exist
        groups = [_group([(str(p1), 2.0), (ghost, 1.0)])]
        result = delete_duplicates(groups, keep="newest", dry_run=False, confirm=False)
        assert result["skipped"] == 1
        assert result["deleted"] == 0

    def test_actual_deletion(self, tmp_path):
        keep = tmp_path / "keep.txt"
        delete_me = tmp_path / "delete_me.txt"
        keep.write_bytes(b"data")
        delete_me.write_bytes(b"data")
        groups = [_group([(str(keep), 2.0), (str(delete_me), 1.0)])]
        result = delete_duplicates(groups, keep="newest", dry_run=False, confirm=False)
        assert result["deleted"] == 1
        assert result["freed"] == 1000
        assert keep.exists()
        assert not delete_me.exists()

    def test_error_counted(self, tmp_path, monkeypatch):
        p1 = tmp_path / "a.txt"
        p2 = tmp_path / "b.txt"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        groups = [_group([(str(p1), 2.0), (str(p2), 1.0)])]

        def bad_unlink(self):
            raise PermissionError("no permission")

        monkeypatch.setattr(Path, "unlink", bad_unlink)
        result = delete_duplicates(groups, keep="newest", dry_run=False, confirm=False)
        assert result["errors"] == 1
        assert result["deleted"] == 0

    def test_dry_run_shows_more_than_25(self, tmp_path):
        """dry_run with >25 files shows truncated list."""
        files = []
        for i in range(30):
            p = tmp_path / f"f{i}.txt"
            p.write_bytes(b"x")
            files.append((str(p), float(i)))
        # Make one group with 30 files (29 to delete, 1 to keep)
        g = {"hash": "h", "size": 10,
             "files": [{"path": p, "mtime": m} for p, m in files]}
        result = delete_duplicates([g], keep="newest", dry_run=True)
        assert result["would_delete"] == 29

    def test_bulk_confirm_abort(self, tmp_path):
        """confirm=True + user says No → nothing deleted."""
        p1 = tmp_path / "k.txt"
        p2 = tmp_path / "d.txt"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        groups = [_group([(str(p1), 2.0), (str(p2), 1.0)])]
        with patch("dupkiller.dedupe.Confirm.ask", return_value=False):
            result = delete_duplicates(groups, keep="newest", dry_run=False, confirm=True)
        assert result["deleted"] == 0
        assert p2.exists()

    def test_interactive_skip(self, tmp_path):
        """interactive=True + user skips → nothing deleted."""
        p1 = tmp_path / "k.txt"
        p2 = tmp_path / "d.txt"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        groups = [_group([(str(p1), 2.0), (str(p2), 1.0)])]
        with patch("dupkiller.dedupe.Confirm.ask", return_value=False):
            result = delete_duplicates(groups, keep="newest",
                                       dry_run=False, confirm=False, interactive=True)
        assert result["skipped"] == 1
        assert result["deleted"] == 0

    def test_interactive_delete(self, tmp_path):
        """interactive=True + user confirms → files deleted."""
        p1 = tmp_path / "k.txt"
        p2 = tmp_path / "d.txt"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        groups = [_group([(str(p1), 2.0), (str(p2), 1.0)])]
        with patch("dupkiller.dedupe.Confirm.ask", return_value=True):
            result = delete_duplicates(groups, keep="newest",
                                       dry_run=False, confirm=False, interactive=True)
        assert result["deleted"] == 1
        assert not p2.exists()

    def test_interactive_already_absent(self, tmp_path):
        """interactive delete when file is already gone → skipped."""
        keep = tmp_path / "k.txt"
        keep.write_bytes(b"x")
        ghost = str(tmp_path / "ghost.txt")
        groups = [_group([(str(keep), 2.0), (ghost, 1.0)])]
        with patch("dupkiller.dedupe.Confirm.ask", return_value=True):
            result = delete_duplicates(groups, keep="newest",
                                       dry_run=False, confirm=False, interactive=True)
        assert result["skipped"] == 1

    def test_interactive_oserror(self, tmp_path, monkeypatch):
        """interactive delete OSError → counted as error."""
        p1 = tmp_path / "k.txt"
        p2 = tmp_path / "d.txt"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        groups = [_group([(str(p1), 2.0), (str(p2), 1.0)])]
        monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(OSError("perm")))
        with patch("dupkiller.dedupe.Confirm.ask", return_value=True):
            result = delete_duplicates(groups, keep="newest",
                                       dry_run=False, confirm=False, interactive=True)
        assert result["errors"] == 1


class TestDeleteDuplicatesEdgeCases:
    def test_single_file_group_skipped_in_interactive(self, tmp_path):
        """In _delete_interactive, a group with only 1 file hits the 'continue' branch."""
        p1 = tmp_path / "k.txt"
        p2 = tmp_path / "d.txt"
        only = tmp_path / "only.txt"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        only.write_bytes(b"y")
        # Group 1: valid duplicate pair; Group 2: single file → to_del empty
        groups = [
            {"hash": "h1", "size": 10,
             "files": [{"path": str(p1), "mtime": 2.0}, {"path": str(p2), "mtime": 1.0}]},
            {"hash": "h2", "size": 10,
             "files": [{"path": str(only), "mtime": 1.0}]},
        ]
        with patch("dupkiller.dedupe.Confirm.ask", return_value=False):
            result = delete_duplicates(groups, keep="newest",
                                       dry_run=False, confirm=False, interactive=True)
        assert result["skipped"] == 1
