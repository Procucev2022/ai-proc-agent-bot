"""
Session management helpers for ChatService.

Contains session-related utility methods extracted from the main 
ChatService class for better organization.
"""

from datetime import datetime, timedelta
from app.config import get_settings
from app.models import ConversationSession


class SessionHelpers:
    """Session management helper methods extracted from ChatService."""
    
    @staticmethod
    async def is_session_expired(session: ConversationSession) -> bool:
        """Check if session has expired based on 12-hour timeout."""
        if not session or not session.created_at:
            return False
        
        timeout_hours = get_settings().session_timeout_hours
        expired_time = datetime.now() - timedelta(hours=timeout_hours)
        return session.created_at < expired_time