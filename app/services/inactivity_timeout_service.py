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
        
        # Worker timeout detection configuration
        self.worker_timeout_threshold = settings.worker_timeout_threshold_seconds  # Default: 135 (2m15s)
        self.pending_reply_ttl = settings.pending_reply_ttl_seconds  # Default: 180 (3 min)
        
        # WhatsApp service for notifications
        self.whatsapp_service = WhatsAppService()
        
        # Task handle for lifecycle management
        self._monitor_task = None
        
        logger.debug(
            f"[TIMEOUT_SERVICE] Initialized: timeout={self.timeout_seconds}s, "
            f"poll_interval={self.poll_interval}s, activity_ttl={self.activity_key_ttl}s, enabled={self.enabled}, "
            f"worker_timeout_threshold={self.worker_timeout_threshold}s"
        )

    # ========================================================================
    # Helper Methods
    # ========================================================================

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
        
        # Try to acquire global lock without blocking to Check Ready Stocks
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
            
            logger.debug(f"[WORKER_TIMEOUT] Checking {len(activity_keys)} users for inactivity/worker timeout")
            
            now = time.time()
            worker_timeout_count = 0
            user_inactivity_timeout_count = 0
            
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
                    
                    # PRIORITY 1: Check for WORKER TIMEOUT (2m15s threshold by default)
                    # This takes priority over user inactivity timeout
                    if inactive_duration >= self.worker_timeout_threshold:
                        # Check if pending_reply flag still exists
                        pending_reply_key = f"{user_phone}:pending_reply"
                        pending_reply_exists = await self.redis.exists(pending_reply_key)
                        
                        logger.debug(
                            f"[WORKER_TIMEOUT] Checking {user_phone}: "
                            f"inactive={inactive_duration:.0f}s, threshold={self.worker_timeout_threshold}s, "
                            f"flag={pending_reply_exists}"
                        )
                        
                        if pending_reply_exists:
                            # WORKER TIMEOUT DETECTED: System hasn't replied within timeout window
                            logger.warning(
                                f"[WORKER_TIMEOUT] DETECTED for {user_phone}: "
                                f"{inactive_duration:.0f}s since last activity with pending_reply flag set"
                            )
                            
                            # Get session to check workflow type
                            session_id = SessionHelpers.generate_session_id(user_phone, "daily")
                            
                            # Handle as worker timeout (different message than user inactivity)
                            await self._handle_worker_timeout(user_phone, session_id, activity_key, pending_reply_key)
                            worker_timeout_count += 1
                            continue  # Skip further checks for this user
                        else:
                            logger.debug(
                                f"[WORKER_TIMEOUT] No timeout for {user_phone} (flag cleared)"
                            )
                    
                    # PRIORITY 2: Check for USER INACTIVITY TIMEOUT (5 min threshold by default)
                    # Only checked if worker timeout not detected
                    if inactive_duration >= self.timeout_seconds:
                        # Get session to check workflow type
                        session_id = SessionHelpers.generate_session_id(user_phone, "daily")
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
                        user_inactivity_timeout_count += 1
                
                except Exception as e:
                    logger.error(
                        f"[TIMEOUT_SERVICE] Error checking activity {activity_key}: {e}",
                        exc_info=True
                    )
            
            if worker_timeout_count > 0:
                logger.info(f"[TIMEOUT_SERVICE] Processed {worker_timeout_count} worker timeout(s)")
            
            if user_inactivity_timeout_count > 0:
                logger.info(f"[TIMEOUT_SERVICE] Processed {user_inactivity_timeout_count} user inactivity timeout(s)")
        
        except Exception as e:
            logger.error(f"[TIMEOUT_SERVICE] Error in _check_inactive_users: {e}", exc_info=True)

    async def _handle_worker_timeout(
        self,
        user_phone: str,
        session_id: str,
        activity_key: str,
        pending_reply_key: str
    ) -> None:
        """
        Handle a worker timeout (system failed to reply within worker timeout window).
        
        Similar to user inactivity timeout but with a different message indicating
        a system-side issue rather than user inactivity.
        
        Steps:
        1. Get session from Redis (preserve conversation history)
        2. Set outcome = ConversationOutcome.abandoned (system error)
        2b. Append worker timeout message to conversation history before persist
        3. Persist to database (audit trail)
        4. Clear all message queues
        5. Reset session in Redis (preserves auth)
        6. Clean up activity and pending_reply keys
        7. Send worker timeout notification
        
        Args:
            user_phone: User's phone number
            session_id: Session ID
            activity_key: Redis key for activity tracking
            pending_reply_key: Redis key for pending reply flag
        """
        try:
            logger.debug(f"[WORKER_TIMEOUT] Handling timeout for {user_phone}")
            
            # 1. Get session from Redis (need conversation history for audit trail)
            try:
                session_data = await self.redis_session.get_session(session_id)
                if session_data:
                    logger.debug(f"[WORKER_TIMEOUT] Retrieved session from Redis for {user_phone}")
                else:
                    logger.debug(f"[WORKER_TIMEOUT] No session in Redis for {user_phone}")
            except Exception as redis_error:
                logger.error(f"[WORKER_TIMEOUT] Error retrieving session from Redis: {redis_error}")
                session_data = None
            
            # Worker timeout message is a constant — define it here so it can be appended
            # to the conversation history BEFORE the DB persist.
            worker_timeout_message = (
                "Sorry, your request is taking longer than expected due to high traffic. "
                "Please try sending your message again in some time."
            )
            
            # 2-3. Set outcome, append worker timeout message to conversation history, persist to DB
            if session_data:
                try:
                    from app.database import DatabaseManager
                    from app.models import ConversationOutcome
                    
                    # Mark as abandoned (system-side error)
                    session_data['outcome'] = ConversationOutcome.abandoned.value
                    session_data['completed_at'] = utc_now().isoformat()
                    
                    # Append worker timeout notification to conversation history before persisting
                    conv_history = session_data.get('conversation_history', {})
                    if not isinstance(conv_history, dict):
                        conv_history = {}
                    for _key in ("messages", "openai_messages", "metadata"):
                        if not isinstance(conv_history.get(_key), list):
                            conv_history[_key] = []
                    now_iso = utc_now().isoformat()
                    conv_history["messages"].append({
                        "role": "assistant",
                        "content": worker_timeout_message,
                        "timestamp": now_iso,
                        "sender": "assistant",
                        "type": "text"
                    })
                    conv_history["openai_messages"].append({
                        "role": "assistant",
                        "content": worker_timeout_message
                    })
                    conv_history["metadata"].append({
                        "timestamp": now_iso,
                        "sender": "assistant",
                        "type": "text",
                        "role": "assistant"
                    })
                    session_data['conversation_history'] = conv_history
                    logger.debug(f"[WORKER_TIMEOUT] Appended worker timeout message to conversation history")
                    
                    # Persist to database
                    db_manager = DatabaseManager()
                    try:
                        # Convert dict back to ConversationSession object
                        from app.models import ConversationSession
                        session_obj = ConversationSession(**{
                            k: v for k, v in session_data.items()
                            if k in [
                                'session_id', 'external_user_id', 'workflow_type',
                                'outcome', 'workflow_state', 'conversation_history',
                                'extracted_entities', 'retention_date', 'created_at',
                                'last_activity_at', 'completed_at'
                            ]
                        })
                        
                        db_manager.save_conversation_session(session_obj)
                        logger.debug(f"[WORKER_TIMEOUT] Persisted session to DB (outcome: abandoned)")
                        
                    except Exception as db_error:
                        logger.error(f"[WORKER_TIMEOUT] Error persisting to DB: {db_error}")
                    finally:
                        db_manager.close()
                        
                except Exception as persist_error:
                    logger.error(f"[WORKER_TIMEOUT] Error in persist flow: {persist_error}")
            
            # 4. Clear all message queue keys
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
            logger.debug(f"[WORKER_TIMEOUT] Cleared {deleted_count} queue keys")
            
            # 5. Reset session in Redis (preserves auth, like CancelService)
            if session_data:
                try:
                    # Reset workflow_state but keep auth token
                    # NOTE: workflow_state matches the new-session shape (see inactivity reset above).
                    session_data['workflow_type'] = None
                    session_data['workflow_state'] = {"extracted_entities": [], "last_activity_at": utc_now().isoformat()}
                    session_data['extracted_entities'] = {}
                    session_data['outcome'] = None
                    
                    # Save reset session back to Redis
                    await self.redis_session.save_session(session_data)
                    logger.debug(f"[WORKER_TIMEOUT] Reset session in Redis")
                    
                except Exception as reset_error:
                    logger.error(f"[WORKER_TIMEOUT] Error resetting session: {reset_error}")
            else:
                logger.debug(f"[WORKER_TIMEOUT] No session to reset")
            
            # 6. Clean up activity and pending_reply keys
            try:
                await self.redis.delete(activity_key)
                await self.redis.delete(pending_reply_key)
                logger.debug(f"[WORKER_TIMEOUT] Cleaned up activity and pending_reply keys")
            except Exception as cleanup_error:
                logger.error(f"[WORKER_TIMEOUT] Error cleaning up keys: {cleanup_error}")
            
            # 7. Send worker timeout notification (system-side error message)
            try:
                await self.whatsapp_service.send_message(
                    recipient_id=user_phone,
                    message=worker_timeout_message,
                    skip_concatenation=True
                )
                logger.info(f"[WORKER_TIMEOUT] Sent timeout notification to {user_phone}")
            except Exception as send_error:
                logger.error(f"[WORKER_TIMEOUT] Error sending timeout notification: {send_error}")
        
        except Exception as e:
            logger.error(f"[WORKER_TIMEOUT] Error in _handle_worker_timeout for {user_phone}: {e}", exc_info=True)

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
        2b. Pre-generate timeout message (needed before DB persist)
        3. Set outcome = ConversationOutcome.timeout
        4. Append timeout message to conversation history, persist to database (audit trail)
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
            
            # 2b. Pre-generate timeout message BEFORE DB persist so it can be included in
            #     the persisted conversation history. Uses the original (un-wiped) session_data.
            normalized_phone = user_phone.lstrip('+')
            user_type = None
            timeout_message = None
            user_details = None
            try:
                from app.redis_db import get_auth_redis_service
                auth_redis = get_auth_redis_service()
                user_details = await auth_redis.retrieve(normalized_phone)
                logger.info(f"[TIMEOUT_SERVICE] Retrieved user from auth token: {bool(user_details)} {user_details}")
                if user_details and not isinstance(user_details, list):
                    user_details = [user_details]
                timeout_message = await self._generate_timeout_message(user_details, session_data)
                logger.debug(f"[TIMEOUT_SERVICE] Pre-generated timeout message: {timeout_message[:100]}...")
                # Also capture user_type for logging
                if user_details:
                    first_user = user_details[0] if isinstance(user_details, list) else user_details
                    if first_user:
                        self_client = getattr(first_user, 'self_client', None)
                        user_type = "buyer" if self_client is True else "seller" if self_client is False else None
            except Exception as msg_gen_error:
                logger.warning(f"[TIMEOUT_SERVICE] Failed to pre-generate timeout message: {msg_gen_error}")
            
            # 3-4. Set outcome, append timeout message to conversation history, and persist to database
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
                    
                    # Build conversation history that includes the timeout notification
                    conv_history = session_data.get('conversation_history', {})
                    if not isinstance(conv_history, dict):
                        conv_history = {}
                    for _key in ("messages", "openai_messages", "metadata"):
                        if not isinstance(conv_history.get(_key), list):
                            conv_history[_key] = []
                    if timeout_message:
                        now_iso = utc_now().isoformat()
                        conv_history["messages"].append({
                            "role": "assistant",
                            "content": timeout_message,
                            "timestamp": now_iso,
                            "sender": "assistant",
                            "type": "text"
                        })
                        conv_history["openai_messages"].append({
                            "role": "assistant",
                            "content": timeout_message
                        })
                        conv_history["metadata"].append({
                            "timestamp": now_iso,
                            "sender": "assistant",
                            "type": "text",
                            "role": "assistant"
                        })
                        logger.debug(f"[TIMEOUT_SERVICE] Appended timeout message to conversation history")
                    
                    # Prepare timeout session data (preserves conversation history + timeout msg)
                    timeout_session_data = {
                        'session_id': session_id,
                        'external_user_id': user_phone,
                        'workflow_type': session_data.get('workflow_type'),
                        'outcome': ConversationOutcome.timeout.value,
                        'workflow_state': workflow_state,
                        'conversation_history': conv_history,
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
            if session_data:
                try:
                    from app.utils.datetime_utils import utc_now
                    from app.services.helpers.session_helpers import SessionHelpers
                    
                    # Get current timestamp
                    now_iso = utc_now().isoformat()
                    
                    # Reset workflow data for fresh start (matching CancelService._clear_workflow_state)
                    # NOTE: workflow_state matches the new-session shape from
                    # session_management_service.get_conversation_context so post-timeout intent
                    # classification sees the same truthy workflow_state (with Conversation Stage
                    # signal) as a fresh session.
                    session_data['workflow_state'] = {"extracted_entities": [], "last_activity_at": now_iso}
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
            # Use the message pre-generated in step 2b; fall back to a generic message if generation
            # failed for any reason so the user always receives something.
            if not timeout_message:
                logger.warning(f"[TIMEOUT_SERVICE] No pre-generated message available, using fallback for {user_phone}")
                timeout_message = (
                    "Looks like you're away for a bit. "
                    "Thank you for using QUA AI! "
                    "You can resume anytime by saying 'Hi.'"
                )
            
            logger.debug(f"[TIMEOUT_SERVICE] Sending timeout notification to {user_phone}")
            try:
                await self.whatsapp_service.send_message(
                    user_phone,
                    timeout_message,
                    skip_concatenation=True
                )
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
