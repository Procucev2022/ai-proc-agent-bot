"""
Asynchronous Redis service with connection pooling and specialized operations.
"""

import redis.asyncio as aioredis
import json
import logging
from typing import Optional, Dict, Any
from app.schemas.user import UserDetailsSchema
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


class AuthRedisService(BaseRedisService):
    """Specialized async Redis service for authentication."""

    async def store(self, phone_number: str, user_data: Dict[str, Any], expiry_seconds: int = 3600) -> bool:
        key = f"auth:{phone_number}"
        return await self.set(key, user_data, expiry_seconds)

    async def retrieve(self, phone_number: str) -> Optional[UserDetailsSchema]:
        key = f"auth:{phone_number}"
        data = await self.get(key, as_json=True)
        if data:
            return UserDetailsSchema(**data)
        return None

    async def delete_auth(self, phone_number: str) -> bool:
        key = f"auth:{phone_number}"
        return await self.delete(key)

    async def is_authenticated(self, phone_number: str) -> bool:
        key = f"auth:{phone_number}"
        return await self.exists(key)


# Singleton instances
_redis_service: Optional[BaseRedisService] = None
_auth_service: Optional[AuthRedisService] = None

def get_redis_service() -> BaseRedisService:
    """Get base Redis service singleton."""
    global _redis_service
    if _redis_service is None:
        _redis_service = BaseRedisService()
    return _redis_service

def get_auth_redis_service() -> AuthRedisService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthRedisService()
    return _auth_service
