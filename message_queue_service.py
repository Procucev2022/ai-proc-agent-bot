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
        """
        # Cancel existing timer task if it exists
        if user_phone in self._active_timers:
            existing_task = self._active_timers[user_phone]
            if not existing_task.done():
                existing_task.cancel()
                try:
                    await existing_task
                except asyncio.CancelledError:
                    pass
            del self._active_timers[user_phone]

        # Update timer timestamp in Redis
        timer_key = self.get_timer_key(user_phone)
        self.redis_client.set(timer_key, time.time(), ex=self.batch_window + 5)

        # Start new timer task
        timer_task = asyncio.create_task(self._timer_countdown(user_phone))
        self._active_timers[user_phone] = timer_task

        logger.debug(f"Timer restarted for user {user_phone}")

    async def _timer_countdown(self, user_phone: str):
        """
        Wait for batch_window seconds, then create a batch.
        """
        try:
            logger.debug(f"Timer countdown started for user {user_phone} ({self.batch_window}s)")
            await asyncio.sleep(self.batch_window)
            
            logger.info(f"Timer expired for user {user_phone}, creating batch")
            await self._create_batch(user_phone)

        except asyncio.CancelledError:
            logger.debug(f"Timer cancelled for user {user_phone}")
            raise
        except Exception as e:
            logger.error(f"Error in timer countdown for user {user_phone}: {e}", exc_info=True)

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
        try:
            # Store batch context in Redis for wrapper methods to access
            current_batch_key = self.get_current_batch_key(batch.user_phone)
            self.redis_client.set(current_batch_key, batch.batch_id, ex=300)  # 5 min expiry
            
            logger.info(
                f"Processing batch {batch.batch_id} for user {batch.user_phone}"
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
            
            # Note: cleanup happens in wrapper methods (send_message/send_configurable_buttons)
            # If processing completes without calling any send method, cleanup here
            if self.redis_client.exists(current_batch_key):
                logger.warning(
                    f"Batch {batch.batch_id} completed without sending message. "
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

    async def send_message(self, recipient_id: str, message: str):
        """
        Send text message via WhatsApp and handle batch cleanup.
        
        This is a wrapper around WhatsAppService.send_message() that adds
        batch queue management logic.
        """
        # Get batch context from Redis
        current_batch_key = self.get_current_batch_key(recipient_id)
        batch_id = self.redis_client.get(current_batch_key)
        
        if not batch_id:
            logger.warning(
                f"send_message called for {recipient_id} but no batch context found. "
                "This might be a direct call outside of batch processing."
            )
            # Fallback: send directly without cleanup
            return await self.whatsapp_service.send_message(recipient_id, message)
        
        try:
            # Send via WhatsApp service
            logger.info(
                f"Sending message to {recipient_id} for batch {batch_id}"
            )
            result = await self.whatsapp_service.send_message(recipient_id, message)
            
            if result.success:
                logger.info(
                    f"Message sent successfully to {recipient_id} "
                    f"for batch {batch_id}. Message ID: {result.message_id}"
                )
            else:
                logger.error(
                    f"Failed to send message to {recipient_id} "
                    f"for batch {batch_id}. Error: {result.error}"
                )
            
            # Handle batch cleanup
            await self._handle_batch_cleanup(batch_id, recipient_id, result.success)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in send_message wrapper: {e}", exc_info=True)
            # Cleanup even on exception
            await self._handle_batch_cleanup(batch_id, recipient_id, success=False)
            raise

    async def send_configurable_buttons(
        self,
        recipient_id: str,
        body: str,
        buttons_config: List[Dict[str, str]],
        header: str = None,
        footer: str = "(Type 'Exit' anytime to end the chat)"
    ):
        """
        Send button message via WhatsApp and handle batch cleanup.
        
        This is a wrapper around WhatsAppService.send_configurable_buttons() that adds
        batch queue management logic.
        """
        # Get batch context from Redis
        current_batch_key = self.get_current_batch_key(recipient_id)
        batch_id = self.redis_client.get(current_batch_key)
        
        if not batch_id:
            logger.warning(
                f"send_configurable_buttons called for {recipient_id} but no batch context found. "
                "This might be a direct call outside of batch processing."
            )
            # Fallback: send directly without cleanup
            return await self.whatsapp_service.send_configurable_buttons(
                recipient_id, body, buttons_config, header, footer
            )
        
        try:
            # Send via WhatsApp service
            logger.info(
                f"Sending configurable buttons to {recipient_id} for batch {batch_id}"
            )
            result = await self.whatsapp_service.send_configurable_buttons(
                recipient_id, body, buttons_config, header, footer
            )
            
            if result.success:
                logger.info(
                    f"Buttons sent successfully to {recipient_id} "
                    f"for batch {batch_id}. Message ID: {result.message_id}"
                )
            else:
                logger.error(
                    f"Failed to send buttons to {recipient_id} "
                    f"for batch {batch_id}. Error: {result.error}"
                )
            
            # Handle batch cleanup
            await self._handle_batch_cleanup(batch_id, recipient_id, result.success)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in send_configurable_buttons wrapper: {e}", exc_info=True)
            # Cleanup even on exception
            await self._handle_batch_cleanup(batch_id, recipient_id, success=False)
            raise

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

            with self.redis_client.lock(batch_lock_key, timeout=10, blocking_timeout=10):
                # Remove the processed batch from outgoing queue
                batch_json = self.redis_client.lpop(outgoing_key)
                
                if batch_json:
                    try:
                        processed_batch = json.loads(batch_json)
                        if processed_batch.get('batch_id') == batch_id:
                            logger.info(f"Batch {batch_id} removed from outgoing queue")
                        else:
                            logger.warning(
                                f"Batch ID mismatch: expected {batch_id}, "
                                f"got {processed_batch.get('batch_id')}"
                            )
                    except Exception as e:
                        logger.error(f"Error parsing batch during removal: {e}")

                # Clear processing marker
                self.redis_client.delete(processing_key)

                # Check for next batch
                next_batch_json = self.redis_client.lindex(outgoing_key, 0)
                
                if next_batch_json:
                    # Process next batch
                    logger.info(f"Next batch found for user {user_phone}, starting processing")
                    await self._check_and_start_processing(user_phone)
                else:
                    # No more batches, check if incoming queue has messages
                    incoming_key = self.get_incoming_key(user_phone)
                    incoming_count = self.redis_client.zcard(incoming_key)
                    
                    if incoming_count > 0:
                        logger.info(
                            f"Outgoing queue empty but incoming has {incoming_count} messages. "
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