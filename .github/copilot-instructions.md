# GitHub Copilot & AI Coding Agent Instructions

## Mandatory Quality & Coverage Contract

Follow `AGENTS.md` before changing code.

### 1. Test Verification On Every Change
- While iterating:
  ```powershell
  python scripts/quality_check.py --fast
  ```
- Before calling any change complete:
  ```powershell
  python scripts/quality_check.py
  ```

### 2. 90% Unit Test Code Coverage Per File
- Scope: 100% of files under `app/**/*.py`. Never skip or omit any file.
- Threshold: Every single file must achieve >=90.0% coverage across all parameters individually:
  - **Statements / Lines**: >= 90%
  - **Branches**: >= 90%
  - **Functions**: >= 90%
- If any parameter falls below 90% on any file, `scripts/check_coverage.py` throws an error (exit code 1).

### 3. Global Test Timeout
- All unit tests run under a global 60-second timeout (`pytest-timeout`).

### 4. Direct Coverage Commands
```powershell
python -m coverage run --branch -m pytest
python -m coverage json -o coverage.json
python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
python -m coverage report --fail-under=90
```

### 5. Reporting
- Always print the aggregate summary and full per-file coverage table in completion reports.
