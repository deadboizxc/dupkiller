# dupkiller v1.0.0 - Initial Release 🎉

High-performance duplicate file finder with BLAKE3 hashing, resumable scans, and 100% test coverage.

## ✨ Features

### Core Functionality
- **⚡ BLAKE3 Hashing** — Ultra-fast content hashing (~3 GB/s with SIMD)
- **🔄 Resumable Scans** — SQLite-backed checkpointing, interrupt and resume anytime
- **💾 Memory Efficient** — Keyset pagination handles millions of files with O(1) memory
- **🎯 Smart Pre-filtering** — Partial hashing (head+mid+tail) reduces full reads by 90%+

### Platform Support
- **🐧 Linux** — Full support with HDD detection and I/O optimization
- **🍎 macOS** — Full support with cross-platform disk monitoring
- **🪟 Windows** — Partial support (core functionality works, tests in progress)

### Developer Experience
- **📊 100% Test Coverage** — 336 tests across all modules
- **🔒 Type Safe** — Strict mypy with no `Any` types
- **📝 Bilingual Docs** — Complete English and Russian documentation
- **🚀 CI/CD Ready** — Automated testing and PyPI publishing

## 📦 Installation

```bash
pip install dupkiller
```

## 🚀 Quick Start

```bash
# Scan directory for duplicates
dupkiller scan /path/to/directory --output duplicates.jsonl

# List duplicate groups
dupkiller list duplicates.jsonl

# Delete duplicates (keep oldest file)
dupkiller delete duplicates.jsonl --keep oldest

# Show cache statistics
dupkiller stats
```

## 🎨 Advanced Features

### Five Keep Strategies
- `oldest` — Keep file with earliest modification time
- `newest` — Keep most recently modified file
- `shortest-name` — Keep file with shortest path
- `longest-name` — Keep file with longest path
- `lexicographic` — Keep first in alphabetical order

### Performance Optimizations
- Adaptive chunk sizing (256KB - 16MB)
- Bounded worker pools prevent memory overflow
- HDD-aware inode sorting reduces seeks
- I/O throttling prevents disk saturation

### Export Formats
- **JSONL** — Machine-readable duplicate groups
- **CSV** — Spreadsheet-friendly format
- **HTML** — Styled report with sortable columns

## 📚 Documentation

- **README**: [English](https://github.com/deadboizxc/dupkiller/blob/main/README.md) | [Russian](https://github.com/deadboizxc/dupkiller/blob/main/README.ru.md)
- **CHANGELOG**: [English](https://github.com/deadboizxc/dupkiller/blob/main/CHANGELOG.md) | [Russian](https://github.com/deadboizxc/dupkiller/blob/main/CHANGELOG.ru.md)
- **Contributing**: [English](https://github.com/deadboizxc/dupkiller/blob/main/CONTRIBUTING.md) | [Russian](https://github.com/deadboizxc/dupkiller/blob/main/CONTRIBUTING.ru.md)

## 🔧 Technical Stack

- **Language**: Python 3.11+
- **CLI**: Click
- **UI**: Rich (progress bars, tables)
- **Hashing**: BLAKE3/BLAKE2b
- **Storage**: SQLite with WAL mode
- **Testing**: pytest with 100% coverage
- **Linting**: Ruff (modern, fast)
- **Type Checking**: mypy (strict mode)

## 📊 Performance Benchmarks

- **Scan Speed**: ~50K files/sec on NVMe, ~5K files/sec on HDD
- **Memory Usage**: O(1) regardless of dataset size
- **Hash Throughput**: Up to 3 GB/s with BLAKE3

## 🙏 Acknowledgments

Built with modern Python best practices, inspired by projects like `rdfind`, `fdupes`, and `jdupes`.

## 📜 License

MIT License - see [LICENSE](https://github.com/deadboizxc/dupkiller/blob/main/LICENSE) for details.

---

**Full Changelog**: Initial release
