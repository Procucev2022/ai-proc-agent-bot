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
import random
from redis import Redis
from typing import Dict, List

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

    def __init__(self):
        
        # Get settings from config
        settings = get_settings()

        # Instantiate Redis client
        self.redis_client = Redis.from_url(settings.redis_url, decode_responses=True)

        # Configuration
        self.batch_window = settings.batch_window_seconds
        
        # Track active timer tasks per user (in-memory, for this worker)
        self._active_timers: Dict[str, asyncio.Task] = {}

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

    def get_timer_lock_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.TIMER_LOCK_SUFFIX}"

    def get_batch_lock_key(self, user_phone: str) -> str:
        return f"{user_phone}{self.BATCH_LOCK_SUFFIX}"

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
            self.redis_client.zadd(
                incoming_key,
                {json.dumps(message.to_dict()): timestamp}
            )

            # Manage timer with lock
            timer_lock_key = self.get_timer_lock_key(user_phone)
            with self.redis_client.lock(timer_lock_key, timeout=5, blocking_timeout=5):
                await self._restart_timer(user_phone)

            logger.info(f"Message {message_id} enqueued successfully for user {user_phone}")

        except Exception as e:
            logger.error(f"Error enqueueing message: {e}", exc_info=True)
            raise

    async def _restart_timer(self, user_phone: str):
        """
        Cancel existing timer and start a new one.
        Must be called within timer lock.
        
        In multi-worker deployments, only cancels local timer.
        Redis timer key coordinates across workers.
        """
        # Cancel LOCAL timer task if it exists in this worker
        if user_phone in self._active_timers:
            existing_task = self._active_timers[user_phone]
            if not existing_task.done():
                existing_task.cancel()
                try:
                    await existing_task
                except asyncio.CancelledError:
                    pass
            del self._active_timers[user_phone]

        # Update timer timestamp in Redis - this is the source of truth
        timer_key = self.get_timer_key(user_phone)
        timer_expiry_time = time.time() + self.batch_window
        
        # Store when the timer should expire (not when it started)
        self.redis_client.set(
            timer_key, 
            timer_expiry_time, 
            ex=self.batch_window + 5  # Extra 5s buffer
        )

        # Start new timer task in THIS worker
        timer_task = asyncio.create_task(self._timer_countdown(user_phone, timer_expiry_time))
        self._active_timers[user_phone] = timer_task

        logger.debug(f"Timer restarted for user {user_phone}, expires at {timer_expiry_time}")

    async def _timer_countdown(self, user_phone: str, expected_expiry_time: float):
        """
        Wait for batch_window seconds, then create a batch.
        
        Checks Redis before creating batch to avoid duplicates in multi-worker setup.
        """
        try:
            logger.debug(f"Timer countdown started for user {user_phone} ({self.batch_window}s)")
            await asyncio.sleep(self.batch_window)
            
            # CRITICAL: Check if timer is still valid before creating batch
            timer_key = self.get_timer_key(user_phone)
            current_timer_value = self.redis_client.get(timer_key)
            
            if not current_timer_value:
                # Timer was cancelled (key deleted)
                logger.debug(f"Timer for {user_phone} was cancelled, not creating batch")
                return
            
            # Check if this is still the same timer
            current_expiry_time = float(current_timer_value)
            if abs(current_expiry_time - expected_expiry_time) > 1:  # Allow 1s tolerance
                # Timer was restarted by another message/worker
                logger.debug(
                    f"Timer for {user_phone} was restarted "
                    f"(expected {expected_expiry_time}, current {current_expiry_time}), "
                    "not creating batch"
                )
                return
            
            # Timer is still valid - try to create batch
            logger.info(f"Timer expired for user {user_phone}, creating batch")
            
            # Use a Redis lock to ensure only ONE worker creates the batch
            batch_creation_lock_key = f"{user_phone}:lock:batch_creation"
            try:
                with self.redis_client.lock(
                    batch_creation_lock_key, 
                    timeout=10, 
                    blocking_timeout=0.1  # Don't wait, return immediately if locked
                ):
                    # Double-check timer is still valid inside lock
                    current_timer_value = self.redis_client.get(timer_key)
                    if current_timer_value and abs(float(current_timer_value) - expected_expiry_time) <= 1:
                        await self._create_batch(user_phone)
                        # Delete timer key after successful batch creation
                        self.redis_client.delete(timer_key)
                    else:
                        logger.debug(f"Timer changed while acquiring lock for {user_phone}")
            except Exception as lock_error:
                # Couldn't acquire lock - another worker is creating batch
                logger.debug(
                    f"Couldn't acquire batch creation lock for {user_phone}, "
                    "another worker is likely creating the batch"
                )
                return

        except asyncio.CancelledError:
            logger.debug(f"Timer cancelled for user {user_phone}")
            raise
        except Exception as e:
            logger.error(f"Error in timer countdown for user {user_phone}: {e}", exc_info=True)
        finally:
            # Clean up local timer reference
            if user_phone in self._active_timers:
                del self._active_timers[user_phone]

    # ================
    # Batch Creation
    # ================

    async def _create_batch(self, user_phone: str):
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
        
        with self.redis_client.lock(batch_lock_key, timeout=10, blocking_timeout=10):
            incoming_key = self.get_incoming_key(user_phone)
            
            # Get all messages (sorted by timestamp)
            message_data_list = self.redis_client.zrange(incoming_key, 0, -1)
            
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
                    logger.error(f"Error parsing message: {e}")
                    continue

            if not messages:
                return

            # Clear incoming queue
            self.redis_client.delete(incoming_key)

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

            # Add to outgoing queue
            outgoing_key = self.get_outgoing_key(user_phone)
            self.redis_client.rpush(outgoing_key, json.dumps(batch.to_dict()))

            logger.info(
                f"Batch {batch_id} created with {len(messages)} messages for user {user_phone}"
            )

            # Check if we should start processing
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
        currently_processing = self.redis_client.get(processing_key)
        
        if currently_processing:
            logger.debug(
                f"User {user_phone} already processing batch {currently_processing}"
            )
            return

        # Get first batch from outgoing queue
        batch_json = self.redis_client.lindex(outgoing_key, 0)
        
        if not batch_json:
            logger.debug(f"No batches in outgoing queue for user {user_phone}")
            return

        # Parse batch
        try:
            batch_dict = json.loads(batch_json)
            batch = Batch.from_dict(batch_dict)
        except Exception as e:
            logger.error(f"Error parsing batch: {e}")
            return

        # Mark as processing
        self.redis_client.set(processing_key, batch.batch_id, ex=300)  # 5 min expiry

        logger.info(f"Starting processing for batch {batch.batch_id}")

        # Start processing (non-blocking)
        asyncio.create_task(self._process_batch(batch))

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
            self.redis_client.set(current_batch_key, batch.batch_id, ex=300)  # 5 min expiry
            
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
            if current_batch_key and self.redis_client.exists(current_batch_key):
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
                
                # Get batch context from Redis
                current_batch_key = self.get_current_batch_key(recipient_id)
                batch_id = self.redis_client.get(current_batch_key)
                
                if not batch_id:
                    logger.warning(
                        f"{name} called for {recipient_id} but no batch context found. "
                        "Sending directly without batch cleanup. "
                        "This is expected for messages sent outside the batch processing flow."
                    )
                    # Call original method directly without cleanup
                    return await attr(*args, **kwargs)
                
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
                    
                    # Handle batch cleanup
                    success = getattr(result, 'success', True)
                    await self._handle_batch_cleanup(batch_id, recipient_id, success)
                    
                    return result
                    
                except Exception as e:
                    logger.error(f"Error in wrapped {name}: {e}", exc_info=True)
                    # Cleanup even on exception
                    await self._handle_batch_cleanup(batch_id, recipient_id, success=False)
                    raise
            
            return wrapped_send_method
        
        # For non-send methods (like format_*), return the original method as-is
        return attr

    # ==================
    # Batch Cleanup
    # ==================

    async def _handle_batch_cleanup(self, batch_id: str, user_phone: str, success: bool):
        """
        Handle batch completion and queue management.
        
        This method:
        1. Removes current batch context from Redis
        2. Removes processed batch from outgoing queue
        3. Clears processing marker
        4. Checks for next batch to process
        5. Starts timer if incoming queue has messages
        """
        from redis.lock import LockError
        
        try:
            # Remove batch context
            current_batch_key = self.get_current_batch_key(user_phone)
            self.redis_client.delete(current_batch_key)
            
            logger.info(
                f"Cleaning up batch {batch_id} for user {user_phone}. "
                f"Send success: {success}"
            )
            
            # Proceed with queue cleanup
            outgoing_key = self.get_outgoing_key(user_phone)
            processing_key = self.get_processing_key(user_phone)
            batch_lock_key = self.get_batch_lock_key(user_phone)

            # Track whether we need to restart timer (check inside lock)
            has_incoming_messages = False
            next_batch_exists = False

            try:
                with self.redis_client.lock(batch_lock_key, timeout=10, blocking_timeout=10):
                    # CRITICAL FIX: Peek at batch BEFORE removing to validate ID
                    batch_json = self.redis_client.lindex(outgoing_key, 0)
                    
                    if batch_json:
                        try:
                            processed_batch = json.loads(batch_json)
                            expected_batch_id = processed_batch.get('batch_id')
                            
                            if expected_batch_id == batch_id:
                                # IDs match - safe to remove
                                self.redis_client.lpop(outgoing_key)
                                logger.info(f"Batch {batch_id} removed from outgoing queue")
                            else:
                                # CRITICAL: IDs don't match - DO NOT REMOVE
                                logger.error(
                                    f"CRITICAL: Batch ID mismatch in cleanup! "
                                    f"Expected to cleanup {batch_id}, but found {expected_batch_id} at front of queue. "
                                    f"NOT removing batch to prevent data loss. "
                                    f"This indicates a race condition or duplicate cleanup attempt."
                                )
                                # Don't proceed with cleanup to avoid corrupting queue
                                # The correct cleanup will happen when the right batch completes
                                return
                        except Exception as e:
                            logger.error(f"Error parsing batch during validation: {e}")
                            # Don't remove if we can't validate
                            return
                    else:
                        logger.warning(f"No batch found in outgoing queue during cleanup of {batch_id}")

                    # Clear processing marker only after successful batch removal
                    self.redis_client.delete(processing_key)

                    # Check for next batch
                    next_batch_json = self.redis_client.lindex(outgoing_key, 0)
                    
                    if next_batch_json:
                        next_batch_exists = True
                    else:
                        # No more batches, check if incoming queue has messages
                        incoming_key = self.get_incoming_key(user_phone)
                        incoming_count = self.redis_client.zcard(incoming_key)
                        has_incoming_messages = incoming_count > 0
            
            except LockError:
                logger.error(
                    f"Failed to acquire batch lock for cleanup of {batch_id} for user {user_phone}. "
                    f"Another process is holding the lock. Attempting emergency cleanup..."
                )
                # Emergency cleanup: at least clear the processing marker so new batches can process
                try:
                    processing_key = self.get_processing_key(user_phone)
                    self.redis_client.delete(processing_key)
                    logger.info(f"Emergency cleanup: Cleared processing marker for {user_phone}")
                except Exception as emergency_error:
                    logger.error(f"Emergency cleanup also failed: {emergency_error}")
                return            # CRITICAL FIX: Release batch lock BEFORE acquiring timer lock to prevent deadlock
            
            if next_batch_exists:
                # Process next batch
                logger.info(f"Next batch found for user {user_phone}, starting processing")
                await self._check_and_start_processing(user_phone)
            elif has_incoming_messages:
                # Restart timer for incoming messages (lock acquisition happens here, outside batch lock)
                logger.info(
                    f"Outgoing queue empty but incoming has messages. "
                    f"Starting timer for user {user_phone}"
                )
                timer_lock_key = self.get_timer_lock_key(user_phone)
                with self.redis_client.lock(timer_lock_key, timeout=5, blocking_timeout=5):
                    await self._restart_timer(user_phone)
            else:
                logger.info(f"All queues empty for user {user_phone}. Flow complete.")
        
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

        incoming_count = self.redis_client.zcard(incoming_key)
        outgoing_count = self.redis_client.llen(outgoing_key)
        currently_processing = self.redis_client.get(processing_key)
        timer_started = self.redis_client.get(timer_key)
        current_batch = self.redis_client.get(current_batch_key)

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
        ]
        
        for key in keys_to_delete:
            self.redis_client.delete(key)
        
        # Cancel active timer if exists
        if user_phone in self._active_timers:
            task = self._active_timers[user_phone]
            if not task.done():
                task.cancel()
            del self._active_timers[user_phone]

        logger.info(f"Cleaned up all queues for user {user_phone}")