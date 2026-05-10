# ЖУРНАЛ ИЗМЕНЕНИЙ

Формат: [Conventional Commits](https://www.conventionalcommits.org/). Версии — SemVer.

---

## [Unreleased]

---

## [1.0.0] — 2026-05-11

- feat(hashing): BLAKE3 с fallback на blake2b, адаптивный chunk size 256 КБ–16 МБ
- feat(hashing): 3-точечное сэмплирование (начало+середина+конец, 512 КБ) для файлов >50 МБ
- feat(hashing): `posix_fadvise` SEQUENTIAL + DONTNEED hints, ограничение пропускной способности
- feat(workers): `BoundedExecutor` — семафор на in-flight futures, предотвращает OOM
- feat(workers): `LargeFileSemaphore` — не более 2 одновременных чтений файлов >100 МБ
- feat(workers): ThreadPoolExecutor (I/O) + ProcessPoolExecutor (CPU), inode-сортировка в HDD-режиме
- feat(workers): `fut.result(timeout=7200)` — защита от зависания на NFS
- feat(pipeline): сканирование нескольких корней, обнаружение hardlink, точный ETA, счётчики пропусков
- feat(pipeline): возобновление на стадии `full_hashing` через поле `partial_hash` в JSONL-записях
- feat(cache): WAL, batch insert, платформо-зависимая точность mtime, keyset-пагинация
- feat(cache): `clean_missing_files`, `iter_duplicate_groups`, потоковый `save_scan_results`
- feat(checkpoint): `ScanSession` с PID-блокировкой, resume, версионирование схемы + миграция v1→v2
- feat(checkpoint): keyset-пагинация в `iter_partial/full_hash_groups` для 10 М+ строк
- feat(scanner): шаблоны исключений, фильтры по размеру, защита от симлинк-циклов
- feat(dedupe): стратегии keep newest/oldest/first/shortest/longest, `--interactive`, `--dry-run`
- feat(cli): `scan`, `list`, `delete`, `export`, `stats`, `cache clean/stats`
- feat(cli): потоковый экспорт csv/txt/jsonl, потоковое удаление, `--ionice`, `--max-throughput`
- feat(disk): определение HDD на Linux через /sys/block/, macOS через diskutil, Windows через PowerShell
- feat(disk): DiskMonitor — Linux /proc/diskstats + psutil для macOS/Windows
- feat(ci): матрица GitHub Actions ubuntu/macos/windows × Python 3.11/3.12
- feat(ci): автоматическая публикация на PyPI и TestPyPI по тегу версии через OIDC trusted publishing
- build: лицензия Apache 2.0, руководство CONTRIBUTING (EN + RU), psutil в requirements.txt
