"""
Technical Failure Handler Utility

Provides a simple function to handle technical failures with clean exit.
"""

import logging
from app.services.global_error_handler import get_global_error_handler, ErrorContext

logger = logging.getLogger(__name__)

async def handle_technical_failure(
    user_phone: str = None,
    error_message: str = "Technical failure occurred",
    error_type: str = "Technical Error"
) -> None:
    """
    Handle technical failure with clean exit message to user.
    
    Args:
        user_phone: User's phone number (optional)
        error_message: Error description for logging
        error_type: Type of error for categorization
    """
    try:
        error_context = ErrorContext(
            error_type=error_type,
            error_message=error_message,
            user_phone=user_phone
        )
        
        handler = get_global_error_handler()
        await handler.handle_error(error_context)
        
        logger.info(f"Technical failure handled for user: {user_phone}")
        
    except Exception as e:
        logger.error(f"Failed to handle technical failure: {e}")