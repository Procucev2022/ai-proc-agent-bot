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
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional, Dict, Any

# Project root directory (parent of 'app' folder)
PROJECT_ROOT = Path(__file__).parent.parent.resolve()

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
        
        # Project root directory (used for relative paths)
        self.PROJECT_ROOT = PROJECT_ROOT
        
        # Database configuration
        self.local_database_url = os.getenv("LOCAL_DATABASE_URL")
        self.client_database_url = os.getenv("CLIENT_DATABASE_URL")
        self.remote_database_url = os.getenv("REMOTE_DATABASE_URL")
        self.database_mode = os.getenv("DATABASE_MODE", "local")
        self.enable_remote_categorization = os.getenv("ENABLE_REMOTE_CATEGORIZATION", "").lower() == "true"
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
        self.WHATSAPP_MEDIA_DOWNLOAD_URL = os.getenv("WHATSAPP_MEDIA_DOWNLOAD_URL", "https://download.sendmsg.in/whatsapp-mediadownloader")
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
        self.redis_expiry_seconds = int(os.getenv("REDIS_EXPIRY_SECONDS", "43200"))  # 12 hours default for token inactivity
        
        # Celery configuration
        self.celery_broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6380/0")
        self.celery_result_backend = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6380/0")
        self.celery_worker_concurrency = int(os.getenv("CELERY_WORKER_CONCURRENCY", "2"))
        self.celery_task_time_limit = int(os.getenv("CELERY_TASK_TIME_LIMIT", "360"))
        self.celery_beat_schedule_enabled = os.getenv("CELERY_BEAT_SCHEDULE_ENABLED", "true").lower() == "true"

        # ChromaDB configuration (server mode for multi-worker support)
        self.chroma_host = os.getenv("CHROMA_HOST", "localhost")
        self.chroma_port = int(os.getenv("CHROMA_PORT", "8100"))
        self.chroma_use_server = os.getenv("CHROMA_USE_SERVER", "true").lower() == "true"
        # Fallback paths for PersistentClient (used when server is not available)
        # Use absolute paths based on project root to avoid issues with current working directory
        self.chroma_persist_directory = os.getenv("CHROMA_PERSIST_DIRECTORY", str(PROJECT_ROOT / "chroma_db"))
        self.chroma_unified_persist_directory = os.getenv("CHROMA_UNIFIED_PERSIST_DIRECTORY", str(PROJECT_ROOT / "unified_chroma_db"))

        # Redis session storage configuration
        self.redis_session_storage_enabled = os.getenv("REDIS_SESSION_STORAGE_ENABLED", "true").lower() == "true"
        self.redis_session_ttl_seconds = int(os.getenv("REDIS_SESSION_TTL_SECONDS", "900"))  # 15 minutes default (timeout + buffer)
        
        # Security configuration
        self.secret_key = os.getenv("SECRET_KEY")
        self.allowed_hosts = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
        self.allowed_ips = os.getenv("ALLOWED_IPS", "").split(",") if os.getenv("ALLOWED_IPS") else []
        
        # Additional fields needed by services
        self.DEBUG = self.debug
        self.ALLOWED_ORIGINS = self.allowed_hosts
        
        # Logging configuration
        self.log_to_database = os.getenv("LOG_TO_DATABASE", "false").lower() == "true"
        self.log_retention_days = int(os.getenv("LOG_RETENTION_DAYS", "7"))
        self.archive_retention_days = int(os.getenv("ARCHIVE_RETENTION_DAYS", "30"))
        self.enable_log_cleanup = os.getenv("ENABLE_LOG_CLEANUP", "true").lower() == "true"
        
        # External API configuration
        self.procurement_api_url = os.getenv("PROCUREMENT_API_URL")
        self.procurement_api_key = os.getenv("PROCUREMENT_API_KEY")
        
        # GMT API configuration - Updated for new API
        self.gmt_base_url = os.getenv("GMT_BASE_URL", "https://p2pv1servicesdev-etfrcte5fhdvfrd4.centralindia-01.azurewebsites.net")
        self.gmt_username = os.getenv("GMT_USERNAME")
        self.gmt_phone = os.getenv("GMT_PHONE")
        # Legacy OAuth fields (kept for backward compatibility)
        self.gmt_client_id = os.getenv("GMT_CLIENT_ID")
        self.gmt_client_secret = os.getenv("GMT_CLIENT_SECRET")
        self.gmt_password = os.getenv("GMT_PASSWORD")
        self.gmt_max_retries = int(os.getenv("GMT_MAX_RETRIES", "3"))
        self.gmt_retry_delay = float(os.getenv("GMT_RETRY_DELAY", "1.0"))
        
        # Enhanced API timeout configuration
        self.api_connect_timeout = int(os.getenv("API_CONNECT_TIMEOUT", "10"))
        self.api_read_timeout = int(os.getenv("API_READ_TIMEOUT", "30"))
        self.api_total_timeout = int(os.getenv("API_TOTAL_TIMEOUT", "60"))
        
        # OTP rate limiting configuration
        self.otp_max_attempts = int(os.getenv("OTP_MAX_ATTEMPTS", "3"))
        self.otp_cooldown_minutes = int(os.getenv("OTP_COOLDOWN_MINUTES", "5"))
        
        # Intent Service configuration
        self.intent_threshold_buy_something = int(os.getenv("INTENT_THRESHOLD_BUY_SOMETHING", "75"))
        self.intent_threshold_general_inquiry = int(os.getenv("INTENT_THRESHOLD_GENERAL_INQUIRY", "60"))
        self.intent_threshold_ambiguous = int(os.getenv("INTENT_THRESHOLD_AMBIGUOUS", "60"))
        
        # Session and timeout configuration
        self.session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
        self.session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
        self.cleanup_completed_sessions = os.getenv("CLEANUP_COMPLETED_SESSIONS", "true").lower() == "true"
        self.max_retry_attempts = int(os.getenv("MAX_RETRY_ATTEMPTS", "3"))
        
        # Chat summarization configuration
        self.chat_summary_retention_days = int(os.getenv("CHAT_SUMMARY_RETENTION_DAYS", "90"))
        self.daily_summary_retention_days = int(os.getenv("DAILY_SUMMARY_RETENTION_DAYS", "365"))
        self.enable_session_summarization = os.getenv("ENABLE_SESSION_SUMMARIZATION", "true").lower() == "true"
        self.enable_daily_summarization = os.getenv("ENABLE_DAILY_SUMMARIZATION", "true").lower() == "true"
        self.max_chat_summaries_for_context = int(os.getenv("MAX_CHAT_SUMMARIES_FOR_CONTEXT", "3"))
        self.summarization_model = os.getenv("SUMMARIZATION_MODEL", "gpt-4o-mini")
        self.max_context_tokens = int(os.getenv("MAX_CONTEXT_TOKENS", "4000"))

        # RFQ Workflow configuration (Global application-level setting)
        # Set to "sectioned" to use the new sectioned RFQ workflow (Track 3) - DEFAULT
        # Set to "legacy" to use the old RFQ creation workflow (Track 1/2)
        self.rfq_workflow_mode = os.getenv("RFQ_WORKFLOW_MODE", "sectioned").lower()
        self.use_sectioned_rfq = self.rfq_workflow_mode == "sectioned"
        
        # Daily aggregation configuration
        self.enable_daily_aggregation = os.getenv("ENABLE_DAILY_AGGREGATION", "true").lower() == "true"
        self.aggregation_retention_days = int(os.getenv("AGGREGATION_RETENTION_DAYS", "730"))  # 2 years
        self.aggregation_schedule_hour = int(os.getenv("AGGREGATION_SCHEDULE_HOUR", "2"))  # 2 AM
        
        # Rate limiting configuration
        self.rate_limit_default = os.getenv("RATE_LIMIT_DEFAULT", "100/hour")
        self.rate_limit_chat = os.getenv("RATE_LIMIT_CHAT", "20/minute")
        self.rate_limit_upload = os.getenv("RATE_LIMIT_UPLOAD", "5/minute")
        self.rate_limit_otp = os.getenv("RATE_LIMIT_OTP", "3/hour")
        self.rate_limit_auth = os.getenv("RATE_LIMIT_AUTH", "10/minute")
        
        # Message retry configuration
        self.retry_max_attempts = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
        self.retry_initial_delay = float(os.getenv("RETRY_INITIAL_DELAY", "1.0"))
        self.retry_exponential_base = float(os.getenv("RETRY_EXPONENTIAL_BASE", "2.0"))
        self.retry_max_delay = float(os.getenv("RETRY_MAX_DELAY", "30.0"))
        
        # RFQ Status settings
        self.rfq_max_allowed = int(os.getenv("RFQ_MAX_ALLOWED", "5"))
        self.rfq_followup_note = os.getenv("RFQ_FOLLOWUP_NOTE",
                                           "https://p2pdevuiindia.azurewebsites.net/login")

        # Procucev website URL for RFQ details (used in "Check Details" button)
        self.procucev_rfq_details_url = os.getenv(
            "PROCUCEV_RFQ_DETAILS_URL",
            "https://p2pdevuiindia.azurewebsites.net/login"
        )

        # Fetch RFQ limit
        self.rfq_fetch_limit = int(os.getenv("RFQ_FETCH_LIMIT", "3"))
        self.procucev_link = os.getenv("PROCUCEV_LINK", "www.procucev.com")
        
        # Email configuration
        self.support_email = os.getenv("SUPPORT_EMAIL", "support@procucev.com")
        self.email_report_sender = os.getenv("EMAIL_REPORT_SENDER", "shubham@mohap.ai")
        self.email_signature = os.getenv("EMAIL_SIGNATURE", "Regards\nQUA")
        self.email_templates_path = os.getenv("EMAIL_TEMPLATES_PATH", os.path.join("app", "email_templates"))
        
        # Error handling configuration
        self.enable_error_notifications = os.getenv("ENABLE_ERROR_NOTIFICATIONS", "true").lower() == "true"
        self.error_notification_cooldown_minutes = int(os.getenv("ERROR_NOTIFICATION_COOLDOWN_MINUTES", "5"))
        
        # Support configuration
        self.support_contact_info = os.getenv("SUPPORT_CONTACT_INFO", "info@procucev.com")
        self.contact_email = os.getenv("CONTACT_EMAIL", "contact@procucev.com")
        self.support_team_email = os.getenv("SUPPORT_TEAM_EMAILS", "priya.soni@mohap.ai")
        self.support_team_email = os.getenv("SUPPORT_TEAM_EMAILS", "priya.soni@mohap.ai")
        self.support_team_numbers = os.getenv("SUPPORT_TEAM_NUMBERS", "8824242260").split(",")

        # Seller Workflow Configuration
        self.seller_rfq_fetch_limit: int = 3
        self.seller_max_rfq_selection: int = 5
        self.eller_credit_check_enabled: bool = True
        self.seller_default_category: str = "General"

        # Payment Configuration
        self.payment_timeout_minutes: int = 5
        self.subscription_plan_cache_minutes: int = 30

        # Workflow Configuration
        self.seller_workflow_timeout_hours: int = 2
        self.seller_session_cleanup_enabled: bool = True

        # Response Configuration
        self.seller_ai_response_enabled: bool = True
        self.seller_fallback_responses_enabled: bool = True

        # Error Handling Configuration
        self.seller_max_retry_attempts: int = 2
        self.seller_error_escalation_enabled: bool = True

        # Monitoring Configuration
        self.seller_detailed_logging_enabled: bool = True
        self.seller_performance_monitoring: bool = True

        # Existing RFQ settings renamed/clarified for seller context
        self.rfq_max_allowed: int = 5  # Used for seller RFQ selection limit
        self.rfq_fetch_limit: int = 3  # Used for buyer RFQ creation, seller uses seller_rfq_fetch_limit

        # Queue Configuration
        self.batch_window_seconds: int = int(os.getenv("BATCH_WINDOW_SECONDS", "3"))
        self.please_wait_threshold_seconds: int = int(os.getenv("PLEASE_WAIT_THRESHOLD_SECONDS", "15"))
        self.max_please_wait_count: int = int(os.getenv("MAX_PLEASE_WAIT_COUNT", "3"))
        self.monitoring_poll_interval_seconds: int = int(os.getenv("MONITORING_POLL_INTERVAL_SECONDS", "2"))
        
        # Inactivity timeout configuration
        self.workflow_timeout_enabled: bool = os.getenv("WORKFLOW_TIMEOUT_ENABLED", "true").lower() == "true"
        self.workflow_timeout_seconds: int = int(os.getenv("WORKFLOW_TIMEOUT_SECONDS", "300"))  # 5 minutes default
        self.timeout_poll_interval_seconds: int = int(os.getenv("TIMEOUT_POLL_INTERVAL_SECONDS", "30"))  # Check every 30 seconds
        self.activity_key_ttl_seconds: int = int(os.getenv("ACTIVITY_KEY_TTL_SECONDS", "420"))  # 7 minutes default (buffer beyond timeout)
        
        # Worker timeout detection configuration
        self.pending_reply_ttl_seconds: int = int(os.getenv("PENDING_REPLY_TTL_SECONDS", "180"))  # 3 minutes default (longer than worker timeout)
        self.worker_timeout_threshold_seconds: int = int(os.getenv("WORKER_TIMEOUT_THRESHOLD_SECONDS", "135"))  # 2m15s default (2min gunicorn timeout + 15s buffer)

        # Feature Flags
        self.USE_TRACK2_RFQ_FLOW = os.getenv("USE_TRACK2_RFQ_FLOW", "false").lower() == "true"

        # Webhook Health Monitoring Configuration
        self.webhook_health_monitoring_enabled = os.getenv(
            "WEBHOOK_HEALTH_MONITORING_ENABLED", "true"
        ).lower() == "true"
        
        self.webhook_health_check_interval_seconds = int(
            os.getenv("WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS", "300")
        )

        self.webhook_api_response_threshold_seconds = float(
            os.getenv("WEBHOOK_API_RESPONSE_THRESHOLD_SECONDS", "5.0")
        )

        self.webhook_api_timeout_seconds = float(
            os.getenv("WEBHOOK_API_TIMEOUT_SECONDS", "10.0")
        )

        self.webhook_failure_grace_period_seconds = int(
            os.getenv("WEBHOOK_FAILURE_GRACE_PERIOD_SECONDS", "900")
        )
        
        self.webhook_recovery_confirmations = int(
            os.getenv("WEBHOOK_RECOVERY_CONFIRMATIONS", "3")
        )
        
        self.webhook_warning_consecutive_threshold = int(
            os.getenv("WEBHOOK_WARNING_CONSECUTIVE_THRESHOLD", "3")
        )
        
        # Webhook alert recipients - NO fallback, must be explicitly configured
        webhook_recipients = os.getenv("WEBHOOK_ALERT_RECIPIENTS", "")
        self.webhook_alert_recipients = self._parse_webhook_recipients(webhook_recipients)
        
        self.webhook_alert_state_ttl_seconds = int(
            os.getenv("WEBHOOK_ALERT_STATE_TTL_SECONDS", "3600")
        )

        # Validate configuration
        self.validate_config()
    
    def _parse_webhook_recipients(self, recipients: str) -> list:
        """
        Parse a comma-separated string of webhook recipients, stripping whitespace and removing empty entries.
        """
        if not recipients:
            return []
        return [r.strip() for r in recipients.split(",") if r.strip()]
        
    def get_database_url(self) -> str:
        """Get database connection URL based on selected mode."""
        if self.database_mode == "client":
            if not self.client_database_url:
                raise ValueError("CLIENT_DATABASE_URL environment variable is required for client mode")
            database_url = self.client_database_url
        else:
            if not self.local_database_url:
                raise ValueError("LOCAL_DATABASE_URL environment variable is required for local mode")
            database_url = self.local_database_url
        
        # Validate that URL is for MySQL (not PostgreSQL)
        if database_url.startswith("postgresql://"):
            raise ValueError("PostgreSQL URL detected. Please use MySQL URL format: mysql+pymysql://...")
        
        return database_url
    
    def get_remote_database_url(self) -> Optional[str]:
        """Get remote database URL for item categorization if enabled."""
        if not self.enable_remote_categorization:
            return None
        
        if not self.remote_database_url:
            raise ValueError("REMOTE_DATABASE_URL environment variable is required when ENABLE_REMOTE_CATEGORIZATION is true")
        
        return self.remote_database_url
    
    def is_ssl_enabled(self) -> bool:
        """Check if SSL should be enabled based on database mode."""
        return self.database_mode == "client"
        
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
            "webhook_url": self.whatsapp_webhook_url,
            "media_download_url": self.WHATSAPP_MEDIA_DOWNLOAD_URL,
            "from_number": self.WHATSAPP_FROM_NUMBER
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
            ("AZURE_OPENAI_API_KEY", self.openai_api_key),
            ("GMT_USERNAME", self.gmt_username),
            ("GMT_PHONE", self.gmt_phone),
        ]
        
        # Check database URL based on mode
        if self.database_mode == "client" and not self.client_database_url:
            required_vars.append(("CLIENT_DATABASE_URL", self.client_database_url))
        elif self.database_mode == "local" and not self.local_database_url:
            required_vars.append(("LOCAL_DATABASE_URL", self.local_database_url))
        
        missing_vars = []
        for var_name, var_value in required_vars:
            if not var_value:
                missing_vars.append(var_name)
        
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
        
        # Validate numeric configurations        
        if self.session_timeout_minutes <= 0:
            raise ValueError("SESSION_TIMEOUT_MINUTES must be positive")
        
        if self.max_retry_attempts <= 0:
            raise ValueError("MAX_RETRY_ATTEMPTS must be positive")
        
        # Validate timeout configurations
        if self.api_connect_timeout <= 0:
            raise ValueError("API_CONNECT_TIMEOUT must be positive")
        
        if self.api_read_timeout <= 0:
            raise ValueError("API_READ_TIMEOUT must be positive")
        
        if self.otp_max_attempts <= 0 or self.otp_max_attempts > 5:
            raise ValueError("OTP_MAX_ATTEMPTS must be between 1 and 5")
        
        # Validate support team numbers
        if self.support_team_numbers:
            self.support_team_numbers = [num.strip() for num in self.support_team_numbers if num.strip()]
        
        # Validate webhook health monitoring configuration
        if self.webhook_health_monitoring_enabled:
            # Ensure check interval is greater than timeout (timeout must complete before next check)
            if self.webhook_health_check_interval_seconds <= self.webhook_api_timeout_seconds:
                raise ValueError(
                    "WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS must be greater than WEBHOOK_API_TIMEOUT_SECONDS. "
                    f"Got interval={self.webhook_health_check_interval_seconds}s, timeout={self.webhook_api_timeout_seconds}s"
                )
            
            # Ensure check interval is less than grace period (need multiple checks within grace period)
            if self.webhook_health_check_interval_seconds >= self.webhook_failure_grace_period_seconds:
                raise ValueError(
                    "WEBHOOK_HEALTH_CHECK_INTERVAL_SECONDS must be less than WEBHOOK_FAILURE_GRACE_PERIOD_SECONDS. "
                    f"Got interval={self.webhook_health_check_interval_seconds}s, grace_period={self.webhook_failure_grace_period_seconds}s"
                )
            
            # Ensure response threshold is less than timeout
            if self.webhook_api_response_threshold_seconds >= self.webhook_api_timeout_seconds:
                raise ValueError(
                    "WEBHOOK_API_RESPONSE_THRESHOLD_SECONDS must be less than WEBHOOK_API_TIMEOUT_SECONDS. "
                    f"Got threshold={self.webhook_api_response_threshold_seconds}s, timeout={self.webhook_api_timeout_seconds}s"
                )
            
            # Ensure positive values
            if self.webhook_recovery_confirmations < 1:
                raise ValueError("WEBHOOK_RECOVERY_CONFIRMATIONS must be at least 1")
            
            if self.webhook_warning_consecutive_threshold < 1:
                raise ValueError("WEBHOOK_WARNING_CONSECUTIVE_THRESHOLD must be at least 1")
            
            # Note: webhook_alert_recipients is optional - if not configured, alerts will only be logged
    
    def get_rfq_status_config(self) -> Dict[str, Any]:
        """Get configuration for RFQ status logic."""
        return {
            "max_allowed": self.rfq_max_allowed,
            "followup_note": self.rfq_followup_note
        }
    
    def get_api_timeout_config(self) -> Dict[str, int]:
        """Get API timeout configuration."""
        return {
            "connect_timeout": self.api_connect_timeout,
            "read_timeout": self.api_read_timeout,
            "total_timeout": self.api_total_timeout
        }
    
    def get_retry_config(self) -> Dict[str, Any]:
        """Get retry configuration with exponential backoff."""
        return {
            "max_attempts": self.retry_max_attempts,
            "initial_delay": self.retry_initial_delay,
            "exponential_base": self.retry_exponential_base,
            "max_delay": self.retry_max_delay
        }
    
    def get_celery_config(self) -> Dict[str, Any]:
        """Get Celery configuration."""
        return {
            "broker_url": self.celery_broker_url,
            "result_backend": self.celery_result_backend,
            "worker_concurrency": self.celery_worker_concurrency,
            "task_time_limit": self.celery_task_time_limit,
            "beat_schedule_enabled": self.celery_beat_schedule_enabled
        }

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