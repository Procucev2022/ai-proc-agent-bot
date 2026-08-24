---
description: Mandatory Quality Check Configuration and Protocol for Workspace Changes
globs: app/**/*.py, tests/**/*.py, scripts/**/*.py
---

# Quality Check Configuration and Protocol

After every code change, AI coding agents MUST run the workspace quality check commands to verify build issues, typecheck issues, lint issues, unit test code coverage, and pending database schema migrations.

## Global Workspace Commands

In single or multi-project workspaces, use global commands to check all issues in the workspace.

- **Full Quality Check Gate**:
  ```powershell
  python scripts/quality_check.py
  ```

- **Fast Change-Only Check** (runs compile, lint, advisory typecheck, and related unit tests on changed files only):
  ```powershell
  python scripts/quality_check.py --fast
  ```

## Mandatory Execution Order

Checks MUST be executed in the following order:
1. **Build & Unit Test Coverage (Checked FIRST)**:
   - Byte-compile modules and test ASGI app construction (`app.main:app`).
   - Run unit test suite and enforce >=90% per-file coverage for statements, branches, and functions across all `app/**/*.py` files.
2. **Type Check**:
   - mypy static type checking against the repository budget (`quality-baseline.json`).
3. **Lint**:
   - flake8 correctness and style verification.
4. **Database Schema & Migrations**:
   - Validate SQLAlchemy model tables against DDL (`python scripts/quality_check.py`).
   - Pass `--apply-schema` when pending migrations/table creation need to be applied to the configured database.
