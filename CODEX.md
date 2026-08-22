# OpenAI Codex Instructions

## Unit Test Coverage & Quality Gate Contract

Follow `AGENTS.md` before changing code.

### 1. Mandatory Quality Gate on Every Change
- Check changed files while iterating:
  ```powershell
  python scripts/quality_check.py --fast
  ```
- Full quality gate before declaring change complete:
  ```powershell
  python scripts/quality_check.py
  ```

### 2. 90% Per-File Unit Test Code Coverage
- Coverage scope is 100% of files under `app/**/*.py`. Do not skip any file.
- Threshold requirement: Every file must achieve >=90.0% coverage across all parameters individually:
  - Statements / Lines: >= 90%
  - Branches: >= 90%
  - Functions: >= 90%
- If per-file coverage on any metric falls below 90%, `scripts/check_coverage.py` throws an error (exit code 1).

### 3. Global Test Timeout
- All unit tests must adhere to a global 60-second timeout (`pytest-timeout`).

### 4. Direct Coverage Commands
```powershell
python -m coverage run --branch -m pytest
python -m coverage json -o coverage.json
python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
python -m coverage report --fail-under=90
```

### 5. Deterministic Unit Tests & Mocking
- Unit tests must be deterministic and offline.
- Mock all external dependencies (databases, Redis, Celery, OpenAI, WhatsApp, ChromaDB, HTTP).
- Print the overall and per-file coverage tables in completion summaries.
