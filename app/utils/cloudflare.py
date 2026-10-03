"""
Cloudflare utility module for AI Procurement Agent.

Provides helper functions for resolving real client IP addresses, extracting
Cloudflare-specific headers (CF-Ray, CF-IPCountry, CF-Visitor), and checking
protocol/HTTPS status behind Cloudflare proxies or Cloudflare Tunnels (cloudflared).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from starlette.requests import Request


def _get_header(request: Request, name: str) -> Optional[str]:
    """Retrieve header value safely with case-insensitivity for dict stubs."""
    headers = getattr(request, "headers", None)
    if not headers:
        return None
    val = headers.get(name)
    if val is not None:
        return str(val)
    target = name.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return str(v)
    return None


def get_client_ip(request: Request) -> str:
    """
    Extract the client's real IP address from request headers.

    Priority:
    1. CF-Connecting-IP (authoritative client IP when proxied by Cloudflare)
    2. True-Client-IP (Cloudflare Enterprise / Akamai)
    3. X-Real-IP (reverse proxies such as Nginx)
    4. X-Forwarded-For (first IP in comma-separated chain)
    5. request.client.host (direct socket connection)
    6. "127.0.0.1" fallback if client is missing
    """
    cf_ip = _get_header(request, "cf-connecting-ip")
    if cf_ip and cf_ip.strip():
        return cf_ip.strip()

    true_client_ip = _get_header(request, "true-client-ip")
    if true_client_ip and true_client_ip.strip():
        return true_client_ip.strip()

    x_real_ip = _get_header(request, "x-real-ip")
    if x_real_ip and x_real_ip.strip():
        return x_real_ip.strip()

    x_forwarded_for = _get_header(request, "x-forwarded-for")
    if x_forwarded_for and x_forwarded_for.strip():
        first_ip = x_forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip

    client = getattr(request, "client", None)
    if client and getattr(client, "host", None):
        return client.host

    return "127.0.0.1"


def get_cloudflare_ray(request: Request) -> Optional[str]:
    """Return the CF-Ray header value if present on the request, or None."""
    ray = _get_header(request, "cf-ray")
    return ray.strip() if ray and ray.strip() else None


def get_cloudflare_country(request: Request) -> Optional[str]:
    """Return the CF-IPCountry 2-letter ISO country code if present on the request, or None."""
    country = _get_header(request, "cf-ipcountry")
    return country.strip() if country and country.strip() else None


def get_cloudflare_visitor(request: Request) -> Optional[Dict[str, Any]]:
    """
    Parse and return the CF-Visitor JSON header if present on the request.
    Example: '{"scheme":"https"}' -> {"scheme": "https"}.
    Returns None if absent or malformed.
    """
    raw = _get_header(request, "cf-visitor")
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


def is_cloudflare_request(request: Request) -> bool:
    """Return True if the request appears to originate via Cloudflare."""
    ray = _get_header(request, "cf-ray")
    cf_ip = _get_header(request, "cf-connecting-ip")
    return bool((ray and ray.strip()) or (cf_ip and cf_ip.strip()))


def is_https_request(request: Request) -> bool:
    """
    Determine if the request originated over HTTPS.
    Supports direct HTTPS, X-Forwarded-Proto, and Cloudflare CF-Visitor.
    """
    if getattr(request, "url", None) and getattr(request.url, "scheme", None) == "https":
        return True

    proto = _get_header(request, "x-forwarded-proto")
    if proto and proto.strip().lower() == "https":
        return True

    visitor = get_cloudflare_visitor(request)
    if visitor and str(visitor.get("scheme", "")).lower() == "https":
        return True

    return False
