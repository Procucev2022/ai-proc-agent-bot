"""
Regression tests for the boot crash in the 2026-08-14 logs.

The container came up, logged `[BOOT] Database Mode: client`, and then died:

    File "/app/app/database.py", line 38, in _get_ssl_connect_args
      raise ValueError("Database TLS CA certificate is required in client mode")
    [ERROR] Worker (pid:9) exited with code 3.
    [ERROR] Shutting down: Master

`_get_ssl_connect_args` looked for a bundled DigiCert root in three places and
raised when none existed. Two of those paths only exist on the VM deployment,
where docker-compose bind-mounts `.../ssl` onto `/app/ssl`; the image built from
Dockerfile.app never copies an `ssl/` directory, so on Azure Container Apps
(`DATABASE_MODE=client`, no such mount) the lookup could never succeed and every
worker failed to boot.

Azure Database for MySQL chains to a public root, so the platform trust store
verifies it. The fallback keeps verification on and only changes where the root
comes from; a missing bundled file is no longer fatal.

Nothing here opens a socket or a database connection.
"""

from __future__ import annotations

import os
import ssl
import sys
from types import SimpleNamespace

import pytest

import app.database as database

BUNDLED = "/app/ssl/DigiCertGlobalRootCA.crt.pem"
DEBIAN_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
REDHAT_BUNDLE = "/etc/pki/tls/certs/ca-bundle.crt"


@pytest.fixture
def client_settings():
    return SimpleNamespace(database_mode="client", PROJECT_ROOT="/app")


def _only(*existing: str):
    """Stub os.path.exists so that exactly the named paths are present."""
    allowed = set(existing)
    return lambda path: path in allowed


def test_bundled_certificate_is_preferred(monkeypatch, client_settings):
    monkeypatch.setattr(os.path, "exists", _only(BUNDLED, DEBIAN_BUNDLE))

    assert database._get_ssl_connect_args(client_settings) == {"ssl": {"ca": BUNDLED}}


def test_missing_bundled_certificate_falls_back_to_the_system_store(monkeypatch, client_settings, caplog):
    """The Azure Container Apps case: no mounted cert, boot must still succeed."""
    monkeypatch.setattr(os.path, "exists", _only(DEBIAN_BUNDLE))
    monkeypatch.setitem(sys.modules, "certifi", None)  # makes `import certifi` raise

    with caplog.at_level("WARNING", logger="app.database"):
        connect_args = database._get_ssl_connect_args(client_settings)

    assert connect_args == {"ssl": {"ca": DEBIAN_BUNDLE}}
    assert "falling back to the system CA bundle" in caplog.text
    # Verification stays on: no ssl_disabled / ssl_verify_cert escape hatch.
    assert "ssl_disabled" not in connect_args["ssl"]


def test_certifi_is_used_when_importable(monkeypatch, client_settings):
    monkeypatch.setitem(sys.modules, "certifi", SimpleNamespace(where=lambda: "/venv/certifi/cacert.pem"))
    monkeypatch.setattr(os.path, "exists", _only("/venv/certifi/cacert.pem", DEBIAN_BUNDLE))

    assert database._get_ssl_connect_args(client_settings) == {"ssl": {"ca": "/venv/certifi/cacert.pem"}}


def test_openssl_default_and_redhat_paths_are_considered(monkeypatch, client_settings):
    monkeypatch.setitem(sys.modules, "certifi", None)
    monkeypatch.setattr(
        ssl, "get_default_verify_paths",
        lambda: SimpleNamespace(cafile="/usr/local/ssl/cert.pem"),
    )
    monkeypatch.setattr(os.path, "exists", _only("/usr/local/ssl/cert.pem"))
    assert database._get_system_ca_bundle() == "/usr/local/ssl/cert.pem"

    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile=None))
    monkeypatch.setattr(os.path, "exists", _only(REDHAT_BUNDLE))
    assert database._get_system_ca_bundle() == REDHAT_BUNDLE


def test_no_trust_store_anywhere_is_still_an_error(monkeypatch, client_settings):
    """A host with no CA material at all must fail loudly rather than skip TLS."""
    monkeypatch.setitem(sys.modules, "certifi", None)
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile=None))
    monkeypatch.setattr(os.path, "exists", _only())

    assert database._get_system_ca_bundle() is None
    with pytest.raises(ValueError, match="TLS CA certificate"):
        database._get_ssl_connect_args(client_settings)


def test_local_mode_is_unaffected():
    local = SimpleNamespace(database_mode="local", PROJECT_ROOT="/app")

    assert database._get_ssl_connect_args(local) == {"ssl_disabled": True}
