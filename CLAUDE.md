# Claude Code / Anthropic Claude Instructions

## Testing & Quality Gate Contract

Follow `AGENTS.md` before changing code.

### 1. Verification on Every Change
Whenever modifying any code:
- Fast check while iterating:
  ```powershell
  python scripts/quality_check.py --fast
  ```
- Full quality gate before declaring any task complete:
  ```powershell
  python scripts/quality_check.py
  ```

### 2. 90% Per-File Unit Test Coverage (No Skipping Any File)
- Scope: 100% of files under `app/**/*.py`.
- Benchmark: Every file must achieve >=90.0% coverage across all parameters individually:
  - **Statements / Lines**: >= 90%
  - **Branches**: >= 90%
  - **Functions**: >= 90%
- If any file falls below 90% on any metric, `scripts/check_coverage.py` throws an error (exit code 1) and fails the build.

### 3. Global Test Timeout
- All unit tests run under a global timeout of 60 seconds (`pytest-timeout`).

### 4. Direct Coverage Commands
```powershell
python -m coverage run --branch -m pytest
python -m coverage json -o coverage.json
python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
python -m coverage report --fail-under=90
```

### 5. Mocking & Output Reporting
- Mock external services (database, Redis, Celery, OpenAI, WhatsApp, ChromaDB, HTTP).
- Always include the overall coverage summary and complete per-file coverage table in the final output.
