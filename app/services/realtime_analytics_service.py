"""
Real-Time Analytics Service.

Provides real-time event publishing, active visitor presence tracking,
recent activity feed buffering, and Redis Pub/Sub stream subscriptions
for the live executive dashboard.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, AsyncGenerator

from app.config import get_settings
from app.redis_db import AsyncRedisConnectionManager
from app.database import get_db_session_context
from app.models import SessionEvent
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

REDIS_CHANNEL_LIVE_EVENTS = "analytics:live_events"
REDIS_KEY_ACTIVE_USERS = "analytics:active_users"
REDIS_KEY_ACTIVE_USER_METADATA = "analytics:active_user_metadata"
REDIS_KEY_RECENT_FEED = "analytics:recent_feed"
REDIS_FEED_MAX_ITEMS = 100
ACTIVE_USER_EXPIRY_SECONDS = 300  # 5 minutes window for live active status


class RealtimeAnalyticsService:
    """Service managing live event streams, active user tracking, and activity feed."""

    def __init__(self):
        self.settings = get_settings()

    async def record_heartbeat(self, user_phone: str, user_type: str = "unknown", session_id: Optional[str] = None) -> None:
        """
        Record a user heartbeat to keep active presence updated.

        Uses a Redis sorted set with unix timestamp as score.
        """
        if not getattr(self.settings, "redis_session_storage_enabled", True):
            return

        try:
            client = await AsyncRedisConnectionManager.get_client()
            if client is None:
                return

            now = time.time()
            await client.zadd(REDIS_KEY_ACTIVE_USERS, {user_phone: now})
            await client.hset(
                REDIS_KEY_ACTIVE_USER_METADATA,
                user_phone,
                json.dumps({"user_type": user_type, "session_id": session_id or ""}),
            )
            # Clean up users older than ACTIVE_USER_EXPIRY_SECONDS
            cutoff = now - ACTIVE_USER_EXPIRY_SECONDS
            await client.zremrangebyscore(REDIS_KEY_ACTIVE_USERS, 0, cutoff)
            await client.expire(REDIS_KEY_ACTIVE_USERS, ACTIVE_USER_EXPIRY_SECONDS * 2)
            await client.expire(REDIS_KEY_ACTIVE_USER_METADATA, ACTIVE_USER_EXPIRY_SECONDS * 2)
        except Exception as e:
            logger.debug(f"[REALTIME_ANALYTICS] Failed to record heartbeat: {e}")

    async def get_active_users_count(self) -> Dict[str, int]:
        """
        Get current live active visitors categorized by user type.
        """
        result = {"total": 0, "buyers": 0, "sellers": 0, "unknown": 0}
        if not getattr(self.settings, "redis_session_storage_enabled", True):
            return result

        try:
            client = await AsyncRedisConnectionManager.get_client()
            if client is None:
                return result

            now = time.time()
            cutoff = now - ACTIVE_USER_EXPIRY_SECONDS
            # Clean stale members
            await client.zremrangebyscore(REDIS_KEY_ACTIVE_USERS, 0, cutoff)
            # Retrieve active members
            members = await client.zrange(REDIS_KEY_ACTIVE_USERS, 0, -1)
            
            seen_phones = set()
            for m in members:
                try:
                    phone = m.decode("utf-8") if isinstance(m, (bytes, bytearray)) else str(m)
                    data = {}
                    if phone.startswith("{"):
                        data = json.loads(phone)
                        phone = data.get("phone")
                    else:
                        metadata = await client.hget(REDIS_KEY_ACTIVE_USER_METADATA, phone)
                        if metadata:
                            data = json.loads(
                                metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata
                            )
                    if phone in seen_phones:
                        continue
                    seen_phones.add(phone)
                    
                    utype = data.get("user_type", "unknown")
                    if utype == "buyer":
                        result["buyers"] += 1
                    elif utype == "seller":
                        result["sellers"] += 1
                    else:
                        result["unknown"] += 1
                    result["total"] += 1
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"[REALTIME_ANALYTICS] Failed to get active users: {e}")

        return result

    async def publish_event(
        self,
        event_type: str,
        user_id: str,
        data: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        persist_db: bool = True
    ) -> Dict[str, Any]:
        """
        Publish a live event to Redis Pub/Sub, prepend to recent feed, and persist to DB.

        Args:
            event_type: Type of event (e.g. 'whatsapp_visitor', 'rfq_created', 'seller_responded')
            user_id: Masked or unmasked phone number / user ID
            data: Additional metadata payload
            session_id: Optional session identifier
            persist_db: Whether to record in session_events table

        Returns:
            The structured event payload dictionary.
        """
        payload_data = data or {}
        timestamp_str = datetime.now(timezone.utc).isoformat()
        
        # Mask phone for privacy in feed unless already masked
        masked_user = user_id
        if user_id and len(user_id) >= 10 and not user_id.startswith("user_"):
            masked_user = f"{user_id[:4]}****{user_id[-2:]}"

        event_payload = {
            "event_type": event_type,
            "user_id": user_id,
            "user_masked": masked_user,
            "session_id": session_id or "",
            "timestamp": timestamp_str,
            "data": payload_data,
        }

        # 1. Update heartbeat
        user_type = payload_data.get("user_type") or payload_data.get("role") or "unknown"
        await self.record_heartbeat(user_id, user_type, session_id)

        # 2. Publish to Redis Pub/Sub & Feed buffer
        if getattr(self.settings, "redis_session_storage_enabled", True):
            try:
                client = await AsyncRedisConnectionManager.get_client()
                if client is not None:
                    serialized = json.dumps(event_payload)
                    await client.publish(REDIS_CHANNEL_LIVE_EVENTS, serialized)
                    # Push to recent activity feed list and trim
                    await client.lpush(REDIS_KEY_RECENT_FEED, serialized)
                    await client.ltrim(REDIS_KEY_RECENT_FEED, 0, REDIS_FEED_MAX_ITEMS - 1)
            except Exception as e:
                logger.debug(f"[REALTIME_ANALYTICS] Redis publish error: {e}")

        # 3. Persist to DB (optional / background)
        if persist_db and session_id:
            try:
                with get_db_session_context() as db:
                    event_row = SessionEvent(
                        session_id=session_id,
                        user_id=user_id,
                        event_type=event_type,
                        event_timestamp=utc_now(),
                        event_data=payload_data,
                    )
                    db.add(event_row)
                    db.commit()
            except Exception as e:
                logger.debug(f"[REALTIME_ANALYTICS] DB persist error: {e}")

        return event_payload

    async def get_recent_feed(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get the most recent events from Redis activity buffer or DB fallback.
        """
        events: List[Dict[str, Any]] = []

        if getattr(self.settings, "redis_session_storage_enabled", True):
            try:
                client = await AsyncRedisConnectionManager.get_client()
                if client is not None:
                    raw_items = await client.lrange(REDIS_KEY_RECENT_FEED, 0, limit - 1)
                    for item in raw_items:
                        try:
                            events.append(json.loads(item))
                        except Exception:
                            continue
                    if events:
                        return events
            except Exception as e:
                logger.debug(f"[REALTIME_ANALYTICS] Failed to fetch feed from Redis: {e}")

        # DB fallback if Redis empty
        try:
            with get_db_session_context() as db:
                rows = (
                    db.query(SessionEvent)
                    .order_by(SessionEvent.event_timestamp.desc())
                    .limit(limit)
                    .all()
                )
                for r in rows:
                    uid = r.user_id or "Anonymous"
                    masked = f"{uid[:4]}****{uid[-2:]}" if len(uid) >= 10 else uid
                    events.append({
                        "event_type": r.event_type,
                        "user_id": r.user_id,
                        "user_masked": masked,
                        "session_id": r.session_id,
                        "timestamp": r.event_timestamp.isoformat() if r.event_timestamp else datetime.now(timezone.utc).isoformat(),
                        "data": r.event_data or {},
                    })
        except Exception as e:
            logger.debug(f"[REALTIME_ANALYTICS] Failed to fetch feed from DB: {e}")

        return events

    async def subscribe_events(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Subscribe to live Redis events stream as an async generator for SSE / WebSockets.
        """
        if not getattr(self.settings, "redis_session_storage_enabled", True):
            while True:
                await asyncio.sleep(5)
                yield {"event_type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat(), "data": {}}

        client = await AsyncRedisConnectionManager.get_client()
        if client is None:
            while True:
                await asyncio.sleep(5)
                yield {"event_type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat(), "data": {}}

        pubsub = client.pubsub()
        await pubsub.subscribe(REDIS_CHANNEL_LIVE_EVENTS)
        try:
            while True:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message and message.get("type") == "message":
                        data_str = message.get("data")
                        if isinstance(data_str, bytes):
                            data_str = data_str.decode("utf-8")
                        try:
                            event_data = json.loads(data_str)
                            yield event_data
                        except Exception:
                            continue
                    else:
                        await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug(f"[REALTIME_ANALYTICS] PubSub loop error: {exc}")
                    await asyncio.sleep(1.0)
        finally:
            try:
                await pubsub.unsubscribe(REDIS_CHANNEL_LIVE_EVENTS)
                await pubsub.close()
            except Exception:
                pass


_realtime_analytics_service: Optional[RealtimeAnalyticsService] = None


def get_realtime_analytics_service() -> RealtimeAnalyticsService:
    """Get RealtimeAnalyticsService singleton."""
    _svc = globals().get("_realtime_analytics_service")
    if _svc is None:
        _svc = RealtimeAnalyticsService()
        globals()["_realtime_analytics_service"] = _svc
    return _svc
