from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

from app.config import Settings
import app.main as main
from app.utils.cloudflare import (
    _get_header,
    get_client_ip,
    get_cloudflare_country,
    get_cloudflare_ray,
    get_cloudflare_visitor,
    is_cloudflare_request,
    is_https_request,
)


class DummyRequest:
    """Mock Request object supporting headers, client, and url."""

    def __init__(
        self,
        headers: dict | None = None,
        client_host: str | None = "127.0.0.1",
        scheme: str = "http",
        path: str = "/health",
        method: str = "GET",
    ):
        self.headers = headers if headers is not None else {}
        self.client = SimpleNamespace(host=client_host) if client_host is not None else None
        self.url = SimpleNamespace(scheme=scheme, path=path)
        self.method = method


# ============================================================================
# Cloudflare Utils Tests
# ============================================================================


def test_get_header_various_cases():
    # None headers
    req_no_headers = SimpleNamespace(headers=None)
    assert _get_header(req_no_headers, "cf-ray") is None

    # Case-insensitive dict lookup
    req_dict = DummyRequest(headers={"CF-RAY": "12345-BOM", "X-Custom": "val"})
    assert _get_header(req_dict, "cf-ray") == "12345-BOM"
    assert _get_header(req_dict, "CF-Ray") == "12345-BOM"
    assert _get_header(req_dict, "missing") is None

    # Starlette Headers object
    starlette_headers = Headers({"CF-Connecting-IP": "203.0.113.195"})
    req_starlette = SimpleNamespace(headers=starlette_headers)
    assert _get_header(req_starlette, "cf-connecting-ip") == "203.0.113.195"


def test_get_client_ip_precedence():
    # 1. CF-Connecting-IP takes highest priority
    req = DummyRequest(
        headers={
            "cf-connecting-ip": " 198.51.100.1 ",
            "true-client-ip": "198.51.100.2",
            "x-real-ip": "198.51.100.3",
            "x-forwarded-for": "198.51.100.4, 10.0.0.1",
        },
        client_host="127.0.0.1",
    )
    assert get_client_ip(req) == "198.51.100.1"

    # 2. True-Client-IP takes second priority
    req = DummyRequest(
        headers={
            "true-client-ip": " 198.51.100.2 ",
            "x-real-ip": "198.51.100.3",
            "x-forwarded-for": "198.51.100.4",
        },
        client_host="127.0.0.1",
    )
    assert get_client_ip(req) == "198.51.100.2"

    # 3. X-Real-IP takes third priority
    req = DummyRequest(
        headers={
            "x-real-ip": " 198.51.100.3 ",
            "x-forwarded-for": "198.51.100.4",
        },
        client_host="127.0.0.1",
    )
    assert get_client_ip(req) == "198.51.100.3"

    # 4. X-Forwarded-For takes fourth priority (first in list)
    req = DummyRequest(
        headers={"x-forwarded-for": " 198.51.100.4 , 10.0.0.1 "},
        client_host="127.0.0.1",
    )
    assert get_client_ip(req) == "198.51.100.4"

    # Empty forwarded-for item falls through to client host
    req = DummyRequest(headers={"x-forwarded-for": ""}, client_host="192.168.1.50")
    assert get_client_ip(req) == "192.168.1.50"

    # Forwarded-for where first element is empty string
    req = DummyRequest(headers={"x-forwarded-for": " , 10.0.0.1"}, client_host="192.168.1.55")
    assert get_client_ip(req) == "192.168.1.55"

    # 5. Socket client host
    req = DummyRequest(headers={}, client_host="192.168.1.100")
    assert get_client_ip(req) == "192.168.1.100"

    # 6. Fallback when client is None
    req = DummyRequest(headers={}, client_host=None)
    assert get_client_ip(req) == "127.0.0.1"

    # Fallback when request has no client attribute
    req = SimpleNamespace(headers={})
    assert get_client_ip(req) == "127.0.0.1"


def test_get_cloudflare_ray():
    req = DummyRequest(headers={"cf-ray": " 8f4b23190a98-BOM "})
    assert get_cloudflare_ray(req) == "8f4b23190a98-BOM"

    assert get_cloudflare_ray(DummyRequest(headers={"cf-ray": " "})) is None
    assert get_cloudflare_ray(DummyRequest(headers={})) is None
    assert get_cloudflare_ray(SimpleNamespace(headers=None)) is None


def test_get_cloudflare_country():
    req = DummyRequest(headers={"cf-ipcountry": " IN "})
    assert get_cloudflare_country(req) == "IN"

    assert get_cloudflare_country(DummyRequest(headers={"cf-ipcountry": ""})) is None
    assert get_cloudflare_country(DummyRequest(headers={})) is None
    assert get_cloudflare_country(SimpleNamespace(headers=None)) is None


def test_get_cloudflare_visitor():
    req = DummyRequest(headers={"cf-visitor": '{"scheme":"https"}'})
    assert get_cloudflare_visitor(req) == {"scheme": "https"}

    # Malformed JSON
    assert get_cloudflare_visitor(DummyRequest(headers={"cf-visitor": "{invalid"})) is None

    # Valid JSON but not a dictionary
    assert get_cloudflare_visitor(DummyRequest(headers={"cf-visitor": '"https"'})) is None

    # Empty or missing
    assert get_cloudflare_visitor(DummyRequest(headers={"cf-visitor": " "})) is None
    assert get_cloudflare_visitor(DummyRequest(headers={})) is None
    assert get_cloudflare_visitor(SimpleNamespace(headers=None)) is None


def test_is_cloudflare_request():
    assert is_cloudflare_request(DummyRequest(headers={"cf-ray": "abc-123"})) is True
    assert is_cloudflare_request(DummyRequest(headers={"cf-connecting-ip": "1.2.3.4"})) is True
    assert is_cloudflare_request(DummyRequest(headers={"other": "header"})) is False
    assert is_cloudflare_request(DummyRequest(headers={})) is False
    assert is_cloudflare_request(SimpleNamespace(headers=None)) is False


def test_is_https_request():
    # Direct HTTPS scheme
    assert is_https_request(DummyRequest(scheme="https")) is True

    # X-Forwarded-Proto
    assert is_https_request(DummyRequest(scheme="http", headers={"x-forwarded-proto": "https"})) is True
    assert is_https_request(DummyRequest(scheme="http", headers={"x-forwarded-proto": "http"})) is False

    # CF-Visitor header
    assert is_https_request(DummyRequest(scheme="http", headers={"cf-visitor": '{"scheme":"https"}'})) is True
    assert is_https_request(DummyRequest(scheme="http", headers={"cf-visitor": '{"scheme":"http"}'})) is False

    # Default HTTP
    assert is_https_request(DummyRequest(scheme="http", headers={})) is False

    # Missing URL and headers
    assert is_https_request(SimpleNamespace(headers=None)) is False


# ============================================================================
# Settings & Configuration Tests
# ============================================================================


def test_settings_cloudflare_configuration(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ENABLED", "true")
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_TOKEN", "mock-tunnel-token-xyz")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "173.245.48.0/20,103.21.244.0/22")

    settings = Settings()
    assert settings.cloudflare_enabled is True
    assert settings.cloudflare_tunnel_token == "mock-tunnel-token-xyz"
    assert settings.forwarded_allow_ips == "173.245.48.0/20,103.21.244.0/22"

    cfg = settings.get_cloudflare_config()
    assert cfg["enabled"] is True
    assert cfg["tunnel_token_configured"] is True
    assert cfg["forwarded_allow_ips"] == "173.245.48.0/20,103.21.244.0/22"


# ============================================================================
# Main App Integration Tests (IP Restriction, Logging, Cookies)
# ============================================================================


@pytest.mark.asyncio
async def test_ip_restriction_with_cloudflare_ip():
    async def next_response(_request):
        return JSONResponse({"ok": True})

    middleware = main.IPRestrictionMiddleware(object(), ["203.0.113.50"])

    # Request with matching CF-Connecting-IP
    allowed_req = DummyRequest(
        headers={"cf-connecting-ip": "203.0.113.50"},
        client_host="10.0.0.1",  # Local proxy IP
    )
    res = await middleware.dispatch(allowed_req, next_response)
    assert res.status_code == 200

    # Request with non-matching CF-Connecting-IP
    forbidden_req = DummyRequest(
        headers={"cf-connecting-ip": "203.0.113.99"},
        client_host="10.0.0.1",
    )
    res = await middleware.dispatch(forbidden_req, next_response)
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_request_logging_middleware_with_cloudflare():
    async def next_response(_request):
        return JSONResponse({"ok": True})

    middleware = main.RequestLoggingMiddleware(object())
    req = DummyRequest(
        headers={"cf-connecting-ip": "198.51.100.25", "cf-ray": "8f4b-TEST"},
        path="/test",
        method="GET",
    )
    res = await middleware.dispatch(req, next_response)
    assert res.status_code == 200


def test_set_dashboard_cookie_with_cloudflare_https():
    response = JSONResponse({"ok": True})
    req = DummyRequest(scheme="http", headers={"cf-visitor": '{"scheme":"https"}'})

    main._set_dashboard_cookie(response, "test-api-key", req)
    cookie_header = response.headers.get("set-cookie", "")
    assert "Secure" in cookie_header or "secure" in cookie_header.lower()
