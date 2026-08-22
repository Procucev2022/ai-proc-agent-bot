---
inclusion: always
---

# Quality checks after every change

Verify your own work. A change is not complete until the quality gate passes, and "the command exited without an error" is not the same as "the gate passed" — read the summary table.

## While iterating

After each edit, run the fast path. It only looks at files changed against the base branch plus the working tree:

```powershell
python scripts/quality_check.py --fast
```

Use `--base <ref>` when the branch targets something other than `origin/main`, for example `--base origin/develop`.

## Before reporting a change complete

Run the full gate. Never skip it because `--fast` was green: `--fast` does not run the aggregate coverage gate or the per-file 90% contract.

```powershell
python scripts/quality_check.py
```

Stages run in this fixed order, and all of them run even when one fails so a single invocation reports everything: `build`, `coverage`, `typecheck`, `lint`, `schema`. Build and coverage come first on purpose — lint findings are noise while the app does not import. Narrow to one stage with `--stage build` (repeatable) when you are iterating on a single failure.

## 90% Unit Test Code Coverage Contract

- The scope is 100% of files under `app/**/*.py`. Do not skip or omit any file.
- Every file must achieve >=90.0% coverage across all parameters individually:
  - Statements / Lines: >= 90%
  - Branches: >= 90%
  - Functions: >= 90%
- If any parameter on any file is below 90%, `scripts/check_coverage.py` throws an error (exit code 1) and blocks.
- Global pytest timeout is 60 seconds (`pytest-timeout`).
- Include the complete per-file coverage table and the aggregate `coverage report` output in your completion report.
- Fix findings. Do not weaken the 90% threshold, shorten the 60-second pytest timeout, add a coverage omission or pragma, or raise a `max_findings` budget in `quality-baseline.json` to get a pass.
- When a change lowers a lint or mypy count below its budget, lower `max_findings` in the same commit. The runner prints the delta.
- The `schema` stage is offline and safe. Only add `--apply-schema` when you intend to write to the database `DATABASE_MODE` points at, and say so first.

`AGENTS.md` holds the full repository contract.
