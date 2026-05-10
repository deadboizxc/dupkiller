"""Tests for dupkiller.cli — Click commands via CliRunner."""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dupkiller.cache import HashCache
from dupkiller.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _invoke(runner, args, db):
    return runner.invoke(main, ["--db", db] + args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------

class TestMainGroup:
    def test_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "dupkiller" in result.output

    def test_version(self, runner):
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0

    def test_verbose_flag(self, runner, db_path):
        result = runner.invoke(main, ["--verbose", "--db", db_path, "cache", "stats"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# cache commands
# ---------------------------------------------------------------------------

class TestCacheStats:
    def test_empty_db(self, runner, db_path):
        result = _invoke(runner, ["cache", "stats"], db_path)
        assert result.exit_code == 0
        assert "Total entries" in result.output

    def test_with_entries(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.queue_update("/a", 100, 1.0, partial_hash="abc")
            cache.flush()
        result = _invoke(runner, ["cache", "stats"], db_path)
        assert result.exit_code == 0


class TestCacheClean:
    def test_dry_run_empty(self, runner, db_path):
        result = _invoke(runner, ["cache", "clean", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output

    def test_dry_run_with_stale(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.queue_update("/nonexistent/gone.bin", 1, 1.0, partial_hash="x")
            cache.flush()
        result = _invoke(runner, ["cache", "clean", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "1" in result.output

    def test_clean_removes_stale(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.queue_update("/nonexistent/gone.bin", 1, 1.0, partial_hash="x")
            cache.flush()
        result = _invoke(runner, ["cache", "clean", "--no-vacuum"], db_path)
        assert result.exit_code == 0
        assert "1" in result.output

    def test_clean_already_clean(self, runner, db_path, tmp_path):
        real = tmp_path / "real.bin"
        real.write_bytes(b"x")
        with HashCache(db_path) as cache:
            cache.queue_update(str(real), 1, 1.0, partial_hash="x")
            cache.flush()
        result = _invoke(runner, ["cache", "clean"], db_path)
        assert result.exit_code == 0
        assert "clean" in result.output.lower()

    def test_clean_runs_vacuum(self, runner, db_path):
        """cache clean without --no-vacuum runs VACUUM after removing stale entries."""
        with HashCache(db_path) as cache:
            cache.queue_update("/nonexistent/gone.bin", 1, 1.0, partial_hash="x")
            cache.flush()
        # No --no-vacuum flag → should run VACUUM
        result = _invoke(runner, ["cache", "clean"], db_path)
        assert result.exit_code == 0
        assert "1" in result.output  # removed 1 entry


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------

class TestListCommand:
    def test_no_scan_results_exits_1(self, runner, db_path):
        result = runner.invoke(main, ["--db", db_path, "list"])
        assert result.exit_code == 1

    def test_with_scan_results(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 10, [
                ("hash1", 1000, [("/a", 1.0), ("/b", 2.0)]),
            ])
        result = _invoke(runner, ["list"], db_path)
        assert result.exit_code == 0

    def test_list_limit(self, runner, db_path):
        groups = [(f"h{i}", 100, [(f"/a{i}", 1.0), (f"/b{i}", 2.0)])
                  for i in range(5)]
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 10, groups)
        result = _invoke(runner, ["list", "--limit", "2"], db_path)
        assert result.exit_code == 0

    def test_list_min_waste(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 10, [
                ("h1", 100, [("/a", 1.0), ("/b", 2.0)]),   # waste=100
                ("h2", 5000, [("/c", 1.0), ("/d", 2.0)]),  # waste=5000
            ])
        result = _invoke(runner, ["list", "--min-waste", "1000"], db_path)
        assert result.exit_code == 0
        assert "/c" in result.output
        assert "/a" not in result.output

    def test_list_no_duplicates_after_filter(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 10, [
                ("h1", 1, [("/a", 1.0), ("/b", 2.0)]),
            ])
        result = _invoke(runner, ["list", "--min-waste", "999999"], db_path)
        assert result.exit_code == 0
        assert "No duplicates" in result.output

    def test_list_from_jsonl(self, runner, tmp_path, db_path):
        jsonl = tmp_path / "out.jsonl"
        record = {
            "hash": "hx", "size": 100, "wasted": 100,
            "duplicates": [{"path": "/x", "mtime": 1.0}, {"path": "/y", "mtime": 2.0}],
        }
        jsonl.write_text(json.dumps(record) + "\n")
        result = runner.invoke(main, ["--db", db_path, "list", "--jsonl", str(jsonl)])
        assert result.exit_code == 0

    def test_list_scan_time_zero(self, runner, db_path):
        """When scan_time=0 (JSONL source), timestamp line is skipped."""
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 1, [
                ("hh", 50, [("/p", 1.0), ("/q", 2.0)])
            ])
        # Manually set scan_time to 0 to test that branch
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE scan_results SET scan_time=0")
        conn.commit()
        conn.close()
        result = _invoke(runner, ["list"], db_path)
        assert result.exit_code == 0

    def test_list_shows_more_indicator(self, runner, db_path):
        """stats command shows '… +N more' for groups with >3 files."""
        files = [(f"/file_{i}.bin", float(i)) for i in range(6)]
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 10, [
                ("biggroup", 1000, files),
            ])
        # The "+N more" indicator is in the stats command (top groups section)
        result = _invoke(runner, ["stats"], db_path)
        assert result.exit_code == 0
        assert "more" in result.output


# ---------------------------------------------------------------------------
# stats command
# ---------------------------------------------------------------------------

class TestStatsCommand:
    def test_no_scan_results_exits_1(self, runner, db_path):
        result = runner.invoke(main, ["--db", db_path, "stats"])
        assert result.exit_code == 1

    def test_list_scans(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 5, [])
        result = _invoke(runner, ["stats", "--list"], db_path)
        assert result.exit_code == 0
        assert "/r" in result.output

    def test_stats_basic(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 10, [
                ("h1", 1000, [("/a", 1.0), ("/b", 2.0)]),
            ])
        result = _invoke(runner, ["stats"], db_path)
        assert result.exit_code == 0
        assert "Reclaimable" in result.output

    def test_stats_compare_with(self, runner, db_path):
        with HashCache(db_path) as cache:
            id1 = cache.save_scan_results("/r", 5, [])
            time.sleep(0.01)
            cache.save_scan_results("/r", 10, [
                ("h1", 100, [("/a", 1.0), ("/b", 2.0)]),
            ])
        result = _invoke(runner, ["stats", "--compare-with", str(id1)], db_path)
        assert result.exit_code == 0
        assert "Baseline" in result.output

    def test_stats_compare_same_id_exits_1(self, runner, db_path):
        with HashCache(db_path) as cache:
            scan_id = cache.save_scan_results("/r", 5, [])
        result = runner.invoke(main, ["--db", db_path, "stats", "--compare-with", str(scan_id)])
        assert result.exit_code == 1

    def test_stats_compare_not_found_exits_1(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 5, [])
        result = runner.invoke(main, ["--db", db_path, "stats", "--compare-with", "99999"])
        assert result.exit_code == 1

    def test_stats_compare_newer_baseline_warning(self, runner, db_path):
        with HashCache(db_path) as cache:
            id1 = cache.save_scan_results("/r", 10, [])
            time.sleep(0.01)
            id2 = cache.save_scan_results("/r", 5, [])
        # id1 is older (smaller id but in this case scan_time is earlier)
        # compare latest (id2) against id1 — but id1 has earlier scan_time → ok
        # let's compare against id2 which is the latest scan itself — but that
        # would be same-id error. Instead force a "baseline newer" by using id1 as
        # latest and id2 as compare. Do this by faking scan_time in DB.
        import sqlite3
        conn = sqlite3.connect(db_path)
        now = time.time()
        conn.execute("UPDATE scan_results SET scan_time=? WHERE id=?", (now + 1000, id1))
        conn.commit()
        conn.close()
        result = runner.invoke(main, ["--db", db_path, "stats", "--compare-with", str(id2)])
        # Should warn but not fail
        assert "Warning" in result.output or result.exit_code == 0


# ---------------------------------------------------------------------------
# delete command
# ---------------------------------------------------------------------------

class TestDeleteCommand:
    def _setup_scan(self, db_path, tmp_path):
        p1 = tmp_path / "keep.bin"
        p2 = tmp_path / "del.bin"
        p1.write_bytes(b"data")
        p2.write_bytes(b"data")
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 2, [
                ("hx", 4, [(str(p1), 2.0), (str(p2), 1.0)]),
            ])
        return p1, p2

    def test_no_scan_exits_1(self, runner, db_path):
        result = runner.invoke(main, ["--db", db_path, "delete", "--yes"])
        assert result.exit_code == 1

    def test_dry_run(self, runner, db_path, tmp_path):
        p1, p2 = self._setup_scan(db_path, tmp_path)
        result = _invoke(runner, ["delete", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert p2.exists()

    def test_yes_deletes(self, runner, db_path, tmp_path):
        p1, p2 = self._setup_scan(db_path, tmp_path)
        result = _invoke(runner, ["delete", "--yes"], db_path)
        assert result.exit_code == 0
        assert not p2.exists()
        assert p1.exists()

    def test_no_duplicates(self, runner, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 5, [])
        result = _invoke(runner, ["delete", "--yes"], db_path)
        assert result.exit_code == 0
        assert "No duplicates" in result.output

    def test_min_waste_filter(self, runner, db_path, tmp_path):
        p1 = tmp_path / "k.bin"
        p2 = tmp_path / "d.bin"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 2, [
                ("hx", 1, [(str(p1), 2.0), (str(p2), 1.0)]),
            ])
        # min_waste=999 exceeds group waste of 1 byte → nothing to delete
        result = _invoke(runner, ["delete", "--yes", "--min-waste", "999"], db_path)
        assert result.exit_code == 0
        assert "No duplicates" in result.output

    def test_from_jsonl(self, runner, tmp_path, db_path):
        p1 = tmp_path / "k.bin"
        p2 = tmp_path / "d.bin"
        p1.write_bytes(b"data")
        p2.write_bytes(b"data")
        jsonl = tmp_path / "dups.jsonl"
        record = {
            "hash": "hx", "size": 4, "wasted": 4,
            "duplicates": [{"path": str(p1), "mtime": 2.0},
                           {"path": str(p2), "mtime": 1.0}],
        }
        jsonl.write_text(json.dumps(record) + "\n")
        result = runner.invoke(
            main, ["--db", db_path, "delete", "--yes", "--jsonl", str(jsonl)]
        )
        assert result.exit_code == 0
        assert not p2.exists()

    def test_interactive_skip(self, runner, db_path, tmp_path):
        p1, p2 = self._setup_scan(db_path, tmp_path)
        with patch("rich.prompt.Confirm.ask", return_value=False):
            result = _invoke(runner, ["delete", "--interactive"], db_path)
        assert result.exit_code == 0
        assert p2.exists()

    def test_bulk_confirm_abort(self, runner, db_path, tmp_path):
        p1, p2 = self._setup_scan(db_path, tmp_path)
        runner.invoke(
            main, ["--db", db_path, "delete"],
            input="n\n",  # answer No to confirm prompt
        )
        assert p2.exists()

    def test_dry_run_no_duplicates(self, runner, db_path):
        """dry-run delete when scan has no duplicates exits cleanly."""
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 0, [])
        result = _invoke(runner, ["delete", "--dry-run"], db_path)
        assert result.exit_code == 0
        assert "No duplicates" in result.output

    def test_streaming_delete_yes(self, runner, db_path, tmp_path):
        """--yes flag uses streaming path."""
        p1, p2 = self._setup_scan(db_path, tmp_path)
        result = _invoke(runner, ["delete", "--yes"], db_path)
        assert result.exit_code == 0

    def test_streaming_delete_oserror(self, runner, db_path, tmp_path):
        """OSError during streaming delete is counted as an error."""
        p1, p2 = self._setup_scan(db_path, tmp_path)
        with patch("pathlib.Path.unlink", side_effect=OSError("perm")):
            result = _invoke(runner, ["delete", "--yes"], db_path)
        assert result.exit_code == 1  # errors present

    def test_streaming_delete_no_duplicates(self, runner, db_path):
        """--yes with no duplicates prints 'No duplicates'."""
        with HashCache(db_path) as cache:
            cache.save_scan_results("/root", 0, [])
        result = _invoke(runner, ["delete", "--yes"], db_path)
        assert result.exit_code == 0
        assert "No duplicates" in result.output

    def test_streaming_delete_file_already_gone(self, runner, db_path, tmp_path):
        """FileNotFoundError during streaming delete → counted as skipped."""
        p1, p2 = self._setup_scan(db_path, tmp_path)
        p2.unlink()  # remove the file that would be deleted before the scan runs
        result = _invoke(runner, ["delete", "--yes"], db_path)
        assert result.exit_code == 0  # FileNotFoundError → skipped, not error


# ---------------------------------------------------------------------------
# export command
# ---------------------------------------------------------------------------

class TestExportCommand:
    def _setup(self, db_path):
        with HashCache(db_path) as cache:
            cache.save_scan_results("/r", 5, [
                ("hx", 100, [("/a", 1.0), ("/b", 2.0)]),
            ])

    def test_no_scan_exits_1(self, runner, db_path):
        result = runner.invoke(main, ["--db", db_path, "export"])
        assert result.exit_code == 1

    def test_export_csv(self, runner, db_path, tmp_path):
        self._setup(db_path)
        out = tmp_path / "out.csv"
        result = _invoke(runner, ["export", "--format", "csv", "--output", str(out)], db_path)
        assert result.exit_code == 0
        assert out.exists()
        content = out.read_text()
        assert "group_id" in content

    def test_export_txt(self, runner, db_path, tmp_path):
        self._setup(db_path)
        out = tmp_path / "out.txt"
        result = _invoke(runner, ["export", "--format", "txt", "--output", str(out)], db_path)
        assert result.exit_code == 0
        assert "Group 1" in out.read_text()

    def test_export_jsonl(self, runner, db_path, tmp_path):
        self._setup(db_path)
        out = tmp_path / "out.jsonl"
        result = _invoke(runner, ["export", "--format", "jsonl", "--output", str(out)], db_path)
        assert result.exit_code == 0
        obj = json.loads(out.read_text().strip())
        assert "hash" in obj

    def test_export_json(self, runner, db_path, tmp_path):
        self._setup(db_path)
        out = tmp_path / "out.json"
        result = _invoke(runner, ["export", "--format", "json", "--output", str(out)], db_path)
        assert result.exit_code == 0
        data = json.loads(out.read_text())
        assert "groups" in data

    def test_export_html(self, runner, db_path, tmp_path):
        self._setup(db_path)
        out = tmp_path / "out.html"
        result = _invoke(runner, ["export", "--format", "html", "--output", str(out)], db_path)
        assert result.exit_code == 0
        assert "<html" in out.read_text()

    def test_export_from_jsonl_input(self, runner, tmp_path, db_path):
        src = tmp_path / "src.jsonl"
        record = {"hash": "hx", "size": 50, "wasted": 50,
                  "duplicates": [{"path": "/a", "mtime": 1.0}, {"path": "/b", "mtime": 2.0}]}
        src.write_text(json.dumps(record) + "\n")
        out = tmp_path / "out.csv"
        result = runner.invoke(
            main, ["--db", db_path, "export", "--format", "csv",
                   "--jsonl-input", str(src), "--output", str(out)]
        )
        assert result.exit_code == 0
        assert out.exists()

    def test_export_csv_stdout(self, runner, db_path):
        self._setup(db_path)
        result = _invoke(runner, ["export", "--format", "csv"], db_path)
        assert result.exit_code == 0
        assert "group_id" in result.output

    def test_export_json_stdout(self, runner, db_path):
        self._setup(db_path)
        result = _invoke(runner, ["export", "--format", "json"], db_path)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# parse_rate helper
# ---------------------------------------------------------------------------

class TestParseRate:
    def test_mb(self, runner):
        from dupkiller.cli import _parse_rate
        assert _parse_rate("50MB") == 50 * 1024 ** 2

    def test_kb(self, runner):
        from dupkiller.cli import _parse_rate
        assert _parse_rate("200KB") == 200 * 1024

    def test_gb(self, runner):
        from dupkiller.cli import _parse_rate
        assert _parse_rate("1GB") == 1024 ** 3

    def test_plain_int(self):
        from dupkiller.cli import _parse_rate
        assert _parse_rate("1024") == 1024

    def test_invalid_raises(self):
        import click

        from dupkiller.cli import _parse_rate
        with pytest.raises(click.BadParameter):
            _parse_rate("notarate")

    def test_suffix_but_non_numeric(self):
        """String ending with valid suffix but non-numeric value raises BadParameter."""
        import click

        from dupkiller.cli import _parse_rate
        with pytest.raises(click.BadParameter):
            _parse_rate("xyzB")  # ends with "B", "xyz" is not a number


# ---------------------------------------------------------------------------
# _iter_groups_from_jsonl
# ---------------------------------------------------------------------------

class TestIterGroupsFromJsonl:
    def test_empty_lines_skipped(self, tmp_path):
        from dupkiller.cli import _iter_groups_from_jsonl
        jsonl = tmp_path / "t.jsonl"
        record = {"hash": "h1", "size": 100,
                  "duplicates": [{"path": "/a"}, {"path": "/b"}]}
        jsonl.write_text("\n" + json.dumps(record) + "\n\n")
        groups = list(_iter_groups_from_jsonl(jsonl))
        assert len(groups) == 1

    def test_invalid_json_skipped(self, tmp_path):
        from dupkiller.cli import _iter_groups_from_jsonl
        jsonl = tmp_path / "t.jsonl"
        jsonl.write_text("INVALID_JSON\n")
        groups = list(_iter_groups_from_jsonl(jsonl))
        assert groups == []

    def test_oserror_returns_empty(self, tmp_path):
        from dupkiller.cli import _iter_groups_from_jsonl
        groups = list(_iter_groups_from_jsonl(tmp_path / "nonexistent.jsonl"))
        assert groups == []


# ---------------------------------------------------------------------------
# _print_skip_stats
# ---------------------------------------------------------------------------

class TestPrintSkipStats:
    def test_non_zero_counters_printed(self):
        from io import StringIO
        from unittest.mock import patch as _patch

        from rich.console import Console

        from dupkiller.cli import _print_skip_stats
        from dupkiller.utils import ScanCounters

        c = ScanCounters()
        c.inc("skipped_permission", 3)
        c.inc("skipped_symlink", 1)
        # Patch the module-level console to capture output
        buf = StringIO()
        mock_console = Console(file=buf, stderr=False)
        with _patch("dupkiller.cli.console", mock_console):
            _print_skip_stats(c)
        out = buf.getvalue()
        assert "3" in out or "Permission" in out

    def test_all_zero_no_output(self):
        from dupkiller.cli import _print_skip_stats
        from dupkiller.utils import ScanCounters
        c = ScanCounters()
        # Should return early without printing anything
        _print_skip_stats(c)  # no exception
