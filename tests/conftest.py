"""Deterministic unit-test bootstrap for the application package."""

from __future__ import annotations

import os
import sys
import types


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
os.environ.setdefault("WHATSAPP_MOCK_MODE", "true")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CHROMA_HOST", "localhost")
os.environ.setdefault("CHROMA_PORT", "8000")
os.environ.setdefault("CHROMA_USE_SERVER", "true")
os.environ.setdefault("WEBHOOK_HEALTH_MONITORING_ENABLED", "false")
