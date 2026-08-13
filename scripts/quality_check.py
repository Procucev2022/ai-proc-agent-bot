"""Run every workspace quality check with one command.

This is the single local entry point for the checks the pull-request gate
enforces (.github/workflows/pr-quality-gate.yml). It runs the same commands in
a fixed order so a local pass means the same thing as a CI pass:

    1. build      byte-compile every module, then construct the ASGI app
    2. coverage   the unit-test suite and the 90% per-file coverage contract
    3. typecheck  mypy against its quality-baseline.json budget
    4. lint       flake8 (correctness + full) against their budgets
    5. schema     the SQLAlchemy schema applies cleanly; migrations if present

Build and coverage run first because they answer "is this code even valid and
tested"; a lint finding is worth nothing while the app does not import.

Every stage runs even when an earlier one fails, so one invocation reports the
whole picture. The exit code is non-zero when any blocking stage failed.

Usage
-----
    python scripts/quality_check.py              # full gate, all stages
    python scripts/quality_check.py --fast       # changed files only, seconds
    python scripts/quality_check.py --stage lint --stage typecheck
    python scripts/quality_check.py --wheel      # also build the distribution
    python scripts/quality_check.py --apply-schema   # touches the real database

The 90% coverage threshold is deliberately not configurable here: the
repository contract in AGENTS.md forbids weakening it.

Exit codes: ``0`` every blocking stage passed, ``1`` a blocking stage failed,
``2`` the runner could not do its job (bad arguments, git unavailable).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
THRESHOLD = "90"
FAST_COVERAGE_FILE = ".coverage.quality-fast"
DEFAULT_BASE_REF = "origin/main"

# Deterministic placeholders that let the build and schema stages import the
# application offline, outside pytest. The values are copied verbatim from
# tests/conftest.py and the env block of .github/workflows/pr-quality-gate.yml:
# tests assert on these exact strings, so they must not diverge.
#
# Only the stages that import the app themselves get these. The test run never
# does: tests/conftest.py owns the test environment through os.environ
# setdefault, and injecting anything here would override it and change results.
OFFLINE_ENV = {
    "AZURE_OPENAI_API_KEY": "unit-test-key",
    "GMT_USERNAME": "unit-test-user",
    "GMT_PHONE": "919876543229",
    "GMT_BASE_URL": "https://gmt.invalid",
    "GMT_PASSWORD": "unit-test-password",
    "GMT_CLIENT_ID": "unit-test-client-id",
    "GMT_CLIENT_SECRET": "unit-test-client-secret",
    "LOCAL_DATABASE_URL": "sqlite:///:memory:",
    "REMOTE_DATABASE_URL": "sqlite:///:memory:",
    "DATABASE_MODE": "local",
    "LICENSE_ENABLED": "false",
    "WHATSAPP_MOCK_MODE": "true",
    "WEBHOOK_HEALTH_MONITORING_ENABLED": "false",
}

# The ASGI build check: the wheel and the Docker images are useless if the app
# does not construct, so this is the part of "build" that catches real breakage.
IMPORT_SMOKE = """
from app.main import app

routes = [getattr(route, "path", "") for route in app.routes]
assert "/health" in routes, f"/health missing from {routes}"
print(f"ASGI app {app.title!r} built with {len(routes)} routes")
"""

# There is no Alembic setup in this repository: app/database.py:init_database()
# calls Base.metadata.create_all() at boot, so "pending schema migrations" means
# "model tables that the next boot will create". This proves the whole metadata
# emits valid DDL without touching any real database.
SCHEMA_VALIDATE = """
from sqlalchemy import create_engine, inspect

from app.models import Base

# In-memory rather than a temp file: on Windows the engine still holds the file
# handle when the temporary directory is cleaned up.
engine = create_engine("sqlite://")
try:
    Base.metadata.create_all(bind=engine)
    created = set(inspect(engine).get_table_names())
    declared = set(Base.metadata.tables)
    missing = sorted(declared - created)
    assert not missing, f"tables declared but not created: {missing}"
    print(f"{len(declared)} model tables emit valid DDL and applied cleanly")
finally:
    engine.dispose()
"""

# Opt-in only: reports the drift between app/models.py and the configured
# database, then creates the missing tables the same way app boot does.
# create_all only ever ADDS tables; it never alters an existing column.
SCHEMA_APPLY = """
from sqlalchemy import create_engine, inspect

import app.database as database
from app.config import get_settings
from app.models import Base

settings = get_settings()
connect_args = getattr(database, "_get_ssl_connect_args", lambda _s: {})(settings)
engine = create_engine(settings.get_database_url(), connect_args=connect_args)
inspector = inspect(engine)
existing = set(inspector.get_table_names())

missing_tables = sorted(set(Base.metadata.tables) - existing)
print(f"database reports {len(existing)} tables; models declare "
      f"{len(Base.metadata.tables)}")
print("pending tables: " + (", ".join(missing_tables) or "none"))

drifted = []
for name in sorted(set(Base.metadata.tables) & existing):
    live = {column["name"] for column in inspector.get_columns(name)}
    declared = {column.name for column in Base.metadata.tables[name].columns}
    for column in sorted(declared - live):
        drifted.append(f"{name}.{column}")
print("columns missing from existing tables: " + (", ".join(drifted) or "none"))

Base.metadata.create_all(bind=engine)
print("create_all applied; missing tables created")
if drifted:
    print("WARNING: create_all cannot add columns to an existing table. The "
          "columns above need manual DDL or a migration.")
"""


@dataclass
class Result:
    """The outcome of one stage."""

    name: str
    title: str
    ok: bool
    seconds: float
    blocking: bool = True
    skipped: bool = False
    note: str = ""

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.ok:
            return "PASS"
        return "FAIL" if self.blocking else "WARN"

    @property
    def failed(self) -> bool:
        return not self.ok and not self.skipped and self.blocking


def banner(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78, flush=True)


def child_env() -> dict[str, str]:
    """The current environment plus offline defaults that never override it.

    Only for stages that import the application outside pytest. Never pass this
    to a pytest run: tests/conftest.py sets the same keys with setdefault, so
    anything set here would win and change what the tests observe.
    """
    env = dict(os.environ)
    for key, value in OFFLINE_ENV.items():
        env.setdefault(key, value)
    return env


def run(command: Sequence[str], env: dict[str, str] | None = None) -> int:
    """Run ``command`` in the repository root, streaming its output.

    ``env=None`` inherits the caller's environment untouched, which is what
    every tool-driving stage wants: the run must behave exactly like invoking
    the tool by hand.
    """
    print(f"$ {' '.join(command)}", flush=True)
    completed = subprocess.run(  # noqa: S603 - fixed commands defined above
        list(command), cwd=REPO_ROOT, env=env
    )
    return completed.returncode


def python(*args: str, env: dict[str, str] | None = None) -> int:
    return run([sys.executable, *args], env=env)


def python_program(source: str, env: dict[str, str] | None = None) -> int:
    """Run an inline program, echoing a readable label instead of the source."""
    label = source.strip().splitlines()[0]
    print(f"$ {Path(sys.executable).name} -c ... # {label}", flush=True)
    completed = subprocess.run(  # noqa: S603 - source is a constant above
        [sys.executable, "-c", source], cwd=REPO_ROOT, env=env
    )
    return completed.returncode


# --------------------------------------------------------------- full stages


def stage_build(options: argparse.Namespace) -> Result:
    start = time.monotonic()
    banner("1/5 BUILD - byte-compile every module, then construct the ASGI app")
    ok = python("-m", "compileall", "-q", "app", "scripts") == 0
    if ok and options.wheel:
        # Off by default: the isolated build environment is slow and CI already
        # produces the wheel. dist/ and build/ are outside app/, so this cannot
        # affect the measured coverage run.
        ok = python("-m", "build", "--wheel", "--outdir", "dist") == 0
    if ok:
        # Imports app.main outside pytest, so it needs the offline placeholders.
        ok = python_program(IMPORT_SMOKE, env=child_env()) == 0
    note = "" if options.wheel else "wheel skipped (pass --wheel to include it)"
    return Result("build", "Build", ok, time.monotonic() - start, note=note)


def stage_coverage(_options: argparse.Namespace) -> Result:
    start = time.monotonic()
    banner("2/5 COVERAGE - unit tests and the 90% per-file coverage contract")
    steps = (
        ("-m", "coverage", "run", "--branch", "-m", "pytest"),
        ("-m", "coverage", "json", "-o", "coverage.json"),
        (
            "scripts/check_coverage.py",
            "--coverage-file",
            "coverage.json",
            "--threshold",
            THRESHOLD,
        ),
        ("-m", "coverage", "report", f"--fail-under={THRESHOLD}"),
    )
    ok = True
    for step in steps:
        # Later steps still run after a failure: the per-file table and the
        # aggregate report are exactly what a failing run needs to show.
        if python(*step) != 0:
            ok = False
    return Result("coverage", "Unit tests & coverage", ok, time.monotonic() - start)


def stage_typecheck(_options: argparse.Namespace) -> Result:
    start = time.monotonic()
    banner("3/5 TYPECHECK - mypy against its quality-baseline.json budget")
    ok = python("scripts/check_quality_budget.py", "--check", "mypy") == 0
    return Result("typecheck", "Type check", ok, time.monotonic() - start)


def stage_lint(_options: argparse.Namespace) -> Result:
    start = time.monotonic()
    banner("4/5 LINT - flake8 correctness (blocking) and full style (advisory)")
    ok = (
        python(
            "scripts/check_quality_budget.py",
            "--check",
            "flake8-errors",
            "--check",
            "flake8-style",
        )
        == 0
    )
    return Result("lint", "Lint", ok, time.monotonic() - start)


def stage_schema(options: argparse.Namespace) -> Result:
    start = time.monotonic()
    banner("5/5 SCHEMA - database schema definitions and pending migrations")
    if (REPO_ROOT / "alembic.ini").is_file():
        # Adopted after this script was written: prefer the real migration tool.
        if options.apply_schema:
            ok = run(["alembic", "upgrade", "head"]) == 0
            note = "alembic upgrade head"
        else:
            ok = run(["alembic", "check"]) == 0
            note = "alembic check (pass --apply-schema to upgrade)"
        return Result("schema", "Database schema", ok, time.monotonic() - start,
                      note=note)

    ok = python_program(SCHEMA_VALIDATE, env=child_env()) == 0
    note = "offline validation; no Alembic in this repository"
    if ok and options.apply_schema:
        # Deliberately the ambient environment: this stage must resolve the real
        # DATABASE_MODE target, not the in-memory placeholder.
        ok = python_program(SCHEMA_APPLY) == 0
        note = "applied to the configured database via create_all"
    elif ok:
        note += "; pass --apply-schema to reconcile a real database"
    return Result("schema", "Database schema", ok, time.monotonic() - start, note=note)


STAGES: dict[str, Callable[[argparse.Namespace], Result]] = {
    "build": stage_build,
    "coverage": stage_coverage,
    "typecheck": stage_typecheck,
    "lint": stage_lint,
    "schema": stage_schema,
}


# ----------------------------------------------------------------- fast mode


def git_lines(*args: str) -> list[str]:
    """Run a read-only git command and return its non-empty output lines."""
    completed = subprocess.run(  # noqa: S603 - read-only git plumbing
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def resolve_base(base_ref: str) -> str | None:
    """Return the merge base with ``base_ref``, or None when it is unknown."""
    if not git_lines("rev-parse", "--verify", "--quiet", base_ref):
        return None
    merge_base = git_lines("merge-base", base_ref, "HEAD")
    return merge_base[0] if merge_base else base_ref


def changed_python_files(base_ref: str) -> tuple[list[Path], str]:
    """Python files changed against ``base_ref`` plus the working tree."""
    names: set[str] = set()
    base = resolve_base(base_ref)
    if base:
        names.update(git_lines("diff", "--name-only", "--diff-filter=ACMR", base))
        source = f"{base_ref}...HEAD plus the working tree"
    else:
        source = "the working tree (base ref not found)"
    names.update(git_lines("diff", "--name-only", "--diff-filter=ACMR"))
    names.update(git_lines("diff", "--name-only", "--diff-filter=ACMR", "--cached"))
    names.update(git_lines("ls-files", "--others", "--exclude-standard"))

    files = []
    for name in sorted(names):
        if not name.endswith(".py"):
            continue
        path = REPO_ROOT / name
        if path.is_file():
            files.append(Path(name))
    return files, source


def dotted_path(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def related_unit_tests(changed: Iterable[Path]) -> list[Path]:
    """Unit tests that reference the changed modules.

    Name-based mapping does not work here: the suite is organised thematically
    (test_coverage_round6_remaining.py and friends), so the reliable signal is
    a test file naming the module, which every ``patch("app.x.y....")`` does.
    """
    unit_dir = REPO_ROOT / "tests" / "unit"
    if not unit_dir.is_dir():
        return []
    sources = {
        path: path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(unit_dir.glob("test_*.py"))
    }

    matched: set[Path] = set()
    for path in changed:
        posix = path.as_posix()
        if posix.startswith("tests/"):
            matched.add(Path(posix))
            continue
        if not posix.startswith("app/"):
            continue
        dotted = dotted_path(path)
        if not dotted:
            continue
        package, _, name = dotted.rpartition(".")
        word = re.compile(rf"\b{re.escape(name)}\b")
        for test_path, text in sources.items():
            if dotted in text:
                matched.add(test_path.relative_to(REPO_ROOT))
            elif package and f"from {package} import" in text and word.search(text):
                matched.add(test_path.relative_to(REPO_ROOT))
    return sorted(matched)


def run_fast(options: argparse.Namespace) -> int:
    """A seconds-scale pre-check over changed files only."""
    banner("FAST CHECK - changed files only; NOT a substitute for the full gate")
    changed, source = changed_python_files(options.base)
    print(f"Comparing against: {source}")
    if not changed:
        print("No changed Python files. Nothing to check.")
        return 0
    print(f"{len(changed)} changed Python file(s):")
    for path in changed:
        print(f"  {path.as_posix()}")

    app_files = [path for path in changed if path.as_posix().startswith("app/")]
    names = [path.as_posix() for path in changed]
    results: list[Result] = []

    start = time.monotonic()
    banner("FAST 1/4 COMPILE - do the changed files parse and compile")
    ok = python("-m", "compileall", "-q", *names) == 0
    results.append(Result("compile", "Compile changed", ok, time.monotonic() - start))

    start = time.monotonic()
    banner("FAST 2/4 LINT - flake8 correctness codes on the changed files")
    ok = (
        python(
            "-m",
            "flake8",
            *names,
            "--select=E9,F63,F7,F82",
            "--isolated",
            "--max-line-length=120",
        )
        == 0
    )
    results.append(Result("lint", "Lint changed", ok, time.monotonic() - start))

    start = time.monotonic()
    banner("FAST 3/4 TYPECHECK - mypy on the changed application files")
    if app_files:
        # Advisory: mypy carries 1320 baseline findings, and a subset run cannot
        # be compared against that budget. The count is signal, not a verdict.
        code = python("-m", "mypy", *[path.as_posix() for path in app_files])
        results.append(
            Result(
                "typecheck",
                "Type check changed",
                code == 0,
                time.monotonic() - start,
                blocking=False,
                note="advisory: subset runs are not comparable to the budget",
            )
        )
    else:
        results.append(
            Result("typecheck", "Type check changed", True, time.monotonic() - start,
                   skipped=True, note="no app/ files changed")
        )

    start = time.monotonic()
    banner("FAST 4/4 TESTS - unit tests that reference the changed modules")
    tests = related_unit_tests(changed)
    if tests:
        for path in tests:
            print(f"  {path.as_posix()}")
        # A separate data file so a fast run never clobbers the gate's .coverage.
        # Otherwise the ambient environment, untouched: conftest.py owns it.
        env = dict(os.environ, COVERAGE_FILE=FAST_COVERAGE_FILE)
        include = ",".join(path.as_posix() for path in app_files)
        command = ["-m", "coverage", "run", "--branch"]
        if include:
            command += [f"--include={include}"]
        # -x and -q keep this a pre-check: stop at the first failure and skip
        # the long warning summary the full gate is there to report.
        command += ["-m", "pytest", "-x", "-q", "--no-header"]
        command += [path.as_posix() for path in tests]
        ok = python(*command, env=env) == 0
        if include and ok:
            # Only meaningful on a complete run: -x leaves partial data behind.
            python("-m", "coverage", "report", f"--include={include}", env=env)
        (REPO_ROOT / FAST_COVERAGE_FILE).unlink(missing_ok=True)
        results.append(
            Result("tests", "Related unit tests", ok, time.monotonic() - start,
                   note=f"{len(tests)} test file(s); changed-file coverage only")
        )
    else:
        results.append(
            Result("tests", "Related unit tests", True, time.monotonic() - start,
                   skipped=True, note="no unit test references the changed modules")
        )

    exit_code = summarise(results)
    print("Fast check complete. The full gate is still required before you call")
    print("a change done:  python scripts/quality_check.py")
    return exit_code


# ------------------------------------------------------------------- reporting


def summarise(results: Sequence[Result]) -> int:
    banner("SUMMARY")
    print("| stage | status | seconds | note |")
    print("| --- | :---: | ---: | --- |")
    for result in results:
        print(
            f"| {result.title} | {result.status} | {result.seconds:.1f} | "
            f"{result.note or '-'} |"
        )
    print()

    failed = [result for result in results if result.failed]
    if failed:
        for result in failed:
            print(f"FAILED: {result.title}")
        print()
        print("Fix the findings. Never weaken the coverage threshold, shorten the")
        print("pytest timeout, exclude an application file, or raise a")
        print("max_findings budget in quality-baseline.json to make this pass.")
        return 1
    print("All blocking quality checks passed.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Changed files only: compile, lint, mypy and the related unit "
             "tests. A pre-check, not the gate.",
    )
    parser.add_argument(
        "--stage",
        action="append",
        dest="stages",
        choices=sorted(STAGES),
        help="Run only this stage; repeatable. Defaults to every stage.",
    )
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE_REF,
        help=f"Base ref for --fast (default: {DEFAULT_BASE_REF}).",
    )
    parser.add_argument(
        "--wheel",
        action="store_true",
        help="Also build the wheel during the build stage.",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Reconcile the CONFIGURED database: report drift and create "
             "missing tables. Off by default because it writes to a real "
             "database.",
    )
    options = parser.parse_args(argv)

    if options.fast and options.stages:
        parser.error("--fast and --stage are mutually exclusive")
    if options.fast:
        return run_fast(options)

    # Fixed order regardless of the order the flags were given in: build and
    # coverage first, so a broken import is never reported as a lint problem.
    selected = [name for name in STAGES if not options.stages or name in options.stages]
    results = [STAGES[name](options) for name in selected]
    return summarise(results)


if __name__ == "__main__":
    raise SystemExit(main())
