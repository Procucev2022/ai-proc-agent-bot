# GitHub Copilot instructions

Follow `AGENTS.md` as the repository-wide quality and testing contract.

## Run the checks after every change

`python scripts/quality_check.py` is the single command for the whole workspace. It runs five stages in a fixed order and reports all of them even when one fails, exiting non-zero if any blocking stage failed:

1. `build` — byte-compile `app` and `scripts`, then confirm the ASGI app constructs and still serves `/health`
2. `coverage` — the unit suite, `scripts/check_coverage.py`, and `coverage report` at 90%
3. `typecheck` — mypy against its `quality-baseline.json` budget
4. `lint` — flake8 correctness codes (blocking) and the full style run (advisory)
5. `schema` — every model emits valid DDL and applies cleanly

Build and coverage run first: a lint finding is worth nothing while the application does not import. These are the same commands `.github/workflows/pr-quality-gate.yml` runs, so a local pass means the same thing as a CI pass.

While iterating, `python scripts/quality_check.py --fast` checks only the files changed against the base branch plus the working tree — it compiles them, runs the blocking flake8 codes, runs mypy as advisory signal, and runs the unit tests that reference the changed modules with changed-file coverage. It does not run the aggregate gate or the per-file contract, so the full run is still required before you call a change done. Add `--base origin/develop` when the branch targets `develop`.

## Testing and coverage

For every Python change, add deterministic unit tests and run the coverage gate. Coverage scope is all `app/**/*.py`; do not omit files or use coverage pragmas to conceal untested application code. Enforce at least 90% per file for statements, branches, and functions, plus 90% aggregate coverage. Pytest has a 60-second global timeout. Mock all external databases, Redis, Celery, OpenAI, WhatsApp, Chroma, and HTTP integrations. Mark live tests `integration` or `manual` so they are not part of the deterministic unit gate. Report the complete aggregate and per-file coverage results.

## Static analysis budgets

Lint and mypy are ratcheted, not pass/fail: `quality-baseline.json` freezes the finding count measured on the trunk. Fix new findings; never raise a `max_findings` value to get a pass. When a change lowers a count, lower `max_findings` in the same commit to lock the improvement in.

## Database schema

There is no Alembic setup. `app/database.py:init_database()` calls `Base.metadata.create_all()` at boot, so pending schema work means the model tables the next boot will create. The `schema` stage validates the full metadata against an in-memory SQLite database and touches nothing real. `--apply-schema` reconciles the database `DATABASE_MODE` points at and is opt-in because it writes to it; note that `create_all` only adds missing tables and cannot alter an existing column.
