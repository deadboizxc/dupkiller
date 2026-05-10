"""End-to-end integration tests — real scan → hash → duplicates found."""
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dupkiller.cache import HashCache
from dupkiller.checkpoint import ScanSession
from dupkiller.cli import main
from dupkiller.pipeline import OutputHandler, PipelineConfig, run_pipeline
from dupkiller.utils import ScanCounters


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _meta(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (scan_root, db_path, jsonl_path) — separate from scan root."""
    scan_root = tmp_path / "data"
    scan_root.mkdir(exist_ok=True)
    meta = tmp_path / "_meta"
    meta.mkdir(exist_ok=True)
    return scan_root, meta / "cache.db", meta / "out.jsonl"


class TestPipelineEndToEnd:
    """Run the full pipeline on a temp directory with known duplicates."""

    def _run(self, tmp_path, files: dict[str, bytes]) -> tuple[int, int, list]:
        scan_root, db_path, jsonl = _meta(tmp_path)
        for rel, data in files.items():
            _write(scan_root / rel, data)

        with HashCache(db_path) as cache:
            session = ScanSession.create(db_path, str(scan_root), config={})
            cfg = PipelineConfig(num_threads=2, num_processes=2)
            with OutputHandler(jsonl_path=jsonl) as output:
                with session:
                    total, groups = run_pipeline(
                        roots=[str(scan_root)],
                        cache=cache,
                        output=output,
                        session=session,
                        cfg=cfg,
                    )

        emitted = []
        if jsonl.exists():
            for line in jsonl.read_text().splitlines():
                if line.strip():
                    emitted.append(json.loads(line))
        return total, groups, emitted

    def test_no_duplicates(self, tmp_path):
        total, groups, emitted = self._run(tmp_path, {
            "a.txt": b"unique_a",
            "b.txt": b"unique_b",
            "c.txt": b"unique_c",
        })
        assert total == 3
        assert groups == 0
        assert emitted == []

    def test_simple_duplicate_pair(self, tmp_path):
        content = b"duplicate content here" * 100
        total, groups, emitted = self._run(tmp_path, {
            "orig.bin": content,
            "copy.bin": content,
            "unique.bin": b"not a duplicate",
        })
        assert total == 3
        assert groups == 1
        assert len(emitted) == 1
        assert len(emitted[0]["duplicates"]) == 2

    def test_multiple_duplicate_groups(self, tmp_path):
        total, groups, emitted = self._run(tmp_path, {
            "a1.bin": b"group_a" * 200,
            "a2.bin": b"group_a" * 200,
            "b1.bin": b"group_b" * 200,
            "b2.bin": b"group_b" * 200,
            "b3.bin": b"group_b" * 200,
            "solo.bin": b"solo file",
        })
        assert total == 6
        assert groups == 2
        assert len(emitted) == 2
        group_sizes = sorted(len(g["duplicates"]) for g in emitted)
        assert group_sizes == [2, 3]

    def test_multi_root(self, tmp_path):
        root_a = tmp_path / "dir_a"
        root_b = tmp_path / "dir_b"
        content = b"same content" * 100
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "f.bin").write_bytes(content)
        (root_b / "f.bin").write_bytes(content)

        meta = tmp_path / "_meta"
        meta.mkdir()
        db_path = meta / "cache.db"
        jsonl   = meta / "out.jsonl"
        with HashCache(db_path) as cache:
            session = ScanSession.create(db_path, str(root_a), config={})
            cfg = PipelineConfig(num_threads=2, num_processes=2)
            with OutputHandler(jsonl_path=jsonl) as output:
                with session:
                    total, groups = run_pipeline(
                        roots=[str(root_a), str(root_b)],
                        cache=cache, output=output,
                        session=session, cfg=cfg,
                    )
        assert total == 2
        assert groups == 1

    def test_hardlinks_not_counted_as_duplicates(self, tmp_path):
        scan_root, db_path, jsonl = _meta(tmp_path)
        original = scan_root / "original.bin"
        original.write_bytes(b"hardlink content" * 100)
        hardlink = scan_root / "hardlink.bin"
        try:
            os.link(str(original), str(hardlink))
        except OSError:
            pytest.skip("hardlinks not supported on this filesystem")

        counters = ScanCounters()
        with HashCache(db_path) as cache:
            session = ScanSession.create(db_path, str(scan_root), config={})
            cfg = PipelineConfig(num_threads=2, num_processes=2)
            with OutputHandler(jsonl_path=jsonl) as output:
                with session:
                    total, groups = run_pipeline(
                        roots=[str(scan_root)],
                        cache=cache, output=output,
                        session=session, cfg=cfg,
                        counters=counters,
                    )
        assert counters.hardlink_groups == 1
        assert groups == 0

    def test_resume_continues(self, tmp_path):
        scan_root, db_path, jsonl = _meta(tmp_path)
        content = b"resume test" * 200
        (scan_root / "f1.bin").write_bytes(content)
        (scan_root / "f2.bin").write_bytes(content)

        # Run once fully
        with HashCache(db_path) as cache:
            s1 = ScanSession.create(db_path, str(scan_root), config={})
            with OutputHandler(jsonl_path=jsonl) as output:
                with s1:
                    run_pipeline(
                        roots=[str(scan_root)], cache=cache,
                        output=output, session=s1,
                        cfg=PipelineConfig(num_threads=2, num_processes=2),
                    )

        lines1 = [line for line in jsonl.read_text().splitlines() if line.strip()]

        # Run again — starts fresh (new session, warm cache)
        jsonl.unlink()
        with HashCache(db_path) as cache:
            s2 = ScanSession.create(db_path, str(scan_root), config={})
            with OutputHandler(jsonl_path=jsonl) as output:
                with s2:
                    run_pipeline(
                        roots=[str(scan_root)], cache=cache,
                        output=output, session=s2,
                        cfg=PipelineConfig(num_threads=2, num_processes=2),
                    )

        lines2 = [line for line in jsonl.read_text().splitlines() if line.strip()]
        assert len(lines1) == len(lines2) == 1


class TestCLI:
    def _data_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "data"
        d.mkdir(exist_ok=True)
        return d

    def test_scan_and_list(self, tmp_path):
        data = self._data_dir(tmp_path)
        content = b"cli test content" * 100
        (data / "a.bin").write_bytes(content)
        (data / "b.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        runner = CliRunner()

        result = runner.invoke(main, [
            "--db", str(db),
            "scan", str(data),
            "--output-dir", str(out_dir),
            "--processes", "1", "--threads", "1",
        ])
        assert result.exit_code == 0, result.output + (result.exception and str(result.exception) or "")

        result = runner.invoke(main, ["--db", str(db), "list"])
        assert result.exit_code == 0
        assert "Group" in result.output

    def test_scan_multiple_roots(self, tmp_path):
        content = b"multi root" * 100
        r1 = tmp_path / "r1"
        r2 = tmp_path / "r2"
        r1.mkdir()
        r2.mkdir()
        (r1 / "f.bin").write_bytes(content)
        (r2 / "f.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        runner = CliRunner()
        result = runner.invoke(main, [
            "--db", str(db),
            "scan", str(r1), str(r2),
            "--output-dir", str(out_dir),
            "--processes", "1", "--threads", "1",
        ])
        assert result.exit_code == 0

    def test_export_csv(self, tmp_path):
        data = self._data_dir(tmp_path)
        content = b"export test" * 100
        (data / "e1.bin").write_bytes(content)
        (data / "e2.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        runner = CliRunner()
        runner.invoke(main, [
            "--db", str(db), "scan", str(data),
            "--output-dir", str(out_dir),
            "--processes", "1", "--threads", "1",
        ])
        result = runner.invoke(main, ["--db", str(db), "export", "--format", "csv"])
        assert result.exit_code == 0
        assert "group_id,hash" in result.output

    def test_export_html(self, tmp_path):
        data = self._data_dir(tmp_path)
        content = b"html test" * 100
        (data / "h1.bin").write_bytes(content)
        (data / "h2.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        out_file = tmp_path / "report.html"
        runner = CliRunner()
        runner.invoke(main, [
            "--db", str(db), "scan", str(data),
            "--output-dir", str(out_dir),
            "--processes", "1", "--threads", "1",
        ])
        result = runner.invoke(main, [
            "--db", str(db), "export", "--format", "html", "--output", str(out_file),
        ])
        assert result.exit_code == 0
        html = out_file.read_text()
        assert "<table>" in html
        assert "dupkiller" in html

    def test_delete_dry_run(self, tmp_path):
        data = self._data_dir(tmp_path)
        content = b"del test" * 100
        (data / "d1.bin").write_bytes(content)
        (data / "d2.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        runner = CliRunner()
        runner.invoke(main, [
            "--db", str(db), "scan", str(data),
            "--output-dir", str(out_dir),
            "--processes", "1", "--threads", "1",
        ])
        result = runner.invoke(main, ["--db", str(db), "delete", "--dry-run"])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        assert (data / "d1.bin").exists()
        assert (data / "d2.bin").exists()

    def test_cache_clean(self, tmp_path):
        db = tmp_path / "cli.db"
        runner = CliRunner()
        result = runner.invoke(main, ["--db", str(db), "cache", "clean"])
        assert result.exit_code == 0

    def test_cache_stats(self, tmp_path):
        db = tmp_path / "cli.db"
        runner = CliRunner()
        result = runner.invoke(main, ["--db", str(db), "cache", "stats"])
        assert result.exit_code == 0
        assert "Cache Statistics" in result.output

    def test_stats_list_scans(self, tmp_path):
        data = self._data_dir(tmp_path)
        content = b"stats test" * 100
        (data / "s1.bin").write_bytes(content)
        (data / "s2.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        runner = CliRunner()
        runner.invoke(main, [
            "--db", str(db), "scan", str(data),
            "--output-dir", str(out_dir),
            "--processes", "1", "--threads", "1",
        ])
        result = runner.invoke(main, ["--db", str(db), "stats", "--list"])
        assert result.exit_code == 0
        assert "Available Scans" in result.output

    def test_max_throughput_flag_parsed(self, tmp_path):
        data = self._data_dir(tmp_path)
        content = b"throttle" * 50
        (data / "t1.bin").write_bytes(content)
        (data / "t2.bin").write_bytes(content)

        db = tmp_path / "cli.db"
        out_dir = tmp_path / "output"
        runner = CliRunner()
        result = runner.invoke(main, [
            "--db", str(db), "scan", str(data),
            "--output-dir", str(out_dir),
            "--max-throughput", "500MB",
            "--processes", "1", "--threads", "1",
        ])
        assert result.exit_code == 0
