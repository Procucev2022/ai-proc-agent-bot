"""
Welcome message service for managing first-time user greetings.

This service manages welcome message flags in Redis to ensure users
receive welcome messages only once per day. The flags automatically
expire at midnight to reset for the next day.
"""

import logging
from typing import Optional
from datetime import datetime, timedelta
from app.redis_db import get_redis_service

logger = logging.getLogger(__name__)


class WelcomeMessageService:
    """
    Service for managing welcome message flags with automatic midnight expiry.
    
    Uses Redis to track which phone numbers have already received welcome
    messages, with automatic expiry at midnight to reset daily.
    """
    
    def __init__(self):
        self.redis_service = get_redis_service()
        self.welcome_flag_prefix = "welcome_msg"
    
    def _get_welcome_key(self, phone_number: str) -> str:
        """Generate Redis key for welcome message flag."""
        clean_phone = ''.join(filter(str.isdigit, phone_number))
        return f"{self.welcome_flag_prefix}:{clean_phone}"
    
    async def should_send_welcome(self, phone_number: str) -> bool:
        """
        Check if welcome message should be sent to this phone number.
        
        Returns True if no welcome flag exists (first message of the day),
        False if welcome was already sent today.
        """
        try:
            welcome_key = self._get_welcome_key(phone_number)
            flag_exists = await self.redis_service.exists(welcome_key)
            
            if flag_exists:
                logger.info(f"Welcome message already sent to {phone_number} today")
                return False
            
            logger.info(f"Welcome message should be sent to {phone_number}")
            return True
            
        except Exception as e:
            logger.error(f"Error checking welcome flag for {phone_number}: {e}")
            return True
    
    async def mark_welcome_sent(self, phone_number: str) -> bool:
        """
        Mark that welcome message has been sent to this phone number.
        
        Sets Redis flag that expires at next midnight (12:00 AM).
        """
        try:
            welcome_key = self._get_welcome_key(phone_number)
            
            # Calculate next midnight timestamp
            now = datetime.now()
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            midnight_timestamp = int(next_midnight.timestamp())
            
            # Set key and expire at midnight
            await self.redis_service.set(welcome_key, "true")
            success = await self.redis_service.expireat(welcome_key, midnight_timestamp)
            
            if success:
                logger.info(f"Welcome flag set for {phone_number}, expires at midnight")
            else:
                logger.error(f"Failed to set welcome flag expiry for {phone_number}")
            
            return success
            
        except Exception as e:
            logger.error(f"Error setting welcome flag for {phone_number}: {e}")
            return False
    
    async def reset_welcome_flag(self, phone_number: str) -> bool:
        """
        Manually reset welcome flag for a phone number.
        
        Removes the welcome flag, allowing welcome message to be sent
        immediately. Useful for testing or manual reset scenarios.
        """
        try:
            welcome_key = self._get_welcome_key(phone_number)
            success = await self.redis_service.delete(welcome_key)
            
            if success:
                logger.info(f"Welcome flag reset for {phone_number}")
            else:
                logger.info(f"No welcome flag found for {phone_number} to reset")
            
            return success
            
        except Exception as e:
            logger.error(f"Error resetting welcome flag for {phone_number}: {e}")
            return False
    
    async def check_and_send_welcome(self, phone_number: str, whatsapp_service) -> bool:
        """
        Check if welcome message should be sent and send it if needed.
        
        Returns True if welcome message was sent, False otherwise.
        """
        import time
        t0 = time.time()
        try:
            should_send = await self.should_send_welcome(phone_number)
            check_time = time.time() - t0
            logger.debug(f"[WELCOME] Flag check for {phone_number} completed in {check_time:.3f}s: should_send={should_send}")

            if should_send:
                welcome_text = (
                    "Hello Namaste 🙏, I'm Qua – Your Procurement Partner.\n"
                    "Thank you for contacting me. Let me check if you have visited us earlier..."
                )
                
                send_start = time.time()
                message_response = await whatsapp_service.send_message(phone_number, welcome_text)
                send_time = time.time() - send_start

                if message_response.success:
                    await self.mark_welcome_sent(phone_number)
                    msg_id = getattr(message_response, 'message_id', 'N/A')
                    logger.info(f"[WELCOME] Welcome message successfully sent to {phone_number} in {send_time:.3f}s (msg_id={msg_id})")
                    return True
                else:
                    err = getattr(message_response, 'error', 'unknown')
                    logger.error(f"[WELCOME] Failed to send welcome message to {phone_number} after {send_time:.3f}s: error={err}")
                    return False
            
            return False
            
        except Exception as e:
            logger.error(f"[WELCOME] Error checking and sending welcome message for {phone_number}: {e}", exc_info=True)
            return False


# Singleton instance
_welcome_service: Optional[WelcomeMessageService] = None

def get_welcome_service() -> WelcomeMessageService:
    """Get welcome message service singleton."""
    pass
    global _welcome_service
    if _welcome_service is None:
        _welcome_service = WelcomeMessageService()
    return _welcome_service