"""
Message Queueing Service for messages received from webhook.

This service handles queueing, and batch processing of user messages.
It queues incoming messages, sorts them into batches, and processes batches sequentially to ensure
that messages are sent in the correct order and without overwhelming the recipient.

======================
Session Management Notes
======================

Database Session Propagation (to prevent connection leaks):
- ChatService receives db_session and passes to:
  ✅ DatabaseManager(session=db_session)
  ✅ VendorService(db_session=db_session)
  ✅ RFQBackgroundService(db_session=db_session)
  ✅ SellerService(db_session=db_session)
  ✅ RFQStatusService(db_session=db_session)
  ✅ ChatSummaryService(db_session=db_session)

- Sub-services that accept db_session but may still leak if not properly passed:
  ⚠️ RFQBackgroundService creates:
     - SellerRecommendationService(self.db_session) ✅
     - RFQIntimationService(self.db_session) ✅
     Note: These receive the session from RFQBackgroundService, but if
     RFQBackgroundService doesn't receive a session, it creates its own
     with get_db_session(), which won't be closed by the context manager.

- Services that always create their own sessions (designed for independent use):
  ℹ️ DailySummaryService - uses get_db_session() internally (background job)
  ℹ️ LearningCategorizationService - uses get_db_session() internally (background job)

Recommendation: 
- Continue to pass db_session through the entire chain
- RFQBackgroundService properly receives and propagates session
- Monitor connection pool metrics (see database.py log_connection_pool_status())
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

@dataclasses.dataclass
class Message:
    """Represents a message received from the webhook."""
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

    def to_dict(self):
        return dataclasses.asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


class MessageQueueService:
    """
    Service for managing message queues using Redis for distributed coordination.
    
    Redis Keys Structure:
    - {user_phone}:incoming -> Sorted Set (score=timestamp, value=json(Message))
    - {user_phone}:outgoing -> List (FIFO queue of json(Batch))
    - {user_phone}:timer -> String (timestamp when timer was started)
    - {user_phone}:processing -> String (batch_id currently being processed)
    - {user_phone}:current_batch -> String (batch_id currently being sent)
    - {user_phone}:ack_sent -> String (flag indicating acknowledgment was sent)
    - {user_phone}:suppressed -> String (batch_id that was suppressed)
    - {user_phone}:lock:timer -> Lock for timer operations
    - {user_phone}:lock:batch -> Lock for batch creation operations
    """

    INCOMING_QUEUE_SUFFIX = ":incoming"
    OUTGOING_QUEUE_SUFFIX = ":outgoing"
    TIMER_KEY_SUFFIX = ":timer"
    PROCESSING_KEY_SUFFIX = ":processing"
    CURRENT_BATCH_SUFFIX = ":current_batch"  # NEW: stores batch_id for current send operation
    TIMER_LOCK_SUFFIX = ":lock:timer"
    BATCH_LOCK_SUFFIX = ":lock:batch"
    PROCESSING_PAYLOAD_SUFFIX = ":processing_payload"
    ACK_SENT_SUFFIX = ":ack_sent"  # NEW: tracks if acknowledgment was sent for current processing session
    SUPPRESSED_SUFFIX = ":suppressed"  # NEW: tracks if batch responses should be suppressed
    ACK_LOCK_SUFFIX = ":lock:ack"  # NEW: lock for atomic acknowledgment sending

    TIMER_SCHEDULE_SET = "message_queue:timers"

    def __init__(self):
        
        # Get settings from config
        settings = get_settings()

        # Instantiate Redis client
        self.redis_client = Redis.from_url(settings.redis_url, decode_responses=True)

        # Configuration
        self.batch_window = settings.batch_window_seconds

        # Scheduler/task bookkeeping
        self._scheduler_task: Optional[asyncio.Task] = None
        self._scheduler_lock: Optional[asyncio.Lock] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_lock: Optional[asyncio.Lock] = None
        self._inflight_tasks: Dict[str, asyncio.Task] = {}

        # Import WhatsAppService for sending messages
        from app.services.whatsapp_service import WhatsAppService
        self.whatsapp_service = WhatsAppService()

        logger.info("MessageQueueService initialized with batch_window=%s seconds", self.batch_window)

    # =================
    # Redis Key Helpers
    # =================

    def get_incoming_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.INCOMING_QUEUE_SUFFIX}"

    def get_outgoing_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.OUTGOING_QUEUE_SUFFIX}"

    def get_timer_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.TIMER_KEY_SUFFIX}"

    def get_processing_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.PROCESSING_KEY_SUFFIX}"

    def get_current_batch_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.CURRENT_BATCH_SUFFIX}"

    def get_processing_payload_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.PROCESSING_PAYLOAD_SUFFIX}"

    def get_timer_lock_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.TIMER_LOCK_SUFFIX}"

    def get_batch_lock_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.BATCH_LOCK_SUFFIX}"

    def get_ack_sent_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.ACK_SENT_SUFFIX}"

    def get_suppressed_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.SUPPRESSED_SUFFIX}"

    def get_ack_lock_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.ACK_LOCK_SUFFIX}"

    # ==================
    # Message Enqueueing
    # ==================

    async def enqueue_message(self, webhook_data: Dict):
        """
        Add message to user's incoming queue and manage timer.
        
        Flow:
        1. Parse webhook data into Message object
        2. Add to Redis sorted set (incoming queue) with timestamp as score
        3. Acquire timer lock
        4. Cancel existing timer if running
        5. Start new timer
        6. Release timer lock
        """
        
        try:
            # Extract and parse timestamp
            timestamp_raw = webhook_data.get("timestamp", time.time())
            
            # Handle string timestamp format '2025-10-15 19:20:49'
            if isinstance(timestamp_raw, str):
                from datetime import datetime
                dt = datetime.strptime(timestamp_raw, '%Y-%m-%d %H:%M:%S')
                timestamp = dt.timestamp()
            else:
                timestamp = float(timestamp_raw)
            
            user_phone = webhook_data.get("from", "").lstrip('+')
            message_id = webhook_data.get("message_id", f"{user_phone}_{timestamp}")
            message_type = webhook_data.get("type", "text")
            
            # Extract content - ICS format uses 'content' field directly
            content = webhook_data.get("content", "")
            
            # Fallback for other formats
            if not content:
                if message_type == "text":
                    content = webhook_data.get("text", {}).get("body", "")
                elif message_type == "image":
                    content = ""
                    logger.warning("Image message received; no content extracted. Something went wrong in webhook.")
                elif message_type == "document":
                    logger.warning("Document message received; no content extracted. Something went wrong in webhook.")
                    content = ""
                else:
                    logger.warning(f"Unsupported message type '{message_type}'; no content extracted. Something went wrong in webhook.")
                    content = ""

            # Create Message object
            message = Message(
                message_id=message_id,
                user_phone=user_phone,
                content=content,
                message_type=message_type,
                timestamp=timestamp,
                webhook_data=webhook_data
            )

            logger.info(f"Enqueueing message {message_id} for user {user_phone}")

            # Add to incoming queue (sorted set)
            incoming_key = self.get_incoming_key(user_phone)
            
            redis_start = time.time()
            try:
                await self.redis_client.zadd(
                    incoming_key,
                    {json.dumps(message.to_dict()): timestamp}
                )
                redis_duration = time.time() - redis_start
                if redis_duration > 0.5:
                    logger.warning(
                        f"[REDIS-SLOW] ZADD operation took {redis_duration:.2f}s "
                        f"for user {user_phone}"
                    )
            except Exception as redis_error:
                logger.error(
                    f"[REDIS-ERROR] Failed to add message to incoming queue: {redis_error}",
                    exc_info=True
                )
                raise

            # Check if we should send acknowledgment (for 2nd+ messages when system is busy)
            should_send_ack = await self._should_send_acknowledgment(user_phone)
            if should_send_ack:
                # Fire-and-forget: don't await to avoid blocking enqueue
                asyncio.create_task(self._send_acknowledgment(user_phone))

            # Ensure global timer scheduler is running
            await self._ensure_scheduler_task()

            # Ensure watchdog is running
            await self._ensure_watchdog_task()

            # Manage timer with lock
            timer_lock_key = self.get_timer_lock_key(user_phone)
            timer_lock = self.redis_client.lock(timer_lock_key, timeout=5, blocking_timeout=5)
            
            lock_acquire_start = time.time()
            async with timer_lock:
                lock_acquire_duration = time.time() - lock_acquire_start
                if lock_acquire_duration > 1:
                    logger.warning(
                        f"[LOCK-SLOW] Timer lock acquisition took {lock_acquire_duration:.2f}s "
                        f"for user {user_phone}. Potential contention."
                    )
                
                await self._schedule_timer(user_phone)

            logger.info(f"Message {message_id} enqueued successfully for user {user_phone}")

        except Exception as e:
            logger.error(f"Error enqueueing message: {e}", exc_info=True)
            raise

    async def _ensure_scheduler_task(self):
        """
        Ensure the global timer scheduler is running.
        Creates the background task lazily when the first message arrives.
        """
        if self._scheduler_lock is None:
            self._scheduler_lock = asyncio.Lock()

        async with self._scheduler_lock:
            if self._scheduler_task and not self._scheduler_task.done():
                return

            loop = asyncio.get_running_loop()
            self._scheduler_task = loop.create_task(self._timer_scheduler_loop())
            self._scheduler_task.add_done_callback(self._handle_scheduler_completion)
            logger.debug("Started timer scheduler task")

    def _handle_scheduler_completion(self, task: asyncio.Task):
        """
        Called when the scheduler task finishes. Logs the outcome and resets state
        so the scheduler can be restarted on demand.
        """
        if task.cancelled():
            logger.warning("Timer scheduler task was cancelled")
        else:
            exc = task.exception()
            if exc:
                logger.error("Timer scheduler task failed: %s", exc, exc_info=True)
        self._scheduler_task = None

    async def _ensure_watchdog_task(self):
        """
        Ensure the global batch watchdog is running.
        Creates the background task lazily when the first message arrives.
        """
        if self._watchdog_lock is None:
            self._watchdog_lock = asyncio.Lock()

        async with self._watchdog_lock:
            if self._watchdog_task and not self._watchdog_task.done():
                return

            loop = asyncio.get_running_loop()
            self._watchdog_task = loop.create_task(self._watchdog_loop())
            self._watchdog_task.add_done_callback(self._handle_watchdog_completion)
            logger.debug("Started batch watchdog task")

    def _handle_watchdog_completion(self, task: asyncio.Task):
        """
        Called when the watchdog task finishes. Logs the outcome and resets state
        so the watchdog can be restarted on demand.
        """
        if task.cancelled():
            logger.warning("Batch watchdog task was cancelled")
        else:
            exc = task.exception()
            if exc:
                logger.error("Batch watchdog task failed: %s", exc, exc_info=True)
        self._watchdog_task = None

    async def _watchdog_loop(self):
        """
        Continuously monitors batches being processed and alerts on stuck batches.
        Runs every 30 seconds to check for batches that have been processing too long.
        
        This helps detect:
        - Batches stuck due to slow external APIs (OpenAI, DB)
        - Batches approaching the 60s TTL limit
        - Worker crashes that left processing keys orphaned
        """
        backoff = 30.0  # Check every 30 seconds
        
        while True:
            try:
                await asyncio.sleep(backoff)
                
                # Scan for all processing keys
                pattern = "*:processing"
                processing_keys = []
                
                # Use SCAN instead of KEYS for production safety
                cursor = 0
                while True:
                    cursor, keys = await self.redis_client.scan(
                        cursor=cursor, 
                        match=pattern, 
                        count=100
                    )
                    processing_keys.extend(keys)
                    if cursor == 0:
                        break
                
                if not processing_keys:
                    continue
                
                logger.debug(f"[WATCHDOG] Checking {len(processing_keys)} active batches")
                
                for key in processing_keys:
                    try:
                        # Extract user phone from key
                        user_phone = key.replace(self.PROCESSING_KEY_SUFFIX, "")
                        
                        # Get batch_id and check how long it's been processing
                        batch_id = await self.redis_client.get(key)
                        if not batch_id:
                            continue
                        
                        # Get TTL to calculate processing duration
                        ttl = await self.redis_client.ttl(key)
                        
                        if ttl == -1:
                            # Key has no expiry - this shouldn't happen with our setup
                            logger.error(
                                f"[WATCHDOG] Processing key has no TTL! "
                                f"user={user_phone}, batch_id={batch_id}. "
                                f"This indicates a code bug."
                            )
                            continue
                        
                        if ttl == -2:
                            # Key doesn't exist - race condition, skip
                            continue
                        
                        # Calculate how long the batch has been processing
                        # TTL=60s initially, so processing_duration = 60 - ttl
                        processing_duration = 60 - ttl
                        
                        # Alert at different thresholds
                        if processing_duration >= 50:
                            logger.critical(
                                f"[WATCHDOG-CRITICAL] Batch approaching TTL limit! "
                                f"user={user_phone}, batch_id={batch_id}, "
                                f"processing_for={processing_duration}s, ttl_remaining={ttl}s. "
                                f"Risk of parallel batch creation!"
                            )
                        elif processing_duration >= 40:
                            logger.error(
                                f"[WATCHDOG-ERROR] Batch processing very slow! "
                                f"user={user_phone}, batch_id={batch_id}, "
                                f"processing_for={processing_duration}s, ttl_remaining={ttl}s"
                            )
                        elif processing_duration >= 30:
                            logger.warning(
                                f"[WATCHDOG-WARNING] Batch processing slowly. "
                                f"user={user_phone}, batch_id={batch_id}, "
                                f"processing_for={processing_duration}s, ttl_remaining={ttl}s"
                            )
                    
                    except Exception as key_error:
                        logger.error(f"[WATCHDOG] Error checking key {key}: {key_error}")
                        continue
                
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[WATCHDOG] Watchdog loop encountered error: %s", exc, exc_info=True)
                await asyncio.sleep(min(backoff, 30))
            else:
                backoff = 30.0  # Reset backoff on success


    async def _timer_scheduler_loop(self):
        """
        Continuously monitors the global timer sorted set in Redis and triggers batch creation
        when timers expire. Designed to cooperate across multiple workers—only the worker
        that successfully pops an expired timer owns the batch creation.
        """
        backoff = 0.5
        while True:
            try:
                result = await self.redis_client.zrange(
                    self.TIMER_SCHEDULE_SET, 0, 0, withscores=True
                )
                if not result:
                    await asyncio.sleep(0.5)
                    continue

                user_phone, expected_expiry_time = result[0]
                now = time.time()
                delay = expected_expiry_time - now
                if delay > 0.05:
                    await asyncio.sleep(min(delay, 1.0))
                    continue

                popped = await self.redis_client.zpopmin(
                    self.TIMER_SCHEDULE_SET, count=1
                )
                if not popped:
                    continue

                popped_user, popped_expiry = popped[0]
                if popped_user != user_phone:
                    continue

                timer_key = self.get_timer_key(popped_user)
                current_timer_value = await self.redis_client.get(timer_key)

                if current_timer_value is not None:
                    try:
                        current_expiry = float(current_timer_value)
                    except (TypeError, ValueError):
                        current_expiry = None
                else:
                    current_expiry = None

                if current_expiry and abs(current_expiry - popped_expiry) > 1:
                    # Timer was restarted after this scheduler pop; reschedule with latest expiry.
                    await self.redis_client.zadd(
                        self.TIMER_SCHEDULE_SET, {popped_user: current_expiry}
                    )
                    continue

                logger.info("Timer expired for user %s, creating batch", popped_user)
                await self._create_batch(popped_user, expected_expiry=popped_expiry)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Timer scheduler encountered error: %s", exc, exc_info=True)
                await asyncio.sleep(min(backoff, 5))
                backoff = min(backoff * 2, 5)
            else:
                backoff = 0.5

    async def _schedule_timer(self, user_phone: str):
        """
        Schedule (or reschedule) the batch timer for the given user by updating both
        the per-user timer key and the global sorted set.
        """
        timer_expiry_time = time.time() + self.batch_window
        timer_key = self.get_timer_key(user_phone)

        await self.redis_client.set(
            timer_key,
            timer_expiry_time,
            ex=self.batch_window + 5
        )

        await self.redis_client.zadd(
            self.TIMER_SCHEDULE_SET,
            {user_phone: timer_expiry_time}
        )

        logger.debug(
            "Scheduled timer for user %s to expire at %s",
            user_phone,
            timer_expiry_time
        )

    async def _should_send_acknowledgment(self, user_phone: str) -> bool:
        """
        Determine if we should send a processing acknowledgment.
        
        Returns True if:
        - A batch is currently being processed, OR
        - There are batches waiting in the outgoing queue, OR
        - There are multiple messages in the incoming queue (2+)
        
        AND acknowledgment hasn't been sent yet for this session.
        """
        # Check if already sent acknowledgment
        ack_sent_key = self.get_ack_sent_key(user_phone)
        already_sent = await self.redis_client.exists(ack_sent_key)
        
        if already_sent:
            logger.debug(f"[ACK_CHECK] Already sent acknowledgment to {user_phone}, skipping")
            return False
        
        # Check if system is busy
        processing_key = self.get_processing_key(user_phone)
        incoming_key = self.get_incoming_key(user_phone)
        outgoing_key = self.get_outgoing_key(user_phone)
        
        is_processing = await self.redis_client.exists(processing_key)
        incoming_count = await self.redis_client.zcard(incoming_key)
        outgoing_count = await self.redis_client.llen(outgoing_key)
        
        # System is busy if:
        # - Processing a batch, OR
        # - Batches waiting in outgoing, OR
        # - Multiple messages in incoming (current message + at least 1 more)
        is_busy = is_processing or outgoing_count > 0 or incoming_count > 1
        
        logger.info(
            f"[ACK_CHECK] User {user_phone} - is_processing={is_processing}, "
            f"incoming_count={incoming_count}, outgoing_count={outgoing_count}, "
            f"is_busy={is_busy}, will_send_ack={is_busy}"
        )
        
        return is_busy

    async def _send_acknowledgment(self, user_phone: str):
        """
        Send processing acknowledgment directly via WhatsApp with atomic flag claiming.
        
        Uses Redis lock to prevent race conditions where multiple workers
        might send duplicate acknowledgments when messages arrive concurrently.
        
        This bypasses the batch processing flow and sends immediately.
        Errors are logged but don't fail the enqueue process.
        """
        ack_lock_key = self.get_ack_lock_key(user_phone)
        ack_lock = self.redis_client.lock(
            ack_lock_key,
            timeout=10,        # Lock expires after 10 seconds (safety)
            blocking_timeout=1  # Wait max 1 second for lock
        )
        
        try:
            async with ack_lock:
                # Check flag inside lock (atomic check-then-set)
                ack_sent_key = self.get_ack_sent_key(user_phone)
                already_sent = await self.redis_client.exists(ack_sent_key)
                
                if already_sent:
                    logger.info(f"[ACK_SEND] Acknowledgment already sent to {user_phone}, skipping (double-check inside lock)")
                    return
                
                # Set flag BEFORE sending (optimistic approach)
                # If send fails, worst case is user doesn't get ack unnecessarily
                # Better than sending duplicate acknowledgments
                await self.redis_client.set(ack_sent_key, "1", ex=300)
                
                logger.info(f"[ACK_SEND] Set acknowledgment flag for {user_phone}, preparing to send")
                
                # Add '+' prefix for WhatsApp API format
                recipient_id = f"+{user_phone}" if not user_phone.startswith('+') else user_phone
                
                # Send directly via WhatsAppService (not through wrapper)
                await self.whatsapp_service.send_message(
                    recipient_id=recipient_id,
                    message="Got it. Please wait while we process your request, we will be back shortly."
                )
                
                logger.info(f"[ACK_SEND] Successfully sent processing acknowledgment to {user_phone}")
        
        except Exception as e:
            # Don't fail enqueue process on acknowledgment failure
            logger.warning(
                f"Failed to send acknowledgment to {user_phone}: {e}",
                exc_info=True
            )

    # ================
    # Batch Creation
    # ================

    async def _create_batch(self, user_phone: str, expected_expiry: Optional[float] = None):
        """
        Create a batch from incoming messages and add to outgoing queue.
        
        Flow:
        1. Check if user is currently processing (prevent batch creation during processing)
        2. Acquire batch lock
        3. Get all messages from incoming queue (sorted by timestamp)
        4. Clear incoming queue
        5. Concatenate message contents
        6. Generate batch_id
        7. Create Batch object
        8. Add to outgoing queue
        9. Check if we should start processing
        10. Release batch lock
        """
        # CRITICAL: Check if user is currently processing before creating batch
        # This ensures sequential batch processing and prevents session state divergence
        processing_key = self.get_processing_key(user_phone)
        is_processing = await self.redis_client.exists(processing_key)
        
        if is_processing:
            logger.info(
                f"[BATCH_CREATE] User {user_phone} is currently processing. "
                f"Rescheduling timer to prevent overlapping batches."
            )
            await self._schedule_timer(user_phone)
            return
        
        logger.info(f"[BATCH_CREATE] Starting batch creation for user {user_phone}")
        
        batch_lock_key = self.get_batch_lock_key(user_phone)
        batch_lock = self.redis_client.lock(batch_lock_key, timeout=10, blocking_timeout=10)

        lock_acquire_start = time.time()
        async with batch_lock:
            lock_acquire_duration = time.time() - lock_acquire_start
            if lock_acquire_duration > 2:
                logger.warning(
                    f"[LOCK-SLOW] Batch lock acquisition took {lock_acquire_duration:.2f}s "
                    f"for user {user_phone}. Potential contention."
                )
            
            incoming_key = self.get_incoming_key(user_phone)

            # Get all messages (sorted by timestamp)
            message_data_list = await self.redis_client.zrange(incoming_key, 0, -1)

            if not message_data_list:
                logger.info(f"[BATCH_CREATE] No messages in incoming queue for user {user_phone}")
                return
            
            logger.info(
                f"[BATCH_CREATE] Found {len(message_data_list)} messages in incoming queue for user {user_phone}"
            )

            # Parse messages
            messages: List[Message] = []
            for msg_json in message_data_list:
                try:
                    msg_dict = json.loads(msg_json)
                    messages.append(Message.from_dict(msg_dict))
                except Exception as e:
                    logger.error(f"[BATCH_CREATE] Error parsing message during batch creation: {e}")
                    continue

            # CRITICAL: Remove only the messages we retrieved (atomic selective removal)
            # This prevents race condition where new messages arrive during parsing
            # Uses ZREM to remove only the retrieved messages by their exact values,
            # allowing any messages added during processing to remain in the queue.
            if message_data_list:
                removed_count = await self.redis_client.zrem(incoming_key, *message_data_list)
                logger.info(
                    f"[BATCH_CREATE] Removed {removed_count}/{len(message_data_list)} messages "
                    f"from incoming queue for {user_phone}"
                )
                
                # Metric tracking: detect anomalies
                if removed_count < len(message_data_list):
                    logger.warning(
                        f"[BATCH_CREATE] Removed count mismatch for {user_phone}: "
                        f"expected {len(message_data_list)}, removed {removed_count}. "
                        f"Possible duplicate messages or concurrent removal."
                    )

            if not messages:
                logger.warning(
                    "[BATCH_CREATE] Incoming queue for user %s contained only unparsable messages; "
                    "removed them to prevent queue blocking",
                    user_phone,
                )
                return
            
            logger.info(
                f"[BATCH_CREATE] Cleared incoming queue for user {user_phone}. "
                f"Parsed {len(messages)} messages successfully."
            )

            # Concatenate content
            concatenated_content = "\n".join([msg.content for msg in messages])

            # Generate batch_id
            batch_id = f"{user_phone}+{int(time.time() * 1000)}"

            # Use message type from first message (assume all same type in batch)
            message_type = messages[0].message_type

            # Create Batch object
            batch = Batch(
                batch_id=batch_id,
                user_phone=user_phone,
                concatenated_content=concatenated_content,
                message_type=message_type,
                message_count=len(messages),
                created_at=time.time()
            )

            batch_json = json.dumps(batch.to_dict())

            # Add to outgoing queue
            outgoing_key = self.get_outgoing_key(user_phone)
            await self.redis_client.rpush(outgoing_key, batch_json)

            logger.info(
                f"[BATCH_CREATE] Batch {batch_id} created with {len(messages)} messages for user {user_phone}. "
                f"Content preview: {concatenated_content[:100]}..."
            )

            # Clear timer key if this creation matches the timer we claimed
            timer_key = self.get_timer_key(user_phone)
            if expected_expiry is not None:
                current_timer_value = await self.redis_client.get(timer_key)
                try:
                    current_timer = float(current_timer_value) if current_timer_value else None
                except (TypeError, ValueError):
                    current_timer = None

                if current_timer is None or abs(current_timer - expected_expiry) <= 1:
                    await self.redis_client.delete(timer_key)
                    logger.debug(f"[BATCH_CREATE] Cleared timer key for user {user_phone}")
            else:
                await self.redis_client.delete(timer_key)
                logger.debug(f"[BATCH_CREATE] Cleared timer key for user {user_phone}")

        # Check if we should start processing (outside the lock)
        logger.info(f"[BATCH_CREATE] Checking if processing should start for user {user_phone}")
        await self._check_and_start_processing(user_phone)

    async def _check_and_start_processing(self, user_phone: str):
        """
        Check if outgoing queue should start processing.
        Start processing if no batch is currently being processed.
        Must be called within batch lock or after batch creation.
        """
        processing_key = self.get_processing_key(user_phone)
        outgoing_key = self.get_outgoing_key(user_phone)

        # Check if already processing
        currently_processing = await self.redis_client.get(processing_key)

        if currently_processing:
            logger.info(
                f"[START_PROCESSING] User {user_phone} already processing batch {currently_processing}. "
                f"New batch will wait in queue."
            )
            return

        # Claim first batch atomically
        batch_json = await self.redis_client.lpop(outgoing_key)

        if not batch_json:
            logger.debug(f"[START_PROCESSING] No batches in outgoing queue for user {user_phone}")
            return

        # Parse batch
        try:
            batch_dict = json.loads(batch_json)
            batch = Batch.from_dict(batch_dict)
        except Exception as e:
            logger.error(f"Error parsing batch: {e}")
            # Push back the raw data so it can be inspected later
            await self.redis_client.lpush(outgoing_key, batch_json)
            return

        # Mark as processing
        await self.redis_client.set(processing_key, batch.batch_id, ex=60)  # 60s TTL

        # Store processing start time for monitoring
        processing_start_key = f"{user_phone}:processing_started_at"
        await self.redis_client.set(processing_start_key, time.time(), ex=60)

        # Persist payload for safe retries if needed
        processing_payload_key = self.get_processing_payload_key(user_phone)
        await self.redis_client.set(processing_payload_key, batch_json, ex=60)  # 60s TTL

        logger.info(
            f"[START_PROCESSING] Starting processing for batch {batch.batch_id}. "
            f"Set processing_key={batch.batch_id}"
        )

        # Start supervised processing task
        self._register_processing_task(batch)

    def _register_processing_task(self, batch: Batch):
        """
        Launch the batch processing coroutine and supervise its lifecycle.
        Ensures we capture cancellations/exceptions and trigger cleanup.
        """
        task = asyncio.create_task(self._process_batch(batch))
        self._inflight_tasks[batch.batch_id] = task

        def _on_task_done(completed_task: asyncio.Task, *, batch_ref: Batch = batch):
            self._inflight_tasks.pop(batch_ref.batch_id, None)

            if completed_task.cancelled():
                logger.error(
                    "Processing task for batch %s was cancelled; triggering cleanup",
                    batch_ref.batch_id
                )
                asyncio.create_task(
                    self._handle_batch_cleanup(
                        batch_ref.batch_id,
                        batch_ref.user_phone,
                        success=False
                    )
                )
                return

            exc = completed_task.exception()
            if exc:
                logger.error(
                    "Processing task for batch %s failed: %s",
                    batch_ref.batch_id,
                    exc,
                    exc_info=True
                )
                asyncio.create_task(
                    self._handle_batch_cleanup(
                        batch_ref.batch_id,
                        batch_ref.user_phone,
                        success=False
                    )
                )

        task.add_done_callback(_on_task_done)

    # ==================
    # Batch Processing
    # ==================

    async def _process_batch(self, batch: Batch):
        """
        Process a batch through ChatService.
        """
        current_batch_key = None
        processing_start = time.time()
        processing_start_key = f"{batch.user_phone}:processing_started_at"
        
        try:
            # Store batch context in Redis for wrapper methods to access
            current_batch_key = self.get_current_batch_key(batch.user_phone)
            await self.redis_client.set(current_batch_key, batch.batch_id, ex=60)  # 60s TTL
            
            logger.info(
                f"[BATCH_PROCESS] Starting batch {batch.batch_id} for user {batch.user_phone}. "
                f"Message count: {batch.message_count}. Content preview: {batch.concatenated_content[:100]}..."
            )
            
            # Import ChatService here to avoid circular import
            from app.services.chat_service import ChatService
            from app.database import get_db_session_context

            # Use context manager for proper session cleanup
            with get_db_session_context() as db:
                chat_service = ChatService(db_session=db, message_queue_service=self)

                # Process through ChatService
                await chat_service.process_message(
                    user_phone=batch.user_phone,
                    message_content=batch.concatenated_content,
                    message_type=batch.message_type
                )
            
            # Note: cleanup should happen in wrapper methods (send_message/send_configurable_buttons)
            # If processing completes without calling any send method, cleanup here as fallback
            if current_batch_key and await self.redis_client.exists(current_batch_key):
                logger.warning(
                    f"[BATCH_PROCESS] Batch {batch.batch_id} completed without sending message. "
                    "This might indicate an error in the processing pipeline. "
                    "Cleaning up manually."
                )
                await self._handle_batch_cleanup(batch.batch_id, batch.user_phone, success=True)

        except Exception as e:
            logger.error(
                f"[BATCH_PROCESS] Error processing batch {batch.batch_id}: {e}",
                exc_info=True
            )
            # Cleanup on error
            await self._handle_batch_cleanup(batch.batch_id, batch.user_phone, success=False)
        
        finally:
            # Log batch processing duration with TTL warnings
            duration = time.time() - processing_start
            logger.info(
                f"[BATCH_TIMING] Batch {batch.batch_id} for {batch.user_phone} "
                f"completed in {duration:.2f}s"
            )
            
            # Warning for slow batches (>30s is unusual)
            if duration > 30:
                logger.warning(
                    f"[SLOW_BATCH] Batch {batch.batch_id} took {duration:.2f}s to process. "
                    f"This is unusually slow. User: {batch.user_phone}, "
                    f"message_count: {batch.message_count}"
                )
            
            # Critical alert for very slow batches approaching TTL
            if duration > 45:
                logger.error(
                    f"[CRITICAL_SLOW_BATCH] Batch {batch.batch_id} took {duration:.2f}s. "
                    f"Approaching 60s TTL limit! Investigate immediately. "
                    f"User: {batch.user_phone}. "
                    f"Risk of parallel batch creation if TTL expires!"
                )
            
            # Critical alert if batch exceeded TTL
            if duration > 60:
                logger.critical(
                    f"[TTL_EXCEEDED] Batch {batch.batch_id} took {duration:.2f}s - EXCEEDED 60s TTL! "
                    f"Processing key likely expired during execution. "
                    f"User: {batch.user_phone}. "
                    f"Parallel batches may have been created. Immediate investigation required!"
                )
            
            # Clean up processing start time tracker
            try:
                await self.redis_client.delete(processing_start_key)
            except Exception as cleanup_error:
                logger.warning(f"Failed to cleanup processing_start_key: {cleanup_error}")

    # ========================================
    # WhatsApp Wrapper Methods (with cleanup)
    # ========================================

    def __getattr__(self, name: str):
        """
        Delegate methods to WhatsAppService with automatic wrapping for send methods.
        
        This allows MessageQueueService to act as a transparent proxy for WhatsAppService.
        Any method starting with 'send_' is automatically wrapped with batch cleanup logic.
        Other methods are delegated directly without modification.
        
        Args:
            name: The attribute/method name being accessed
            
        Returns:
            The wrapped method if it's a send method, otherwise the original attribute
        """
        # Get the attribute from WhatsAppService
        try:
            attr = getattr(self.whatsapp_service, name)
        except AttributeError:
            raise AttributeError(
                f"'{self.__class__.__name__}' object has no attribute '{name}' "
                f"and neither does WhatsAppService"
            )
        
        # If it's not callable, just return it
        if not callable(attr):
            return attr
        
        # If it's a method that starts with 'send_', wrap it with batch cleanup
        if name.startswith('send_'):
            async def wrapped_send_method(*args, **kwargs):
                # Get recipient_id - it's always the first positional argument
                recipient_id = args[0] if args else kwargs.get('recipient_id')
                
                if not recipient_id:
                    logger.error(f"{name} called without recipient_id")
                    raise ValueError(f"{name} requires recipient_id as first argument")
                
                # Normalize phone number: remove '+' prefix for Redis key lookup
                # Batch context is stored with user_phone from webhook (without '+')
                normalized_phone = recipient_id.lstrip('+') if isinstance(recipient_id, str) else recipient_id
                
                # Get batch context from Redis
                current_batch_key = self.get_current_batch_key(normalized_phone)
                batch_id = await self.redis_client.get(current_batch_key)
                
                if not batch_id:
                    # Check if a batch was suppressed
                    suppressed_key = self.get_suppressed_key(normalized_phone)
                    suppressed_batch_id = await self.redis_client.get(suppressed_key)
                    
                    if suppressed_batch_id:
                        logger.info(
                            f"Skipping {name} for {recipient_id} - "
                            f"batch {suppressed_batch_id} was suppressed. "
                            f"Returning mock success."
                        )
                        return self._create_mock_result()
                    
                    logger.warning(
                        f"{name} called for {recipient_id} but no batch context found. "
                        "Sending directly without batch cleanup. "
                        "This is expected for messages sent outside the batch processing flow."
                    )
                    # Call original method directly without cleanup
                    return await attr(*args, **kwargs)
                
                # Check if we should suppress this send
                should_suppress = await self._should_suppress_response(normalized_phone)
                
                if should_suppress:
                    logger.info(
                        f"Suppressing {name} for {recipient_id} in batch {batch_id} - "
                        f"newer messages detected. User will receive final response from last batch."
                    )
                    
                    # Set suppression flag with 60s TTL
                    suppressed_key = self.get_suppressed_key(normalized_phone)
                    await self.redis_client.set(suppressed_key, batch_id, ex=60)
                    
                    # Trigger cleanup (without sending)
                    await self._handle_batch_cleanup(batch_id, normalized_phone, success=True)
                    
                    # Return mock success result
                    return self._create_mock_result()
                
                try:
                    # Send via WhatsApp service
                    logger.info(f"Calling {name} for {recipient_id} in batch {batch_id}")
                    result = await attr(*args, **kwargs)
                    
                    # Log result
                    if hasattr(result, 'success'):
                        if result.success:
                            logger.info(
                                f"{name} succeeded for {recipient_id} in batch {batch_id}. "
                                f"Message ID: {getattr(result, 'message_id', 'N/A')}"
                            )
                        else:
                            logger.error(
                                f"{name} failed for {recipient_id} in batch {batch_id}. "
                                f"Error: {getattr(result, 'error', 'Unknown')}"
                            )
                    
                    # Handle batch cleanup (use normalized phone)
                    success = getattr(result, 'success', True)
                    await self._handle_batch_cleanup(batch_id, normalized_phone, success)
                    
                    return result
                    
                except Exception as e:
                    logger.error(f"Error in wrapped {name}: {e}", exc_info=True)
                    # Cleanup even on exception (use normalized phone)
                    await self._handle_batch_cleanup(batch_id, normalized_phone, success=False)
                    raise
            
            return wrapped_send_method
        
        # For non-send methods (like format_*), return the original method as-is
        return attr

    async def _should_suppress_response(self, user_phone: str) -> bool:
        """
        Determine if we should suppress sending the response.
        
        Returns True if:
        - There are messages waiting in the incoming queue, OR
        - There are batches waiting in the outgoing queue
        
        This ensures user only receives final response with full context.
        """
        incoming_key = self.get_incoming_key(user_phone)
        outgoing_key = self.get_outgoing_key(user_phone)
        
        incoming_count = await self.redis_client.zcard(incoming_key)
        outgoing_count = await self.redis_client.llen(outgoing_key)
        
        should_suppress = incoming_count > 0 or outgoing_count > 0
        
        if should_suppress:
            logger.info(
                f"[SUPPRESS_CHECK] User {user_phone} has newer messages. "
                f"incoming_count={incoming_count}, outgoing_count={outgoing_count}. "
                f"Will suppress current response."
            )
        else:
            logger.debug(
                f"[SUPPRESS_CHECK] User {user_phone} has no newer messages. "
                f"Will send current response."
            )
        
        return should_suppress

    def _create_mock_result(self):
        """
        Create a mock successful result for suppressed sends.
        
        This allows ChatService to continue normally without knowing
        that the message was suppressed.
        """
        class MockResult:
            success = True
            message_id = None
        
        return MockResult()

    # ==================
    # Batch Cleanup
    # ==================

    async def _handle_batch_cleanup(self, batch_id: str, user_phone: str, success: bool):
        """
        Handle batch completion and queue management.
        
        This method:
        1. Removes current batch context from Redis
        2. Clears processing markers
        3. Re-queues payloads on failure
        4. Triggers next batch if available
        """
        from redis.lock import LockError
        
        try:
            # Remove batch context
            current_batch_key = self.get_current_batch_key(user_phone)
            await self.redis_client.delete(current_batch_key)
            
            # Clear suppression flag
            suppressed_key = self.get_suppressed_key(user_phone)
            await self.redis_client.delete(suppressed_key)
            
            logger.info(
                f"Cleaning up batch {batch_id} for user {user_phone}. "
                f"Send success: {success}"
            )
            
            # Proceed with queue cleanup
            outgoing_key = self.get_outgoing_key(user_phone)
            processing_key = self.get_processing_key(user_phone)
            batch_lock_key = self.get_batch_lock_key(user_phone)
            processing_payload_key = self.get_processing_payload_key(user_phone)
            payload: Optional[str] = None

            try:
                batch_lock = self.redis_client.lock(batch_lock_key, timeout=10, blocking_timeout=10)
                
                lock_acquire_start = time.time()
                async with batch_lock:
                    lock_acquire_duration = time.time() - lock_acquire_start
                    if lock_acquire_duration > 2:
                        logger.warning(
                            f"[LOCK-SLOW] Cleanup lock acquisition took {lock_acquire_duration:.2f}s "
                            f"for user {user_phone}. Potential contention."
                        )
                    
                    payload = await self.redis_client.get(processing_payload_key)
                    await self.redis_client.delete(processing_key)
                    await self.redis_client.delete(processing_payload_key)
            except LockError:
                logger.error(
                    f"Failed to acquire batch lock for cleanup of {batch_id} for user {user_phone}. "
                    f"Another process is holding the lock. Attempting emergency cleanup..."
                )
                try:
                    await self.redis_client.delete(processing_key)
                    await self.redis_client.delete(processing_payload_key)
                    logger.info(f"Emergency cleanup: Cleared processing metadata for {user_phone}")
                except Exception as emergency_error:
                    logger.error(f"Emergency cleanup also failed: {emergency_error}")
                return

            # Re-queue payload if processing failed
            if not success and payload:
                await self.redis_client.lpush(outgoing_key, payload)
                logger.info(
                    "Re-queued batch %s for user %s after failure",
                    batch_id,
                    user_phone
                )

            # Trigger next batch processing if available
            await self._check_and_start_processing(user_phone)
            
            logger.info(
                f"[CLEANUP] Batch cleanup completed for {batch_id}. "
                f"Checked for next batch to process."
            )
            
            # Clear acknowledgment flag if queues are now empty
            incoming_key = self.get_incoming_key(user_phone)
            outgoing_key = self.get_outgoing_key(user_phone)
            
            incoming_count = await self.redis_client.zcard(incoming_key)
            outgoing_count = await self.redis_client.llen(outgoing_key)
            is_processing = await self.redis_client.exists(processing_key)
            
            # If all queues are empty, clear the acknowledgment flag
            if incoming_count == 0 and outgoing_count == 0 and not is_processing:
                ack_sent_key = self.get_ack_sent_key(user_phone)
                await self.redis_client.delete(ack_sent_key)
                logger.info(
                    f"[CLEANUP] Cleared acknowledgment flag for {user_phone} - "
                    f"all queues are now empty (incoming={incoming_count}, outgoing={outgoing_count}, processing={is_processing})"
                )
        
        except Exception as e:
            logger.error(f"Error in batch cleanup for {batch_id}: {e}", exc_info=True)

    # Remove old send_whatsapp_response method - it's replaced by wrapper methods
    # async def send_whatsapp_response(...):  # DELETE THIS

    # ==================
    # Utility Methods
    # ==================

    async def audit_queue_health(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check on all user queues.
        
        Detects:
        - Stale incoming queues (messages waiting >30s without batch creation)
        - Orphaned processing keys (no corresponding inflight task)
        - Queues exceeding depth thresholds
        
        Triggers recovery for detected issues.
        
        Returns:
            Dict with health status and issues found
        """
        health_report = {
            "timestamp": time.time(),
            "total_users_checked": 0,
            "issues": [],
            "warnings": [],
            "healthy_queues": 0,
            "recoveries_attempted": []
        }
        
        try:
            # Scan for all incoming queues
            incoming_pattern = f"*{self.INCOMING_QUEUE_SUFFIX}"
            incoming_keys = []
            
            cursor = 0
            while True:
                cursor, keys = await self.redis_client.scan(
                    cursor=cursor,
                    match=incoming_pattern,
                    count=100
                )
                incoming_keys.extend(keys)
                if cursor == 0:
                    break
            
            health_report["total_users_checked"] = len(incoming_keys)
            
            for key in incoming_keys:
                user_phone = key.replace(self.INCOMING_QUEUE_SUFFIX, "")
                
                # Check incoming queue depth and age
                incoming_count = await self.redis_client.zcard(key)
                
                if incoming_count > 0:
                    # Get oldest message timestamp
                    oldest = await self.redis_client.zrange(key, 0, 0, withscores=True)
                    if oldest:
                        oldest_timestamp = oldest[0][1]
                        age = time.time() - oldest_timestamp
                        
                        # Critical: Messages waiting >60s - attempt recovery
                        if age > 60:
                            health_report["issues"].append({
                                "user": user_phone,
                                "type": "stale_incoming_queue",
                                "severity": "critical",
                                "message_count": incoming_count,
                                "oldest_age_seconds": age,
                                "description": f"Messages stuck in incoming queue for {age:.0f}s"
                            })
                            logger.error(
                                f"[HEALTH-CRITICAL] Stale incoming queue detected: "
                                f"user={user_phone}, count={incoming_count}, age={age:.0f}s. "
                                f"Attempting recovery..."
                            )
                            
                            # Attempt recovery by forcing batch creation
                            try:
                                recovery_result = await self._recover_stale_queue(user_phone)
                                health_report["recoveries_attempted"].append({
                                    "user": user_phone,
                                    "type": "stale_queue_recovery",
                                    "success": recovery_result.get("success", False),
                                    "details": recovery_result
                                })
                            except Exception as recovery_error:
                                logger.error(
                                    f"[RECOVERY-FAILED] Failed to recover stale queue for {user_phone}: {recovery_error}",
                                    exc_info=True
                                )
                        
                        # Warning: Messages waiting >30s
                        elif age > 30:
                            health_report["warnings"].append({
                                "user": user_phone,
                                "type": "slow_incoming_queue",
                                "severity": "warning",
                                "message_count": incoming_count,
                                "oldest_age_seconds": age,
                                "description": f"Messages in incoming queue for {age:.0f}s"
                            })
                            logger.warning(
                                f"[HEALTH-WARNING] Slow incoming queue: "
                                f"user={user_phone}, count={incoming_count}, age={age:.0f}s"
                            )
                        
                        # Warning: Large queue depth
                        if incoming_count > 10:
                            health_report["warnings"].append({
                                "user": user_phone,
                                "type": "large_queue_depth",
                                "severity": "warning",
                                "message_count": incoming_count,
                                "description": f"Incoming queue has {incoming_count} messages"
                            })
                
                # Check for orphaned processing keys
                processing_key = self.get_processing_key(user_phone)
                is_processing = await self.redis_client.exists(processing_key)
                
                if is_processing:
                    batch_id = await self.redis_client.get(processing_key)
                    # Check if we have an inflight task for this batch
                    if batch_id not in self._inflight_tasks:
                        health_report["issues"].append({
                            "user": user_phone,
                            "type": "orphaned_processing_key",
                            "severity": "critical",
                            "batch_id": batch_id,
                            "description": "Processing key exists but no inflight task found"
                        })
                        logger.error(
                            f"[HEALTH-CRITICAL] Orphaned processing key detected: "
                            f"user={user_phone}, batch_id={batch_id}. Attempting cleanup..."
                        )
                        
                        # Clean up orphaned processing key
                        try:
                            await self._handle_batch_cleanup(batch_id, user_phone, success=False)
                            health_report["recoveries_attempted"].append({
                                "user": user_phone,
                                "type": "orphaned_key_cleanup",
                                "success": True,
                                "batch_id": batch_id
                            })
                        except Exception as cleanup_error:
                            logger.error(
                                f"[RECOVERY-FAILED] Failed to cleanup orphaned key for {user_phone}: {cleanup_error}",
                                exc_info=True
                            )
                
                # If no issues, count as healthy
                if incoming_count == 0 and not is_processing:
                    health_report["healthy_queues"] += 1
            
            # Log summary
            if health_report["issues"]:
                logger.error(
                    f"[HEALTH-AUDIT] Found {len(health_report['issues'])} critical issues, "
                    f"{len(health_report['warnings'])} warnings across {health_report['total_users_checked']} queues. "
                    f"Attempted {len(health_report['recoveries_attempted'])} recoveries."
                )
            elif health_report["warnings"]:
                logger.warning(
                    f"[HEALTH-AUDIT] Found {len(health_report['warnings'])} warnings "
                    f"across {health_report['total_users_checked']} queues"
                )
            else:
                logger.info(
                    f"[HEALTH-AUDIT] All {health_report['healthy_queues']} queues healthy"
                )
            
            return health_report
            
        except Exception as e:
            logger.error(f"[HEALTH-AUDIT] Failed to audit queue health: {e}", exc_info=True)
            health_report["error"] = str(e)
            return health_report

    async def _recover_stale_queue(self, user_phone: str) -> Dict[str, Any]:
        """
        Attempt to recover a stale incoming queue by forcing batch creation.
        
        This is called when messages have been sitting in the incoming queue
        for too long without a timer triggering batch creation (likely due to
        timer key expiry, worker crash, or session abandonment).
        
        Args:
            user_phone: Phone number of user with stale queue
            
        Returns:
            Dict with recovery status
        """
        try:
            logger.info(f"[RECOVERY] Attempting to recover stale queue for {user_phone}")
            
            # Check if already processing - don't interfere
            processing_key = self.get_processing_key(user_phone)
            is_processing = await self.redis_client.exists(processing_key)
            
            if is_processing:
                logger.info(f"[RECOVERY] User {user_phone} is already processing, skipping recovery")
                return {"success": False, "reason": "already_processing"}
            
            # Force batch creation by calling _create_batch directly
            await self._create_batch(user_phone, expected_expiry=None)
            
            logger.info(f"[RECOVERY] Successfully triggered batch creation for {user_phone}")
            return {"success": True, "action": "batch_created"}
            
        except Exception as e:
            logger.error(f"[RECOVERY] Failed to recover stale queue for {user_phone}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def get_queue_status(self, user_phone: str) -> Dict:
        """
        Get current queue status for a user.
        Useful for debugging and monitoring.
        """
        incoming_key = self.get_incoming_key(user_phone)
        outgoing_key = self.get_outgoing_key(user_phone)
        processing_key = self.get_processing_key(user_phone)
        timer_key = self.get_timer_key(user_phone)
        current_batch_key = self.get_current_batch_key(user_phone)

        incoming_count = await self.redis_client.zcard(incoming_key)
        outgoing_count = await self.redis_client.llen(outgoing_key)
        currently_processing = await self.redis_client.get(processing_key)
        timer_started = await self.redis_client.get(timer_key)
        current_batch = await self.redis_client.get(current_batch_key)

        return {
            "user_phone": user_phone,
            "incoming_queue_size": incoming_count,
            "outgoing_queue_size": outgoing_count,
            "currently_processing": currently_processing,
            "current_batch_being_sent": current_batch,
            "timer_active": timer_started is not None,
            "timer_started_at": float(timer_started) if timer_started else None
        }

    async def cleanup_user_queues(self, user_phone: str):
        """
        Clean up all Redis keys for a user.
        Use with caution - for testing/debugging only.
        """
        keys_to_delete = [
            self.get_incoming_key(user_phone),
            self.get_outgoing_key(user_phone),
            self.get_timer_key(user_phone),
            self.get_processing_key(user_phone),
            self.get_current_batch_key(user_phone),
            self.get_processing_payload_key(user_phone),
            self.get_ack_sent_key(user_phone),
            self.get_suppressed_key(user_phone),
        ]
        
        for key in keys_to_delete:
            await self.redis_client.delete(key)

        # Remove pending timer schedule entry
        await self.redis_client.zrem(self.TIMER_SCHEDULE_SET, user_phone)

        logger.info(f"Cleaned up all queues for user {user_phone}")

    async def get_health_metrics(self) -> Dict[str, Any]:
        """
        Get comprehensive health metrics for monitoring and alerting.
        
        Returns metrics on:
        - Active users with queues
        - Processing batches count
        - Stale queues detection
        - System-wide statistics
        
        This can be exposed via API endpoint for external monitoring.
        """
        try:
            metrics = {
                "timestamp": time.time(),
                "system_status": "healthy",
                "active_users": 0,
                "processing_count": 0,
                "pending_incoming": 0,
                "pending_outgoing": 0,
                "stale_queues": 0,
                "issues": []
            }
            
            # Quick health check using audit
            health_report = await self.audit_queue_health()
            
            metrics["active_users"] = health_report.get("total_users_checked", 0)
            metrics["stale_queues"] = len(health_report.get("issues", []))
            metrics["issues"] = health_report.get("issues", [])
            metrics["warnings"] = health_report.get("warnings", [])
            
            # Count processing batches
            processing_pattern = f"*{self.PROCESSING_KEY_SUFFIX}"
            cursor = 0
            processing_count = 0
            while True:
                cursor, keys = await self.redis_client.scan(
                    cursor=cursor,
                    match=processing_pattern,
                    count=100
                )
                processing_count += len(keys)
                if cursor == 0:
                    break
            
            metrics["processing_count"] = processing_count
            
            # Determine overall system status
            if metrics["stale_queues"] > 0:
                metrics["system_status"] = "degraded"
            if metrics["stale_queues"] > 5:
                metrics["system_status"] = "critical"
            
            return metrics
            
        except Exception as e:
            logger.error(f"Failed to get health metrics: {e}", exc_info=True)
            return {
                "timestamp": time.time(),
                "system_status": "error",
                "error": str(e)
            }

