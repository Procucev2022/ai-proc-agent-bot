"""Deterministic unit-test bootstrap for the application package."""

from __future__ import annotations

import os
import sys
import types

import pytest


# Some Windows-hosted runners block NumPy's optional random extension DLL even
# though pandas and the rest of the deterministic suite do not use NumPy random
# APIs. Keep the compatibility fallback confined to the test bootstrap so the
# application imports and coverage scope remain unchanged.
try:
    import numpy.random  # noqa: F401
except ImportError:
    import numpy as _numpy

    _blocked_random = types.ModuleType("numpy.random")
    _blocked_random.Generator = object
    _blocked_random.RandomState = object
    _blocked_random.BitGenerator = object
    _blocked_random.SeedSequence = object
    _blocked_random.default_rng = lambda *args, **kwargs: None
    sys.modules["numpy.random"] = _blocked_random
    _numpy.random = _blocked_random


# Set safe values before any application module is imported. Unit tests never
# connect to these services; individual tests replace clients with mocks.
os.environ.setdefault("AZURE_OPENAI_API_KEY", "unit-test-key")
os.environ.setdefault("GMT_USERNAME", "unit-test-user")
os.environ.setdefault("GMT_PHONE", "919876543229")
# Settings.validate_config() also requires the GMT credential set. These are
# unroutable placeholders: every HTTP client is mocked in the unit suite.
os.environ.setdefault("GMT_BASE_URL", "https://gmt.invalid")
os.environ.setdefault("GMT_PASSWORD", "unit-test-password")
os.environ.setdefault("GMT_CLIENT_ID", "unit-test-client-id")
os.environ.setdefault("GMT_CLIENT_SECRET", "unit-test-client-secret")
os.environ.setdefault("LOCAL_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REMOTE_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DATABASE_MODE", "local")
os.environ.setdefault("LICENSE_ENABLED", "false")
os.environ.setdefault("DASHBOARD_API_KEY", "unit-test-dashboard-key")
os.environ.setdefault("WHATSAPP_MOCK_MODE", "true")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("WEBHOOK_HEALTH_MONITORING_ENABLED", "false")

# A test that pointed a service's prompts_dir or tools_dir at the real package and then
# wrote a stub into it emptied app/tools/user_selection_analysis.json and reduced
# app/prompts/profile_selection/user_selection_analysis.txt to the word "system". Both
# shipped that way, so profile selection answered HTTP 400 in production. Tests must
# write their fixtures under tmp_path; this guard fails the run if any of them does not.
_GUARDED_ASSET_DIRS = ("app/tools", "app/prompts", "app/email_templates")


def _asset_fingerprints() -> dict:
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    fingerprints = {}
    for relative in _GUARDED_ASSET_DIRS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in {".json", ".txt"}:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                fingerprints[str(path.relative_to(root)).replace("\\", "/")] = digest
    return fingerprints


def pytest_configure(config):
    config._shipped_asset_fingerprints = _asset_fingerprints()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session.config, "_shipped_asset_fingerprints", None)
    if not before:
        return
    after = _asset_fingerprints()
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    if changed:
        raise pytest.UsageError(
            "Tests modified shipped prompt or tool assets: "
            + ", ".join(changed)
            + ". Point prompts_dir/tools_dir at tmp_path instead of the real package."
        )
