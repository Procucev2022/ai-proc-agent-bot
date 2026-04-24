"""
Simple logging utilities for the AI Procurement Agent.
"""

import logging
import os
from concurrent_log_handler import ConcurrentTimedRotatingFileHandler
from typing import Dict, Any, Optional
from datetime import datetime
from functools import wraps
from contextvars import ContextVar
import threading

# Context variables for storing user phone number across async contexts
_user_phone_context: ContextVar[Optional[str]] = ContextVar('user_phone', default=None)
_thread_local = threading.local()


class CustomFormatter(logging.Formatter):
    """Custom formatter that includes timestamp, source, and user phone number."""

    def format(self, record):
        # Add timestamp
        record.timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        # Add source (module name)
        record.source = record.name

        # Try to get phone number from context or record
        phone = None
        if hasattr(record, 'phone_number'):
            phone = record.phone_number
        else:
            # Try to get from context variable
            try:
                phone = _user_phone_context.get()
            except:
                pass

            # Fallback to thread local
            if not phone:
                phone = getattr(_thread_local, 'phone_number', None)

        record.phone_number = phone if phone else 'N/A'

        # Format: timestamp | phone_number | source | level | message
        return f"{record.timestamp} | {record.phone_number} | {record.source} | {record.levelname} | {record.getMessage()}"


def set_user_phone_context(phone_number: str):
    """Set the user phone number in the current context."""
    _user_phone_context.set(phone_number)
    _thread_local.phone_number = phone_number


def clear_user_phone_context():
    """Clear the user phone number from the current context."""
    _user_phone_context.set(None)
    if hasattr(_thread_local, 'phone_number'):
        delattr(_thread_local, 'phone_number')


def get_user_phone_context() -> Optional[str]:
    """Get the current user phone number from context."""
    try:
        phone = _user_phone_context.get()
        if phone:
            return phone
    except:
        pass
    return getattr(_thread_local, 'phone_number', None)


class UserPhoneContext:
    """Context manager for setting user phone number in logs."""

    def __init__(self, phone_number: str):
        self.phone_number = phone_number
        self.previous_phone = None

    def __enter__(self):
        self.previous_phone = get_user_phone_context()
        set_user_phone_context(self.phone_number)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.previous_phone:
            set_user_phone_context(self.previous_phone)
        else:
            clear_user_phone_context()
        return False

    async def __aenter__(self):
        self.previous_phone = get_user_phone_context()
        set_user_phone_context(self.phone_number)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.previous_phone:
            set_user_phone_context(self.previous_phone)
        else:
            clear_user_phone_context()
        return False


def setup_basic_logging(level: str = "INFO"):
    """Setup logging: single app.log rotated daily at midnight, multi-process safe."""
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    formatter = CustomFormatter()

    log_dir = os.getenv("LOG_DIR", "/app/logs/app")
    os.makedirs(log_dir, exist_ok=True)

    file_handler = ConcurrentTimedRotatingFileHandler(
        filename=os.path.join(log_dir, "app.log"),
        when="midnight",
        interval=1,
        backupCount=int(os.getenv("LOG_BACKUP_COUNT", "30")),
        encoding="utf-8",
        utc=False,
        delay=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(formatter)

    root_logger.setLevel(getattr(logging, level))
    root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    noisy_loggers = [
        "openai",
        "openai._base_client",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpx",
        "urllib3",
        "asyncio",
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

def log_info(logger: logging.Logger, message: str, user_id: str = None, phone_number: str = None, **kwargs):
    """Log info message with optional context."""
    extra = {}
    if phone_number:
        extra['phone_number'] = phone_number

    if user_id or kwargs:
        context = f"[user={user_id}]" if user_id else ""
        if kwargs:
            context += f" {kwargs}"
        message = f"{message} {context}"

    logger.info(message, extra=extra)

def log_error(logger: logging.Logger, message: str, error: Exception = None, user_id: str = None, phone_number: str = None, **kwargs):
    """Log error message with optional context."""
    extra = {}
    if phone_number:
        extra['phone_number'] = phone_number

    if user_id or kwargs:
        context = f"[user={user_id}]" if user_id else ""
        if kwargs:
            context += f" {kwargs}"
        message = f"{message} {context}"

    if error:
        logger.error(f"{message} - Error: {str(error)}", exc_info=True, extra=extra)
    else:
        logger.error(message, extra=extra)

def log_debug(logger: logging.Logger, message: str, user_id: str = None, phone_number: str = None, **kwargs):
    """Log debug message with optional context."""
    extra = {}
    if phone_number:
        extra['phone_number'] = phone_number

    if user_id or kwargs:
        context = f"[user={user_id}]" if user_id else ""
        if kwargs:
            context += f" {kwargs}"
        message = f"{message} {context}"

    logger.debug(message, extra=extra)

def log_to_database(level: str, message: str, service: str = None, user_id: str = None, 
                   session_id: str = None, context: dict = None):
    """Log message to database for persistence."""
    try:
        from app.database import get_db_session
        from app.models import SystemLog
        
        db = get_db_session()
        
        log_entry = SystemLog(
            level=level,
            message=message,
            service=service,
            user_id=user_id,
            session_id=session_id,
            context=context
        )
        
        db.add(log_entry)
        db.commit()
        db.close()
        
    except Exception as e:
        # Don't let logging errors break the application
        logging.getLogger(__name__).error(f"Failed to log to database: {str(e)}")

def log_service_method(service_name: str):
    """Decorator to automatically log service method calls."""
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            logger = logging.getLogger(f"app.services.{service_name}")
            user_id = kwargs.get('user_phone') or kwargs.get('user_id')
            
            logger.info(f"{func.__name__} called", extra={'user_id': user_id})
            
            try:
                result = await func(*args, **kwargs)
                logger.info(f"{func.__name__} completed", extra={'user_id': user_id})
                return result
            except Exception as e:
                logger.error(f"{func.__name__} failed: {str(e)}", extra={'user_id': user_id}, exc_info=True)
                raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            logger = logging.getLogger(f"app.services.{service_name}")
            user_id = kwargs.get('user_phone') or kwargs.get('user_id')
            
            logger.info(f"{func.__name__} called", extra={'user_id': user_id})
            
            try:
                result = func(*args, **kwargs)
                logger.info(f"{func.__name__} completed", extra={'user_id': user_id})
                return result
            except Exception as e:
                logger.error(f"{func.__name__} failed: {str(e)}", extra={'user_id': user_id}, exc_info=True)
                raise
        
        return async_wrapper if hasattr(func, '__code__') and func.__code__.co_flags & 0x80 else sync_wrapper
    return decorator