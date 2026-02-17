# Contributing

Thanks for your interest in contributing to `rms-scan`!

## Development setup

Prereqs: Python 3.10+

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Run the CLI locally:

```bash
python -m rms_scan --help
```

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## Code style

This project aims for a small, readable codebase.

- Prefer clear names and small functions.
- Keep changes minimal and focused.
- Format/lint with Ruff (optional but recommended):

```bash
python -m ruff check .
python -m ruff format .
```

## Proposing changes

1. Open an issue describing the problem or proposed improvement.
2. Submit a PR with a clear description and rationale.
3. Include tests for behavior changes when practical.
