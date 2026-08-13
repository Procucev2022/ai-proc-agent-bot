---
name: unit-test-coverage
description: Maintains deterministic Python tests and enforces the repository coverage contract.
---

# Unit Test Coverage Agent

Before changing application code, inspect its callers and existing tests. Add deterministic unit tests for every changed path and mock external databases, Redis, Celery, OpenAI, WhatsApp, Chroma, filesystem services, and HTTP clients. Keep integration and manual tests explicitly marked.

The measured source is every `app/**/*.py` file. Run quality checks after every change (Build and Unit Test coverage first, followed by type check, lint, and database migrations):

```powershell
# Fast check (changed files only):
python scripts/quality_check.py --fast

# Full quality gate (all checks in required order):
python scripts/quality_check.py
```

Direct coverage gate commands:
```powershell
python -m coverage run --branch -m pytest
python -m coverage json -o coverage.json
python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
python -m coverage report --fail-under=90
```

Do not skip files, lower the 90% threshold, or use exclusions to conceal untested behavior. If a test exposes an import-time side effect, isolate it with fixtures/mocks and preserve production behavior. Completion reports must print aggregate and complete per-file coverage.

