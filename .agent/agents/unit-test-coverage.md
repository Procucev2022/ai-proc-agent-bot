---
name: unit-test-coverage
description: Enforces 90% per-file unit test code coverage across all metrics and runs quality gate checks on every change.
---

# Unit Test Coverage Agent

Before changing application code, inspect its callers and existing tests. Add deterministic unit tests for every changed path and mock external databases, Redis, Celery, OpenAI, WhatsApp, Chroma, filesystem services, and HTTP clients. Keep integration and manual tests explicitly marked.

## Mandatory Quality Contract

1. **Test Coverage Scope**:
   - Scope is 100% of files under `app/**/*.py`. Do not skip or omit any file.
   - Every file must achieve >=90.0% coverage across all parameters individually:
     - Statements / Lines: >= 90%
     - Branches: >= 90%
     - Functions: >= 90%
   - If any parameter on any file is below 90%, `scripts/check_coverage.py` throws an error (exit code 1).

2. **Global Timeout**:
   - All unit tests run under a global 60-second timeout (`pytest-timeout`).

3. **Check Coverage on Every Change**:
   - While iterating:
     ```powershell
     python scripts/quality_check.py --fast
     ```
   - Before declaring completion:
     ```powershell
     python scripts/quality_check.py
     ```

4. **Direct Coverage Commands**:
   ```powershell
   python -m coverage run --branch -m pytest
   python -m coverage json -o coverage.json
   python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
   python -m coverage report --fail-under=90
   ```

5. **Reporting**:
   - Completion reports must print the aggregate coverage and the complete per-file coverage table for every application file without skipping.
