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
import time
import json
from typing import Dict, List, Optional, Any

from redis.asyncio import Redis

from app.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

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
        return cls(**data)


@dataclasses.dataclass
class Batch:
    """Represents a batch of messages."""
    batch_id: str
    user_phone: str
    concatenated_content: str
    message_type: str
    message_count: int
    created_at: float

    def to_dict(self) -> Dict:
        return dataclasses.asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Batch':
        return cls(**data)


@dataclasses.dataclass
class ProcessingSession:
    """
    Consolidated session state for a user's active processing.
    Stored as JSON in Redis with 60s TTL.
    """
    batch_id: str
    started_at: float
    ack_sent: bool = False
    please_wait_sent: bool = False
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
    - Batch poller: Creates batches when timer expires
    - Monitor: Sends please-wait messages, logs slow batches
    """

    def __init__(self):
        settings = get_settings()
        
        # Redis client
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        
        # Configuration
        self.batch_window = settings.batch_window_seconds  # Default: 3s
        self.please_wait_threshold = settings.please_wait_threshold_seconds  # Default: 15s
        self.monitoring_poll_interval = settings.monitoring_poll_interval_seconds  # Default: 5s
        
        # WhatsApp service for direct sending (ack, please-wait)
        from app.services.whatsapp_service import WhatsAppService
        self.whatsapp_service = WhatsAppService()
        
        # Background task handles (for lifecycle management)
        self._background_tasks: List[asyncio.Task] = []
        
        logger.debug(
            f"[INIT] MessageQueueService initialized: "
            f"batch_window={self.batch_window}s, "
            f"please_wait_threshold={self.please_wait_threshold}s, "
            f"monitoring_poll_interval={self.monitoring_poll_interval}s"
        )

    # ========================================================================
    # Redis Key Helpers
    # ========================================================================

    def _key_incoming(self, user_phone: str) -> str:
        return f"{user_phone}:incoming"

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
        try:
            # Parse webhook data
            timestamp_raw = webhook_data.get("timestamp", time.time())
            
            # Handle string timestamp format
            if isinstance(timestamp_raw, str):
                from datetime import datetime
                dt = datetime.strptime(timestamp_raw, '%Y-%m-%d %H:%M:%S')
                timestamp = dt.timestamp()
            else:
                timestamp = float(timestamp_raw)
            
            user_phone = webhook_data.get("from", "").lstrip('+')
            message_id = webhook_data.get("message_id", f"{user_phone}_{timestamp}")
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
            await self.redis.zadd(
                incoming_key,
                {json.dumps(message.to_dict()): timestamp}
            )
            
            logger.debug(
                f"[ENQUEUE] message_id='{message_id}', user={user_phone}, "
                f"type={message_type}"
            )
            
            # Check if should send acknowledgment
            should_ack = await self._should_send_acknowledgment(user_phone)
            if should_ack:
                asyncio.create_task(self._send_acknowledgment(user_phone))
            
            # Start/refresh batch timer
            await self._refresh_batch_timer(user_phone)
            
            # NOTE: Background tasks are started in main.py lifespan
            # No need to ensure them here (prevents duplication)
            
        except Exception as e:
            logger.error(f"[ENQUEUE] Error: {e}", exc_info=True)
            raise

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
        logger.debug(f"[TIMER] Refreshed batch timer for {user_phone}")

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

    async def run_batch_poller(self) -> None:
        """
        Background task: Poll for users with expired batch timers.
        Creates batches when timer expires and messages exist.
        
        Runs every 1 second. Uses distributed lock to ensure only ONE worker
        polls at a time across all Gunicorn workers (prevents duplicate polling).
        """
        logger.debug("[POLLER] Batch poller started")
        
        try:
            while True:
                await asyncio.sleep(1)  # Poll every second
                
                # Global poller lock - only one worker should poll at a time
                global_poller_lock_key = "global:poller:lock"
                poller_lock = self.redis.lock(
                    global_poller_lock_key,
                    timeout=2,  # 2s timeout (longer than poll cycle)
                    blocking_timeout=0  # Non-blocking - skip if another worker is polling
                )
                
                try:
                    # Try to acquire global poller lock (non-blocking)
                    acquired = await poller_lock.acquire()
                    if not acquired:
                        # Another worker is polling, skip this cycle
                        continue
                    
                    try:
                        # Find all incoming queues
                        cursor = 0
                        incoming_keys = []
                        while True:
                            cursor, keys = await self.redis.scan(
                                cursor=cursor,
                                match="*:incoming",
                                count=100
                            )
                            incoming_keys.extend(keys)
                            if cursor == 0:
                                break
                        
                        # Check each user for expired timer
                        for key in incoming_keys:
                            user_phone = key.rsplit(":incoming", 1)[0]
                            
                            # Check if timer exists
                            trigger_key = self._key_batch_trigger(user_phone)
                            timer_exists = await self.redis.exists(trigger_key)
                            if not timer_exists:
                                # Timer expired, check if messages exist
                                message_count = await self.redis.zcard(key)
                                if message_count > 0:
                                    logger.debug(
                                        f"[POLLER] Timer expired for {user_phone}, "
                                        f"{message_count} messages, creating batch"
                                    )
                                    await self._create_batch(user_phone)
                    
                    finally:
                        # Always release the global poller lock
                        await poller_lock.release()
                
                except Exception as e:
                    logger.error(f"[POLLER] Error in poll cycle: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.debug("[POLLER] Batch poller cancelled")
            raise
        except Exception as e:
            logger.error(f"[POLLER] Batch poller failed: {e}", exc_info=True)

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
            while True:
                await asyncio.sleep(self.monitoring_poll_interval)  # Configurable poll interval
                
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
                            
                            # Send please-wait if threshold exceeded
                            if duration >= self.please_wait_threshold and not session.please_wait_sent:
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
                                    ex=5
                                )
                                
                                if lock_acquired:
                                    # This worker won the race - double-check and send
                                    try:
                                        session_json_check = await self.redis.get(key)
                                        if not session_json_check:
                                            continue
                                        
                                        session_check = ProcessingSession.from_json(session_json_check)
                                        if session_check.please_wait_sent:
                                            logger.debug(
                                                f"[MONITOR] Please-wait already sent by another worker "
                                                f"for {user_phone}"
                                            )
                                            continue
                                        
                                        logger.debug(
                                            f"[MONITOR] Sending please-wait to {user_phone} "
                                            f"after {duration:.1f}s"
                                        )
                                        await self._send_please_wait(user_phone)
                                        
                                        # Update session
                                        session.please_wait_sent = True
                                        await self.redis.setex(
                                            key,
                                            60,  # Refresh TTL
                                            session.to_json()
                                        )
                                    except Exception as send_error:
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
                    logger.error(f"[MONITOR] Error in monitor cycle: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.debug("[MONITOR] Monitoring loop cancelled")
            raise
        except Exception as e:
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
                
                # Create batch
                batch_id = f"{user_phone}+{int(time.time() * 1000)}"
                concatenated_content = "\n".join([msg.content for msg in messages])
                
                batch = Batch(
                    batch_id=batch_id,
                    user_phone=user_phone,
                    concatenated_content=concatenated_content,
                    message_type=messages[0].message_type,
                    message_count=len(messages),
                    created_at=time.time()
                )
                
                # Add to outgoing queue
                outgoing_key = self._key_outgoing(user_phone)
                await self.redis.rpush(outgoing_key, json.dumps(batch.to_dict()))
                
                logger.debug(
                    f"[BATCH_CREATE] Created batch {batch_id} for {user_phone}: "
                    f"{len(messages)} messages, "
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
                
                logger.debug(
                    f"[START] Starting processing for batch {batch.batch_id}, "
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
        try:
            logger.debug(
                f"[PROCESS] Batch {batch.batch_id} for {batch.user_phone}: "
                f"{batch.message_count} messages"
            )
            
            # Import here to avoid circular dependency
            from app.services.chat_service import ChatService
            from app.database import get_db_session_context
            
            # Process through ChatService with session management
            with get_db_session_context() as db:
                chat_service = ChatService(
                    db_session=db,
                    message_queue_service=self  # Pass self as whatsapp_service
                )
                try:
                    await chat_service.process_message(
                        user_phone=batch.user_phone,
                        message_content=batch.concatenated_content,
                        message_type=batch.message_type
                    )
                finally:
                    # Cleanup to prevent unclosed aiohttp sessions
                    await chat_service.cleanup()
            
            # Fallback cleanup if no send method was called
            session_key = self._key_session(batch.user_phone)
            session_exists = await self.redis.exists(session_key)
            
            if session_exists:
                logger.warning(
                    f"[PROCESS] Batch {batch.batch_id} completed without cleanup. "
                    f"This indicates processing finished without sending a response. "
                    f"Cleaning up manually."
                )
                await self._cleanup_and_next(batch.batch_id, batch.user_phone, success=True)
        
        except Exception as e:
            logger.error(
                f"[PROCESS] Error processing batch {batch.batch_id}: {e}",
                exc_info=True
            )
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
                user_phone = recipient_id.lstrip('+') if isinstance(recipient_id, str) else recipient_id
                
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
                try:
                    # Mark response as ready before sending (prevents late please-wait)
                    response_ready_key = self._key_response_ready(user_phone)
                    await self.redis.setex(response_ready_key, 10, "1")  # 10s TTL
                    
                    # Calculate processing time for logging
                    processing_time = time.time() - session.started_at
                    logger.debug(
                        f"[SEND] Response ready for {recipient_id} after {processing_time:.1f}s "
                        f"(batch {session.batch_id}) - marked to prevent late please-wait"
                    )
                    
                    logger.debug(
                        f"[SEND] Calling {name} for {recipient_id} "
                        f"in batch {session.batch_id}"
                    )
                    result = await attr(*args, **kwargs)
                    
                    # Log result
                    success = getattr(result, 'success', True)
                    if success:
                        logger.debug(
                            f"[SEND] {name} succeeded for {recipient_id}, "
                            f"message_id={getattr(result, 'message_id', 'N/A')}"
                        )
                    else:
                        logger.error(
                            f"[SEND] {name} failed for {recipient_id}, "
                            f"error={getattr(result, 'error', 'Unknown')}"
                        )
                    
                    # Cleanup and trigger next batch
                    await self._cleanup_and_next(session.batch_id, user_phone, success)
                    
                    return result
                
                except Exception as e:
                    logger.error(f"[SEND] Error in {name}: {e}", exc_info=True)
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
            logger.debug(
                f"[CLEANUP] Batch {batch_id} for {user_phone}, "
                f"success={success}"
            )
            
            # Clear processing state
            processing_key = self._key_processing(user_phone)
            session_key = self._key_session(user_phone)
            response_ready_key = self._key_response_ready(user_phone)
            
            await self.redis.delete(processing_key)
            await self.redis.delete(session_key)
            await self.redis.delete(response_ready_key)
            
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
        
        Returns True if:
        - System is busy (processing, or queues not empty)
        - AND acknowledgment not already sent this conversation session
        
        Note: Uses separate ack_sent flag (300s TTL) independent of batch session
        to prevent duplicate acks across multiple batches in same conversation.
        """
        # Check if ack already sent in this conversation session
        ack_sent_key = self._key_ack_sent(user_phone)
        ack_already_sent = await self.redis.get(ack_sent_key)
        
        if ack_already_sent:
            logger.debug(f"[ACK] Already sent in this conversation for {user_phone}")
            return False
        
        # Check if system is busy
        processing_key = self._key_processing(user_phone)
        incoming_key = self._key_incoming(user_phone)
        outgoing_key = self._key_outgoing(user_phone)
        
        is_processing = await self.redis.exists(processing_key)
        incoming_count = await self.redis.zcard(incoming_key)
        outgoing_count = await self.redis.llen(outgoing_key)
        
        # Busy if processing, or batches waiting, or multiple messages in incoming
        is_busy = is_processing or outgoing_count > 0 or incoming_count > 1
        
        logger.debug(
            f"[ACK] {user_phone}: processing={is_processing}, "
            f"incoming={incoming_count}, outgoing={outgoing_count}, "
            f"busy={is_busy}, ack_sent={bool(ack_already_sent)}"
        )
        
        return is_busy

    async def _send_acknowledgment(self, user_phone: str) -> None:
        """
        Send acknowledgment message with atomic flag claiming.
        Uses lock to prevent duplicate sends across workers.
        Sets separate ack_sent flag with 300s TTL (5 minutes) that persists
        across multiple batches in the same conversation session.
        """
        lock_key = self._key_lock_ack(user_phone)
        lock = self.redis.lock(lock_key, timeout=10, blocking_timeout=1)
        
        try:
            async with lock:
                # Double-check inside lock using separate ack_sent flag
                ack_sent_key = self._key_ack_sent(user_phone)
                ack_already_sent = await self.redis.get(ack_sent_key)
                
                if ack_already_sent:
                    logger.debug(f"[ACK] Already sent (double-check) for {user_phone}")
                    return
                
                # Set ack_sent flag with 300s TTL (5 minutes)
                # This persists across batches in the same conversation
                await self.redis.setex(ack_sent_key, 300, "1")
                
                # Also update session if it exists (for backward compatibility)
                session_key = self._key_session(user_phone)
                session_json = await self.redis.get(session_key)
                
                if session_json:
                    session = ProcessingSession.from_json(session_json)
                    session.ack_sent = True
                    await self.redis.setex(session_key, 60, session.to_json())
                else:
                    # No session yet - create minimal session for ack tracking
                    # This can happen if ack is sent before first batch starts processing
                    session = ProcessingSession(
                        batch_id="pending",
                        started_at=time.time(),
                        ack_sent=True
                    )
                    await self.redis.setex(session_key, 60, session.to_json())
                
                # Send acknowledgment
                recipient_id = f"+{user_phone}" if not user_phone.startswith('+') else user_phone
                
                await self.whatsapp_service.send_message(
                    recipient_id=recipient_id,
                    message="Got it. Please wait while we process your request, we will be back shortly."
                )
                
                logger.debug(f"[ACK] Sent acknowledgment to {user_phone} (flag expires in 300s)")
        
        except Exception as e:
            logger.warning(
                f"[ACK] Failed to send acknowledgment to {user_phone}: {e}",
                exc_info=True
            )

    async def _send_please_wait(self, user_phone: str) -> None:
        """
        Send please-wait message directly.
        Called by monitoring loop when processing exceeds threshold.
        """
        try:
            recipient_id = f"+{user_phone}" if not user_phone.startswith('+') else user_phone
            
            await self.whatsapp_service.send_message(
                recipient_id=recipient_id,
                message="Your request is taking longer than expected. Please wait while we process..."
            )
            
            logger.debug(f"[PLEASE_WAIT] Sent to {user_phone}")
        
        except Exception as e:
            logger.warning(
                f"[PLEASE_WAIT] Failed to send to {user_phone}: {e}",
                exc_info=True
            )

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
                    "please_wait_sent": session.please_wait_sent,
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
        logger.debug("[SHUTDOWN] Cancelling background tasks...")
        
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        
        # Wait for tasks to complete cancellation
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        
        # Close Redis connection
        await self.redis.close()
        
        logger.debug("[SHUTDOWN] MessageQueueService shutdown complete")