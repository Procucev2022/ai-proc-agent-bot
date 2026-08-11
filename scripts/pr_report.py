"""Render the pull-request quality-gate report as Markdown.

Combines the pytest JUnit XML result file, coverage.py's JSON report and the
per-file metrics produced by :mod:`check_coverage` into one Markdown document
that the workflow writes to both the job summary and a sticky pull-request
comment.

Per the repository contract in AGENTS.md the report carries the complete
per-file table from ``scripts/check_coverage.py`` alongside the aggregate
figures; the table is wrapped in a collapsed ``<details>`` block so the comment
stays readable.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_coverage import (  # noqa: E402 - resolved via the sys.path line above
    Metric,
    _coverage_entry,
    _function_metric,
    _metric,
    _source_files,
)

MARKER = "<!-- pr-quality-gate -->"
PASS = "PASS"
FAIL = "FAIL"


@dataclass
class TestOutcome:
    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    failed_names: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return max(self.total - self.failures - self.errors - self.skipped, 0)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.failures == 0 and self.errors == 0

    @property
    def pass_rate(self) -> float:
        ran = self.total - self.skipped
        return 100.0 if ran <= 0 else self.passed * 100.0 / ran


def parse_junit(path: Path) -> TestOutcome:
    outcome = TestOutcome()
    if not path.is_file():
        return outcome
    root = ET.parse(path).getroot()
    suites = (
        [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    )
    for suite in suites:
        outcome.total += int(suite.get("tests", 0))
        outcome.failures += int(suite.get("failures", 0))
        outcome.errors += int(suite.get("errors", 0))
        outcome.skipped += int(suite.get("skipped", 0))
        outcome.duration += float(suite.get("time", 0.0))
    for case in root.iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        classname = case.get("classname", "")
        name = case.get("name", "")
        outcome.failed_names.append(f"{classname}::{name}" if classname else name)
    return outcome


@dataclass
class CoverageSummary:
    statements: Metric
    branches: Metric
    functions: Metric
    files_checked: int
    files_below: list[tuple[str, str, float]]

    @property
    def worst_percent(self) -> float:
        return min(
            self.statements.percent, self.branches.percent, self.functions.percent
        )


def collect_coverage(
    coverage_json: Path, source_root: Path, threshold: float
) -> CoverageSummary:
    """Aggregate the same three metrics ``check_coverage.py`` enforces per file."""
    report = json.loads(coverage_json.read_text(encoding="utf-8"))
    files = report.get("files", {})
    stmt_covered = stmt_total = 0
    branch_covered = branch_total = 0
    func_covered = func_total = 0
    below: list[tuple[str, str, float]] = []
    paths = _source_files(source_root)

    for path in paths:
        entry = _coverage_entry(files, path, source_root)
        executed = set(entry.get("executed_lines", []))
        try:
            functions = _function_metric(path.read_text(encoding="utf-8"), executed)
        except SyntaxError:
            functions = Metric(0, 0)
        statements = _metric(entry, "covered_lines", "num_statements")
        branches = _metric(entry, "covered_branches", "num_branches")

        stmt_covered += statements.covered
        stmt_total += statements.total
        branch_covered += branches.covered
        branch_total += branches.total
        func_covered += functions.covered
        func_total += functions.total

        for label, metric in (
            ("statements", statements),
            ("branches", branches),
            ("functions", functions),
        ):
            if metric.percent < threshold:
                below.append((path.as_posix(), label, metric.percent))

    return CoverageSummary(
        statements=Metric(stmt_covered, stmt_total),
        branches=Metric(branch_covered, branch_total),
        functions=Metric(func_covered, func_total),
        files_checked=len(paths),
        files_below=below,
    )


def _read(path: Path | None) -> str:
    """Read a captured tool log, tolerating a BOM or stray bytes."""
    if not path or not path.is_file():
        return ""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _status(ok: bool) -> str:
    return PASS if ok else FAIL


def _as_markdown_block(text: str) -> str:
    """Insert a blank line before the first table row of a captured log.

    ``check_coverage.py`` prints a title line directly above its table. GitHub
    only renders a table that starts its own block, so the title has to be
    separated from it.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        if index > 0 and lines[index - 1].strip():
            lines.insert(index, "")
        break
    return "\n".join(lines)


def render(
    tests: TestOutcome,
    coverage: CoverageSummary | None,
    threshold: float,
    per_file_table: str,
    aggregate_report: str,
    quality_table: str,
    step_statuses: Sequence[tuple[str, str]],
    context: dict[str, str],
) -> str:
    gate_ok = all(status == "success" for _, status in step_statuses)
    lines = [MARKER, "## Pull request quality gate", ""]
    lines.append(
        f"**{'All checks passed' if gate_ok else 'Quality gate failed'}**"
        f" - per-file threshold {threshold:.0f}% on statements, branches and functions."
    )
    lines.append("")

    if step_statuses:
        lines += ["### Checks", "", "| check | result |", "| --- | :---: |"]
        for label, status in step_statuses:
            lines.append(f"| {label} | {_status(status == 'success')} |")
        lines.append("")

    lines += ["### Unit tests", ""]
    if tests.total == 0:
        lines.append(
            "No test results were produced. The suite failed before or during "
            "collection; see the workflow log."
        )
    else:
        lines += [
            "| metric | value |",
            "| --- | ---: |",
            f"| Passed | {tests.passed} |",
            f"| Failed | {tests.failures} |",
            f"| Errors | {tests.errors} |",
            f"| Skipped | {tests.skipped} |",
            f"| Total collected | {tests.total} |",
            f"| Pass rate | {tests.pass_rate:.2f}% |",
            f"| Duration | {tests.duration:.1f}s |",
            "",
            f"Result: {_status(tests.ok)}",
        ]
        if tests.failed_names:
            shown = tests.failed_names[:25]
            lines += ["", "<details><summary>Failing tests "
                      f"({len(tests.failed_names)})</summary>", ""]
            lines += [f"- `{name}`" for name in shown]
            if len(tests.failed_names) > len(shown):
                lines.append(f"- ... and {len(tests.failed_names) - len(shown)} more")
            lines += ["", "</details>"]
    lines.append("")

    lines += ["### Coverage", ""]
    if coverage is None:
        lines.append(
            "No coverage report was produced, so the per-file contract could "
            "not be evaluated."
        )
    else:
        rows = (
            ("Statements", coverage.statements),
            ("Branches", coverage.branches),
            ("Functions", coverage.functions),
        )
        lines += [
            f"Overall unit-test coverage across {coverage.files_checked} "
            "`app/**/*.py` files:",
            "",
            "| parameter | covered / total | overall | threshold | status |",
            "| --- | ---: | ---: | ---: | :---: |",
        ]
        for label, metric in rows:
            lines.append(
                f"| {label} | {metric.covered} / {metric.total} | "
                f"{metric.percent:.2f}% | {threshold:.0f}% | "
                f"{_status(metric.percent >= threshold)} |"
            )
        lines.append("")
        if coverage.files_below:
            lines += [
                f"**{len(coverage.files_below)} per-file metric(s) below "
                f"{threshold:.0f}%:**",
                "",
            ]
            for name, label, percent in coverage.files_below[:30]:
                lines.append(f"- `{name}`: {label} = {percent:.2f}%")
            if len(coverage.files_below) > 30:
                lines.append(
                    f"- ... and {len(coverage.files_below) - 30} more; see the "
                    "job summary for the full table."
                )
        else:
            lines.append(
                f"Every `app/**/*.py` file meets {threshold:.0f}% on statements, "
                "branches and functions."
            )
        lines.append("")

    if quality_table:
        lines += ["### Lint and type check", "", quality_table, ""]

    if per_file_table:
        lines += [
            "<details><summary>Complete per-file coverage table</summary>",
            "",
            _as_markdown_block(per_file_table),
            "",
            "</details>",
            "",
        ]

    if aggregate_report:
        lines += [
            "<details><summary>Aggregate <code>coverage report</code> output"
            "</summary>",
            "",
            "```text",
            aggregate_report,
            "```",
            "",
            "</details>",
            "",
        ]

    footer = " | ".join(f"{key}: {value}" for key, value in context.items() if value)
    if footer:
        lines.append(f"<sub>{footer}</sub>")
    return "\n".join(lines).rstrip() + "\n"


def fit_for_comment(full: str, max_chars: int, **kwargs) -> str:
    """Re-render without the bulky detail blocks until the body fits.

    A pull-request comment body is capped at 65536 characters. The complete
    per-file table always survives in the job summary and the uploaded
    artifact, so the comment sheds it last and only when it has to.
    """
    if len(full) <= max_chars:
        return full
    note = (
        "\n> Trimmed to fit the pull-request comment limit. The complete "
        "per-file coverage table is in the job summary and the "
        "`quality-gate-reports` artifact.\n"
    )
    for dropped in ("aggregate_report", "per_file_table"):
        kwargs[dropped] = ""
        candidate = render(**kwargs) + note
        if len(candidate) <= max_chars:
            return candidate
    return candidate[: max_chars - len(note)] + note


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit-xml", type=Path, default=Path("junit.xml"))
    parser.add_argument("--coverage-json", type=Path, default=Path("coverage.json"))
    parser.add_argument("--source", type=Path, default=Path("app"))
    parser.add_argument("--threshold", type=float, default=90.0)
    parser.add_argument("--per-file-table", type=Path)
    parser.add_argument("--aggregate-report", type=Path)
    parser.add_argument("--quality-table", type=Path)
    parser.add_argument(
        "--status",
        action="append",
        default=[],
        metavar="LABEL=OUTCOME",
        help="Step outcome to tabulate, e.g. 'Unit tests=success'. Repeatable.",
    )
    parser.add_argument("--commit")
    parser.add_argument("--run-url")
    parser.add_argument("--output", type=Path, default=Path("pr-report.md"))
    parser.add_argument(
        "--comment-output",
        type=Path,
        help="Also write a copy trimmed to --max-chars for the PR comment.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=65000,
        help="Character budget for --comment-output (GitHub's limit is 65536).",
    )
    args = parser.parse_args(argv)

    statuses: list[tuple[str, str]] = []
    for item in args.status:
        label, _, outcome = item.partition("=")
        statuses.append((label.strip(), outcome.strip() or "unknown"))

    tests = parse_junit(args.junit_xml)
    coverage: CoverageSummary | None = None
    if args.coverage_json.is_file() and args.source.is_dir():
        try:
            coverage = collect_coverage(
                args.coverage_json, args.source, args.threshold
            )
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Could not read coverage report: {exc}", file=sys.stderr)

    sections = dict(
        tests=tests,
        coverage=coverage,
        threshold=args.threshold,
        per_file_table=_read(args.per_file_table),
        aggregate_report=_read(args.aggregate_report),
        quality_table=_read(args.quality_table),
        step_statuses=statuses,
        context={"commit": args.commit or "", "run": args.run_url or ""},
    )
    markdown = render(**sections)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")

    if args.comment_output:
        comment = fit_for_comment(markdown, args.max_chars, **sections)
        args.comment_output.parent.mkdir(parents=True, exist_ok=True)
        args.comment_output.write_text(comment, encoding="utf-8")
        print(
            f"Wrote {args.output} ({len(markdown)} chars) and "
            f"{args.comment_output} ({len(comment)} chars)"
        )

    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
