# Quality checks

One command runs every check in this workspace. Run it after every change, before you call the change complete:

```powershell
python scripts/quality_check.py
```

While iterating, use the fast path instead. It looks only at files changed against the base branch plus the working tree, so it finishes in a fraction of the time:

```powershell
python scripts/quality_check.py --fast
```

`--fast` is a pre-check, never a substitute for the full gate. It compiles the changed files, runs the blocking flake8 correctness codes on them, runs mypy over the changed `app/` files as advisory signal, and runs only the unit tests that reference the changed modules (matched by the module's dotted path, which every `patch("app.x.y....")` target provides) with changed-file coverage. It skips the aggregate gate and the per-file contract, so a green `--fast` says nothing about whether the gate passes.

## Stage order

The gate runs all five stages even when one fails, so a single run reports the whole picture, and exits non-zero if any blocking stage failed. Build and coverage run first, because a lint finding is worth nothing while the application does not import.

| # | Stage | What runs | Blocking |
| --- | --- | --- | :---: |
| 1 | `build` | `compileall` over `app` and `scripts`, then the ASGI app constructs and still serves `/health` | yes |
| 2 | `coverage` | the unit suite, then `scripts/check_coverage.py` and `coverage report` at 90% | yes |
| 3 | `typecheck` | `mypy` against its `quality-baseline.json` budget | yes |
| 4 | `lint` | `flake8` correctness codes (blocking) and the full style run (advisory) | yes |
| 5 | `schema` | every model emits valid DDL and applies cleanly | yes |

Run a single stage with `--stage`, repeatable: `python scripts/quality_check.py --stage build --stage coverage`. The stage order is fixed regardless of flag order.

The wheel build is off by default because CI already produces it; add `--wheel` to include it. These are the same commands `.github/workflows/pr-quality-gate.yml` runs, so a local pass means the same thing as a CI pass.

## Repository testing contract

- Treat every Python file under `app/**/*.py` as coverage scope. Do not add coverage omissions or pragmas to hide untested application code; empty package files are reported as zero-statement files and remain in the report.
- Every behavior change must include or update deterministic unit tests. Unit tests must mock databases, Redis, Celery, OpenAI, WhatsApp, Chroma, and other network services; live/integration tests belong behind the `integration` or `manual` marker.
- Run the required gate before declaring a change complete:
  ```powershell
  python -m coverage run --branch -m pytest
  python -m coverage json -o coverage.json
  python scripts/check_coverage.py --coverage-file coverage.json --threshold 90
  python -m coverage report --fail-under=90
  ```
  `python scripts/quality_check.py --stage coverage` runs exactly these four commands in order.
- The global pytest timeout is 60 seconds. A file fails the gate when statements, branches, or functions are below 90%. The custom checker is authoritative for per-file metrics; the final report must include its complete per-file table and the aggregate `coverage report` output.
- Never weaken the threshold, shorten the timeout, or exclude an application file to make a test pass. If a module cannot be imported safely, isolate its external side effects behind mocks and test the module's behavior.

## Static analysis budgets

Lint and type checking are ratcheted, not pass/fail: the application predates both gates, so `quality-baseline.json` freezes the finding count measured on the trunk. A check fails when a change pushes its count above the recorded maximum.

- Never raise a `max_findings` value to make a run pass. Fix the finding instead.
- When a change lowers a count, lower `max_findings` in the same commit to lock the improvement in. The runner prints the delta so you can see it.
- Tool versions are pinned in `requirements.txt` because these counts are only reproducible for a fixed version. `black` and `isort` are installed but intentionally not wired into the gate; do not reformat files wholesale as part of an unrelated change.

## Database schema

This repository has no Alembic setup, so there is nothing to migrate in the usual sense. `app/database.py:init_database()` calls `Base.metadata.create_all()` at boot, which means "pending schema migrations" here are the model tables the next boot will create.

- The `schema` stage applies the full `Base.metadata` to an in-memory SQLite database. It proves every table and column emits valid DDL, and it touches no real database.
- `python scripts/quality_check.py --stage schema --apply-schema` reconciles the database that `DATABASE_MODE` points at: it reports the tables and columns that models declare but the database lacks, then runs `create_all`. It is opt-in because it writes to a real database.
- `create_all` only ever adds missing tables. It never alters an existing table, so a changed or removed column needs manual DDL. The `--apply-schema` output flags that case rather than reporting a clean pass.
- If Alembic is ever adopted, drop an `alembic.ini` in the repository root: the `schema` stage detects it and switches to `alembic check`, or `alembic upgrade head` under `--apply-schema`.
