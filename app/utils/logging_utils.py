"""
Simple logging utilities for the AI Procurement Agent.
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime
from functools import wraps

def setup_basic_logging(level: str = "INFO"):
    """Setup basic logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

def log_info(logger: logging.Logger, message: str, user_id: str = None, **kwargs):
    """Log info message with optional context."""
    if user_id or kwargs:
        context = f"[user={user_id}]" if user_id else ""
        if kwargs:
            context += f" {kwargs}"
        message = f"{message} {context}"
    logger.info(message)

def log_error(logger: logging.Logger, message: str, error: Exception = None, user_id: str = None, **kwargs):
    """Log error message with optional context."""
    if user_id or kwargs:
        context = f"[user={user_id}]" if user_id else ""
        if kwargs:
            context += f" {kwargs}"
        message = f"{message} {context}"
    
    if error:
        logger.error(f"{message} - Error: {str(error)}", exc_info=True)
    else:
        logger.error(message)

def log_debug(logger: logging.Logger, message: str, user_id: str = None, **kwargs):
    """Log debug message with optional context."""
    if user_id or kwargs:
        context = f"[user={user_id}]" if user_id else ""
        if kwargs:
            context += f" {kwargs}"
        message = f"{message} {context}"
    logger.debug(message)

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