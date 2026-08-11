"""
Asynchronous Redis service with connection pooling and specialized operations.
"""

import redis.asyncio as aioredis
import json
import logging
from typing import Optional, Dict, Any
from app.schemas.user import User
from app.config import get_settings

logger = logging.getLogger(__name__)

class AsyncRedisConnectionManager:
    """Manages async Redis connection pool."""
    _pool: Optional[aioredis.Redis] = None

    @classmethod
    async def get_client(cls) -> aioredis.Redis:
        """Get or create async Redis client with connection pooling."""
        if cls._pool is None:
            settings = get_settings()
            logger.info("[REDIS] Connecting to Redis pool")
            try:
                cls._pool = await aioredis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    max_connections=20,
                    socket_timeout=5.0,
                    socket_connect_timeout=5.0
                )
                logger.info(f"[REDIS] ✓ Connected to Redis pool successfully")
            except Exception as e:
                logger.error(f"[REDIS] ⚠️ Failed to connect to Redis pool: {e}")
                raise
        return cls._pool


class BaseRedisService:
    """Base async Redis service with common operations."""
    
    def __init__(self):
        self.client: Optional[aioredis.Redis] = None

    async def init_client(self):
        if self.client is None:
            self.client = await AsyncRedisConnectionManager.get_client()

    async def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:
        await self.init_client()
        try:
            if isinstance(value, dict):
                value = json.dumps(value)
            if ex:
                return await self.client.set(key, value, ex=ex)
            return await self.client.set(key, value)
        except Exception as e:
            logger.error(f"Redis SET error for key {key}: {e}")
            return False

    async def get(self, key: str, as_json: bool = False) -> Optional[Any]:
        await self.init_client()
        try:
            value = await self.client.get(key)
            if value and as_json:
                return json.loads(value)
            return value
        except Exception as e:
            logger.error(f"Redis GET error for key {key}: {e}")
            return None

    async def delete(self, key: str) -> bool:
        await self.init_client()
        try:
            return await self.client.delete(key) > 0
        except Exception as e:
            logger.error(f"Redis DELETE error for key {key}: {e}")
            return False

    async def exists(self, key: str) -> bool:
        await self.init_client()
        try:
            return await self.client.exists(key) > 0
        except Exception as e:
            logger.error(f"Redis EXISTS error for key {key}: {e}")
            return False
    
    async def ttl(self, key: str) -> Optional[int]:
        await self.init_client()
        try:
            return await self.client.ttl(key)
        except Exception as e:
            logger.error(f"Redis TTL error for key {key}: {e}")
            return None
    
    async def expire(self, key: str, seconds: int) -> bool:
        """Set expiry time for a key."""
        await self.init_client()
        try:
            return await self.client.expire(key, seconds)
        except Exception as e:
            logger.error(f"Redis EXPIRE error for key {key}: {e}")
            return False
    
    async def expireat(self, key: str, timestamp: int) -> bool:
        """Set expiry time for a key at specific Unix timestamp."""
        await self.init_client()
        try:
            return await self.client.expireat(key, timestamp)
        except Exception as e:
            logger.error(f"Redis EXPIREAT error for key {key}: {e}")
            return False
    
    async def incr(self, key: str) -> Optional[int]:
        await self.init_client()
        try:
            return await self.client.incr(key)
        except Exception as e:
            logger.error(f"Redis INCR error for key {key}: {e}")
            return None

    async def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a pattern using SCAN."""
        await self.init_client()
        try:
            deleted_count = 0
            cursor = 0
            while True:
                cursor, keys = await self.client.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted_count += await self.client.delete(*keys)
                if cursor == 0:
                    break
            logger.info(f"Deleted {deleted_count} keys matching pattern '{pattern}'")
            return deleted_count
        except Exception as e:
            logger.error(f"Redis SCAN/DELETE error for pattern {pattern}: {e}")
            return 0


class AuthRedisService(BaseRedisService):
    """Specialized async Redis service for authentication."""

    def __init__(self):
        super().__init__()
        self.settings = get_settings()

    async def store(self, phone_number: str, user_data: Dict[str, Any], expiry_seconds: Optional[int] = None) -> bool:
        key = f"auth:{phone_number}"
        if expiry_seconds is None:
            expiry_seconds = self.settings.redis_expiry_seconds 
        return await self.set(key, user_data, expiry_seconds)

    async def retrieve(self, phone_number: str) -> Optional[User]:
        key = f"auth:{phone_number}"
        data = await self.get(key, as_json=True)
        if data:
            # Refresh token on successful retrieval (user activity)
            await self.refresh_user_token(phone_number)
            try:
                return User.from_mixed_data(data)
            except Exception as e:
                logger.error(f"Error creating User from stored data for {phone_number}: {e}")
                return None
        return None

    async def delete_auth(self, phone_number: str) -> bool:
        key = f"auth:{phone_number}"
        return await self.delete(key)

    async def is_authenticated(self, phone_number: str) -> bool:
        key = f"auth:{phone_number}"
        return await self.exists(key)
    
    async def refresh_user_token(self, phone_number: str) -> bool:
        """Refresh user token to extend session for active users."""
        key = f"auth:{phone_number}"
        if await self.exists(key):
            try:
                await self.init_client()
                return await self.client.expire(key, 43200)  # Reset to 12 hours
            except Exception as e:
                logger.error(f"Redis token refresh error for key {key}: {e}")
                return False
        return False


class SessionRedisService(BaseRedisService):
    """Specialized async Redis service for conversation session storage."""

    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        # Default TTL = workflow_timeout + 10-minute buffer (safety net for missed timeouts)
        # InactivityTimeoutService handles timeout at workflow_timeout_seconds
        # Redis TTL expires sessions at workflow_timeout_seconds + 600 (cleanup safety net)
        self.default_ttl = self.settings.workflow_timeout_seconds + 600

    async def store_session(self, session_id: str, session_data: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        """
        Store session in Redis with TTL.

        Args:
            session_id: Unique session identifier
            session_data: Session data dictionary
            ttl: Time to live in seconds (defaults to 30 minutes)

        Returns:
            True if stored successfully
        """
        key = f"session:{session_id}"
        ttl = ttl or self.default_ttl
        return await self.set(key, session_data, ex=ttl)

    async def save_session(self, session_data: Dict[str, Any], ttl: Optional[int] = None) -> bool:
        """
        Alias for store_session, accepting dictionary with session_id field or explicit session_id.
        """
        session_id = session_data.get('session_id') if isinstance(session_data, dict) else None
        if not session_id:
            logger.error("[REDIS] Cannot save session: missing session_id in session_data")
            return False
        return await self.store_session(session_id, session_data, ttl)

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve session from Redis.

        Args:
            session_id: Unique session identifier

        Returns:
            Session data dictionary if found, None otherwise
        """
        key = f"session:{session_id}"
        return await self.get(key, as_json=True)

    async def refresh_ttl(self, session_id: str, ttl: Optional[int] = None) -> bool:
        """
        Refresh session TTL on activity.

        Args:
            session_id: Unique session identifier
            ttl: New TTL in seconds (defaults to 30 minutes)

        Returns:
            True if TTL refreshed successfully
        """
        key = f"session:{session_id}"
        ttl = ttl or self.default_ttl
        return await self.expire(key, ttl)

    async def delete_session(self, session_id: str) -> bool:
        """
        Delete session from Redis.

        Args:
            session_id: Unique session identifier

        Returns:
            True if deleted successfully
        """
        key = f"session:{session_id}"
        return await self.delete(key)

    async def session_exists(self, session_id: str) -> bool:
        """
        Check if session exists in Redis.

        Args:
            session_id: Unique session identifier

        Returns:
            True if session exists
        """
        key = f"session:{session_id}"
        return await super().exists(key)

    async def get_session_ttl(self, session_id: str) -> Optional[int]:
        """
        Get remaining TTL for a session.

        Args:
            session_id: Unique session identifier

        Returns:
            Remaining seconds, or None if key doesn't exist
        """
        key = f"session:{session_id}"
        return await self.ttl(key)

    async def append_message_to_history(self, session_id: str, role: str, content: str, message_type: str = "text") -> bool:
        """
        Append a message to session's conversation history atomically.

        This method reads the session, appends the message, and writes back.
        Used by WhatsAppService to track bot messages without requiring session object.

        Args:
            session_id: Unique session identifier
            role: Message role ('user' or 'assistant')
            content: Message content
            message_type: Type of message (default: 'text')

        Returns:
            True if message appended successfully
        """
        try:
            from app.utils.datetime_utils import utc_now

            key = f"session:{session_id}"
            session_data = await self.get(key, as_json=True)

            if not session_data:
                logger.warning(f"Cannot append message - session {session_id} not found in Redis")
                return False

            # Initialize conversation_history if needed
            if 'conversation_history' not in session_data:
                session_data['conversation_history'] = {"messages": [], "metadata": [], "openai_messages": []}

            conv_history = session_data['conversation_history']

            # Ensure all required lists exist
            if 'messages' not in conv_history:
                conv_history['messages'] = []
            if 'metadata' not in conv_history:
                conv_history['metadata'] = []
            if 'openai_messages' not in conv_history:
                conv_history['openai_messages'] = []

            # Create message entry
            timestamp = utc_now().isoformat()
            message_entry = {
                "role": role,
                "content": content,
                "timestamp": timestamp,
                "message_type": message_type
            }

            # Append to all history formats
            conv_history['messages'].append(message_entry)
            conv_history['metadata'].append({
                "timestamp": timestamp,
                "message_type": message_type,
                "role": role
            })
            conv_history['openai_messages'].append({
                "role": role,
                "content": content
            })

            # Update last_activity_at
            session_data['last_activity_at'] = timestamp

            # Get current TTL to preserve it
            current_ttl = await self.ttl(key)
            ttl_to_use = current_ttl if current_ttl and current_ttl > 0 else self.default_ttl

            # Save back to Redis
            success = await self.set(key, session_data, ex=ttl_to_use)

            if success:
                logger.debug(f"Appended {role} message to session {session_id} conversation history")
            else:
                logger.error(f"Failed to save session {session_id} after appending message")

            return success

        except Exception as e:
            logger.error(f"Error appending message to session {session_id}: {e}")
            return False




# Singleton instances
_redis_service: Optional[BaseRedisService] = None
_auth_service: Optional[AuthRedisService] = None
_session_service: Optional[SessionRedisService] = None
def get_redis_service() -> BaseRedisService:
    """Get base Redis service singleton."""
    global _redis_service
    if _redis_service is None:
        _redis_service = BaseRedisService()
    return _redis_service

def get_auth_redis_service() -> AuthRedisService:
    """Get auth Redis service singleton."""
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthRedisService()
    return _auth_service

def get_session_redis_service() -> SessionRedisService:
    """Get session Redis service singleton."""
    global _session_service
    if _session_service is None:
        _session_service = SessionRedisService()
    return _session_service
