"""
FastAPI Middleware for License Validation.

Validates license token on every incoming request.
Blocks expired/invalid licenses with clear admin contact message.
"""

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable, Tuple, Optional, Dict, Any
from datetime import datetime
import logging
import os
import json
import base64
import hashlib
from ..config import get_settings
from . import validate_license as _validate_license_standalone

logger = logging.getLogger(__name__)


class LicenseMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce license validation on every API request.
    
    Validates license before processing any request.
    Returns 403 Forbidden with admin contact message if license is invalid/expired.
    """
    
    # Endpoints that bypass license check (health checks, docs)
    EXEMPT_PATHS = [
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json"
    ]
    
    def __init__(self, app, license_file_path: str = "app/license/license.lic"):
        """
        Initialize License Middleware.
        
        Args:
            app: FastAPI application instance
            license_file_path: Path to license file
        """
        super().__init__(app)
        self.license_file_path = license_file_path
        
        # Validate license on startup
        is_valid, message = self.validate_license()
        if is_valid:
            logger.info(f"✔ License validation: {message}")
        else:
            logger.warning(f"✘ License validation failed: {message}")
    

    
    def _decrypt_license_data(self, encrypted_data: str) -> str:
        """Decrypt license data using SHA-256 based decryption."""
        try:
            # Create decryption key from secret
            settings = get_settings()
            key = hashlib.sha256(settings.license_secret_key.encode()).digest()
            
            # Base64 decode
            encrypted_bytes = base64.b64decode(encrypted_data.encode('utf-8'))
            
            # XOR decryption with key
            decrypted = bytearray()
            for i, byte in enumerate(encrypted_bytes):
                decrypted.append(byte ^ key[i % len(key)])
            
            return bytes(decrypted).decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise
    
    def _read_license_file(self) -> Optional[Dict[str, Any]]:
        """Read and decrypt license file."""
        if not os.path.exists(self.license_file_path):
            return None
        
        try:
            # Read license file
            with open(self.license_file_path, 'r') as f:
                license_content = f.read().strip()
            
            # Verify format
            if "::" not in license_content:
                logger.error("Invalid license format")
                return None
            
            signature, encrypted_data = license_content.split("::", 1)
            
            # Verify signature
            settings = get_settings()
            expected_signature = hashlib.sha256(
                (encrypted_data + settings.license_secret_key).encode()
            ).hexdigest()
            
            if signature != expected_signature:
                logger.error("License signature verification failed")
                return None
            
            # Decrypt license data
            decrypted_data = self._decrypt_license_data(encrypted_data)
            parts = decrypted_data.split("|")
            
            if len(parts) != 6:
                logger.error("Invalid license data format")
                return None
            
            token, machine_id, created_at, expires_at, validity_days, version = parts
            
            return {
                "token": token,
                "machine_id": machine_id,
                "created_at": created_at,
                "expires_at": expires_at,
                "validity_days": int(validity_days),
                "version": version
            }
            
        except Exception as e:
            logger.error(f"License file read error: {e}")
            return None
    
    def validate_license(self) -> Tuple[bool, str]:
        """
        Validate current license status.
        
        Delegates to the standalone validate_license function.
        
        Returns:
            Tuple of (is_valid, message)
        """
        return _validate_license_standalone(self.license_file_path)
    
    async def dispatch(self, request: Request, call_next: Callable):
        """
        Process each request with license validation.
        
        Args:
            request: Incoming HTTP request
            call_next: Next middleware/handler in chain
            
        Returns:
            Response or 403 error if license invalid
        """
        # Skip license check for exempt paths
        if any(request.url.path.startswith(path) for path in self.EXEMPT_PATHS):
            return await call_next(request)
        
        # Validate license
        is_valid, message = self.validate_license()
        
        if not is_valid:
            logger.warning(f"License validation failed for {request.url.path}: {message}")
            return JSONResponse(
                status_code=403,
                content={
                    "error": "License Expired",
                    "message": message,
                    "action": "Please contact Admin to renew your license"
                }
            )
        
        # License valid - proceed with request
        response = await call_next(request)
        return response
