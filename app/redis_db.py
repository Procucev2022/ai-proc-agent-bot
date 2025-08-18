"""
Scalable Redis service with connection pooling and specialized operations.
"""

import redis
import json
import logging
from typing import Optional, Dict, Any, Union
from redis.connection import ConnectionPool

from app.config import get_settings

logger = logging.getLogger(__name__)

class RedisConnectionManager:
    """Manages Redis connection pool for scalable operations."""
    
    _pool: Optional[ConnectionPool] = None
    _client: Optional[redis.Redis] = None
    
    @classmethod
    def get_pool(cls) -> ConnectionPool:
        """Get or create Redis connection pool."""
        if cls._pool is None:
            settings = get_settings()
            cls._pool = redis.ConnectionPool.from_url(
                settings.redis_url,
                decode_responses=True,
                max_connections=20,
                retry_on_timeout=True,
                socket_keepalive=True,
                socket_keepalive_options={}
            )
        return cls._pool
    
    @classmethod
    def get_client(cls) -> redis.Redis:
        """Get Redis client with connection pooling."""
        if cls._client is None:
            cls._client = redis.Redis(connection_pool=cls.get_pool())
        return cls._client

class BaseRedisService:
    """Base Redis service with common operations."""
    
    def __init__(self):
        self.client = RedisConnectionManager.get_client()
    
    def set(self, key: str, value: Union[str, dict], ex: Optional[int] = None) -> bool:
        """Set key-value with optional expiration."""
        try:
            if isinstance(value, dict):
                value = json.dumps(value)
            
            if ex:
                return bool(self.client.setex(key, ex, value))
            return bool(self.client.set(key, value))
        except Exception as e:
            logger.error(f"Redis SET error for key {key}: {e}")
            return False
    
    def get(self, key: str, as_json: bool = False) -> Optional[Union[str, dict]]:
        """Get value by key with optional JSON parsing."""
        try:
            value = self.client.get(key)
            if value and as_json:
                return json.loads(value)
            return value
        except Exception as e:
            logger.error(f"Redis GET error for key {key}: {e}")
            return None
    
    def delete(self, key: str) -> bool:
        """Delete key if present."""
        try:
            return bool(self.client.delete(key))
        except Exception as e:
            logger.error(f"Redis DELETE error for key {key}: {e}")
            return False
    
    def exists(self, key: str) -> bool:
        """Check if key exists."""
        try:
            return bool(self.client.exists(key))
        except Exception as e:
            logger.error(f"Redis EXISTS error for key {key}: {e}")
            return False

class AuthRedisService(BaseRedisService):
    """Specialized Redis service for authentication operations."""
    
    def store(self, phone_number: str, user_data: Dict[str, Any], 
             expiry_seconds: int = 3600) -> bool:
        """Store auth data with key format: auth:{phone_number}"""
        key = f"auth:{phone_number}"
        return self.set(key, user_data, expiry_seconds)
    
    def retrieve(self, phone_number: str) -> Optional[Dict[str, Any]]:
        """Retrieve auth data by phone number."""
        key = f"auth:{phone_number}"
        return self.get(key, as_json=True)
    
    def delete_auth(self, phone_number: str) -> bool:
        """Delete auth data by phone number."""
        key = f"auth:{phone_number}"
        return self.delete(key)
    
    def is_authenticated(self, phone_number: str) -> bool:
        """Check if user is authenticated."""
        key = f"auth:{phone_number}"
        return self.exists(key)

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
    """Get auth Redis service singleton."""
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthRedisService()
    return _auth_service