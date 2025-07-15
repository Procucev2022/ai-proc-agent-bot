"""
Configuration and environment variables for the AI Procurement Agent.

This module manages application configuration, environment variables, and
settings for different deployment environments (development, staging, production).
It provides centralized configuration management with validation and
type safety for all application settings.

Key responsibilities:
- Load and validate environment variables
- Provide configuration for database connections
- Manage API keys and external service credentials
- Configure logging levels and application behavior
- Support multiple deployment environments
- Validate configuration completeness and correctness
"""

import os
from dotenv import load_dotenv
from typing import Optional, Dict, Any
import logging

class Settings:
    """
    Application settings and configuration management.
    
    Centralizes all configuration management including environment variables,
    API credentials, database settings, and deployment-specific configurations.
    """
    
    def __init__(self):
        # Load environment variables
        load_dotenv()
        
        # Core application settings
        self.app_name = os.getenv("APP_NAME", "AI Procurement Agent")
        self.app_version = os.getenv("APP_VERSION", "1.0.0")
        self.debug = os.getenv("DEBUG", "false").lower() == "true"
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        
        # Database configuration
        self.database_url = os.getenv("DATABASE_URL")
        self.sql_debug = os.getenv("SQL_DEBUG", "false").lower() == "true"
        
        # OpenAI configuration
        self.openai_api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.openai_model_default = os.getenv("OPENAI_MODEL_DEFAULT", "gpt-4.1-mini")
        self.openai_model_advanced = os.getenv("OPENAI_MODEL_ADVANCED", "gpt-4.1-mini")
        self.azure_openai_base_url = os.getenv("AZURE_OPENAI_ENDPOINT")
        # WhatsApp configuration
        self.WHATSAPP_USERNAME = os.getenv("WHATSAPP_USERNAME", "test_user")
        self.WHATSAPP_PASSWORD = os.getenv("WHATSAPP_PASSWORD", "test_password")
        self.WHATSAPP_FROM_NUMBER = os.getenv("WHATSAPP_FROM_NUMBER", "918147745000")
        self.WHATSAPP_BASE_URL = os.getenv("WHATSAPP_BASE_URL", "https://media.sendmsg.in")
        self.WHATSAPP_TEMPLATE_BASE_URL = os.getenv("WHATSAPP_TEMPLATE_BASE_URL", "https://wsapi.sendmsg.in")
        self.WHATSAPP_WEBHOOK_URL = os.getenv("WHATSAPP_WEBHOOK_URL")
        self.WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
        self.WHATSAPP_API_KEY = os.getenv("WHATSAPP_API_KEY", "test_api_key")
        self.WHATSAPP_MOCK_MODE = os.getenv("WHATSAPP_MOCK_MODE", "true").lower() == "true"
        
        # Legacy fields for backward compatibility
        self.whatsapp_access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        self.whatsapp_verify_token = self.WHATSAPP_VERIFY_TOKEN
        self.whatsapp_webhook_url = self.WHATSAPP_WEBHOOK_URL
        
        # Redis configuration
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        
        # Security configuration
        self.secret_key = os.getenv("SECRET_KEY")
        self.allowed_hosts = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
        
        # Additional fields needed by services
        self.DEBUG = self.debug
        self.ALLOWED_ORIGINS = self.allowed_hosts
        
        # Logging configuration
        self.log_to_database = os.getenv("LOG_TO_DATABASE", "false").lower() == "true"
        self.log_retention_days = int(os.getenv("LOG_RETENTION_DAYS", "30"))
        
        # External API configuration
        self.procurement_api_url = os.getenv("PROCUREMENT_API_URL")
        self.procurement_api_key = os.getenv("PROCUREMENT_API_KEY")
        
        # GMT API configuration
        self.gmt_base_url = os.getenv("GMT_BASE_URL", "https://devp2pindia-c5c7gfhhbsdxaycm.centralindia-01.azurewebsites.net")
        self.gmt_client_id = os.getenv("GMT_CLIENT_ID")
        self.gmt_client_secret = os.getenv("GMT_CLIENT_SECRET")
        self.gmt_username = os.getenv("GMT_USERNAME")
        self.gmt_password = os.getenv("GMT_PASSWORD")
        
        # Intent Service configuration
        self.intent_threshold_buy_something = int(os.getenv("INTENT_THRESHOLD_BUY_SOMETHING", "75"))
        self.intent_threshold_general_inquiry = int(os.getenv("INTENT_THRESHOLD_GENERAL_INQUIRY", "60"))
        self.intent_threshold_ambiguous = int(os.getenv("INTENT_THRESHOLD_AMBIGUOUS", "60"))
        
        # Session and timeout configuration
        self.session_timeout_hours = int(os.getenv("SESSION_TIMEOUT_HOURS", "12"))
        self.session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))  # Keep for backward compatibility
        self.cleanup_completed_sessions = os.getenv("CLEANUP_COMPLETED_SESSIONS", "true").lower() == "true"
        self.max_retry_attempts = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
        
        # Message retry configuration
        self.retry_max_attempts = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
        self.retry_initial_delay = float(os.getenv("RETRY_INITIAL_DELAY", "1.0"))
        
        # Validate configuration
        self.validate_config()
        
    def get_database_url(self) -> str:
        """Get database connection URL from environment."""
        if not self.database_url:
            raise ValueError("DATABASE_URL environment variable is required")
        return self.database_url
        
    def get_openai_config(self) -> Dict[str, Any]:
        """Get OpenAI API configuration and credentials."""
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")
        
        return {
            "api_key": self.openai_api_key,
            "default_model": self.openai_model_default,
            "advanced_model": self.openai_model_advanced,
            "azure_openai_base_url": self.azure_openai_base_url
        }
        
    def get_whatsapp_config(self) -> Dict[str, Optional[str]]:
        """Get WhatsApp API configuration and credentials."""
        return {
            "access_token": self.whatsapp_access_token,
            "verify_token": self.whatsapp_verify_token,
            "webhook_url": self.whatsapp_webhook_url
        }
        
    def get_logging_config(self) -> Dict[str, Any]:
        """Get logging configuration for the application."""
        return {
            "level": self.log_level,
            "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            "handlers": ["console"],
            "log_to_database": self.log_to_database,
            "retention_days": self.log_retention_days
        }
        
    def validate_config(self) -> None:
        """Validate that all required configuration is present."""
        required_vars = [
            ("DATABASE_URL", self.database_url),
            ("OPENAI_API_KEY", self.openai_api_key),
            ("GMT_CLIENT_ID", self.gmt_client_id),
            ("GMT_CLIENT_SECRET", self.gmt_client_secret),
            ("GMT_USERNAME", self.gmt_username),
            ("GMT_PASSWORD", self.gmt_password),
        ]
        
        missing_vars = []
        for var_name, var_value in required_vars:
            if not var_value:
                missing_vars.append(var_name)
        
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
        
        # Validate numeric configurations
        if self.session_timeout_hours <= 0:
            raise ValueError("SESSION_TIMEOUT_HOURS must be positive")
        
        if self.session_timeout_minutes <= 0:
            raise ValueError("SESSION_TIMEOUT_MINUTES must be positive")
        
        if self.max_retry_attempts <= 0:
            raise ValueError("MAX_RETRY_ATTEMPTS must be positive")

# Global settings instance
_settings: Optional[Settings] = None

def load_environment() -> None:
    """
    Load environment variables from .env file.
    
    Loads configuration from environment file and validates
    that all required settings are present.
    """
    global _settings
    _settings = Settings()

def get_settings() -> Settings:
    """
    Get application settings singleton.
    
    Returns the configured settings instance for the application.
    """
    global _settings
    if _settings is None:
        load_environment()
    return _settings