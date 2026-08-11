"""Import smoke tests for every application module."""

from __future__ import annotations

import importlib
import pkgutil

import app


def test_every_application_package_module_imports():
    """All app modules must remain importable with unit-safe environment values."""
    module_names = [module.name for module in pkgutil.walk_packages(app.__path__, "app.")]
    assert module_names
    for module_name in module_names:
        assert importlib.import_module(module_name).__name__ == module_name
