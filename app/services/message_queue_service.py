"""
Message Queueing Service for messages received from webhook.

This service handles queueing, and batch processing of user messages.
It queues incoming messages, sorts them into batches, and processes batches sequentially to ensure
that messages are sent in the correct order and without overwhelming the recipient.
"""

import logging
import dataclasses
import asyncio
import time
import json
from typing import Dict, List, Optional

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
                    content = webhook_data.get("image", {}).get("caption", "[Image]")
                elif message_type == "document":
                    content = ""
                else:
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
            await self.redis_client.zadd(
                incoming_key,
                {json.dumps(message.to_dict()): timestamp}
            )

            # Check if we should send acknowledgment (for 2nd+ messages when system is busy)
            should_send_ack = await self._should_send_acknowledgment(user_phone)
            if should_send_ack:
                # Fire-and-forget: don't await to avoid blocking enqueue
                asyncio.create_task(self._send_acknowledgment(user_phone))

            # Ensure global timer scheduler is running
            await self._ensure_scheduler_task()

            # Manage timer with lock
            timer_lock_key = self.get_timer_lock_key(user_phone)
            timer_lock = self.redis_client.lock(timer_lock_key, timeout=5, blocking_timeout=5)
            async with timer_lock:
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
                    logger.debug(f"Acknowledgment already sent to {user_phone}, skipping")
                    return
                
                # Set flag BEFORE sending (optimistic approach)
                # If send fails, worst case is user doesn't get ack unnecessarily
                # Better than sending duplicate acknowledgments
                await self.redis_client.set(ack_sent_key, "1", ex=300)
                
                # Add '+' prefix for WhatsApp API format
                recipient_id = f"+{user_phone}" if not user_phone.startswith('+') else user_phone
                
                # Send directly via WhatsAppService (not through wrapper)
                await self.whatsapp_service.send_message(
                    recipient_id=recipient_id,
                    message="Your message has been received. You can send more messages, they will be processed."
                )
                
                logger.info(f"Sent processing acknowledgment to {user_phone}")
        
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
        1. Acquire batch lock
        2. Get all messages from incoming queue (sorted by timestamp)
        3. Clear incoming queue
        4. Concatenate message contents
        5. Generate batch_id
        6. Create Batch object
        7. Add to outgoing queue
        8. Check if we should start processing
        9. Release batch lock
        """
        batch_lock_key = self.get_batch_lock_key(user_phone)
        batch_lock = self.redis_client.lock(batch_lock_key, timeout=10, blocking_timeout=10)

        async with batch_lock:
            incoming_key = self.get_incoming_key(user_phone)

            # Get all messages (sorted by timestamp)
            message_data_list = await self.redis_client.zrange(incoming_key, 0, -1)

            if not message_data_list:
                logger.info(f"No messages in incoming queue for user {user_phone}")
                return

            # Parse messages
            messages: List[Message] = []
            for msg_json in message_data_list:
                try:
                    msg_dict = json.loads(msg_json)
                    messages.append(Message.from_dict(msg_dict))
                except Exception as e:
                    logger.error(f"Error parsing message during batch creation: {e}")
                    continue

            if not messages:
                logger.warning(
                    "Incoming queue for user %s contained only unparsable messages; skipping batch",
                    user_phone,
                )
                await self.redis_client.delete(incoming_key)
                return

            # Clear incoming queue
            await self.redis_client.delete(incoming_key)

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
                f"Batch {batch_id} created with {len(messages)} messages for user {user_phone}"
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
            else:
                await self.redis_client.delete(timer_key)

        # Check if we should start processing (outside the lock)
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
            logger.debug(
                f"User {user_phone} already processing batch {currently_processing}"
            )
            return

        # Claim first batch atomically
        batch_json = await self.redis_client.lpop(outgoing_key)

        if not batch_json:
            logger.debug(f"No batches in outgoing queue for user {user_phone}")
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
        await self.redis_client.set(processing_key, batch.batch_id, ex=300)  # 5 min expiry

        # Persist payload for safe retries if needed
        processing_payload_key = self.get_processing_payload_key(user_phone)
        await self.redis_client.set(processing_payload_key, batch_json, ex=300)

        logger.info(f"Starting processing for batch {batch.batch_id}")

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
        try:
            # Store batch context in Redis for wrapper methods to access
            current_batch_key = self.get_current_batch_key(batch.user_phone)
            await self.redis_client.set(current_batch_key, batch.batch_id, ex=300)  # 5 min expiry
            
            logger.info(
                f"Processing batch {batch.batch_id} for user {batch.user_phone}. "
                f"Content: {batch.concatenated_content[:100]}..."  # Log first 100 chars
            )
            
            # Import ChatService here to avoid circular import
            from app.services.chat_service import ChatService
            chat_service = ChatService(message_queue_service=self)
            
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
                    f"Batch {batch.batch_id} completed without sending message. "
                    "This might indicate an error in the processing pipeline. "
                    "Cleaning up manually."
                )
                await self._handle_batch_cleanup(batch.batch_id, batch.user_phone, success=True)

        except Exception as e:
            logger.error(
                f"Error processing batch {batch.batch_id}: {e}",
                exc_info=True
            )
            # Cleanup on error
            await self._handle_batch_cleanup(batch.batch_id, batch.user_phone, success=False)

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
        
        return incoming_count > 0 or outgoing_count > 0

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
                async with batch_lock:
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
                logger.debug(f"Cleared acknowledgment flag for {user_phone} - queues are now empty")
        
        except Exception as e:
            logger.error(f"Error in batch cleanup for {batch_id}: {e}", exc_info=True)

    # Remove old send_whatsapp_response method - it's replaced by wrapper methods
    # async def send_whatsapp_response(...):  # DELETE THIS

    # ==================
    # Utility Methods
    # ==================

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
