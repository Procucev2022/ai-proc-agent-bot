"""
License management package for AI Procurement Agent.

Provides token-based license validation and expiry management.
"""

from typing import Tuple
import logging
import os
import base64
import hashlib
from datetime import datetime

logger = logging.getLogger(__name__)


def validate_license(license_file_path: str = None) -> Tuple[bool, str]:
    """
    Standalone license validation function.

    Reads the license file, verifies signature, decrypts data, and checks expiry.
    Called once per session creation — no caching needed since license is only
    validated at session creation boundary.

    Args:
        license_file_path: Path to the license file.

    Returns:
        Tuple of (is_valid, message)
    """
    if license_file_path is None:
        license_file_path = os.path.join(os.path.dirname(__file__), "license.lic")
    try:
        from app.config import get_settings
        settings = get_settings()

        # Check if license file exists
        if not os.path.exists(license_file_path):
            logger.error(f"License file not found: {license_file_path}")
            return False, "License file not found. Please contact Admin."

        # Read license file
        with open(license_file_path, 'r') as f:
            license_content = f.read().strip()

        # Verify format
        if "::" not in license_content:
            logger.error("Invalid license format")
            return False, "License file corrupted. Please contact Admin."

        signature, encrypted_data = license_content.split("::", 1)

        # Verify signature
        expected_signature = hashlib.sha256(
            (encrypted_data + settings.license_secret_key).encode()
        ).hexdigest()

        if signature != expected_signature:
            logger.error("License signature verification failed")
            return False, "License signature invalid. Please contact Admin."

        # Decrypt license data
        key = hashlib.sha256(settings.license_secret_key.encode()).digest()
        encrypted_bytes = base64.b64decode(encrypted_data.encode('utf-8'))
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key[i % len(key)])
        decrypted_data = bytes(decrypted).decode('utf-8')

        parts = decrypted_data.split("|")
        if len(parts) != 6:
            logger.error("Invalid license data format")
            return False, "License data corrupted. Please contact Admin."

        token, machine_id, created_at, expires_at, validity_days, version = parts

        # Check expiry
        expiry_date = datetime.fromisoformat(expires_at)
        now = datetime.now()

        if now > expiry_date:
            days_expired = (now - expiry_date).days
            logger.warning(f"License expired {days_expired} days ago (expired: {expires_at})")
            return False, f"License expired {days_expired} days ago. Please contact Admin to renew."

        days_remaining = (expiry_date - now).days

        # Warning if license expires soon
        if days_remaining <= 7:
            logger.warning(f"License expires soon: {days_remaining} days remaining (expires: {expires_at})")
            return True, f"License valid but expires soon in {days_remaining} days. Please renew."

        logger.debug(f"License valid — {days_remaining} days remaining (expires: {expires_at})")
        return True, "License valid."

    except Exception as e:
        logger.error(f"License validation error: {e}")
        return False, f"License validation failed: {str(e)}. Please contact Admin."


# Import only the middleware from this package to avoid circular imports
from .license_middleware import LicenseMiddleware

__all__ = ["LicenseMiddleware", "validate_license"]
