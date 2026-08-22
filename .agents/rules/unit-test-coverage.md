# Antigravity Unit Test Code Coverage & Quality Rules

## Mandatory Quality Contract

Every AI coding agent operating in this repository must strictly adhere to the following rules:

1. **Always Check Coverage On Every Change**:
   - While iterating on changes, run:
     ```powershell
     python scripts/quality_check.py --fast
     ```
   - Before declaring ANY task or change complete, always execute the full quality gate:
     ```powershell
     python scripts/quality_check.py
     ```

2. **90% Unit Test Code Coverage Per File (No Skipping Any File)**:
   - Scope is 100% of files under `app/**/*.py`. No application file may be skipped, omitted, or excluded.
   - Every file must achieve >=90.0% across ALL parameters individually:
     - **Statements / Lines**: >= 90%
     - **Branches**: >= 90%
     - **Functions**: >= 90%
   - If any parameter on any file is below 90%, `scripts/check_coverage.py` throws an error (exit code 1) and blocks the build.

3. **Global Test Timeout**:
   - All unit tests run under a mandatory global timeout of 60 seconds (`pytest-timeout`).
   - Never shorten or remove the global timeout.

4. **Deterministic Unit Tests & Mocking**:
   - Unit tests must be completely deterministic and offline.
   - Mock all external dependencies: databases, Redis, Celery, OpenAI, WhatsApp, ChromaDB, filesystem APIs, and network/HTTP calls.
   - Any live/integration tests must be explicitly marked with `@pytest.mark.integration` or `@pytest.mark.manual`.

5. **Direct Coverage Verification Commands**:
   ```powershell
   python -m coverage run --branch -m pytest
   python -m coverage json -o coverage.json
   python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
   python -m coverage report --fail-under=90
   ```

6. **Completion Report Requirement**:
   - Every completion report must print the complete per-file coverage table and the aggregate `coverage report` output.
