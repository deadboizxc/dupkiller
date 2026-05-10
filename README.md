# dupkiller

High-performance duplicate file finder with multi-core hashing, resume support,
and streaming output for datasets of any size.

## Features

- **Fast hashing** — BLAKE3 (auto-detected) with blake2b fallback; 3-point
  sampling (head + mid + tail, 512 KB each) for files > 50 MB; adaptive chunk
  sizes 256 KB – 16 MB
- **Multi-core** — ThreadPoolExecutor for I/O-bound partial hashing,
  ProcessPoolExecutor for CPU-bound full hashing; `BoundedExecutor` semaphore
  prevents queue unbounded growth and OOM on 1 M+ candidates
- **HDD-aware** — inode-ordered reads, reduced concurrency, `--ionice` idle
  class; auto-detected via `/sys/block/` on Linux
- **Resume** — SQLite-backed `ScanSession` with PID-lock and stage tracking;
  interrupted scans resume from the exact stage they stopped at
- **1 TB+ safe** — all iterators use keyset pagination; streaming output and
  streaming delete keep memory usage O(1) regardless of dataset size
- **Hard-link detection** — files sharing `(device, inode)` are identified and
  excluded from duplicate counts so you never delete a hard-linked copy
- **Rich CLI** — live progress bars, statistics tables, delta between scans,
  interactive per-group delete, five export formats
- **100% test coverage** — unit + integration tests across all modules

## Requirements

- Python 3.11+
- `click >= 8.1`
- `rich >= 13.0`
- `blake3` (optional, ~5× faster): `pip install blake3`

## Installation

```bash
pip install dupkiller
```

Or from source:

```bash
git clone https://github.com/example/dupkiller
cd dupkiller
pip install -e .
```

## Quick start

```bash
# Scan one or more directories
dupkiller scan ~/Downloads ~/Documents

# List duplicate groups
dupkiller list

# Summary statistics
dupkiller stats

# Dry-run: show what would be deleted
dupkiller delete --keep newest --dry-run

# Delete with confirmation prompt
dupkiller delete --keep newest --yes

# Delete file-by-file with interactive prompts
dupkiller delete --keep newest --interactive

# Export to CSV
dupkiller export --format csv --output duplicates.csv
```

## Commands

### `scan`

Scan one or more directory trees and store results.

```
dupkiller scan [OPTIONS] PATH [PATH ...]

Options:
  -t, --threads N         I/O threads for partial hashing  [auto: 2 HDD / 16 SSD]
  -p, --processes N       CPU processes for full hashing   [default: cpu_count()]
  --hdd / --ssd           Force disk mode (auto-detected on Linux)
  --max-throughput RATE   Throttle reads, e.g. 50MB or 200KB
  --ionice                Run at idle I/O priority (Linux; requires ionice)
  --min-size N            Skip files smaller than N bytes  [default: 1]
  --max-size N            Skip files larger than N bytes
  --exclude PATTERN       Glob pattern to exclude (repeatable)
  --follow-symlinks       Follow symbolic links
  --resume                Continue the last interrupted scan
  --output-dir DIR        Directory for duplicates.jsonl and scan.log
  --db PATH               SQLite database path             [default: ~/.dupkiller/cache.db]
```

### `list`

Show duplicate groups from the most recent scan.

```
dupkiller list [OPTIONS]

Options:
  --scan-id ID    Show results from a specific scan
  --min-size N    Filter groups whose files are at least N bytes
  --limit N       Maximum number of groups to display     [default: 50]
```

### `stats`

Print summary statistics; compare two scans to see what changed.

```
dupkiller stats [OPTIONS]

Options:
  --scan-id ID              Show stats for a specific scan
  --compare-with ID         Show delta vs another scan
```

### `delete`

Remove duplicate files; keep one copy per group according to a strategy.

```
dupkiller delete [OPTIONS]

Options:
  --keep STRATEGY   Which copy to keep: newest | oldest | first | shortest | longest
                    [default: newest]
  --dry-run         Print what would be deleted without touching anything
  --yes             Skip the bulk confirmation prompt
  --interactive     Confirm each duplicate group individually
  --scan-id ID      Operate on a specific scan
```

### `export`

Export duplicate groups to a file.

```
dupkiller export [OPTIONS]

Options:
  --format FMT    json | csv | txt | jsonl | html  [default: json]
  --output PATH   Write to file instead of stdout
  --scan-id ID    Export a specific scan
```

### `cache clean`

Remove stale cache entries for files that no longer exist.

```
dupkiller cache clean [--db PATH]
```

### `cache stats`

Print database size and row counts.

```
dupkiller cache stats [--db PATH]
```

## Keep strategies

| Strategy   | Description                                      |
|------------|--------------------------------------------------|
| `newest`   | Keep the file with the most recent modification time |
| `oldest`   | Keep the file with the oldest modification time  |
| `first`    | Keep the lexicographically first path            |
| `shortest` | Keep the path with fewest characters             |
| `longest`  | Keep the path with most characters               |

## Environment variables

| Variable       | Default                    | Description               |
|----------------|----------------------------|---------------------------|
| `DUPKILLER_DB` | `~/.dupkiller/cache.db`    | SQLite database path      |

## Architecture

```
CLI (click)
  └── run_pipeline()
        ├── Phase 1 — scan_files()       ThreadPoolExecutor, exclude filters
        ├── Phase 2 — partial hashing    ThreadPoolExecutor, LargeFileSemaphore
        ├── Phase 3 — full hashing       ProcessPoolExecutor, BoundedExecutor
        └── Phase 4 — emit groups        OutputHandler → duplicates.jsonl
HashCache  (SQLite WAL, batch insert, keyset pagination)
ScanSession (PID-lock, stage machine, resume)
```

All four phases are checkpointed in SQLite. A scan interrupted at any stage
resumes from that stage without re-reading already-processed files.

## Performance

| Scenario                    | Typical throughput          |
|-----------------------------|-----------------------------|
| SSD + BLAKE3, full hash     | ~3 GB/s                     |
| SSD + blake2b, full hash    | ~700 MB/s                   |
| HDD, inode-ordered reads    | ~100–150 MB/s               |
| Prehash (50 MB+ files)      | skips up to 99 % of I/O     |

Tips:
- Install `blake3` for the fastest hashing: `pip install blake3`
- On HDDs use `--hdd` (or rely on auto-detection) to sort reads by inode
- Use `--max-throughput` to throttle dupkiller on a busy server
- For network filesystems, `--processes 1` avoids fork overhead on NFS

## Development

```bash
pip install -e ".[dev]"

# Run tests
pytest

# Tests with coverage report
pytest --cov=dupkiller --cov-report=term-missing

# Lint
ruff check dupkiller tests

# Type check
mypy dupkiller
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
