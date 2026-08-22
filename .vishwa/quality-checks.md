# Vishwa Quality & Test Coverage Rules

## Quality Gate Contract

Follow `AGENTS.md` before changing code.

1. **Check Coverage On Every Change**:
   - While iterating: `python scripts/quality_check.py --fast`
   - Before completing tasks: `python scripts/quality_check.py`

2. **90% Unit Test Code Coverage Per File**:
   - Scope is 100% of files under `app/**/*.py`. Do not skip any file.
   - Every file must achieve >=90.0% coverage across all parameters individually:
     - Statements / Lines: >= 90%
     - Branches: >= 90%
     - Functions: >= 90%
   - If any parameter on any file is below 90%, `scripts/check_coverage.py` throws an error (exit code 1).

3. **Global Timeout**:
   - All unit tests run under a global 60-second timeout (`pytest-timeout`).

4. **Direct Verification Commands**:
   ```powershell
   python -m coverage run --branch -m pytest
   python -m coverage json -o coverage.json
   python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
   python -m coverage report --fail-under=90
   ```

5. **Reporting**:
   - Include complete per-file coverage output and aggregate summary in completion reports.
