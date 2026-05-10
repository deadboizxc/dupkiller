# CHANGELOG

Format: [Conventional Commits](https://www.conventionalcommits.org/). Versioning — SemVer.

---

## [Unreleased]

---

## [1.0.0] — 2026-05-11

- feat(hashing): BLAKE3 with blake2b fallback, adaptive chunk size 256 KB–16 MB
- feat(hashing): 3-point prehash sampling (head+mid+tail 512 KB) for files >50 MB
- feat(hashing): `posix_fadvise` SEQUENTIAL + DONTNEED hints, bandwidth throttle
- feat(workers): `BoundedExecutor` — semaphore on in-flight futures, prevents OOM
- feat(workers): `LargeFileSemaphore` — at most 2 concurrent reads of files >100 MB
- feat(workers): ThreadPoolExecutor (I/O) + ProcessPoolExecutor (CPU), inode sort in HDD mode
- feat(workers): `fut.result(timeout=7200)` guard against hangs on NFS
- feat(pipeline): multi-root scan, hardlink detection, accurate ETA, skip counters
- feat(pipeline): resume in `full_hashing` stage via `partial_hash` in JSONL records
- feat(cache): WAL, batch insert, platform-aware mtime tolerance, keyset pagination
- feat(cache): `clean_missing_files`, `iter_duplicate_groups`, streaming `save_scan_results`
- feat(checkpoint): `ScanSession` with PID-lock, resume, schema versioning + v1→v2 migration
- feat(checkpoint): keyset pagination in `iter_partial/full_hash_groups` for 10 M+ rows
- feat(scanner): exclude patterns, size filters, symlink cycle guard
- feat(dedupe): keep newest/oldest/first/shortest/longest strategies, `--interactive`, `--dry-run`
- feat(cli): `scan`, `list`, `delete`, `export`, `stats`, `cache clean/stats`
- feat(cli): streaming export csv/txt/jsonl, streaming delete, `--ionice`, `--max-throughput`
- feat(disk): Linux HDD detection via /sys/block/, macOS via diskutil, Windows via PowerShell
- feat(disk): DiskMonitor — Linux /proc/diskstats + psutil for macOS/Windows
- feat(ci): GitHub Actions matrix ubuntu/macos/windows × Python 3.11/3.12
- feat(ci): automated PyPI and TestPyPI publish on version tag via OIDC trusted publishing
- build: Apache 2.0 license, CONTRIBUTING guide (EN + RU), psutil pinned in requirements.txt
