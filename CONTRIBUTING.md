# Contributing to dupkiller

Thank you for considering a contribution!  This document covers how to set up
a development environment, run tests, and submit changes.

## Development setup

```bash
git clone https://github.com/example/dupkiller
cd dupkiller
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Optional — install BLAKE3 for faster hashing during local testing:

```bash
pip install blake3
```

## Running tests

```bash
# All tests with coverage report
pytest --cov=dupkiller --cov-report=term-missing

# Single module
pytest tests/test_hashing.py -v
```

Coverage must remain at 100%.  New code must be accompanied by tests.

## Linting and type checking

```bash
ruff check dupkiller tests   # lint
mypy dupkiller               # type check
```

Both must pass with zero errors before opening a pull request.

## Commit style

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

<body — explain what and why, not how>
```

Common types: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`.

Examples:

```
feat(hashing): add xxHash fallback for environments without blake3

perf(cache): replace OFFSET pagination with keyset cursor

fix(scanner): skip broken symlinks instead of raising PermissionError
```

- Keep the summary under 72 characters.
- Use the body to explain *why*, not just *what*.
- Reference issues as `Closes #123` at the end of the body when applicable.

## Pull request checklist

- [ ] All tests pass: `pytest --cov-fail-under=100`
- [ ] No lint errors: `ruff check dupkiller tests`
- [ ] No type errors: `mypy dupkiller`
- [ ] CHANGELOG.md and CHANGELOG.ru.md updated under `[Unreleased]`
- [ ] New public functions/classes have Google-style docstrings

## Reporting issues

Please include:

1. dupkiller version (`dupkiller --version`)
2. Python version and OS
3. Minimal reproduction steps
4. Full error output (with `--verbose` if applicable)

## License

By contributing you agree that your changes will be released under the
[Apache 2.0 License](LICENSE).
