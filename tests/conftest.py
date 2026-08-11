"""Deterministic unit-test bootstrap for the application package."""

from __future__ import annotations

import os


# Set safe values before any application module is imported. Unit tests never
# connect to these services; individual tests replace clients with mocks.
os.environ.setdefault("AZURE_OPENAI_API_KEY", "unit-test-key")
os.environ.setdefault("GMT_USERNAME", "unit-test-user")
os.environ.setdefault("GMT_PHONE", "919876543229")
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
