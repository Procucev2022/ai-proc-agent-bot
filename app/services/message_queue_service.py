"""
Message Queueing Service - Rewrite v2.0

Drop-in replacement for message batching. Key improvements:
- Redis-only state (no in-memory timers/tasks)
- Simplified key structure (5 keys vs 9+)
- Consolidated session management
- Clearer control flow
- Maintains backward compatibility (wrapper pattern)

Usage (unchanged):
    # In webhook.py
    message_queue_service = MessageQueueService()
    await message_queue_service.enqueue_message(webhook_data)
    
    # In chat_service.py
    chat_service = ChatService(
        db_session=db,
        message_queue_service=message_queue_service  # Passed as whatsapp_service
    )
"""

import logging
import dataclasses
import asyncio
import hashlib
import time
import json
from datetime import datetime
from typing import Dict, List, Optional, Any

from redis.asyncio import Redis

from app.config import get_settings
from app.utils.logging_utils import UserPhoneContext
from app.utils.turn_trace import (
    RECEIVED_AT_FIELD,
    TURN_ID_FIELD,
    current_turn_id,
    resume_turn,
    stage,
)

logger = logging.getLogger(__name__)

# Values a gateway sends when it has no per-message identifier. The ICS gateway
# posts 'mid=NA&smsgid=NA' on every message, so treating the raw value as the
# deduplication key claimed '<phone>:message:NA' for its full 24 hour TTL and
# silently dropped every subsequent message from that user.
PLACEHOLDER_MESSAGE_IDS = frozenset({"", "-", "NA", "N/A", "NONE", "NULL", "NIL"})


# ============================================================================
# Data Classes
# ============================================================================

def _only_known_fields(cls: Any, data: Dict) -> Dict:
    """
    Drop keys the dataclass does not declare.

    These records round-trip through Redis, so during a rolling deploy one
    revision reads what another wrote. Without this filter, adding a field makes
    the older revision raise ``TypeError: unexpected keyword argument`` on every
    queued message it picks up -- which silently strands those messages and the
    user never gets a reply.
    """
    known = {f.name for f in dataclasses.fields(cls)}
    return {key: value for key, value in data.items() if key in known}


@dataclasses.dataclass
class Message:
    """Represents a message received from webhook."""
    message_id: str
    user_phone: str
    content: str
    message_type: str 
    timestamp: float
    webhook_data: dict

    def to_dict(self) -> Dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'Message':
        return cls(**_only_known_fields(cls, data))

    @property
    def turn_id(self) -> str:
        """Correlation id stamped by the webhook, or ``'-'`` when absent."""
        return str((self.webhook_data or {}).get(TURN_ID_FIELD) or "-")

    @property
    def received_at(self) -> Optional[float]:
        """Wall-clock time the webhook accepted this message, when known."""
        value = (self.webhook_data or {}).get(RECEIVED_AT_FIELD)
        return float(value) if isinstance(value, (int, float)) else None


@dataclasses.dataclass
class Batch:
    """Represents a batch of messages."""
    batch_id: str
    user_phone: str
    concatenated_content: str
    message_type: str
    message_count: int
    created_at: float
    # Correlation ids of the messages merged into this batch, so the reply can be
    # traced back to the exact webhook that triggered it even after the message
    # has been through Redis and a different worker.
    turn_ids: List[str] = dataclasses.field(default_factory=list)
    # Receipt time of the oldest message in the batch. Latency has to be measured
    # from when the user's message arrived, not from when the batch was built.
    received_at: Optional[float] = None

    def to_dict(self) -> Dict:
        return dataclasses.asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Batch':
        return cls(**_only_known_fields(cls, data))

    @property
    def turn_id(self) -> str:
        """Primary correlation id for this batch."""
        return self.turn_ids[0] if self.turn_ids else "-"

    def as_trace_payload(self) -> Dict[str, Any]:
        """Build the payload ``turn_trace.resume_turn`` expects."""
        return {
            TURN_ID_FIELD: self.turn_id,
            RECEIVED_AT_FIELD: self.received_at,
            "from": self.user_phone,
            "content": self.concatenated_content,
        }


@dataclasses.dataclass
class ProcessingSession:
    """
    Consolidated session state for a user's active processing.
    Stored as JSON in Redis with 60s TTL.
    """
    batch_id: str
    started_at: float
    ack_sent: bool = False
    please_wait_sent_count: int = 0
    please_wait_last_sent: float = 0.0
    suppressed: bool = False
    
    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))
    
    @classmethod
    def from_json(cls, data: str) -> 'ProcessingSession':
        return cls(**json.loads(data))


# ============================================================================
# Message Queue Service
# ============================================================================

class MessageQueueService:
    """
    Simplified message queue service using Redis-only state.
    
    Redis Keys:
    - {user}:incoming -> Sorted Set (messages awaiting batch)
    - {user}:outgoing -> List (batches awaiting processing)
    - {user}:processing -> String (current batch_id, 60s TTL)
    - {user}:batch_trigger -> String (timer key, expires to trigger batch)
    - {user}:session -> JSON (ProcessingSession, 60s TTL)
    
    Background Tasks (run in each worker, idempotent):
    - Batch flush: closes one user's window and answers immediately (hot path)
    - Batch poller: safety net for windows and batches the flush missed
    - Monitor: Sends please-wait messages, logs slow batches
    """

    # Class-level defaults so an instance built without __init__ still behaves.
    # __getattr__ forwards unknown names to WhatsAppService, so a missing tuning
    # attribute would otherwise surface as a baffling error about the wrong class
    # from inside a background loop. Only immutable values belong here; mutable
    # state is created per instance (see flush_tasks).
    poll_interval: float = 1.0
    max_flush_waits: int = 10
    batch_window: int = 1
    _running: bool = True

    def __init__(self):
        settings = get_settings()
        
        # Redis client with resilient connection pooling
        self.redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=getattr(settings, 'redis_socket_timeout', 1.5),
            socket_connect_timeout=getattr(settings, 'redis_socket_connect_timeout', 1.5),
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
        
        # Configuration
        self.batch_window = settings.batch_window_seconds  # Default: 3s
        self.please_wait_threshold = settings.please_wait_threshold_seconds  # Default: 15s
        self.max_please_wait_count = settings.max_please_wait_count  # Default: 3
        self.monitoring_poll_interval = settings.monitoring_poll_interval_seconds  # Default: 5s
        self.poll_interval = max(0.1, settings.batch_poll_interval_seconds)
        self.response_ready_ttl = max(60, self.please_wait_threshold * 4)
        self.monitor_lock_ttl = 180
        self.please_wait_interval_ttl = 600
        
        # WhatsApp service for direct sending (ack, please-wait)
        from app.services.whatsapp_service import WhatsAppService
        self.whatsapp_service = WhatsAppService()
        
        # Background task handles (for lifecycle management).
        # Assigned before anything that can fail, because __getattr__ delegates
        # unknown attributes to WhatsAppService: a half-initialised instance turns
        # every `self._running` read into a confusing AttributeError about
        # WhatsAppService and kills the poller for the lifetime of the worker.
        self._background_tasks: List[asyncio.Task] = []
        self._running = True

        # Upper bound on how many times a flush task will re-arm while the user
        # keeps typing. Without a bound, a user sending a message every window
        # could defer their own reply indefinitely.
        self.max_flush_waits = max(1, settings.max_batch_flush_waits)

        logger.info(
            f"[MESSAGE_QUEUE] [INIT] MessageQueueService initialized: "
            f"batch_window={self.batch_window}s, "
            f"max_flush_waits={self.max_flush_waits}, "
            f"please_wait_threshold={self.please_wait_threshold}s, "
            f"monitoring_poll_interval={self.monitoring_poll_interval}s, "
            f"response_ready_ttl={self.response_ready_ttl}s, "
            f"monitor_lock_ttl={self.monitor_lock_ttl}s"
        )

    # ========================================================================
    # Redis Key Helpers
    # ========================================================================

    def _key_incoming(self, user_phone: str) -> str:
        return f"{user_phone}:incoming"

    def _key_message(self, user_phone: str, message_id: str) -> str:
        return f"{user_phone}:message:{message_id}"

    # Timestamp shapes the gateway has been observed to send, plus the
    # microsecond form its own documentation uses
    # ('timestamp=2025-09-13 13:54:22.125300'). Ordered most precise first so the
    # sub-second value is kept when it is there: the dedupe key is built from this
    # timestamp, so truncating to whole seconds makes two genuinely different
    # messages sent inside the same second collide, and the second one is then
    # silently dropped for 24 hours.
    _TIMESTAMP_FORMATS = (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    )

    @classmethod
    def _parse_timestamp(cls, raw: Any) -> float:
        """
        Convert the gateway's timestamp to an epoch float, never raising.

        Previously this parsed with a single hard-coded ``'%Y-%m-%d %H:%M:%S'``.
        Any other shape -- including the microsecond form in the gateway's own
        documentation -- raised ValueError out of ``enqueue_message``, which
        logged an error and re-raised, so the message was dropped and the user got
        no reply at all. A timestamp is metadata; failing to read it must never
        cost the message.
        """
        if isinstance(raw, (int, float)):
            return float(raw)

        text = str(raw or "").strip()
        if text:
            for fmt in cls._TIMESTAMP_FORMATS:
                try:
                    return datetime.strptime(text, fmt).timestamp()
                except ValueError:
                    continue
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                pass

        # Arrival time is a usable stand-in: it keeps ordering sane and keeps the
        # dedupe key unique, which is all this value is used for.
        logger.warning(
            f"[ENQUEUE] Unrecognised gateway timestamp {raw!r}; using arrival time "
            f"so the message is still processed"
        )
        return time.time()

    @staticmethod
    def _resolve_message_id(
        raw_message_id: Any,
        user_phone: str,
        timestamp: float,
        content: str,
    ) -> str:
        """
        Return an identifier that is unique per message, not per gateway.

        A real provider ID is used as-is so a re-posted webhook is recognised as
        a retry. When the gateway sends a placeholder such as 'NA', identity is
        derived from the sender, the gateway's own timestamp and a digest of the
        content: a genuine retry carries all three unchanged and is still
        deduplicated, while a new message gets a new key instead of colliding
        with every previous message from the same user.
        """
        candidate = str(raw_message_id or "").strip()
        if candidate.upper() not in PLACEHOLDER_MESSAGE_IDS:
            return candidate

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return f"{user_phone}_{timestamp}_{digest}"

    def _key_outgoing(self, user_phone: str) -> str:
        return f"{user_phone}:outgoing"

    def _key_processing(self, user_phone: str) -> str:
        return f"{user_phone}:processing"

    def _key_batch_trigger(self, user_phone: str) -> str:
        return f"{user_phone}:batch_trigger"

    def _key_session(self, user_phone: str) -> str:
        return f"{user_phone}:session"

    def _key_lock_batch(self, user_phone: str) -> str:
        return f"{user_phone}:lock:batch"

    def _key_lock_ack(self, user_phone: str) -> str:
        return f"{user_phone}:lock:ack"

    def _key_lock_monitor(self, user_phone: str) -> str:
        return f"{user_phone}:lock:monitor"

    def _key_lock_monitor_log(self, user_phone: str) -> str:
        return f"{user_phone}:lock:monitor_log"

    def _key_please_wait_interval(self, user_phone: str, interval: int) -> str:
        """Redis key to dedupe please-wait sends per interval across workers."""
        return f"{user_phone}:please_wait:interval:{interval}"

    def _key_response_ready(self, user_phone: str) -> str:
        """Redis key for response ready flag (prevents late please-wait)."""
        return f"{user_phone}:response_ready"

    def _key_ack_sent(self, user_phone: str) -> str:
        """Redis key for acknowledgment sent flag (persists across batches)."""
        return f"{user_phone}:ack_sent"

    # ========================================================================
    # Public API - Entry Point
    # ========================================================================

    async def enqueue_message(self, webhook_data: Dict) -> None:
        """
        Add message to user's incoming queue and manage batch timer.
        
        Flow:
        1. Parse webhook data into Message
        2. Add to incoming queue (sorted by timestamp)
        3. Check if should send acknowledgment
        4. Start/refresh batch timer
        5. Ensure background tasks are running
        """
        turn_id = current_turn_id()
        try:
            # Parse webhook data
            timestamp_raw = webhook_data.get("timestamp", time.time())
            timestamp = self._parse_timestamp(timestamp_raw)

            user_phone = webhook_data.get("from", "").lstrip('+')
            message_type = webhook_data.get("type", "text")
            content = webhook_data.get("content", "")
            
            # Fallback content extraction
            if not content:
                if message_type == "text":
                    content = webhook_data.get("text", {}).get("body", "")
                else:
                    logger.warning(
                        f"[ENQUEUE] Non-text message type '{message_type}' "
                        f"with no content, user={user_phone}"
                    )
            
            # Resolved after content, because a gateway that sends no real
            # message ID needs the content to tell two messages apart.
            message_id = self._resolve_message_id(
                webhook_data.get("message_id"), user_phone, timestamp, content
            )
            
            # Create Message object
            message = Message(
                message_id=message_id,
                user_phone=user_phone,
                content=content,
                message_type=message_type,
                timestamp=timestamp,
                webhook_data=webhook_data
            )
            
            # Add to incoming queue
            incoming_key = self._key_incoming(user_phone)
            # Deduplicate retries by message_id while preserving identical content.
            if not await self.redis.set(self._key_message(user_phone, message_id), "1", nx=True, ex=86400):
                logger.info(
                    f"[MESSAGE_QUEUE] [ENQUEUE] [TURN:{turn_id}] Duplicate message_id='{message_id}', "
                    f"user={user_phone}, ignored (gateway retry or identical content in the same second)"
                )
                return
            await self.redis.zadd(incoming_key, {json.dumps(message.to_dict()): timestamp})

            # Deliberately no queue-depth read here: this is the latency-critical
            # path and the depth is already reported by [BATCH_CREATE], so an
            # extra Redis round trip per message would buy nothing.
            logger.info(
                f"[MESSAGE_QUEUE] [ENQUEUE] [TURN:{turn_id}] message_id='{message_id}', user={user_phone}, "
                f"type={message_type}, chars={len(content)}"
            )

            # Check if user is currently processing
            processing_key = self._key_processing(user_phone)
            is_processing = await self.redis.exists(processing_key)
            if is_processing:
                logger.info(
                    f"[MESSAGE_QUEUE] [ENQUEUE] [TURN:{turn_id}] {user_phone} is already processing an "
                    f"earlier batch; this message waits for that turn to finish"
                )

            # Always allow the batching window to collect messages, including for idle users.
            await self._refresh_batch_timer(user_phone)

            # Flush this user's batch as soon as the window closes. Previously the
            # only trigger was the shared one-second poller, which added its own
            # tick plus global-lock contention on top of the window -- around a
            # second of dead time on every single reply. The poller stays as a
            # safety net for anything this worker fails to flush.
            self._schedule_batch_flush(user_phone)

        except Exception as e:
            logger.error(f"[ENQUEUE] Error: {e}", exc_info=True)
            raise

    @property
    def flush_tasks(self) -> Dict[str, "asyncio.Task"]:
        """
        Per-user tasks waiting to close a batch window, created on first use.

        Read straight out of ``__dict__`` so the lookup can never fall through to
        ``__getattr__``, and so an instance built without ``__init__`` still gets
        its own dict rather than sharing one across instances.
        """
        tasks = self.__dict__.get("_flush_tasks")
        if tasks is None:
            tasks = {}
            self.__dict__["_flush_tasks"] = tasks
        return tasks

    def _schedule_batch_flush(self, user_phone: str) -> None:
        """
        Ensure exactly one task per user is waiting to close the batch window.

        Idempotent: a second message arriving inside the window extends the timer
        via Redis, and the task already waiting picks the new deadline up, so no
        extra task is created.
        """
        existing = self.flush_tasks.get(user_phone)
        if existing is not None and not existing.done():
            logger.debug(
                f"[MESSAGE_QUEUE] [FLUSH] Flush already armed for {user_phone}, "
                f"window extended instead of arming a second one"
            )
            return

        coroutine = self._flush_after_window(user_phone)
        try:
            task = asyncio.create_task(coroutine, name=f"batch_flush:{user_phone}")
        except RuntimeError:
            # No running loop. The poller remains responsible for this user's
            # batch, so it is not fatal, but the coroutine has to be closed or it
            # leaks and emits a "never awaited" warning.
            coroutine.close()
            logger.warning(
                f"[MESSAGE_QUEUE] [FLUSH] No running event loop to arm a flush for "
                f"{user_phone}; the batch poller will pick it up instead"
            )
            return

        self.flush_tasks[user_phone] = task
        task.add_done_callback(lambda _t, phone=user_phone: self.flush_tasks.pop(phone, None))

    async def _flush_after_window(self, user_phone: str) -> None:
        """
        Wait out the batching window for one user, then create their batch.

        Reads the remaining TTL from Redis rather than sleeping a fixed interval,
        so a window extended by a follow-up message is honoured exactly and the
        batch is created the moment the window truly closes.
        """
        trigger_key = self._key_batch_trigger(user_phone)
        turn_id = current_turn_id()
        try:
            waits = 0
            while waits < self.max_flush_waits:
                remaining_ms = await self.redis.pttl(trigger_key)
                # Redis returns -2 when the key is gone and -1 when it has no
                # expiry; both mean there is nothing left to wait for.
                if remaining_ms is None or remaining_ms < 0:
                    break
                waits += 1
                await asyncio.sleep(remaining_ms / 1000.0)

            if waits >= self.max_flush_waits:
                logger.warning(
                    f"[MESSAGE_QUEUE] [FLUSH] [TURN:{turn_id}] {user_phone} kept extending the batch "
                    f"window {waits} times; answering now to bound the wait"
                )

            logger.info(
                f"[MESSAGE_QUEUE] [FLUSH] [TURN:{turn_id}] Batch window closed for {user_phone} "
                f"after {waits} wait(s); creating batch without waiting for the poller"
            )
            await self._create_batch(user_phone)
        except asyncio.CancelledError:
            logger.debug(f"[MESSAGE_QUEUE] [FLUSH] Flush for {user_phone} cancelled during shutdown")
            raise
        except Exception as e:
            # The poller still scans for this user, so a failed flush delays the
            # reply by one poll interval instead of losing it.
            logger.error(
                f"[MESSAGE_QUEUE] [FLUSH] Flush failed for {user_phone}, falling back to the "
                f"batch poller: {e}",
                exc_info=True,
            )

    # ========================================================================
    # Batch Timer Management
    # ========================================================================

    async def _refresh_batch_timer(self, user_phone: str) -> None:
        """
        Start or refresh the batch timer for a user.
        Timer is a Redis key with TTL = batch_window.
        When it expires, the poller will create a batch.
        """
        trigger_key = self._key_batch_trigger(user_phone)
        await self.redis.setex(trigger_key, self.batch_window, "1")
        logger.info(f"[MESSAGE_QUEUE] [TIMER] Refreshed batch timer for {user_phone} (window={self.batch_window}s)")

    # ========================================================================
    # Background Tasks (Idempotent)
    # ========================================================================

    def _ensure_background_tasks(self) -> None:
        """
        Ensure background tasks are running.
        Called on every enqueue (idempotent).
        """
        # Check if tasks already running
        if self._background_tasks:
            # Clean up done tasks
            self._background_tasks = [t for t in self._background_tasks if not t.done()]
            
            # Check if we have both tasks
            if len(self._background_tasks) >= 2:
                return
        
        # Start batch poller
        if not any(t.get_name() == "batch_poller" for t in self._background_tasks):
            task = asyncio.create_task(self.run_batch_poller(), name="batch_poller")
            self._background_tasks.append(task)
            logger.debug("[BACKGROUND] Started batch poller task")
        
        # Start monitoring loop
        if not any(t.get_name() == "monitoring_loop" for t in self._background_tasks):
            task = asyncio.create_task(self.run_monitoring_loop(), name="monitoring_loop")
            self._background_tasks.append(task)
            logger.debug("[BACKGROUND] Started monitoring loop task")

    async def _scan_keys(self, pattern: str) -> List[str]:
        """Collect every key matching ``pattern`` with a cursor-based scan."""
        cursor = 0
        found: List[str] = []
        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match=pattern, count=100)
            found.extend(keys)
            if cursor == 0 or not self._running:
                break
        return found

    async def _poll_once(self) -> int:
        """
        Run one poll cycle and return how many batches it kicked off.

        Split out of the loop so a failure has an obvious blast radius and so the
        cycle can be tested without driving the infinite loop around it.
        """
        started = 0

        # Users with messages still waiting for their window to close.
        for key in await self._scan_keys("*:incoming"):
            if not self._running:
                break
            user_phone = key.rsplit(":incoming", 1)[0]
            trigger_key = self._key_batch_trigger(user_phone)
            if await self.redis.exists(trigger_key):
                continue
            message_count = await self.redis.zcard(key)
            if message_count > 0:
                logger.info(
                    f"[MESSAGE_QUEUE] [POLLER] Timer expired for {user_phone}, "
                    f"{message_count} messages, creating batch"
                )
                await self._create_batch(user_phone)
                started += 1

        # Batches already built but never started. Previously nothing looked here:
        # a worker that died between claiming a batch and answering it, or a
        # cleanup that failed, left the batch in {phone}:outgoing where the
        # incoming-only scan could never see it, and that user's reply was lost
        # permanently unless they happened to send another message.
        for key in await self._scan_keys("*:outgoing"):
            if not self._running:
                break
            user_phone = key.rsplit(":outgoing", 1)[0]
            if await self.redis.exists(self._key_processing(user_phone)):
                continue
            pending = await self.redis.llen(key)
            if pending > 0:
                logger.warning(
                    f"[MESSAGE_QUEUE] [POLLER] Recovering {pending} stranded batch(es) for "
                    f"{user_phone}: built but never processed, so this reply was already late"
                )
                await self._try_start_processing(user_phone)
                started += 1

        return started

    async def run_batch_poller(self) -> None:
        """
        Background task: safety net that creates and restarts batches.

        Since :meth:`_schedule_batch_flush` now closes each user's window
        directly, this loop is no longer on the happy path. It exists to catch
        what that flush cannot: a window armed by a worker that then died, and a
        batch stranded mid-flight.

        The loop must outlive Redis. It previously died for the lifetime of the
        worker on any error that escaped the inner handler, after which no text
        message on that worker was ever answered again; the observed trigger was
        an ``AttributeError`` on ``self._running`` misreported through
        ``__getattr__`` as a missing ``WhatsAppService`` attribute. Every
        iteration is therefore wrapped, and the loop only exits when explicitly
        stopped or cancelled.
        """
        logger.info(
            f"[MESSAGE_QUEUE] [POLLER] Batch poller started "
            f"(interval={self.poll_interval}s, safety net for direct flush)"
        )
        consecutive_failures = 0

        while True:
            try:
                if not self._running:
                    break
                await asyncio.sleep(self.poll_interval)
                if not self._running:
                    break

                # Global poller lock - only one worker should poll at a time
                poller_lock = self.redis.lock(
                    "global:poller:lock",
                    timeout=max(2.0, self.poll_interval * 2),
                    blocking_timeout=0,  # Non-blocking - skip if another worker is polling
                )
                acquired = await poller_lock.acquire()
                if not acquired:
                    # Another worker is polling, skip this cycle
                    continue
                try:
                    await self._poll_once()
                finally:
                    try:
                        await poller_lock.release()
                    except Exception:
                        pass

                consecutive_failures = 0

            except asyncio.CancelledError:
                logger.info("[MESSAGE_QUEUE] [POLLER] Batch poller cancelled")
                raise
            except Exception as e:
                if not self._running:
                    logger.debug(f"[POLLER] Poller cycle interrupted during shutdown: {e}")
                    break
                consecutive_failures += 1
                # Escalate but keep going: a Redis outage produces one of these per
                # cycle, and the loop surviving it is what lets queued replies go
                # out once Redis returns.
                logger.error(
                    f"[MESSAGE_QUEUE] [POLLER] Poll cycle failed "
                    f"({consecutive_failures} in a row), poller still running: "
                    f"{type(e).__name__}: {e}",
                    exc_info=consecutive_failures <= 3,
                )
                # Back off a little while the dependency is down so a hard outage
                # does not spin the log at the full poll rate.
                await asyncio.sleep(min(5.0, self.poll_interval * consecutive_failures))

        logger.warning(
            "[MESSAGE_QUEUE] [POLLER] Batch poller loop exited. Text messages on this "
            "worker will only be answered by another worker's poller from now on."
        )

    async def run_monitoring_loop(self) -> None:
        """
        Background task: Monitor active processing sessions.
        - Send please-wait messages when threshold exceeded
        - Log warnings for slow batches
        
        Poll interval is configurable via MONITORING_POLL_INTERVAL_SECONDS.
        Idempotent across workers - workers coordinate via Redis SET NX EX to prevent duplicates.
        """
        logger.debug("[MONITOR] Monitoring loop started")
        
        try:
            while self._running:
                await asyncio.sleep(self.monitoring_poll_interval)  # Configurable poll interval
                if not self._running:
                    break
                
                try:
                    # Find all active sessions
                    cursor = 0
                    session_keys = []
                    while True:
                        cursor, keys = await self.redis.scan(
                            cursor=cursor,
                            match="*:session",
                            count=100
                        )
                        session_keys.extend(keys)
                        if cursor == 0:
                            break
                    
                    now = time.time()
                    
                    for key in session_keys:
                        try:
                            user_phone = key.replace(":session", "")
                            
                            # Get session data
                            session_json = await self.redis.get(key)
                            if not session_json:
                                continue
                            
                            session = ProcessingSession.from_json(session_json)
                            duration = now - session.started_at
                            
                            # Calculate which interval we're in based on duration
                            # E.g., threshold=15s: intervals at 15s, 30s, 45s, 60s...
                            intervals_passed = int(duration // self.please_wait_threshold)
                            
                            # Send please-wait if we've entered a new interval AND haven't reached max count
                            should_send = (
                                intervals_passed > 0 and
                                session.please_wait_sent_count < intervals_passed and
                                session.please_wait_sent_count < self.max_please_wait_count
                            )
                            
                            if should_send:
                                # Check if response is already ready (prevents late please-wait)
                                response_ready_key = self._key_response_ready(user_phone)
                                response_ready = await self.redis.get(response_ready_key)
                                
                                if response_ready:
                                    logger.debug(
                                        f"[MONITOR] Response ready for {user_phone} "
                                        f"(batch {session.batch_id}, duration {duration:.1f}s), "
                                        f"skipping please-wait to avoid race condition"
                                    )
                                    continue
                                
                                # Atomic lock to prevent duplicate sends across workers
                                lock_key = self._key_lock_monitor(user_phone)
                                lock_acquired = await self.redis.set(
                                    lock_key,
                                    "1",
                                    nx=True,
                                    ex=self.monitor_lock_ttl
                                )
                                
                                if lock_acquired:
                                    # This worker won the race - double-check and send
                                    interval_key = None
                                    try:
                                        # CRITICAL: Double-check response_ready INSIDE lock (race condition prevention)
                                        response_ready_recheck = await self.redis.get(response_ready_key)
                                        if response_ready_recheck:
                                            logger.debug(
                                                f"[MONITOR] Response ready detected inside lock for {user_phone}, "
                                                f"skipping please-wait (double-checked locking)"
                                            )
                                            continue
                                        
                                        # Double-check session hasn't been updated by another worker
                                        session_json_check = await self.redis.get(key)
                                        if not session_json_check:
                                            continue
                                        
                                        session_check = ProcessingSession.from_json(session_json_check)
                                        
                                        # Verify we should still send (counter might have been updated)
                                        if session_check.please_wait_sent_count >= self.max_please_wait_count:
                                            logger.debug(
                                                f"[MONITOR] Max please-wait count ({self.max_please_wait_count}) "
                                                f"reached for {user_phone}"
                                            )
                                            continue
                                        
                                        if session_check.please_wait_sent_count >= intervals_passed:
                                            logger.debug(
                                                f"[MONITOR] Please-wait already sent for interval {intervals_passed} "
                                                f"by another worker for {user_phone}"
                                            )
                                            continue

                                        # Claim this interval once across all workers.
                                        interval_key = self._key_please_wait_interval(user_phone, intervals_passed)
                                        interval_claimed = await self.redis.set(
                                            interval_key,
                                            "1",
                                            nx=True,
                                            ex=self.please_wait_interval_ttl
                                        )
                                        if not interval_claimed:
                                            logger.debug(
                                                f"[MONITOR] Interval {intervals_passed} already claimed "
                                                f"for {user_phone}, skipping duplicate send"
                                            )
                                            continue
                                        
                                        logger.debug(
                                            f"[MONITOR] Sending please-wait #{session_check.please_wait_sent_count + 1} "
                                            f"to {user_phone} after {duration:.1f}s "
                                            f"(interval {intervals_passed}/{self.max_please_wait_count})"
                                        )
                                        await self._send_please_wait(user_phone)
                                        
                                        # Update session with counter and timestamp
                                        session_check.please_wait_sent_count += 1
                                        session_check.please_wait_last_sent = time.time()
                                        session_still_exists = await self.redis.exists(key)
                                        if session_still_exists:
                                            await self.redis.setex(
                                                key,
                                                60,  # Refresh TTL
                                                session_check.to_json()
                                            )
                                        else:
                                            logger.debug(
                                                f"[MONITOR] Session removed during send for {user_phone}, "
                                                f"skipping session rewrite"
                                            )
                                    except Exception as send_error:
                                        if interval_key:
                                            # Release interval claim on failure so a later cycle can retry.
                                            await self.redis.delete(interval_key)
                                        logger.error(
                                            f"[MONITOR] Error sending please-wait to {user_phone}: {send_error}",
                                            exc_info=True
                                        )
                                else:
                                    # Another worker is handling it
                                    logger.debug(
                                        f"[MONITOR] Another worker is handling please-wait "
                                        f"for {user_phone}"
                                    )
                            
                            # Log warnings for slow processing (deduplicated across workers)
                            if duration > 30:
                                log_lock_key = self._key_lock_monitor_log(user_phone)
                                try:
                                    log_flag_set = await self.redis.set(
                                        log_lock_key, 
                                        "1", 
                                        nx=True, 
                                        ex=5
                                    )
                                    
                                    if log_flag_set:
                                        if duration > 50:
                                            logger.warning(
                                                f"[MONITOR-WARNING] Batch {session.batch_id} "
                                                f"for {user_phone} processing for {duration:.1f}s "
                                                f"(approaching TTL limit!)"
                                            )
                                        else:  # 30-50 seconds
                                            logger.debug(
                                                f"[MONITOR-INFO] Batch {session.batch_id} "
                                                f"for {user_phone} processing for {duration:.1f}s"
                                            )
                                except Exception:
                                    # Ignore logging coordination errors
                                    pass
                        
                        except Exception as e:
                            logger.error(f"[MONITOR] Error checking session {key}: {e}")
                
                except Exception as e:
                    if not self._running:
                        logger.debug(f"[MONITOR] Monitor cycle interrupted during shutdown: {e}")
                        break
                    logger.error(f"[MONITOR] Error in monitor cycle: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.debug("[MONITOR] Monitoring loop cancelled")
            raise
        except Exception as e:
            # Reported at error level and then re-armed by the caller: this loop
            # tracks how long turns are taking, and losing it silently is how a
            # stalled turn stops being visible at all.
            logger.error(f"[MONITOR] Monitoring loop failed: {e}", exc_info=True)

    # ========================================================================
    # Batch Creation
    # ========================================================================

    async def _create_batch(self, user_phone: str) -> None:
        """
        Create batch from incoming messages and add to outgoing queue.
        
        Critical: Checks if user is currently processing before creating batch.
        This ensures sequential batch processing.
        """
        # Acquire batch lock FIRST for atomic check-and-create
        lock_key = self._key_lock_batch(user_phone)
        lock = self.redis.lock(lock_key, timeout=10, blocking_timeout=10)
        
        try:
            async with lock:
                # Check if already processing INSIDE lock (prevent race condition)
                processing_key = self._key_processing(user_phone)
                is_processing = await self.redis.exists(processing_key)
                
                if is_processing:
                    logger.debug(
                        f"[BATCH_CREATE] {user_phone} already processing, "
                        f"skip batch creation (sequential processing)"
                    )
                    return
                
                incoming_key = self._key_incoming(user_phone)
                
                # Get all messages (sorted by timestamp)
                message_data_list = await self.redis.zrange(incoming_key, 0, -1)
                
                if not message_data_list:
                    logger.debug(f"[BATCH_CREATE] No messages for {user_phone}")
                    return
                
                # Parse messages
                messages: List[Message] = []
                for msg_json in message_data_list:
                    try:
                        msg_dict = json.loads(msg_json)
                        messages.append(Message.from_dict(msg_dict))
                    except Exception as e:
                        logger.error(
                            f"[BATCH_CREATE] Error parsing message: {e}",
                            exc_info=True
                        )
                
                if not messages:
                    # Remove unparsable messages
                    await self.redis.delete(incoming_key)
                    logger.warning(
                        f"[BATCH_CREATE] All messages unparsable for {user_phone}, "
                        f"cleared queue"
                    )
                    return
                
                # Remove messages from incoming queue (atomic)
                await self.redis.zrem(incoming_key, *message_data_list)
                
                # Preserve every message; only message_id is used for deduplication.
                batch_id = f"{user_phone}+{int(time.time() * 1000)}"
                concatenated_content = "\n".join(msg.content.strip() for msg in messages)

                # Carry the webhook correlation ids and the earliest receipt time
                # into the batch so latency is still measured from when the user
                # sent the message, not from when this batch happened to be built.
                turn_ids = [msg.turn_id for msg in messages if msg.turn_id != "-"]
                receipts = [msg.received_at for msg in messages if msg.received_at is not None]

                batch = Batch(
                    batch_id=batch_id,
                    user_phone=user_phone,
                    concatenated_content=concatenated_content,
                    message_type=messages[0].message_type,
                    message_count=len(messages),
                    created_at=time.time(),
                    turn_ids=turn_ids,
                    received_at=min(receipts) if receipts else None,
                )
                
                # Add to outgoing queue
                outgoing_key = self._key_outgoing(user_phone)
                await self.redis.rpush(outgoing_key, json.dumps(batch.to_dict()))

                queued_for = (
                    f"{time.time() - batch.received_at:.3f}s"
                    if batch.received_at is not None else "unknown"
                )
                logger.info(
                    f"[MESSAGE_QUEUE] [BATCH_CREATE] [TURN:{batch.turn_id}] Created batch {batch_id} "
                    f"for {user_phone}: {len(messages)} messages, queued_for={queued_for}, "
                    f"content='{concatenated_content[:100]}...'"
                )
        
        except Exception as e:
            logger.error(f"[BATCH_CREATE] Error: {e}", exc_info=True)
            return
        
        # Try to start processing (outside lock)
        await self._try_start_processing(user_phone)

    # ========================================================================
    # Batch Processing
    # ========================================================================

    async def _try_start_processing(self, user_phone: str) -> None:
        """
        Atomically claim next batch and start processing if not already processing.
        Uses distributed lock to ensure atomic check-claim-process operation.
        """
        # Acquire lock FIRST for atomic check-claim-process
        lock_key = self._key_lock_batch(user_phone)
        lock = self.redis.lock(lock_key, timeout=10, blocking_timeout=1)
        
        batch = None  # Initialize to avoid NameError if early return occurs
        
        try:
            async with lock:
                processing_key = self._key_processing(user_phone)
                
                # Check if already processing INSIDE lock (prevent race condition)
                is_processing = await self.redis.exists(processing_key)
                if is_processing:
                    logger.debug(f"[START] {user_phone} already processing, batch will wait")
                    return
                
                # Claim first batch from queue (atomic with check)
                outgoing_key = self._key_outgoing(user_phone)
                batch_json = await self.redis.lpop(outgoing_key)
                
                if not batch_json:
                    logger.debug(f"[START] No batches in queue for {user_phone}")
                    return
                
                # Parse batch
                try:
                    batch = Batch.from_dict(json.loads(batch_json))
                except Exception as e:
                    logger.error(f"[START] Error parsing batch: {e}", exc_info=True)
                    # Re-queue for retry
                    await self.redis.lpush(outgoing_key, batch_json)
                    return
                
                # Mark as processing (60s TTL for crash recovery)
                await self.redis.setex(processing_key, 60, batch.batch_id)
                
                # Create session (inside lock to ensure atomicity)
                session = ProcessingSession(
                    batch_id=batch.batch_id,
                    started_at=time.time()
                )
                session_key = self._key_session(user_phone)
                await self.redis.setex(session_key, 60, session.to_json())
                
                logger.info(
                    f"[MESSAGE_QUEUE] [START] Starting processing for batch {batch.batch_id}, "
                    f"user={user_phone}"
                )
        
        except Exception as e:
            logger.error(f"[START] Error claiming batch: {e}", exc_info=True)
            return
        
        # Process batch (outside lock to avoid blocking other operations)
        if batch is not None:
            asyncio.create_task(self._process_batch(batch))

    async def _process_batch(self, batch: Batch) -> None:
        """
        Process batch through ChatService.
        
        Note: Cleanup happens in wrapper methods when ChatService sends response.
        """
        # Re-establish the turn the webhook opened. This task was created by
        # _try_start_processing, potentially in a different worker to the one that
        # took the webhook, so the correlation id has to come back out of the
        # batch rather than out of the context.
        trace = resume_turn(
            batch.as_trace_payload(), source="batch", user_phone=batch.user_phone
        )
        try:
            waited = (
                f"{time.time() - batch.created_at:.3f}s"
                if batch.created_at else "unknown"
            )
            logger.info(
                f"[MESSAGE_QUEUE] [PROCESS] [TURN:{trace.turn_id}] Batch {batch.batch_id} for "
                f"{batch.user_phone}: {batch.message_count} messages, "
                f"type={batch.message_type}, waited_in_queue={waited}"
            )

            # Import here to avoid circular dependency
            from app.services.chat_service import ChatService
            from app.database import get_db_session_context

            # Bind the phone for every log line this turn emits. The text path ran
            # outside any request, so all of its lines used to be attributed to
            # 'N/A' and could not be filtered by user.
            async with UserPhoneContext(batch.user_phone):
                # Process through ChatService with session management
                with get_db_session_context() as db:
                    with stage("chat_service_init"):
                        chat_service = ChatService(
                            db_session=db,
                            message_queue_service=self  # Pass self as whatsapp_service
                        )
                    try:
                        with stage("chat_process_message", type=batch.message_type):
                            await chat_service.process_message(
                                user_phone=batch.user_phone,
                                message_content=batch.concatenated_content,
                                message_type=batch.message_type
                            )
                    finally:
                        # Cleanup to prevent unclosed aiohttp sessions
                        with stage("chat_cleanup"):
                            await chat_service.cleanup()

            # Fallback cleanup if no send method was called
            session_key = self._key_session(batch.user_phone)
            session_exists = await self.redis.exists(session_key)
            
            if session_exists:
                logger.warning(
                    f"[PROCESS] [TURN:{trace.turn_id}] Batch {batch.batch_id} completed without cleanup. "
                    f"This indicates processing finished without sending a response, so "
                    f"{batch.user_phone} received nothing for this message. Cleaning up manually."
                )
                trace.finish("no_reply", reason="completed_without_send")
                await self._cleanup_and_next(batch.batch_id, batch.user_phone, success=True)
            else:
                trace.finish("complete")

        except Exception as e:
            logger.error(
                f"[PROCESS] [TURN:{trace.turn_id}] Error processing batch {batch.batch_id}: {e}",
                exc_info=True
            )
            trace.finish("error", error=type(e).__name__)
            await self._cleanup_and_next(batch.batch_id, batch.user_phone, success=False)

    # ========================================================================
    # WhatsApp Wrapper (Backward Compatibility)
    # ========================================================================

    def __getattr__(self, name: str):
        """
        Delegate methods to WhatsAppService with automatic wrapping for send methods.
        
        This maintains backward compatibility - ChatService can call send methods
        on MessageQueueService as if it were WhatsAppService, and we intercept
        to add suppression logic and cleanup.
        """
        # Never delegate private attributes. __getattr__ only runs when normal
        # lookup fails, so a private name reaching here means this instance is
        # half-built (or is being unpickled/copied). Forwarding it to
        # WhatsAppService turned a plain missing-attribute bug into
        # "'MessageQueueService' object has no attribute '_running' and neither
        # does WhatsAppService", raised from inside the batch poller, which then
        # died for the lifetime of the worker and stopped answering text
        # messages entirely. Failing fast and honestly keeps that a local bug.
        if name.startswith("_"):
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}' "
                f"(private attributes are never delegated to WhatsAppService)"
            )

        # Get attribute from WhatsAppService
        try:
            attr = getattr(self.whatsapp_service, name)
        except AttributeError:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}' "
                f"and neither does WhatsAppService"
            )
        
        # If not callable, return as-is
        if not callable(attr):
            return attr
        
        # If it's a send method, wrap it
        if name.startswith('send_'):
            async def wrapped_send(*args, **kwargs):
                # Extract recipient_id (first positional arg)
                recipient_id = args[0] if args else kwargs.get('recipient_id')
                
                if not recipient_id:
                    logger.error(f"{name} called without recipient_id")
                    raise ValueError(f"{name} requires recipient_id")
                
                # Normalize phone (remove '+' for Redis key)
                user_phone = str(recipient_id).lstrip('+').strip()
                
                # Get session context
                session_key = self._key_session(user_phone)
                session_json = await self.redis.get(session_key)
                
                if not session_json:
                    # No session - this is a direct send (non-queued)
                    logger.debug(
                        f"[SEND] {name} for {recipient_id} without session context, "
                        f"sending directly"
                    )
                    return await attr(*args, **kwargs)
                
                # We have a session - check suppression
                session = ProcessingSession.from_json(session_json)
                
                # Check if should suppress
                should_suppress = await self._should_suppress_response(user_phone)
                
                if should_suppress:
                    logger.debug(
                        f"[SEND] Suppressing {name} for {recipient_id} "
                        f"in batch {session.batch_id} - newer messages exist"
                    )
                    
                    # Mark as suppressed
                    session.suppressed = True
                    await self.redis.setex(session_key, 60, session.to_json())
                    
                    # Cleanup and trigger next batch
                    await self._cleanup_and_next(session.batch_id, user_phone, success=True)
                    
                    # Return mock success
                    return self._mock_success()
                
                # Not suppressed - send for real
                turn_id = current_turn_id()
                try:
                    # Mark response as ready before sending (prevents late please-wait)
                    response_ready_key = self._key_response_ready(user_phone)
                    await self.redis.setex(response_ready_key, self.response_ready_ttl, "1")
                    
                    # Calculate processing time for logging
                    processing_time = time.time() - session.started_at
                    logger.info(
                        f"[MESSAGE_QUEUE] [SEND] [TURN:{turn_id}] Response ready for {recipient_id} "
                        f"after {processing_time:.2f}s (batch {session.batch_id})"
                    )
                    
                    logger.info(
                        f"[MESSAGE_QUEUE] [SEND] [TURN:{turn_id}] Calling {name} for {recipient_id} "
                        f"in batch {session.batch_id}"
                    )
                    with stage(f"send:{name}"):
                        result = await attr(*args, **kwargs)
                    
                    # Log result
                    success = getattr(result, 'success', True)
                    if success:
                        logger.info(
                            f"[MESSAGE_QUEUE] [SEND] [TURN:{turn_id}] {name} succeeded for {recipient_id}, "
                            f"message_id={getattr(result, 'message_id', 'N/A')}"
                        )
                    else:
                        logger.error(
                            f"[MESSAGE_QUEUE] [SEND] [TURN:{turn_id}] {name} failed for {recipient_id}, "
                            f"error={getattr(result, 'error', 'Unknown')} - the user did not receive this reply"
                        )
                    
                    # Cleanup and trigger next batch
                    await self._cleanup_and_next(session.batch_id, user_phone, success)
                    
                    return result
                
                except Exception as e:
                    logger.error(f"[SEND] [TURN:{turn_id}] Error in {name}: {e}", exc_info=True)
                    await self._cleanup_and_next(session.batch_id, user_phone, success=False)
                    raise
            
            return wrapped_send
        
        # Non-send methods returned as-is
        return attr

    async def _should_suppress_response(self, user_phone: str) -> bool:
        """
        Check if response should be suppressed.
        Returns True if newer messages/batches exist.
        """
        incoming_key = self._key_incoming(user_phone)
        outgoing_key = self._key_outgoing(user_phone)
        
        incoming_count = await self.redis.zcard(incoming_key)
        outgoing_count = await self.redis.llen(outgoing_key)
        
        should_suppress = incoming_count > 0 or outgoing_count > 0
        
        if should_suppress:
            logger.debug(
                f"[SUPPRESS] {user_phone} has newer messages: "
                f"incoming={incoming_count}, outgoing={outgoing_count}"
            )
        
        return should_suppress

    def _mock_success(self):
        """Return mock success result for suppressed sends."""
        class MockResult:
            success = True
            message_id = None
        return MockResult()

    # ========================================================================
    # Cleanup & Next Batch
    # ========================================================================

    async def _cleanup_and_next(
        self,
        batch_id: str,
        user_phone: str,
        success: bool
    ) -> None:
        """
        Clean up after batch processing and trigger next batch.
        
        Args:
            batch_id: ID of completed batch
            user_phone: User's phone number
            success: Whether processing succeeded
        """
        try:
            logger.info(
                f"[MESSAGE_QUEUE] [CLEANUP] Batch {batch_id} for {user_phone}, "
                f"success={success}"
            )
            
            # Clear processing state
            processing_key = self._key_processing(user_phone)
            session_key = self._key_session(user_phone)
            response_ready_key = self._key_response_ready(user_phone)
            
            await self.redis.delete(processing_key)
            await self.redis.delete(session_key)
            await self.redis.delete(response_ready_key)

            # Remove interval dedupe markers for this user.
            cursor = 0
            while True:
                cursor, interval_keys = await self.redis.scan(
                    cursor=cursor,
                    match=f"{user_phone}:please_wait:interval:*",
                    count=100
                )
                if interval_keys:
                    await self.redis.delete(*interval_keys)
                if cursor == 0:
                    break
            
            logger.debug(f"[CLEANUP] Cleared processing state and response ready flag for {user_phone}")
            
            # If all queues empty, clear ack flag for next conversation session
            incoming_key = self._key_incoming(user_phone)
            outgoing_key = self._key_outgoing(user_phone)
            
            incoming_count = await self.redis.zcard(incoming_key)
            outgoing_count = await self.redis.llen(outgoing_key)
            
            if incoming_count == 0 and outgoing_count == 0:
                # Session complete - clear ack flag so user can start fresh next time
                ack_sent_key = self._key_ack_sent(user_phone)
                await self.redis.delete(ack_sent_key)
                logger.debug(
                    f"[CLEANUP] All queues empty for {user_phone}, "
                    f"conversation session complete, cleared ack flag"
                )
            
            # If new unbatched messages arrived during processing, merge them into a single batch now
            if incoming_count > 0 and outgoing_count == 0:
                logger.debug(
                    f"[CLEANUP] Merging {incoming_count} pending incoming messages for {user_phone} into next batch"
                )
                await self._create_batch(user_phone)
            else:
                # Trigger next batch if available
                await self._try_start_processing(user_phone)
        
        except Exception as e:
            logger.error(
                f"[CLEANUP] Error cleaning up batch {batch_id}: {e}",
                exc_info=True
            )

    # ========================================================================
    # Acknowledgment & Please-Wait Messages
    # ========================================================================

    async def _should_send_acknowledgment(self, user_phone: str) -> bool:
        """
        Determine if acknowledgment should be sent.
        Disabled to prevent repetitive 'Got it. Please wait...' messages on WhatsApp.
        """
        return False

    async def _send_acknowledgment(self, user_phone: str) -> None:
        """Disabled auto-ack."""
        pass

    async def _send_please_wait(self, user_phone: str) -> None:
        """Disabled intermediate please-wait messages to avoid chat spam."""
        pass

    # ========================================================================
    # Utility Methods
    # ========================================================================

    async def get_queue_status(self, user_phone: str) -> Dict:
        """
        Get current queue status for a user.
        Useful for debugging and monitoring.
        """
        incoming_key = self._key_incoming(user_phone)
        outgoing_key = self._key_outgoing(user_phone)
        processing_key = self._key_processing(user_phone)
        session_key = self._key_session(user_phone)
        trigger_key = self._key_batch_trigger(user_phone)
        
        incoming_count = await self.redis.zcard(incoming_key)
        outgoing_count = await self.redis.llen(outgoing_key)
        processing_batch = await self.redis.get(processing_key)
        session_json = await self.redis.get(session_key)
        timer_active = await self.redis.exists(trigger_key)
        
        session_data = None
        if session_json:
            try:
                session = ProcessingSession.from_json(session_json)
                session_data = {
                    "batch_id": session.batch_id,
                    "started_at": session.started_at,
                    "duration": time.time() - session.started_at,
                    "ack_sent": session.ack_sent,
                    "please_wait_sent": session.please_wait_sent_count > 0,
                    "suppressed": session.suppressed
                }
            except Exception as e:
                logger.error(f"Error parsing session: {e}")
        
        return {
            "user_phone": user_phone,
            "incoming_queue_size": incoming_count,
            "outgoing_queue_size": outgoing_count,
            "currently_processing": processing_batch,
            "timer_active": timer_active,
            "session": session_data
        }

    async def cleanup_user_state(self, user_phone: str) -> None:
        """
        Clean up all Redis state for a user.
        Use with caution - for testing/debugging only.
        """
        keys_to_delete = [
            self._key_incoming(user_phone),
            self._key_outgoing(user_phone),
            self._key_processing(user_phone),
            self._key_batch_trigger(user_phone),
            self._key_session(user_phone),
        ]
        
        for key in keys_to_delete:
            await self.redis.delete(key)
        
        logger.debug(f"[CLEANUP] Cleaned up all state for {user_phone}")

    async def get_health_metrics(self) -> Dict[str, Any]:
        """
        Get system-wide health metrics.
        
        Returns:
            Dict with active users, processing count, queue depths
        """
        try:
            metrics = {
                "timestamp": time.time(),
                "active_users": 0,
                "processing_count": 0,
                "total_incoming": 0,
                "total_outgoing": 0,
                "slow_batches": 0
            }
            
            # Count processing batches
            cursor = 0
            while True:
                cursor, keys = await self.redis.scan(
                    cursor=cursor,
                    match="*:processing",
                    count=100
                )
                metrics["processing_count"] += len(keys)
                if cursor == 0:
                    break
            
            # Count users with messages
            cursor = 0
            incoming_users = set()
            while True:
                cursor, keys = await self.redis.scan(
                    cursor=cursor,
                    match="*:incoming",
                    count=100
                )
                for key in keys:
                    count = await self.redis.zcard(key)
                    if count > 0:
                        incoming_users.add(key.replace(":incoming", ""))
                        metrics["total_incoming"] += count
                if cursor == 0:
                    break
            
            # Count users with batches
            cursor = 0
            outgoing_users = set()
            while True:
                cursor, keys = await self.redis.scan(
                    cursor=cursor,
                    match="*:outgoing",
                    count=100
                )
                for key in keys:
                    count = await self.redis.llen(key)
                    if count > 0:
                        outgoing_users.add(key.replace(":outgoing", ""))
                        metrics["total_outgoing"] += count
                if cursor == 0:
                    break
            
            # Count slow batches (>30s)
            cursor = 0
            now = time.time()
            while True:
                cursor, keys = await self.redis.scan(
                    cursor=cursor,
                    match="*:session",
                    count=100
                )
                for key in keys:
                    try:
                        session_json = await self.redis.get(key)
                        if session_json:
                            session = ProcessingSession.from_json(session_json)
                            duration = now - session.started_at
                            if duration > 30:
                                metrics["slow_batches"] += 1
                    except Exception:
                        pass
                if cursor == 0:
                    break
            
            metrics["active_users"] = len(incoming_users | outgoing_users)
            
            return metrics
        
        except Exception as e:
            logger.error(f"[HEALTH] Error getting metrics: {e}", exc_info=True)
            return {
                "timestamp": time.time(),
                "error": str(e)
            }

    async def shutdown(self) -> None:
        """
        Graceful shutdown - cancel background tasks.
        Call this when shutting down the application.
        """
        logger.info(
            f"[MESSAGE_QUEUE] [SHUTDOWN] Cancelling {len(self._background_tasks)} background "
            f"task(s) and {len(self.flush_tasks)} pending batch flush(es)..."
        )
        self._running = False

        # Pending flushes hold messages that have not been answered yet. They are
        # left in {phone}:incoming with their trigger key, so another worker's
        # poller picks them up; logging the count makes that hand-off visible
        # instead of looking like lost messages.
        flush_tasks = list(self.flush_tasks.values())
        if flush_tasks:
            logger.warning(
                f"[MESSAGE_QUEUE] [SHUTDOWN] {len(flush_tasks)} user(s) had a batch window open; "
                f"their messages stay queued for another worker's poller"
            )
        for task in flush_tasks:
            if not task.done():
                task.cancel()
        if flush_tasks:
            await asyncio.gather(*flush_tasks, return_exceptions=True)
        self.flush_tasks.clear()

        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        
        # Wait for tasks to complete cancellation
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        
        # Close Redis connection safely
        try:
            await self.redis.close()
        except Exception as e:
            logger.debug(f"[SHUTDOWN] Redis connection close log: {e}")
        
        logger.debug("[SHUTDOWN] MessageQueueService shutdown complete")