"""Enforce the repository's per-file coverage contract.

Coverage.py's ``--fail-under`` is aggregate-only and does not expose a
function percentage. This checker consumes coverage.py's JSON report and
combines its statement/branch counters with AST-based function counters.
Every Python file under ``app/`` is included in the report, including empty
package initializers and test-support modules in the application tree.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Metric:
    covered: int
    total: int

    @property
    def percent(self) -> float:
        return 100.0 if self.total == 0 else self.covered * 100.0 / self.total


def _normalise(path: str | Path) -> str:
    return Path(path).as_posix().lstrip("./")


def _function_body_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return the first executable line in a function body."""
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            body.pop(0)
    return (body[0] if body else node).lineno


def _function_metric(source: str, executed_lines: set[int]) -> Metric:
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    covered = sum(
        _function_body_line(node) in executed_lines for node in functions
    )
    return Metric(covered, len(functions))


def _source_files(source_root: Path) -> list[Path]:
    return sorted(source_root.rglob("*.py"))


def _coverage_entry(files: dict[str, Any], path: Path, root: Path) -> dict[str, Any]:
    candidates = {
        _normalise(path),
        _normalise(path.relative_to(root.parent)),
        _normalise(path.resolve()),
    }
    for name, entry in files.items():
        normalised = _normalise(name)
        if normalised in candidates or normalised.endswith(_normalise(path)):
            return entry
    return {}


def _metric(entry: dict[str, Any], covered_key: str, total_key: str) -> Metric:
    summary = entry.get("summary", {})
    return Metric(int(summary.get(covered_key, 0)), int(summary.get(total_key, 0)))


def check_report(report_path: Path, source_root: Path, threshold: float) -> int:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = report.get("files", {})
    failures: list[tuple[str, str, float]] = []
    rows: list[tuple[str, Metric, Metric, Metric]] = []

    for path in _source_files(source_root):
        entry = _coverage_entry(files, path, source_root)
        executed_lines = set(entry.get("executed_lines", []))
        try:
            function_metric = _function_metric(
                path.read_text(encoding="utf-8"), executed_lines
            )
        except SyntaxError as exc:
            print(f"ERROR {path}: cannot parse source for function coverage: {exc}")
            return 2

        statements = _metric(entry, "covered_lines", "num_statements")
        branches = _metric(entry, "covered_branches", "num_branches")
        rows.append((path.as_posix(), statements, branches, function_metric))
        for label, metric in (
            ("statements", statements),
            ("branches", branches),
            ("functions", function_metric),
        ):
            if metric.percent < threshold:
                failures.append((path.as_posix(), label, metric.percent))

    print("Per-file coverage (threshold: %.2f%%)" % threshold)
    print("file | statements | branches | functions")
    print("--- | ---: | ---: | ---:")
    for name, statements, branches, functions in rows:
        print(
            f"{name} | {statements.percent:.2f}% "
            f"({statements.covered}/{statements.total}) | "
            f"{branches.percent:.2f}% ({branches.covered}/{branches.total}) | "
            f"{functions.percent:.2f}% ({functions.covered}/{functions.total})"
        )

    if failures:
        print("\nCoverage contract failed:")
        for name, label, percent in failures:
            print(f"- {name}: {label}={percent:.2f}% (< {threshold:.2f}%)")
        return 1

    print("\nCoverage contract passed for every app/**/*.py file.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-file", type=Path, default=Path("coverage.json"))
    parser.add_argument("--source", type=Path, default=Path("app"))
    parser.add_argument("--threshold", type=float, default=90.0)
    args = parser.parse_args()

    if not args.coverage_file.is_file():
        print(f"Coverage report not found: {args.coverage_file}", file=sys.stderr)
        return 2
    if not args.source.is_dir():
        print(f"Coverage source directory not found: {args.source}", file=sys.stderr)
        return 2
    return check_report(args.coverage_file, args.source, args.threshold)


if __name__ == "__main__":
    raise SystemExit(main())
