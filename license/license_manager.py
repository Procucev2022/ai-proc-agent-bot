"""
License Manager for AI Procurement Agent.

Handles license creation, validation, renewal, and expiry management.
Stores licenses in app/license/ directory as encrypted strings.
"""

import os
import hashlib
import secrets
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Tuple, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)

# Import settings to get license secret key
try:
    from app.config import get_settings
except ImportError:
    # Fallback for when running from license directory
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from app.config import get_settings


class LicenseManager:
    """
    Manages application licensing with token-based validation.
    
    Features:
    - Auto-generate 30-day trial license on first run
    - Manual license installation support
    - Programmatic license creation and renewal
    - Token expiry validation
    - Secure token generation with machine binding
    """
    
    def __init__(self, license_dir: Optional[str] = None):
        """
        Initialize License Manager.
        
        Args:
            license_dir: Custom license directory path (optional)
        """
        if license_dir:
            self.license_dir = Path(license_dir)
        else:
            # Default: {project_root}/app/license
            project_root = Path(__file__).parent.parent
            self.license_dir = project_root / "app" / "license"
        
        self.license_file = self.license_dir / "license.lic"
        self.license_dir.mkdir(parents=True, exist_ok=True)
        
        # Auto-create trial license if none exists
        if not self.license_file.exists():
            logger.info("No license found. Creating 30-day trial license...")
            self.create_license(days=30)
    
    def _get_machine_id(self) -> str:
        """Generate unique machine identifier for license binding."""
        try:
            import platform
            machine_info = f"{platform.node()}-{platform.machine()}-{platform.system()}"
            return hashlib.sha256(machine_info.encode()).hexdigest()[:16]
        except Exception as e:
            logger.warning(f"Could not generate machine ID: {e}")
            return "default-machine"
    
    def _generate_token(self) -> str:
        """Generate secure random token."""
        return secrets.token_hex(32)
    
    def _encrypt_license_data(self, data: str) -> str:
        """Encrypt license data using SHA-256 based encryption."""
        try:
            # Create encryption key from secret
            settings = get_settings()
            key = hashlib.sha256(settings.license_secret_key.encode()).digest()
            
            # Simple XOR encryption with key
            data_bytes = data.encode('utf-8')
            encrypted = bytearray()
            
            for i, byte in enumerate(data_bytes):
                encrypted.append(byte ^ key[i % len(key)])
            
            # Base64 encode for storage
            return base64.b64encode(bytes(encrypted)).decode('utf-8')
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise
    
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
    
    def create_license(self, days: int = 30) -> bool:
        """
        Create new license with specified validity period.
        
        Args:
            days: License validity in days (default: 30)
            
        Returns:
            True if license created successfully
        """
        try:
            created_at = datetime.now()
            expiry_date = (created_at + timedelta(days=days)).replace(hour=23, minute=59, second=59, microsecond=0)
            
            # Create license data string (pipe-separated for easy parsing)
            license_data_str = "|".join([
                self._generate_token(),
                self._get_machine_id(),
                created_at.isoformat(),
                expiry_date.isoformat(),
                str(days),
                "1.0"
            ])
            
            # Encrypt the license data
            encrypted_license = self._encrypt_license_data(license_data_str)
            
            # Add signature hash for integrity check
            settings = get_settings()
            signature = hashlib.sha256(
                (encrypted_license + settings.license_secret_key).encode()
            ).hexdigest()
            
            # Store as: SIGNATURE::ENCRYPTED_DATA
            final_license = f"{signature}::{encrypted_license}"
            
            with open(self.license_file, 'w') as f:
                f.write(final_license)
            
            logger.info(f"License created successfully. Valid until: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create license: {e}")
            return False
    
    def renew_license(self, additional_days: int = 30) -> bool:
        """
        Renew existing license by extending expiry date.
        
        Args:
            additional_days: Days to add to current expiry (default: 30)
            
        Returns:
            True if license renewed successfully
        """
        try:
            if not self.license_file.exists():
                logger.error("No license file found to renew")
                return False
            
            # Read and decrypt current license
            with open(self.license_file, 'r') as f:
                license_content = f.read().strip()
            
            # Verify signature
            if "::" not in license_content:
                logger.error("Invalid license format")
                return False
            
            signature, encrypted_data = license_content.split("::", 1)
            settings = get_settings()
            expected_signature = hashlib.sha256(
                (encrypted_data + settings.license_secret_key).encode()
            ).hexdigest()
            
            if signature != expected_signature:
                logger.error("License signature verification failed")
                return False
            
            # Decrypt license data
            decrypted_data = self._decrypt_license_data(encrypted_data)
            parts = decrypted_data.split("|")
            
            if len(parts) != 6:
                logger.error("Invalid license data format")
                return False
            
            token, machine_id, created_at, expires_at, validity_days, version = parts
            
            # Calculate new expiry
            current_expiry = datetime.fromisoformat(expires_at)
            new_expiry = (current_expiry + timedelta(days=additional_days)).replace(hour=23, minute=59, second=59, microsecond=0)
            
            # Create renewed license data
            renewed_data_str = "|".join([
                token,
                machine_id,
                created_at,
                new_expiry.isoformat(),
                str(int(validity_days) + additional_days),
                version
            ])
            
            # Encrypt and sign
            encrypted_license = self._encrypt_license_data(renewed_data_str)
            settings = get_settings()
            new_signature = hashlib.sha256(
                (encrypted_license + settings.license_secret_key).encode()
            ).hexdigest()
            
            final_license = f"{new_signature}::{encrypted_license}"
            
            with open(self.license_file, 'w') as f:
                f.write(final_license)
            
            logger.info(f"License renewed. New expiry: {new_expiry.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to renew license: {e}")
            return False
    
    def read_license_data(self) -> Optional[Dict[str, Any]]:
        """
        Read and decrypt license data.
        
        Returns:
            Dictionary with license data or None if invalid
        """
        try:
            if not self.license_file.exists():
                return None
            
            # Read license file
            with open(self.license_file, 'r') as f:
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
            logger.error(f"Failed to read license data: {e}")
            return None
    
    def get_license_info(self) -> Optional[Dict[str, Any]]:
        """
        Get detailed license information.
        
        Returns:
            Dictionary with license details or None if not found
        """
        try:
            license_data = self.read_license_data()
            if not license_data:
                return None
            
            expiry_date = datetime.fromisoformat(license_data["expires_at"])
            created_date = datetime.fromisoformat(license_data["created_at"])
            now = datetime.now()
            
            return {
                "created_at": created_date.strftime('%Y-%m-%d %H:%M:%S'),
                "expires_at": expiry_date.strftime('%Y-%m-%d %H:%M:%S'),
                "days_remaining": max(0, (expiry_date - now).days),
                "is_expired": now > expiry_date,
                "validity_days": license_data.get("validity_days", "N/A"),
                "version": license_data.get("version", "1.0"),
                "machine_id": license_data.get("machine_id", "N/A")
            }
            
        except Exception as e:
            logger.error(f"Failed to get license info: {e}")
            return None
