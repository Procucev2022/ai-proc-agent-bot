#!/usr/bin/env python
"""
Validate that every GitHub Actions workflow file parses as YAML.

An invalid workflow file cannot fail its own run. GitHub refuses to start it and
reports only an "Invalid workflow file" annotation, so a deploy that never
happens looks identical to a deploy that was never triggered. That is how an
unquoted ``sqlite:///:memory:`` value silently blocked every dev deployment:

    LOCAL_DATABASE_URL: sqlite:///:memory:

The trailing colon makes YAML read the value as a nested mapping, which fails
with "mapping values are not allowed here". Quoting the scalar fixes it.

This script is deliberately dependency-light so it can run as an early step in
the quality gate. It checks two things:

1. every ``.github/workflows/*.yml`` file is parseable YAML
2. each one declares a trigger, since PyYAML resolves the bare key ``on`` to the
   boolean ``True`` and a typo such as ``ON:`` would otherwise pass unnoticed

Exit code is 0 when every file is valid, 1 otherwise. Findings are emitted as
GitHub workflow annotations so they surface inline on the pull request.
"""

from __future__ import annotations

import glob
import sys
from typing import Any, List, Tuple

import yaml

WORKFLOW_GLOB = ".github/workflows/*.yml"


def _annotate(path: str, line: int, message: str) -> None:
    """Emit a GitHub Actions error annotation, and a readable line locally."""
    print(f"::error file={path},line={line}::{message}")
    print(f"  {path}:{line}: {message}")


def _triggers(document: Any) -> Any:
    """
    Return the workflow's trigger block.

    ``on`` is a YAML 1.1 boolean, so PyYAML parses the key as ``True`` rather
    than the string ``"on"``. Accept either spelling.
    """
    if not isinstance(document, dict):
        return None
    return document.get(True, document.get("on"))


def check_file(path: str) -> List[str]:
    """Validate a single workflow file. Returns a list of problem descriptions."""
    problems: List[str] = []

    try:
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = mark.line + 1 if mark else 1
        detail = getattr(error, "problem", None) or str(error)
        _annotate(path, line, f"not valid YAML: {detail}")
        return [f"{path}: invalid YAML at line {line}"]
    except OSError as error:
        _annotate(path, 1, f"could not be read: {error}")
        return [f"{path}: unreadable"]

    if document is None:
        _annotate(path, 1, "is empty")
        problems.append(f"{path}: empty")
        return problems

    if _triggers(document) is None:
        _annotate(path, 1, "declares no 'on:' trigger block")
        problems.append(f"{path}: no trigger")

    if not document.get("jobs"):
        _annotate(path, 1, "declares no 'jobs:' block")
        problems.append(f"{path}: no jobs")

    return problems


def main(argv: List[str]) -> int:
    paths = sorted(argv[1:]) or sorted(glob.glob(WORKFLOW_GLOB))

    if not paths:
        print(f"No workflow files matched {WORKFLOW_GLOB}")
        return 1

    results: List[Tuple[str, List[str]]] = [(path, check_file(path)) for path in paths]
    problems = [problem for _, found in results for problem in found]

    print()
    print(f"Workflow definitions ({len(paths)} file(s))")
    print("| file | status |")
    print("| --- | :---: |")
    for path, found in results:
        print(f"| {path} | {'FAIL' if found else 'OK'} |")

    if problems:
        print()
        print(f"{len(problems)} problem(s) found. GitHub cannot run an invalid "
              f"workflow, so these would fail silently rather than fail a build.")
        return 1

    print()
    print("Every workflow definition is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
