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
            cls._pool = await aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=20
            )
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
        self.default_ttl = self.settings.redis_session_ttl_seconds  # 1800 (30 min)

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
