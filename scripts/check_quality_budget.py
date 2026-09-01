"""Run a lint or type-check tool and enforce its ratchet budget.

The application predates any static-analysis gate, so the pull-request workflow
freezes the finding count recorded in ``quality-baseline.json`` instead of
demanding a clean run. A check fails when its finding count rises above the
recorded maximum; a drop is reported so the baseline can be lowered in the same
pull request.

Exit codes: ``0`` within budget, ``1`` over budget on a blocking check, ``2``
for a configuration or tool-invocation error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

# flake8: ``path:row:col: CODE message``
_FLAKE8_FINDING = re.compile(r"^.+?:\d+:\d+: [A-Z]+\d+ ")
# mypy: ``path:row: error: message`` (``note:`` lines annotate a prior error).
_MYPY_FINDING = re.compile(r"^.+?:\d+:(?:\d+:)? error: ")
_MYPY_TOTAL = re.compile(r"^Found (\d+) error")


@dataclass(frozen=True)
class Result:
    name: str
    title: str
    findings: int
    budget: int
    blocking: bool
    output: str

    @property
    def over_budget(self) -> bool:
        return self.findings > self.budget

    @property
    def improved(self) -> bool:
        return self.findings < self.budget

    @property
    def failed(self) -> bool:
        return self.over_budget and self.blocking

    @property
    def status(self) -> str:
        if self.over_budget:
            return "FAIL" if self.blocking else "WARN"
        return "PASS"


def count_findings(tool: str, output: str) -> int:
    """Count findings in ``output`` for ``tool``."""
    lines = output.splitlines()
    if tool == "mypy":
        for line in reversed(lines):
            match = _MYPY_TOTAL.match(line.strip())
            if match:
                return int(match.group(1))
        return sum(bool(_MYPY_FINDING.match(line)) for line in lines)
    if tool == "flake8":
        return sum(bool(_FLAKE8_FINDING.match(line)) for line in lines)
    raise ValueError(f"Unsupported tool: {tool}")


def run_check(name: str, spec: dict[str, Any]) -> Result:
    command: Sequence[str] = spec["command"]
    tool = spec["tool"]
    cmd_list = list(command)
    if cmd_list and cmd_list[0] == "python":
        cmd_list[0] = sys.executable
    completed = subprocess.run(  # noqa: S603 - command comes from the committed baseline
        cmd_list,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    findings = count_findings(tool, output)
    if completed.returncode not in (0, 1):
        raise RuntimeError(
            f"{name}: {' '.join(command)} exited {completed.returncode}:\n"
            f"{output.strip()}"
        )
    if completed.returncode == 1 and findings == 0:
        raise RuntimeError(
            f"{name}: {' '.join(command)} exited 1 but no findings were parsed. "
            "Check the tool output format and the parser patterns.\n"
            f"{output.strip()}"
        )
    return Result(
        name=name,
        title=spec.get("title", name),
        findings=findings,
        budget=int(spec["max_findings"]),
        blocking=bool(spec.get("blocking", True)),
        output=output,
    )


def render_markdown(results: Sequence[Result], include_header: bool = True) -> str:
    lines = []
    if include_header:
        lines += [
            "| check | findings | budget | delta | status |",
            "| --- | ---: | ---: | ---: | :---: |",
        ]
    for result in results:
        delta = result.findings - result.budget
        lines.append(
            f"| {result.title} (`{result.name}`) | {result.findings} | "
            f"{result.budget} | {delta:+d} | {result.status} |"
        )
    return "\n".join(lines)


def report(results: Sequence[Result]) -> int:
    exit_code = 0
    for result in results:
        header = f"{result.status} {result.name}: {result.findings} findings (budget {result.budget})"
        print("=" * len(header))
        print(header)
        print("=" * len(header))
        if result.output.strip():
            print(result.output.rstrip())
        if result.over_budget:
            excess = result.findings - result.budget
            scope = "blocking" if result.blocking else "advisory"
            print(
                f"\n{result.name} is {excess} finding(s) over its {scope} budget. "
                "Fix the new findings; do not raise max_findings in "
                "quality-baseline.json."
            )
            if result.blocking:
                exit_code = 1
        elif result.improved:
            print(
                f"\n{result.name} improved by {result.budget - result.findings} "
                "finding(s). Lower max_findings in quality-baseline.json to "
                "lock the improvement in."
            )
        print()
    return exit_code


def load_checks(baseline_path: Path, names: Sequence[str] | None) -> dict[str, Any]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    checks = baseline.get("checks", {})
    if not checks:
        raise KeyError(f"No checks defined in {baseline_path}")
    if not names:
        return checks
    missing = [name for name in names if name not in checks]
    if missing:
        raise KeyError(f"Unknown check(s) {', '.join(missing)} in {baseline_path}")
    return {name: checks[name] for name in names}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, default=Path("quality-baseline.json")
    )
    parser.add_argument(
        "--check",
        action="append",
        dest="checks",
        help="Baseline check to run; repeatable. Defaults to every check.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Append a Markdown result table to this file.",
    )
    args = parser.parse_args(argv)

    if not args.baseline.is_file():
        print(f"Baseline file not found: {args.baseline}", file=sys.stderr)
        return 2
    try:
        specs = load_checks(args.baseline, args.checks)
    except (KeyError, json.JSONDecodeError) as exc:
        print(f"Invalid baseline: {exc}", file=sys.stderr)
        return 2

    results = []
    for name, spec in specs.items():
        try:
            results.append(run_check(name, spec))
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            print(f"Could not run check {name}: {exc}", file=sys.stderr)
            return 2

    exit_code = report(results)
    print(render_markdown(results))
    if args.summary_file:
        args.summary_file.parent.mkdir(parents=True, exist_ok=True)
        # Several invocations append to one file (lint, then type check), so the
        # header is written once to keep it a single Markdown table.
        started = args.summary_file.exists() and args.summary_file.stat().st_size > 0
        with args.summary_file.open("a", encoding="utf-8") as handle:
            handle.write(render_markdown(results, include_header=not started) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
