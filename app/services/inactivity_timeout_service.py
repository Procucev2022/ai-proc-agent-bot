"""
Inactivity Timeout Service - monitors user activity and cancels stale workflows.

Runs as independent background service, polls Redis activity keys every 30 seconds,
and cancels workflows that have been inactive for 5+ minutes.

This service operates independently from message processing:
- Activity tracking via public method: update_user_activity()
- Timeout detection runs in background with distributed locking
- Clean separation: no coupling to chat/queue/session services
- Singleton pattern: shared instance across webhook and main.py
"""

import logging
import asyncio
import time
from typing import Optional

from redis.asyncio import Redis

from app.config import get_settings
from app.redis_db import get_session_redis_service
from app.models import WorkflowType, ConversationSession , User
from app.services.whatsapp_service import WhatsAppService
from app.services.helpers.session_helpers import SessionHelpers
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

# Global singleton instance
_timeout_service_instance: Optional['InactivityTimeoutService'] = None


def get_timeout_service() -> 'InactivityTimeoutService':
    """
    Get singleton instance of InactivityTimeoutService.
    
    Returns:
        Shared InactivityTimeoutService instance
    """
    global _timeout_service_instance
    if _timeout_service_instance is None:
        _timeout_service_instance = InactivityTimeoutService()
    return _timeout_service_instance


class InactivityTimeoutService:
    """
    Background service to monitor user activity and cancel inactive workflows.
    
    Polls Redis activity keys every 30 seconds (configurable), checks for 5-minute 
    inactivity (configurable), and performs cleanup for timed-out sessions.
    
    Architecture:
    - Independent service (no coupling to webhook/queue/chat)
    - Distributed locking (single worker monitors at a time)
    - Activity tracking via Redis keys updated by webhook.py
    - Comprehensive cleanup: queues + session + DB persistence
    - Direct Redis client (not using BaseRedisService wrapper)
    """

    def __init__(self):
        settings = get_settings()
        
        # Create direct Redis client (following message_queue_service pattern)
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self.redis_session = get_session_redis_service()
        
        # Configuration
        self.timeout_seconds = settings.workflow_timeout_seconds  # Default: 300 (5 min)
        self.poll_interval = settings.timeout_poll_interval_seconds  # Default: 30
        self.activity_key_ttl = settings.activity_key_ttl_seconds  # Default: 420 (7 min)
        self.enabled = settings.workflow_timeout_enabled  # Default: True
        self.worker_timeout_threshold = settings.worker_timeout_threshold_seconds  # Default: 135 (2m15s)
        
        # WhatsApp service for notifications
        self.whatsapp_service = WhatsAppService()
        
        # Task handle for lifecycle management
        self._monitor_task = None
        
        logger.debug(
            f"[TIMEOUT_SERVICE] Initialized: timeout={self.timeout_seconds}s, "
            f"poll_interval={self.poll_interval}s, activity_ttl={self.activity_key_ttl}s, "
            f"worker_timeout_threshold={self.worker_timeout_threshold}s, enabled={self.enabled}"
        )

    # ========================================================================
    # Helper Methods
    # ========================================================================

    async def _get_last_message_sender(self, session_id: str) -> Optional[str]:
        """
        Get the role of the last message sender from conversation history.
        
        Handles edge cases:
        - Session doesn't exist
        - conversation_history is None or not a dict
        - messages array is empty
        - message doesn't have 'role' field
        
        Args:
            session_id: Redis session ID
            
        Returns:
            'user' if last message from user
            'assistant' if last message from system/assistant
            None if cannot determine (empty history or errors)
        """
        try:
            # Get session from Redis
            session_data = await self.redis_session.get_session(session_id)
            
            if not session_data:
                logger.debug(f"[LAST_SENDER] No session data found for {session_id}")
                return None
            
            # Get conversation_history
            conversation_history = session_data.get('conversation_history')
            
            if not conversation_history or not isinstance(conversation_history, dict):
                logger.debug(f"[LAST_SENDER] No valid conversation_history for {session_id}")
                return None
            
            # Get messages array
            messages = conversation_history.get('messages', [])
            
            if not messages:
                logger.debug(f"[LAST_SENDER] Empty messages array for {session_id}")
                return None
            
            # Get last message
            last_message = messages[-1]
            last_sender = last_message.get('role')
            
            if not last_sender:
                logger.debug(f"[LAST_SENDER] No 'role' field in last message for {session_id}")
                return None
            
            return last_sender
            
        except Exception as e:
            logger.error(f"[LAST_SENDER] Error getting last sender for {session_id}: {e}", exc_info=True)
            return None

    async def _handle_worker_timeout(self, user_phone: str, session_id: str, inactive_duration: float) -> None:
        """
        Handle worker timeout by notifying user and cleaning up session.
        
        This is triggered when:
        - User sent a message (last_sender == 'user')
        - Worker processing has exceeded 135s (120s worker timeout + 15s buffer)
        - User inactivity timeout (300s) not yet reached
        
        Args:
            user_phone: User's phone number
            session_id: Redis session ID
            inactive_duration: How long user has been inactive (seconds)
        """
        try:
            logger.info(
                f"[WORKER_TIMEOUT] Handling worker timeout for {user_phone} "
                f"(inactive for {inactive_duration:.1f}s)"
            )
            
            # Get session data
            session_data = await self.redis_session.get_session(session_id)
            
            if not session_data:
                logger.warning(f"[WORKER_TIMEOUT] No session found for {user_phone}, cannot handle timeout")
                return
            
            # Convert session data to ConversationSession object
            # Use the same conversion method as inactivity timeout handling
            from app.services.session_management_service import SessionManagementService
            from app.services.chat_summary_service import ChatSummaryService
            from app.services.daily_summary_service import DailySummaryService
            from app.database import DatabaseManager
            
            db_manager = DatabaseManager()
            chat_summary_service = ChatSummaryService(db_manager)
            daily_summary_service = DailySummaryService(db_manager)
            
            session_manager = SessionManagementService(
                db_manager, self.whatsapp_service, chat_summary_service, daily_summary_service
            )
            
            session = session_manager._dict_to_session(session_data)
            
            # Send timeout notification message
            timeout_message = (
                "Your request has timed out due to high server load. "
                "Please try again. If this persists, please contact support.\\n\\n"
                "Your session has been cleared."
            )
            
            try:
                await self.whatsapp_service.send_message(user_phone, timeout_message)
                logger.info(f"[WORKER_TIMEOUT] Sent timeout notification to {user_phone}")
            except Exception as send_error:
                logger.error(f"[WORKER_TIMEOUT] Failed to send timeout message to {user_phone}: {send_error}")
            
            # Perform same cleanup as user inactivity timeout
            # Mark session as timed out and persist to database
            from app.models import ConversationOutcome
            session.outcome = ConversationOutcome.timeout
            
            # Save to database for audit trail
            await session_manager.save_session(session, session.workflow_type, persist_to_db=True)
            logger.info(f"[WORKER_TIMEOUT] Persisted timed-out session to database for {user_phone}")
            
            # Clear Redis session (same as inactivity timeout cleanup)
            await self.redis_session.delete_session(session_id)
            logger.info(f"[WORKER_TIMEOUT] Cleared Redis session for {user_phone}")
            
            # Clear activity key to prevent repeated timeout handling
            activity_key = f"{user_phone}:last_activity"
            await self.redis.delete(activity_key)
            logger.info(f"[WORKER_TIMEOUT] Cleared activity key for {user_phone}")
            
            # Clear incoming/outgoing queues
            incoming_key = f"{user_phone}:incoming_queue"
            outgoing_key = f"{user_phone}:outgoing_queue"
            await self.redis.delete(incoming_key)
            await self.redis.delete(outgoing_key)
            logger.info(f"[WORKER_TIMEOUT] Cleared message queues for {user_phone}")
            
            logger.info(
                f"[WORKER_TIMEOUT] Successfully handled worker timeout for {user_phone} "
                f"after {inactive_duration:.1f}s of inactivity"
            )
            
        except Exception as e:
            logger.error(
                f"[WORKER_TIMEOUT] Error handling worker timeout for {user_phone}: {e}",
                exc_info=True
            )

    async def _generate_timeout_message(self, user_details: User, session_data: ConversationSession) -> str:
        """
        Generate user-type-specific timeout message.
        
        Args:
            user_details: User details from cache service
            session_data: Session data containing workflow_state
            
        Returns:
            Appropriate timeout message based on user type
        """
        
        try:
            user_type = None
            first_user = None

            if user_details:
                if isinstance(user_details, list):
                    first_user = user_details[0]
                else:
                    first_user = user_details

                if first_user:
                    if hasattr(first_user, 'self_client'):
                        self_client = first_user.self_client
                    elif hasattr(first_user, 'get'):
                        self_client = first_user.get('self_client') or first_user.get('selfClient')
                    else:
                        self_client = None
                    
                    user_type = "buyer" if self_client is True else "seller" if self_client is False else None
                    logger.info(f"[TIMEOUT_MESSAGE] user_type: {user_type}")
            else:
                logger.debug(f"[TIMEOUT_MESSAGE] User details invalid or missing")
            
            if user_type == "buyer":
                return (
                    "Looks like you're away for a bit. "
                    "Thank you for using QUA AI! "
                    "You can resume creating RFQs or checking status anytime by saying 'Hi.'"
                )
            elif user_type == "seller":
                logger.info("user is seller")
                logger.debug(f"[TIMEOUT_MESSAGE] Generating seller timeout message")
                
                # For sellers, try to get remainder message from seller service
                if user_details and session_data:
                    try:
                        from app.services.seller_service import SellerService
                        seller_service = SellerService()
                        # Create temporary objects from raw data for seller service

                        # Create user object with required fields
                        class UserObj:
                            def __init__(self, org_id, phone_number):
                                self.org_id = org_id
                                self.phone_number = phone_number
                        
                        org_id = getattr(first_user, 'org_id', None) or (first_user.get('org_id') if hasattr(first_user, 'get') else None)
                        phone = getattr(first_user, 'phone_number', None) or (first_user.get('phone_number') if hasattr(first_user, 'get') else None)
                        user = UserObj(org_id, phone)
                        logger.info(f"user in inactibity is:{user}")
                        session_obj = ConversationSession(**session_data)
                        remainder_result = await seller_service.handle_seller_flow_completion(user, session_obj)
                        logger.info(f"[TIMEOUT_MESSAGE] Seller remainder result: {remainder_result}")

                        base_msg = (
                            "Thank you for using QUA AI! "
                            "You can resume viewing RFQs or managing bids anytime by saying 'Hi.'"
                        )
                        
                        if remainder_result.get("success") and remainder_result.get("message"):
                            return f"{remainder_result['message']}"
                        else:
                            logger.info(f"[TIMEOUT_MESSAGE] Using base seller message")
                            return base_msg
                    except Exception as seller_error:
                        logger.warning(f"[TIMEOUT_MESSAGE] Seller service error: {seller_error}")
                        return (
                            "Looks like you're away for a bit. "
                            "Thank you for using QUA AI! "
                            "You can resume viewing RFQs or managing bids anytime by saying 'Hi.'"
                        )
                else:
                    logger.debug(f"[TIMEOUT_MESSAGE] Using fallback seller message (no user/session data)")
                    return (
                        "Looks like you're away for a bit. "
                        "Thank you for using QUA AI! "
                        "You can resume viewing RFQs or managing bids anytime by saying 'Hi.'"
                    )
          
            else:
                # Default/generic message for unspecified or other user types
                logger.debug(f"[TIMEOUT_MESSAGE] Using default timeout message for user_type: {user_type}")
                return (
                    "Looks like you're away for a bit. "
                    "Thank you for using QUA AI! "
                    "You can resume anytime by saying 'Hi.'"
                )
        except Exception as e:
            logger.error(f"[TIMEOUT_MESSAGE] Error generating timeout message: {e}")
            logger.error(f"[TIMEOUT_MESSAGE] Error type: {type(e)}")
            import traceback
            logger.error(f"[TIMEOUT_MESSAGE] Full traceback: {traceback.format_exc()}")
            logger.debug(f"[TIMEOUT_MESSAGE] Using fallback timeout message")
            return (
                "Looks like you're away for a bit. "
                "Thank you for using QUA AI! "
                "You can resume viewing RFQs or managing bids anytime by saying 'Hi.'"
                )

    # ========================================================================
    # Public API - Activity Tracking
    # ========================================================================

    async def update_user_activity(self, user_phone: str) -> None:
        """
        Update user's last activity timestamp for timeout tracking.
        
        Public API called from webhook to track user activity.
        Uses non-blocking lock with force-update fallback to ensure timestamp
        is always updated (prevents premature timeout).
        
        Architecture:
        - Redis key: {user_phone}:last_activity (7-min TTL for auto-cleanup)
        - Non-blocking lock (0 timeout) - doesn't block request processing
        - Force update on lock failure - critical to prevent premature timeout
        
        Args:
            user_phone: User's phone number (will be normalized)
        """
        # Normalize phone number (remove +)
        normalized_phone = user_phone.lstrip('+')
        activity_key = f"{normalized_phone}:last_activity"
        lock_key = f"{normalized_phone}:activity_lock"
        
        try:
            # Try non-blocking lock
            lock = self.redis.lock(lock_key, timeout=1, blocking_timeout=0)
            acquired = await lock.acquire()
            
            if acquired:
                try:
                    # Got lock - update with fresh timestamp
                    timestamp = time.time()
                    # Use configured TTL (buffer beyond timeout, auto-cleanup)
                    await self.redis.setex(activity_key, self.activity_key_ttl, timestamp)
                    logger.debug(f"[ACTIVITY] Updated for {normalized_phone} (locked)")
                finally:
                    await lock.release()
            else:
                # Lock busy - FORCE update anyway to prevent premature timeout
                timestamp = time.time()
                await self.redis.setex(activity_key, self.activity_key_ttl, timestamp)
                logger.debug(f"[ACTIVITY] Force-updated for {normalized_phone} (lock busy)")
        
        except Exception as e:
            # Error with lock - FORCE update anyway (critical to prevent premature timeout)
            logger.warning(f"[ACTIVITY] Lock error for {normalized_phone}, forcing update: {e}")
            try:
                timestamp = time.time()
                await self.redis.setex(activity_key, self.activity_key_ttl, timestamp)
            except Exception as update_error:
                logger.error(f"[ACTIVITY] Failed to update activity for {normalized_phone}: {update_error}")

    # ========================================================================
    # Background Monitoring
    # ========================================================================

    async def start_monitoring(self) -> None:
        """Start the background monitoring task."""
        if not self.enabled:
            logger.debug("[TIMEOUT_SERVICE] Timeout monitoring disabled in config")
            return
        
        if self._monitor_task and not self._monitor_task.done():
            logger.warning("[TIMEOUT_SERVICE] Monitor already running")
            return
        
        self._monitor_task = asyncio.create_task(self._run_monitor_loop())
        logger.debug("[TIMEOUT_SERVICE] Monitoring started")
    
    async def try_start_monitoring_if_available(self) -> bool:
        """
        Try to start monitoring only if no other worker is running it.
        
        Multi-worker optimization: Checks if another worker is already running
        the monitor loop before starting. This prevents multiple workers from
        running idle monitor loops.
        
        Returns:
            True if monitoring started on this worker, False if another worker has it.
        
        Note: This is an optimization. If the check fails, the existing distributed
        lock in _run_monitor_loop() will still prevent duplicate execution.
        """
        if not self.enabled:
            logger.debug("[TIMEOUT_SERVICE] Timeout monitoring disabled in config")
            return False
        
        if self._monitor_task and not self._monitor_task.done():
            logger.warning("[TIMEOUT_SERVICE] Monitor already running on this worker")
            return True
        
        # Try to acquire global lock without blocking to check availability
        lock_key = "global:timeout_monitor:lock"
        lock = self.redis.lock(lock_key, timeout=5, blocking_timeout=0)
        
        try:
            acquired = await lock.acquire()
            if not acquired:
                # Another worker already running monitor
                logger.debug("[TIMEOUT_SERVICE] Monitor already running on another worker, skipping startup")
                return False
            
            # We got the lock - we should run the monitor
            await lock.release()  # Release immediately, monitor loop will re-acquire
            
            # Start monitoring on this worker
            self._monitor_task = asyncio.create_task(self._run_monitor_loop())
            logger.debug("[TIMEOUT_SERVICE] Monitoring started on this worker")
            return True
            
        except Exception as e:
            logger.warning(
                f"[TIMEOUT_SERVICE] Error checking monitor availability: {e}. "
                f"Starting anyway (distributed lock will prevent duplication)"
            )
            # Fallback: start anyway, distributed lock in monitor loop will handle it
            self._monitor_task = asyncio.create_task(self._run_monitor_loop())
            return True

    async def stop_monitoring(self) -> None:
        """Stop the background monitoring task gracefully."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            logger.debug("[TIMEOUT_SERVICE] Monitoring stopped")

    async def _run_monitor_loop(self) -> None:
        """
        Main monitoring loop - polls Redis for inactive users.
        Runs with distributed lock to ensure single worker execution.
        """
        logger.debug("[TIMEOUT_SERVICE] Monitor loop started")
        
        try:
            while True:
                await asyncio.sleep(self.poll_interval)
                
                # Global lock - only one worker monitors at a time
                lock_key = "global:timeout_monitor:lock"
                lock = self.redis.lock(
                    lock_key, 
                    timeout=self.poll_interval + 5,  # Slightly longer than poll interval
                    blocking_timeout=0
                )
                
                try:
                    acquired = await lock.acquire()
                    if not acquired:
                        # Another worker is monitoring
                        continue
                    
                    try:
                        await self._check_inactive_users()
                    finally:
                        await lock.release()
                
                except Exception as e:
                    logger.error(f"[TIMEOUT_SERVICE] Error in monitor cycle: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.debug("[TIMEOUT_SERVICE] Monitor loop cancelled")
            raise
        except Exception as e:
            logger.error(f"[TIMEOUT_SERVICE] Monitor loop failed: {e}", exc_info=True)

    async def _check_inactive_users(self) -> None:
        """
        Scan all activity keys and check for timeouts.
        This is the core polling logic.
        """
        # Early exit if feature is disabled
        if not self.enabled:
            logger.debug("[TIMEOUT_SERVICE] Feature disabled, skipping check")
            return
        
        try:
            # Scan for all activity tracking keys
            cursor = 0
            activity_keys = []
            
            while True:
                cursor, keys = await self.redis.scan(
                    cursor=cursor,
                    match="*:last_activity",
                    count=100
                )
                activity_keys.extend(keys)
                if cursor == 0:
                    break
            
            if not activity_keys:
                logger.debug("[TIMEOUT_SERVICE] No activity keys found")
                return
            
            logger.debug(f"[TIMEOUT_SERVICE] Checking {len(activity_keys)} users for inactivity")
            
            now = time.time()
            timeout_count = 0
            
            for activity_key in activity_keys:
                try:
                    # Extract user phone from key
                    user_phone = activity_key.replace(":last_activity", "")
                    
                    # Get last activity timestamp
                    last_activity = await self.redis.get(activity_key)
                    if not last_activity:
                        continue
                    
                    last_activity_timestamp = float(last_activity)
                    inactive_duration = now - last_activity_timestamp
                    
                    # Get session to check workflow type
                    session_id = SessionHelpers.generate_session_id(user_phone, "daily")
                    
                    # WORKER TIMEOUT DETECTION:
                    # Detect when worker times out (120s) before user inactivity timeout (300s)
                    # Check last message sender to determine if user is waiting for a response
                    # Worker timeout threshold is 135s (120s worker timeout + 15s buffer)
                    if self.worker_timeout_threshold <= inactive_duration < self.timeout_seconds:
                        last_sender = await self._get_last_message_sender(session_id)
                        if last_sender == 'user':
                            logger.warning(
                                f"[WORKER_TIMEOUT] Detected worker timeout for {user_phone}: "
                                f"inactive_duration={inactive_duration:.1f}s, last_sender={last_sender}"
                            )
                            await self._handle_worker_timeout(user_phone, session_id, inactive_duration)
                            continue  # Skip to next user - timeout already handled
                    
                    # Check if timed out
                    if inactive_duration >= self.timeout_seconds:
                        session_data = await self.redis_session.get_session(session_id)
                        
                        if not session_data:
                            # Session not in Redis - check database for recently completed sessions
                            logger.debug(f"[TIMEOUT_SERVICE] Session not in Redis for {user_phone}, checking database")
                            
                            try:
                                from app.database import DatabaseManager
                                from datetime import timedelta
                                
                                db_manager = DatabaseManager()
                                try:
                                    db_session = db_manager.get_conversation_session(session_id)
                                    
                                    if not db_session:
                                        # No session in DB either - clean up stale activity key
                                        await self.redis.delete(activity_key)
                                        logger.debug(f"[TIMEOUT_SERVICE] Cleaned stale activity key for {user_phone} (no session in DB)")
                                        continue
                                    
                                    # Check if session was recently completed (within activity key TTL)
                                    # Only send timeout message for sessions completed within the activity key lifetime
                                    if db_session.completed_at:
                                        # Convert naive datetime from DB to timezone-aware UTC
                                        from app.utils.datetime_utils import utc_from_naive
                                        completed_at_utc = utc_from_naive(db_session.completed_at)
                                        time_since_completion = utc_now() - completed_at_utc
                                        
                                        # Use activity_key_ttl as the recency threshold
                                        if time_since_completion > timedelta(seconds=self.activity_key_ttl):
                                            # Too old - activity key should have expired naturally
                                            await self.redis.delete(activity_key)
                                            logger.debug(
                                                f"[TIMEOUT_SERVICE] Session completed too long ago ({time_since_completion}), "
                                                f"cleaning up activity key (TTL threshold: {self.activity_key_ttl}s)"
                                            )
                                            continue
                                    
                                    # Convert DB session to dict format for _handle_timeout
                                    from app.services.workflow_manager import WorkflowManager
                                    workflow_type_enum = WorkflowManager.get_workflow_type(db_session)
                                    
                                    session_data = {
                                        'session_id': db_session.session_id,
                                        'external_user_id': db_session.external_user_id,
                                        'workflow_type': workflow_type_enum.value if workflow_type_enum else None,
                                        'outcome': db_session.outcome.value if db_session.outcome and hasattr(db_session.outcome, 'value') else db_session.outcome,
                                        'workflow_state': db_session.workflow_state or {},
                                        'conversation_history': db_session.conversation_history or {},
                                        'extracted_entities': db_session.extracted_entities or {},
                                        'retention_date': db_session.retention_date.isoformat() if db_session.retention_date else None,
                                        'created_at': db_session.created_at.isoformat() if db_session.created_at else None,
                                        'last_activity_at': db_session.last_activity_at.isoformat() if db_session.last_activity_at else None,
                                        'completed_at': db_session.completed_at.isoformat() if db_session.completed_at else None,
                                    }
                                    
                                    logger.debug(
                                        f"[TIMEOUT_SERVICE] Retrieved session from DB for {user_phone} "
                                        f"(outcome: {session_data.get('outcome')}, completed_at: {session_data.get('completed_at')})"
                                    )
                                    
                                finally:
                                    db_manager.close()
                                    
                            except Exception as db_error:
                                logger.error(f"[TIMEOUT_SERVICE] Error retrieving session from DB for {user_phone}: {db_error}")
                                # Clean up activity key and skip
                                await self.redis.delete(activity_key)
                                continue
                        
                        workflow_type = session_data.get('workflow_type')
                        outcome = session_data.get('outcome')

                        # Skip if session already has a completed outcome (exit, abandoned, etc.)
                        if outcome and outcome not in ['None', None]:
                            logger.debug(
                                f"[TIMEOUT_SERVICE] Skipping {user_phone}: session already completed "
                                f"(outcome: {outcome}, inactive {inactive_duration:.0f}s)"
                            )
                            # Clean up orphaned activity key
                            await self.redis.delete(activity_key)
                            continue

                        # Timeout ALL workflows if workflow_type is set (per requirement #4)
                        # Skip only if workflow_type is None or 'None' (no active workflow)
                        if not workflow_type or workflow_type == 'None':
                            # No active workflow - skip timeout but log for debugging
                            logger.debug(
                                f"[TIMEOUT_SERVICE] Skipping {user_phone}: no workflow "
                                f"(inactive {inactive_duration:.0f}s)"
                            )
                            continue
                        
                        logger.debug(
                            f"[TIMEOUT_SERVICE] Timeout detected for {user_phone}: "
                            f"{inactive_duration:.0f}s inactive (workflow: {workflow_type})"
                        )
                        
                        # Cancel workflow due to timeout
                        await self._handle_timeout(user_phone, session_id, activity_key)
                        timeout_count += 1
                
                except Exception as e:
                    logger.error(
                        f"[TIMEOUT_SERVICE] Error checking activity {activity_key}: {e}",
                        exc_info=True
                    )
            
            if timeout_count > 0:
                logger.debug(f"[TIMEOUT_SERVICE] Processed {timeout_count} timeouts in this cycle")
        
        except Exception as e:
            logger.error(f"[TIMEOUT_SERVICE] Error in _check_inactive_users: {e}", exc_info=True)

    async def _handle_timeout(
        self,
        user_phone: str,
        session_id: str,
        activity_key: str
    ) -> None:
        """
        Handle a timed-out workflow following cancel pattern.
        
        Steps:
        1. Get session from Redis (preserve conversation history)
        2. Double-check activity (race condition protection)
        3. Set outcome = ConversationOutcome.timeout
        4. Persist to database (audit trail)
        5. Clear all message queues
        6. Reset session in Redis (preserves auth, like CancelService)
        7. Clean up activity key
        8. Send timeout notification LAST (after cleanup complete)
        """
        try:
            logger.debug(f"[TIMEOUT_SERVICE] Handling timeout for {user_phone}")
            
            # Initialize variables to avoid UnboundLocalError
            session_data = None
            remainder_session = None
            
            # 1. Get session from Redis (need conversation history for audit trail)
            try:
                session_data = await self.redis_session.get_session(session_id)
                if session_data:
                    remainder_session = session_data  # Save for seller remainder message
                    logger.debug(f"[TIMEOUT_SERVICE] Retrieved session from Redis for {user_phone}")
                else:
                    logger.warning(f"[TIMEOUT_SERVICE] No session found in Redis for {session_id}")
            except Exception as redis_error:
                logger.error(f"[TIMEOUT_SERVICE] Failed to retrieve session from Redis: {redis_error}")
            
            # 2. Double-check activity timestamp (race condition protection)
            # User might have sent a message while timeout was being processed
            latest_activity = await self.redis.get(activity_key)
            if latest_activity:
                try:
                    inactive_duration = time.time() - float(latest_activity)
                    if inactive_duration < self.timeout_seconds:
                        logger.debug(
                            f"[TIMEOUT_SERVICE] User {user_phone} became active "
                            f"(inactive only {inactive_duration:.0f}s), aborting timeout"
                        )
                        return
                except Exception as e:
                    logger.warning(f"[TIMEOUT_SERVICE] Error checking latest activity: {e}")
            
            # 3-4. Set outcome and persist to database (if session exists)
            if session_data:
                try:
                    from app.models import ConversationOutcome
                    from app.database import DatabaseManager
                    from app.utils.datetime_utils import utc_now
                    
                    # Add timeout metadata to workflow_state
                    workflow_state = session_data.get('workflow_state', {})
                    if not isinstance(workflow_state, dict):
                        workflow_state = {}
                    
                    workflow_state["timeout_completed"] = True
                    workflow_state["timeout_timestamp"] = utc_now().isoformat()
                    
                    # Prepare timeout session data (preserves conversation history)
                    timeout_session_data = {
                        'session_id': session_id,
                        'external_user_id': user_phone,
                        'workflow_type': session_data.get('workflow_type'),
                        'outcome': ConversationOutcome.timeout.value,
                        'workflow_state': workflow_state,
                        'conversation_history': session_data.get('conversation_history', {}),
                        'extracted_entities': session_data.get('extracted_entities', {}),
                        'retention_date': session_data.get('retention_date'),
                        'completed_at': utc_now().isoformat(),
                        'last_activity_at': utc_now().isoformat()
                    }
                    
                    # Persist to database (audit trail - follows exit_service pattern)
                    db_manager = DatabaseManager()
                    try:
                        db_manager.append_session_data(timeout_session_data)
                        logger.debug(f"[TIMEOUT_SERVICE] Persisted timeout session to DB for {user_phone}")
                    except Exception as db_error:
                        logger.error(f"[TIMEOUT_SERVICE] Failed to persist to DB for {user_phone}: {db_error}")
                    finally:
                        db_manager.close()
                    
                except Exception as persist_error:
                    logger.error(f"[TIMEOUT_SERVICE] Error preparing/persisting timeout data: {persist_error}")
            
            # 5. Clear all message queue keys (if user has any)
            normalized_phone = user_phone.lstrip('+')
            queue_keys = [
                f"{normalized_phone}:incoming",
                f"{normalized_phone}:outgoing",
                f"{normalized_phone}:processing",
                f"{normalized_phone}:session",
                f"{normalized_phone}:batch_trigger",
                f"{normalized_phone}:response_ready",
                f"{normalized_phone}:ack_sent",
            ]
            
            deleted_count = await self.redis.delete(*queue_keys)
            logger.debug(f"[TIMEOUT_SERVICE] Cleared {deleted_count} queue keys for {user_phone}")
            
            # 6. Reset session in Redis instead of deleting (preserves auth, like CancelService)
            
            # Extract user_type BEFORE resetting workflow_state (needed for timeout message)
            user_type = None
            
            if session_data:
                workflow_state = session_data.get('workflow_state', {})
                if isinstance(workflow_state, dict):
                    user_type = workflow_state.get('user_type')
            
            if session_data:
                try:
                    from app.utils.datetime_utils import utc_now
                    from app.services.helpers.session_helpers import SessionHelpers

                    # Save Session object for building remiander msg
                    remainder_session = session_data
                    
                    # Get current timestamp
                    now_iso = utc_now().isoformat()
                    
                    # Reset workflow data for fresh start (matching CancelService._clear_workflow_state)
                    session_data['workflow_state'] = {}
                    session_data['extracted_entities'] = {}
                    session_data['conversation_history'] = {"messages": [], "metadata": [], "openai_messages": []}
                    session_data['workflow_type'] = None
                    session_data['outcome'] = None
                    session_data['completed_at'] = None
                    
                    # Update top-level last_activity_at (matching save_session pattern)
                    session_data['last_activity_at'] = now_iso
                    
                    # Preserve important fields that should NOT be reset:
                    # - session_id (already in session_data)
                    # - external_user_id (already in session_data)
                    # - whatsapp_context (preserves auth and other context)
                    # - retention_date (preserves data retention policy)
                    # - created_at (preserves session creation time)
                    
                    # Clean for JSON serialization (matching save_session → _session_to_dict pattern)
                    clean_session_data = SessionHelpers.clean_for_json_serialization(session_data)
                    
                    # Save reset session back to Redis (preserves auth state)
                    await self.redis_session.store_session(session_id, clean_session_data)
                    logger.debug(f"[TIMEOUT_SERVICE] Reset session in Redis for {user_phone} (preserved auth)")
                    
                    # Clear meaningful message cache (user is starting fresh)
                    from app.services.user_cache_service import get_user_cache_service
                    user_cache_service = get_user_cache_service()
                    meaningful_cleared = await user_cache_service.clear_meaningful_message(user_phone)
                    logger.debug(f"[TIMEOUT_SERVICE] Meaningful message cleared: {meaningful_cleared}")
                    
                except Exception as reset_error:
                    logger.error(f"[TIMEOUT_SERVICE] Failed to reset Redis session for {user_phone}: {reset_error}")
            else:
                logger.warning(f"[TIMEOUT_SERVICE] No session data to reset for {user_phone}")
            
            # 7. Clean up activity tracking key
            try:
                await self.redis.delete(activity_key)
                logger.debug(f"[TIMEOUT_SERVICE] Cleaned up activity key for {user_phone}")
            except Exception as cleanup_error:
                logger.error(f"[TIMEOUT_SERVICE] Failed to clean activity key for {user_phone}: {cleanup_error}")
            
            # 8. Send timeout notification LAST (after all cleanup complete)
            # Generate user-type-specific timeout message
            logger.debug(f"[TIMEOUT_SERVICE] Generating timeout message for {user_phone}")
            
            # Get user details from auth token
            from app.redis_db import get_auth_redis_service
            auth_redis = get_auth_redis_service()
            normalized_phone = user_phone.lstrip('+')
            user_details = await auth_redis.retrieve(normalized_phone)
            logger.info(f"[TIMEOUT_SERVICE] Retrieved user from auth token: {bool(user_details)} {user_details}")
            
            # Convert to list format for _generate_timeout_message compatibility
            if user_details and not isinstance(user_details, list):
                user_details = [user_details]

            # Always prefer original session snapshot for constructing reminder
            session_snapshot = remainder_session or session_data
            timeout_session_data = session_snapshot
            logger.debug(f"[TIMEOUT_SERVICE] Using session snapshot: {bool(timeout_session_data)}")
            
            timeout_message = await self._generate_timeout_message(user_details, timeout_session_data)
            logger.debug(f"[TIMEOUT_SERVICE] Generated timeout message: {timeout_message[:100]}...")
            
            try:
                logger.debug(f"[TIMEOUT_SERVICE] Sending timeout notification to {user_phone}")
                await self.whatsapp_service.send_message(user_phone, timeout_message)
                logger.debug(
                    f"[TIMEOUT_SERVICE] Successfully sent timeout notification to {user_phone} "
                    f"(user_type={user_type})"
                )
            except Exception as msg_error:
                logger.error(f"[TIMEOUT_SERVICE] Failed to send notification to {user_phone}: {msg_error}")
                logger.error(f"[TIMEOUT_SERVICE] Error type: {type(msg_error)}")
                import traceback
                logger.error(f"[TIMEOUT_SERVICE] Full traceback: {traceback.format_exc()}")
                # Continue - cleanup is complete, notification failure is non-critical
            
            logger.debug(f"[TIMEOUT_SERVICE] Timeout handling completed for {user_phone}")
        
        except Exception as e:
            logger.error(
                f"[TIMEOUT_SERVICE] Error handling timeout for {user_phone}: {e}",
                exc_info=True
            )
