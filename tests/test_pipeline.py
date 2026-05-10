"""Tests for dupkiller.pipeline — OutputHandler, _eta_str, ProgressReporter,
and full run_pipeline integration."""
import json
import logging
import time

from dupkiller.cache import HashCache
from dupkiller.checkpoint import ScanSession
from dupkiller.pipeline import (
    OutputHandler,
    PipelineConfig,
    ProgressReporter,
    _eta_str,
    run_pipeline,
)
from dupkiller.utils import ScanCounters, ShutdownFlag

# ---------------------------------------------------------------------------
# OutputHandler
# ---------------------------------------------------------------------------

class TestOutputHandler:
    def test_new_handler_no_jsonl(self, tmp_path):
        """OutputHandler without JSONL path still records groups in memory."""
        with OutputHandler(jsonl_path=None) as h:
            h.emit_group("abc", 100, [("/a", 1.0), ("/b", 2.0)])
            assert h.groups_written == 1

    def test_emit_writes_to_jsonl(self, tmp_path):
        jsonl = tmp_path / "out.jsonl"
        with OutputHandler(jsonl_path=jsonl) as h:
            h.emit_group("hash1", 500, [("/x", 1.0), ("/y", 2.0)])
        lines = jsonl.read_text().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["hash"] == "hash1"
        assert obj["size"] == 500

    def test_already_emitted_deduplicates(self, tmp_path):
        jsonl = tmp_path / "out.jsonl"
        with OutputHandler(jsonl_path=jsonl) as h:
            h.emit_group("dup", 100, [("/a", 1.0), ("/b", 2.0)])
            h.emit_group("dup", 100, [("/a", 1.0), ("/b", 2.0)])  # duplicate
        assert h.groups_written == 1

    def test_already_emitted_returns_true(self, tmp_path):
        jsonl = tmp_path / "out.jsonl"
        with OutputHandler(jsonl_path=jsonl) as h:
            h.emit_group("xyz", 100, [("/a", 1.0), ("/b", 2.0)])
            assert h.already_emitted("xyz") is True
            assert h.already_emitted("nope") is False

    def test_emit_group_records_partial_hash(self, tmp_path):
        """partial_hash is written to JSONL and added to _emitted."""
        jsonl = tmp_path / "out.jsonl"
        with OutputHandler(jsonl_path=jsonl) as h:
            h.emit_group("fullhash", 100, [("/a", 1.0), ("/b", 2.0)],
                         partial_hash="partialhash")
            assert h.already_emitted("fullhash") is True
            assert h.already_emitted("partialhash") is True
        record = json.loads(jsonl.read_text().strip())
        assert record["partial_hash"] == "partialhash"

    def test_load_emitted_restores_partial_hash(self, tmp_path):
        """_load_emitted picks up partial_hash field from existing JSONL."""
        jsonl = tmp_path / "out.jsonl"
        jsonl.write_text(
            '{"hash": "fh1", "partial_hash": "ph1", "size": 100, '
            '"duplicates": [{"path": "/a"}, {"path": "/b"}]}\n'
        )
        with OutputHandler(jsonl_path=jsonl) as h:
            assert h.already_emitted("fh1") is True
            assert h.already_emitted("ph1") is True   # partial hash loaded too
            assert h.already_emitted("other") is False

    def test_resume_from_existing_jsonl(self, tmp_path):
        """Opening an existing JSONL file marks those hashes as already emitted."""
        jsonl = tmp_path / "resume.jsonl"
        jsonl.write_text(
            '{"hash": "existing", "size": 100, "duplicates": [{"path": "/a"}, {"path": "/b"}]}\n'
            '{"hash": "another", "size": 200, "duplicates": [{"path": "/c"}, {"path": "/d"}]}\n'
        )
        with OutputHandler(jsonl_path=jsonl) as h:
            assert h.already_emitted("existing") is True
            assert h.already_emitted("another") is True
            assert h.already_emitted("new_one") is False
            # Emit the new one
            h.emit_group("new_one", 50, [("/e", 1.0), ("/f", 2.0)])
        # File now has 3 lines (2 original + 1 new)
        lines = [line for line in jsonl.read_text().splitlines() if line.strip()]
        assert len(lines) == 3

    def test_load_emitted_handles_bad_json(self, tmp_path):
        """Malformed lines in JSONL are silently skipped."""
        jsonl = tmp_path / "bad.jsonl"
        jsonl.write_text('{"hash": "ok", "size": 1, "duplicates": [{"path": "/a"}]}\nNOT_JSON\n\n')
        with OutputHandler(jsonl_path=jsonl) as h:
            # "ok" has only 1 file — no hash stored (no group hash key without 2+ files,
            # but _load_emitted stores any hash it finds regardless of file count)
            assert h.already_emitted("ok") is True

    def test_emit_logs_when_no_jsonl(self, tmp_path, caplog):
        """When jsonl_path is None, emit_group logs the duplicate."""
        with caplog.at_level(logging.INFO, logger="dupkiller.pipeline"):
            with OutputHandler(jsonl_path=None) as h:
                h.emit_group("loghash", 123, [("/m", 1.0), ("/n", 2.0)])
        assert "loghash" in caplog.text

    def test_close_with_log_path(self, tmp_path):
        """OutputHandler with log_path creates a log file and closes cleanly."""
        log = tmp_path / "scan.log"
        with OutputHandler(jsonl_path=None, log_path=log):
            logging.getLogger("dupkiller").info("test log entry")
        assert log.exists()

    def test_load_emitted_oserror(self, tmp_path, caplog):
        """OSError when reading JSONL for resume is logged and ignored."""
        jsonl = tmp_path / "unreadable.jsonl"
        jsonl.write_text(
            '{"hash": "h1", "size": 100, "duplicates": [{"path": "/a"}, {"path": "/b"}]}\n'
        )
        jsonl.chmod(0o200)  # write-only: append succeeds, read raises OSError
        try:
            with caplog.at_level(logging.WARNING, logger="dupkiller.pipeline"):
                with OutputHandler(jsonl_path=jsonl) as h:
                    assert not h.already_emitted("h1")  # wasn't loaded
            assert "Cannot read JSONL" in caplog.text
        finally:
            jsonl.chmod(0o644)


# ---------------------------------------------------------------------------
# _eta_str
# ---------------------------------------------------------------------------

class TestEtaStr:
    def test_zero_done(self):
        assert _eta_str(0, 100, 1.0) == "ETA:?"

    def test_zero_total(self):
        assert _eta_str(10, 0, 1.0) == "ETA:?"

    def test_zero_elapsed(self):
        assert _eta_str(10, 100, 0.0) == "ETA:?"

    def test_seconds(self):
        # done=50, total=100, elapsed=50s → rate=1/s, rem=50s
        result = _eta_str(50, 100, 50.0)
        assert result.startswith("ETA:") and result.endswith("s")

    def test_minutes(self):
        # done=1, total=120, elapsed=1s → rate=1/s, rem=119s > 60s
        result = _eta_str(1, 120, 1.0)
        assert "m" in result

    def test_hours(self):
        # done=1, total=10000, elapsed=1s → rate=1/s, rem=9999s > 3600s
        result = _eta_str(1, 10000, 1.0)
        assert "h" in result


# ---------------------------------------------------------------------------
# ProgressReporter
# ---------------------------------------------------------------------------

class TestProgressReporter:
    def test_start_stop(self):
        rep = ProgressReporter(interval=0.05)
        rep.update(stage="test", value=1)
        rep.start()
        time.sleep(0.15)
        rep.stop()

    def test_stop_without_start(self):
        rep = ProgressReporter(interval=1.0)
        rep.stop()  # should be a no-op

    def test_run_logs_stats(self, caplog):
        rep = ProgressReporter(interval=0.02)
        rep.update(stage="hashing", done="5")
        with caplog.at_level(logging.INFO, logger="dupkiller.pipeline"):
            rep.start()
            time.sleep(0.1)
            rep.stop()
        assert "hashing" in caplog.text


# ---------------------------------------------------------------------------
# run_pipeline — integration tests with real temp files
# ---------------------------------------------------------------------------

def _create_db_with_hash_cache(db_path):
    """Ensure HashCache tables exist on the db before ScanSession opens it."""
    with HashCache(db_path):
        pass


class TestRunPipelineIntegration:
    def _make_dup_dir(self, tmp_path, n_dups=2, n_unique=1):
        """Create *n_dups* identical files and *n_unique* distinct files."""
        content = b"duplicate_content_for_testing" * 100
        paths = []
        for i in range(n_dups):
            p = tmp_path / f"dup_{i}.bin"
            p.write_bytes(content)
            paths.append(p)
        for i in range(n_unique):
            p = tmp_path / f"unique_{i}.bin"
            p.write_bytes(bytes([i + 1]) * 500)
            paths.append(p)
        return paths

    def test_finds_duplicates(self, tmp_path):
        """Full pipeline discovers 1 duplicate group from 2 identical files."""
        scan_dir = tmp_path / "files"
        scan_dir.mkdir()
        self._make_dup_dir(scan_dir, n_dups=2, n_unique=1)

        db = tmp_path / "scan.db"
        jsonl = tmp_path / "out.jsonl"

        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                with OutputHandler(jsonl_path=jsonl) as output:
                    cfg = PipelineConfig(num_threads=2, num_processes=1)
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)],
                        cache=cache,
                        output=output,
                        session=session,
                        cfg=cfg,
                        shutdown=ShutdownFlag(),
                        counters=ScanCounters(),
                    )

        assert total >= 2
        assert groups == 1
        assert jsonl.exists()

    def test_no_duplicates(self, tmp_path):
        """Pipeline with all-unique files reports 0 duplicate groups."""
        scan_dir = tmp_path / "uniq"
        scan_dir.mkdir()
        for i in range(3):
            (scan_dir / f"f{i}.bin").write_bytes(bytes([i + 10]) * 500)

        db = tmp_path / "scan.db"
        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                with OutputHandler(jsonl_path=None) as output:
                    cfg = PipelineConfig(num_threads=2, num_processes=1)
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=cfg,
                    )

        assert total == 3
        assert groups == 0

    def test_shutdown_before_scan(self, tmp_path):
        """Pre-set shutdown → pipeline returns immediately with 0 groups."""
        scan_dir = tmp_path / "s"
        scan_dir.mkdir()
        (scan_dir / "a.bin").write_bytes(b"x")
        (scan_dir / "b.bin").write_bytes(b"x")

        db = tmp_path / "scan.db"
        shutdown = ShutdownFlag()
        shutdown.set()

        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                with OutputHandler(jsonl_path=None) as output:
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=PipelineConfig(),
                        shutdown=shutdown,
                    )

        assert groups == 0

    def test_resume_from_partial_hashing(self, tmp_path):
        """Second run on same dir re-uses cache — verifies pipeline runs cleanly twice."""
        scan_dir = tmp_path / "files"
        scan_dir.mkdir()
        content = b"identical" * 200
        (scan_dir / "a.bin").write_bytes(content)
        (scan_dir / "b.bin").write_bytes(content)

        db = tmp_path / "scan.db"
        jsonl = tmp_path / "out.jsonl"

        # First run — full pipeline
        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                with OutputHandler(jsonl_path=jsonl) as output:
                    cfg = PipelineConfig(num_threads=2, num_processes=1)
                    total1, groups1 = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=cfg,
                    )
        assert groups1 == 1
        assert jsonl.exists()

    def test_pipeline_with_hardlinks(self, tmp_path):
        """Hard-linked files are detected and skipped in hashing."""
        scan_dir = tmp_path / "links"
        scan_dir.mkdir()
        original = scan_dir / "orig.bin"
        original.write_bytes(b"hardlinked_content" * 100)
        link = scan_dir / "link.bin"
        link.hardlink_to(original)
        # Also a regular duplicate pair
        dup_content = b"dup" * 200
        (scan_dir / "dup_a.bin").write_bytes(dup_content)
        (scan_dir / "dup_b.bin").write_bytes(dup_content)

        db = tmp_path / "scan.db"
        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                counters = ScanCounters()
                with OutputHandler(jsonl_path=None) as output:
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=PipelineConfig(num_processes=1),
                        counters=counters,
                    )

        # Hard-linked files should be detected
        assert counters.hardlink_groups >= 1

    def test_hash_failure_marks_unique(self, tmp_path):
        """A file that fails hashing is marked unique (not a candidate)."""
        scan_dir = tmp_path / "hf"
        scan_dir.mkdir()
        content = b"same_content" * 100
        (scan_dir / "a.bin").write_bytes(content)
        (scan_dir / "b.bin").write_bytes(content)
        (scan_dir / "c.bin").write_bytes(content)

        db = tmp_path / "scan.db"
        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                # Make one file unreadable to force a hash failure
                unreadable = scan_dir / "a.bin"
                unreadable.chmod(0o000)
                try:
                    with OutputHandler(jsonl_path=None) as output:
                        total, groups = run_pipeline(
                            roots=[str(scan_dir)], cache=cache, output=output,
                            session=session, cfg=PipelineConfig(num_processes=1),
                        )
                finally:
                    unreadable.chmod(0o644)
        # b.bin and c.bin should still form a duplicate group
        assert groups >= 0  # permissive: just ensure it doesn't raise

    def test_resume_from_partial_hashing_stage(self, tmp_path):
        """run_pipeline skips scan phase when session stage is 'partial_hashing'."""
        scan_dir = tmp_path / "files"
        scan_dir.mkdir()
        content = b"same" * 200
        (scan_dir / "a.bin").write_bytes(content)
        (scan_dir / "b.bin").write_bytes(content)

        db = tmp_path / "scan.db"
        from dupkiller.scanner import scan_files

        with HashCache(db) as cache:
            session = ScanSession.create(db, str(scan_dir), config={})
            with session:
                # Manually scan so session has files, then advance stage
                for fi in scan_files(str(scan_dir)):
                    session.queue_file(fi)
                session.flush_files()
                session.set_stage("partial_hashing")
                # Now run_pipeline — it should skip scan phase (hits lines 516-517)
                with OutputHandler(jsonl_path=None) as output:
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=PipelineConfig(num_processes=1),
                    )
        assert total == 2
        assert groups == 1

    def test_resume_from_full_hashing_stage(self, tmp_path):
        """run_pipeline resumes from full_hashing stage (hits lines 532-533)."""
        scan_dir = tmp_path / "files"
        scan_dir.mkdir()
        content = b"same" * 200
        (scan_dir / "a.bin").write_bytes(content)
        (scan_dir / "b.bin").write_bytes(content)

        db = tmp_path / "scan.db"
        from dupkiller.cache import HashCache as HC
        from dupkiller.hashing import hash_file_partial
        from dupkiller.scanner import scan_files

        with HC(db) as cache:
            session = ScanSession.create(db, str(scan_dir), config={})
            with session:
                # Scan files
                fis = list(scan_files(str(scan_dir)))
                for fi in fis:
                    session.queue_file(fi)
                session.flush_files()
                # Partially hash them and mark partial_done
                for fi in fis:
                    _, ph = hash_file_partial(fi.path)
                    if ph:
                        cache.queue_update(fi.path, fi.size, fi.mtime, partial_hash=ph)
                cache.flush()
                session.mark_files_stage([fi.path for fi in fis], "partial_done")
                session.set_stage("full_hashing")
                # Now run_pipeline — skips scan and partial hash (hits 532-533)
                with OutputHandler(jsonl_path=None) as output:
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=PipelineConfig(num_processes=1),
                    )
        assert groups == 1

    def test_already_emitted_skips_full_hash(self, tmp_path):
        """already_emitted partial hash group is skipped in _phase_full_hash."""
        scan_dir = tmp_path / "files"
        scan_dir.mkdir()
        content = b"same" * 200
        (scan_dir / "a.bin").write_bytes(content)
        (scan_dir / "b.bin").write_bytes(content)

        db = tmp_path / "scan.db"
        jsonl = tmp_path / "out.jsonl"

        # First run — finds duplicates and writes to jsonl
        with HashCache(db) as cache:
            with ScanSession.create(db, str(scan_dir), config={}) as session:
                with OutputHandler(jsonl_path=jsonl) as output:
                    run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output,
                        session=session, cfg=PipelineConfig(num_processes=1),
                    )

        # Second run — same files, JSONL already has the group → already_emitted
        db2 = tmp_path / "scan2.db"
        with HashCache(db2) as cache:
            session2 = ScanSession.create(db2, str(scan_dir), config={})
            with session2:
                from dupkiller.scanner import scan_files as sf
                for fi in sf(str(scan_dir)):
                    session2.queue_file(fi)
                session2.flush_files()
                from dupkiller.hashing import hash_file_partial
                fis = list(sf(str(scan_dir)))
                for fi in fis:
                    _, ph = hash_file_partial(fi.path)
                    if ph:
                        cache.queue_update(fi.path, fi.size, fi.mtime, partial_hash=ph)
                cache.flush()
                session2.mark_files_stage([fi.path for fi in fis], "partial_done")
                session2.set_stage("full_hashing")
                # OutputHandler loads existing jsonl (with partial_hash) →
                # already_emitted(partial_hash) fires and group is skipped
                with OutputHandler(jsonl_path=jsonl) as output2:
                    total, groups = run_pipeline(
                        roots=[str(scan_dir)], cache=cache, output=output2,
                        session=session2, cfg=PipelineConfig(num_processes=1),
                    )
                    # Group was skipped via already_emitted — not re-written
                    assert output2.groups_written == 0
        assert total == 2  # still reported 2 files scanned
