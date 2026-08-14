"""
Simple message retry service for WhatsApp message failures.

Provides basic retry functionality with exponential backoff for failed
WhatsApp message deliveries.
"""

import asyncio
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class RetryService:
    """Simple retry service for WhatsApp messages."""
    
    def __init__(self, max_retries: int = 3, initial_delay: float = 1.0):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
    
    async def retry_with_backoff(self, func, *args, **kwargs) -> Dict[str, Any]:
        """
        Retry a function with exponential backoff.
        
        Args:
            func: Function to retry
            *args, **kwargs: Arguments for the function
            
        Returns:
            Result from successful function call or error info
        """
        last_error = None
        
        for attempt in range(self.max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                
                # Check if result indicates success
                if hasattr(result, 'success') and result.success:
                    if attempt > 0:
                        logger.info(f"Message sent successfully on attempt {attempt + 1}")
                    return {"success": True, "result": result, "attempts": attempt + 1}
                elif isinstance(result, dict) and result.get('success'):
                    if attempt > 0:
                        logger.info(f"Message sent successfully on attempt {attempt + 1}")
                    return {"success": True, "result": result, "attempts": attempt + 1}
                else:
                    # Treat as failure, continue to retry
                    last_error = f"Function returned unsuccessful result: {result}"

                    # A permanent failure such as a malformed recipient number
                    # cannot be fixed by trying again. Give up now rather than
                    # spending the whole backoff schedule on it.
                    if not self._is_retryable(result):
                        logger.error(f"Not retrying permanent failure: {last_error}")
                        return {"success": False, "error": last_error, "attempts": attempt + 1}
                    
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt + 1} failed: {last_error}")
            
            # Don't sleep after the last attempt
            if attempt < self.max_retries:
                delay = self.initial_delay * (2 ** attempt)
                logger.info(f"Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
        
        logger.error(f"All {self.max_retries + 1} attempts failed. Last error: {last_error}")
        return {"success": False, "error": last_error, "attempts": self.max_retries + 1}

    @staticmethod
    def _is_retryable(result: Any) -> bool:
        """
        Decide whether an unsuccessful result is worth another attempt.

        Callers opt out by setting a falsey ``retryable`` on the result. Anything
        that does not say otherwise stays retryable, so existing callers keep
        their current behaviour.
        """
        if isinstance(result, dict):
            return bool(result.get('retryable', True))
        return bool(getattr(result, 'retryable', True))


# Global instance
_retry_service = RetryService()


def get_retry_service() -> RetryService:
    """Get the global retry service instance."""
    return _retry_service