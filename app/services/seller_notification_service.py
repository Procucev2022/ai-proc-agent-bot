"""
Seller Notification Service

Handles sending RFQ notifications to matched sellers via WhatsApp.
Includes workflow state checking to avoid interrupting active seller conversations.
Deferred notifications are queued and sent when seller workflows complete/timeout.
"""

import logging
import asyncio
import time
from typing import Dict, List, Any, Optional, Tuple

from datetime import datetime
from redis.asyncio import Redis

from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.services.helpers.session_helpers import SessionHelpers
from app.redis_db import get_session_redis_service, get_deferred_notification_redis_service
from app.config import get_settings

logger = logging.getLogger(__name__)


class SellerNotificationService:
    """
    Service for sending RFQ notifications to sellers.

    Features:
    - Workflow state checking to avoid interrupting active conversations
    - Deferred notification queue for sellers in active workflows
    - Background monitor to process deferred notifications
    """

    # Button IDs for handling responses
    BUTTON_CHECK_DETAILS = "rfq_check_details"
    BUTTON_INTERESTED = "rfq_interested"

    # Workflow types that should NOT be interrupted by notifications
    ACTIVE_WORKFLOW_TYPES = {
        "rfq_creation",
        "authentication",
        "registration",
        "excel_rfq_upload",
        "seller_rfq_view",
    }

    # Deferred notification configuration
    MAX_RETRY_ATTEMPTS = 5
    BASE_RETRY_DELAY = 300  # 5 minutes base delay
    POLL_INTERVAL = 30  # Check queue every 30 seconds

    def __init__(self):
        self.whatsapp_service = WhatsAppService()
        self.settings = get_settings()
        self.session_redis = get_session_redis_service()
        self.notification_redis = get_deferred_notification_redis_service()

        # Background monitor state
        self._monitor_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._redis: Optional[Redis] = None

    def format_rfq_message(self, rfq_data: Dict[str, Any]) -> str:
        """
        Format RFQ data into a WhatsApp message for sellers.

        Args:
            rfq_data: Dictionary containing RFQ information

        Returns:
            Formatted message string
        """
        rfq_id = rfq_data.get('rfq_id', 'N/A')
        categories = rfq_data.get('categories', [])
        description = rfq_data.get('description', '').strip()
        delivery_date = rfq_data.get('delivery_date')
        delivery_location = rfq_data.get('delivery_location', {})

        # Build message with the 5 required fields
        # Note: Header "New RFQ Opportunity" is set separately in send_configurable_buttons
        lines = []

        # 1. RFQ ID
        lines.append(f"*RFQ ID:* {rfq_id}")

        # 2. Category
        if categories:
            categories_str = ", ".join(categories)
            lines.append(f"*Category:* {categories_str}")

        # 3. Delivery Details (date)
        if delivery_date:
            formatted_date = self._format_date(delivery_date)
            lines.append(f"*Delivery Date:* {formatted_date}")

        # 4. Delivery Location
        if delivery_location:
            city = delivery_location.get('city', '')
            state = delivery_location.get('state', '')
            location_str = ", ".join(filter(None, [city, state]))
            if location_str:
                lines.append(f"*Delivery Location:* {location_str}")

        # 5. Project Description
        if description:
            lines.append(f"*Project Description:* {description}")

        return "\n".join(lines)

    def _format_date(self, date_value) -> str:
        """
        Format date value to readable string.

        Args:
            date_value: Date as string, datetime, or None

        Returns:
            Formatted date string
        """
        if not date_value:
            return "N/A"

        try:
            if isinstance(date_value, datetime):
                return date_value.strftime("%d-%b-%Y")
            elif isinstance(date_value, str):
                # Try to parse common formats
                for fmt in ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y"]:
                    try:
                        dt = datetime.strptime(date_value, fmt)
                        return dt.strftime("%d-%b-%Y")
                    except ValueError:
                        continue
                return date_value  # Return as-is if parsing fails
            else:
                return str(date_value)
        except Exception:
            return str(date_value)

    def _get_rfq_buttons(self, rfq_id: str) -> List[Dict[str, str]]:
        """
        Get button configuration for RFQ notification.

        Args:
            rfq_id: The RFQ ID to include in button IDs

        Returns:
            List of button configurations
        """
        return [
            {
                "id": f"{self.BUTTON_CHECK_DETAILS}_{rfq_id}",
                "title": "Check Details"
            },
            {
                "id": f"{self.BUTTON_INTERESTED}_{rfq_id}",
                "title": "I'm Interested"
            }
        ]

    async def check_seller_workflow_status(
        self,
        phone_number: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Check if a seller is currently in an active workflow that should not be interrupted.

        Args:
            phone_number: Seller's phone number

        Returns:
            Tuple of (is_in_active_workflow, workflow_type, workflow_stage)
            - is_in_active_workflow: True if seller should NOT receive notification now
            - workflow_type: Current workflow type if active, None otherwise
            - workflow_stage: Current workflow stage if active, None otherwise
        """
        try:
            # Generate session ID using the same strategy as session management
            session_id = SessionHelpers.generate_session_id(phone_number, "daily")

            # Check Redis for active session
            session_data = await self.session_redis.get_session(session_id)

            if not session_data:
                # No active session - safe to send notification
                logger.debug(f"No active session found for {phone_number}")
                return (False, None, None)

            # Check workflow type
            workflow_type = session_data.get('workflow_type')
            workflow_state = session_data.get('workflow_state', {})
            workflow_stage = workflow_state.get('stage')

            # Check if session has a completed/abandoned outcome
            outcome = session_data.get('outcome')
            if outcome in ['completed', 'abandoned', 'timeout']:
                logger.debug(f"Session for {phone_number} has outcome={outcome}, safe to notify")
                return (False, None, None)

            # Check if workflow type is one that shouldn't be interrupted
            if workflow_type and workflow_type in self.ACTIVE_WORKFLOW_TYPES:
                logger.info(
                    f"Seller {phone_number} is in active workflow: {workflow_type} "
                    f"(stage: {workflow_stage}), notification will be skipped"
                )
                return (True, workflow_type, workflow_stage)

            # Check for pending confirmations that indicate active interaction
            pending_flags = [
                'pending_rfq',
                'pending_combined_rfq',
                'pending_optional_rfq',
                'pending_optional_combined_rfq',
                'pending_role_switch',
                'pending_account_switch',
                'awaiting_attachment_decision',
            ]

            for flag in pending_flags:
                if workflow_state.get(flag):
                    logger.info(
                        f"Seller {phone_number} has pending flag: {flag}, notification will be skipped"
                    )
                    return (True, workflow_type, f"pending:{flag}")

            # No blocking workflow - safe to send notification
            logger.debug(f"Seller {phone_number} has no blocking workflow, safe to notify")
            return (False, workflow_type, workflow_stage)

        except Exception as e:
            logger.error(f"Error checking workflow status for {phone_number}: {e}")
            # On error, default to allowing notification (fail-open)
            return (False, None, None)

    async def send_rfq_notifications(
        self,
        rfq_data: Dict[str, Any],
        sellers: List[Dict[str, Any]],
        skip_workflow_check: bool = False,
        queue_if_busy: bool = True
    ) -> Dict[str, Any]:
        """
        Send RFQ notifications to a list of sellers with interactive buttons.

        Checks each seller's workflow state before sending to avoid interrupting
        active conversations. Sellers in active workflows will be queued for
        deferred delivery (unless queue_if_busy=False).

        Args:
            rfq_data: RFQ information dictionary
            sellers: List of seller dictionaries with phone_number field
            skip_workflow_check: If True, skip workflow checking (default False)
            queue_if_busy: If True, queue notifications for busy sellers (default True)

        Returns:
            Dictionary with success count, failure count, queued count, and details
        """
        rfq_id = rfq_data.get('rfq_id', 'unknown')

        if not sellers:
            logger.warning(f"No sellers to notify for RFQ {rfq_id}")
            return {
                "success": True,
                "rfq_id": rfq_id,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "queued": 0,
                "results": []
            }

        # Format the message once (same for all sellers)
        message_body = self.format_rfq_message(rfq_data)
        buttons = self._get_rfq_buttons(str(rfq_id))

        # Debug: Log RFQ data being sent
        logger.info(f"RFQ notification data - ID: {rfq_id}, Categories: {rfq_data.get('categories')}, "
                    f"Delivery Date: {rfq_data.get('delivery_date')}, "
                    f"Delivery Location: {rfq_data.get('delivery_location')}, "
                    f"Description: {rfq_data.get('description')}")
        logger.info(f"Formatted message body:\n{message_body}")

        logger.info(f"Sending RFQ {rfq_id} notification to {len(sellers)} sellers")

        results = []
        sent_count = 0
        failed_count = 0
        queued_count = 0

        for seller in sellers:
            seller_id = seller.get('seller_id', 'unknown')
            seller_name = seller.get('seller_name', 'Unknown')
            phone_number = seller.get('phone_number')

            if not phone_number:
                logger.warning(f"No phone number for seller {seller_id} ({seller_name})")
                results.append({
                    "seller_id": seller_id,
                    "seller_name": seller_name,
                    "success": False,
                    "error": "No phone number"
                })
                failed_count += 1
                continue

            # Check if seller is in an active workflow (unless skip_workflow_check is True)
            if not skip_workflow_check:
                is_in_workflow, workflow_type, workflow_stage = await self.check_seller_workflow_status(
                    phone_number
                )

                if is_in_workflow:
                    if queue_if_busy:
                        # Queue for deferred delivery
                        queued = await self.queue_deferred_notification(rfq_data, seller)
                        if queued:
                            logger.info(
                                f"Queued RFQ {rfq_id} notification for seller {seller_name} ({phone_number}) - "
                                f"active workflow: {workflow_type} (stage: {workflow_stage})"
                            )
                            queued_count += 1
                            results.append({
                                "seller_id": seller_id,
                                "seller_name": seller_name,
                                "phone_number": phone_number,
                                "success": False,
                                "queued": True,
                                "reason": f"Active workflow: {workflow_type}",
                                "workflow_type": workflow_type,
                                "workflow_stage": workflow_stage
                            })
                        else:
                            logger.error(
                                f"Failed to queue notification for seller {seller_name} ({phone_number})"
                            )
                            failed_count += 1
                            results.append({
                                "seller_id": seller_id,
                                "seller_name": seller_name,
                                "phone_number": phone_number,
                                "success": False,
                                "error": "Failed to queue deferred notification"
                            })
                    else:
                        # Just skip without queuing
                        logger.info(
                            f"Skipping RFQ {rfq_id} notification for seller {seller_name} ({phone_number}) - "
                            f"active workflow: {workflow_type} (stage: {workflow_stage})"
                        )
                        results.append({
                            "seller_id": seller_id,
                            "seller_name": seller_name,
                            "phone_number": phone_number,
                            "success": False,
                            "skipped": True,
                            "reason": f"Active workflow: {workflow_type}",
                            "workflow_type": workflow_type,
                            "workflow_stage": workflow_stage
                        })
                    continue

            try:
                # Send message with interactive buttons
                response: MessageResponse = await self.whatsapp_service.send_configurable_buttons(
                    recipient_id=phone_number,
                    body=message_body,
                    buttons_config=buttons,
                    header="New RFQ Opportunity",
                    footer="Select an option to proceed"
                )

                if response.success:
                    logger.info(f"Successfully sent RFQ {rfq_id} to seller {seller_name} ({phone_number})")
                    sent_count += 1
                    results.append({
                        "seller_id": seller_id,
                        "seller_name": seller_name,
                        "phone_number": phone_number,
                        "success": True,
                        "message_id": response.message_id
                    })
                else:
                    logger.error(f"Failed to send RFQ {rfq_id} to seller {seller_name}: {response.error}")
                    failed_count += 1
                    results.append({
                        "seller_id": seller_id,
                        "seller_name": seller_name,
                        "phone_number": phone_number,
                        "success": False,
                        "error": response.error
                    })

            except Exception as e:
                logger.error(f"Exception sending to seller {seller_id}: {e}")
                failed_count += 1
                results.append({
                    "seller_id": seller_id,
                    "seller_name": seller_name,
                    "success": False,
                    "error": str(e)
                })

        logger.info(
            f"RFQ {rfq_id} notifications complete: {sent_count} sent, "
            f"{failed_count} failed, {queued_count} queued for later"
        )

        return {
            "success": failed_count == 0,
            "rfq_id": rfq_id,
            "total": len(sellers),
            "sent": sent_count,
            "failed": failed_count,
            "queued": queued_count,
            "results": results
        }

    # ==================== Deferred Notification Methods ====================

    async def queue_deferred_notification(
        self,
        rfq_data: Dict[str, Any],
        seller: Dict[str, Any],
        initial_delay: int = None
    ) -> bool:
        """
        Queue a notification for deferred delivery.

        Args:
            rfq_data: RFQ information dictionary
            seller: Seller information dictionary
            initial_delay: Initial delay in seconds before first retry (default: BASE_RETRY_DELAY)

        Returns:
            True if queued successfully
        """
        rfq_id = rfq_data.get("rfq_id", "unknown")
        phone_number = seller.get("phone_number")

        if not phone_number:
            logger.warning("[DEFERRED_NOTIF] Cannot queue - no phone number for seller")
            return False

        notification_id = f"{rfq_id}:{phone_number}"
        delay = initial_delay if initial_delay is not None else self.BASE_RETRY_DELAY
        retry_after = time.time() + delay

        return await self.notification_redis.queue_notification(
            notification_id=notification_id,
            rfq_data=rfq_data,
            seller=seller,
            retry_after=retry_after,
            attempt=1
        )

    # ==================== Background Monitor Methods ====================

    async def _init_redis(self) -> None:
        """Initialize Redis connection for distributed locking."""
        if self._redis is None:
            self._redis = Redis.from_url(self.settings.redis_url, decode_responses=True)

    async def start_monitoring(self) -> None:
        """Start the background monitoring task for deferred notifications."""
        if self._monitor_task and not self._monitor_task.done():
            logger.warning("[DEFERRED_NOTIF] Monitor already running")
            return

        self._is_running = True
        self._monitor_task = asyncio.create_task(self._run_monitor_loop())
        logger.info("[DEFERRED_NOTIF] Monitoring started")

    async def try_start_monitoring_if_available(self) -> bool:
        """
        Try to start monitoring only if no other worker is running it.
        Uses distributed locking for multi-worker optimization.
        """
        await self._init_redis()

        lock_key = "global:deferred_notification_monitor:lock"
        lock = self._redis.lock(lock_key, timeout=5, blocking_timeout=0)

        try:
            acquired = await lock.acquire()
            if not acquired:
                logger.info("[DEFERRED_NOTIF] Monitor already running on another worker, skipping")
                return False

            await lock.release()
            self._is_running = True
            self._monitor_task = asyncio.create_task(self._run_monitor_loop())
            logger.info("[DEFERRED_NOTIF] Started monitoring on this worker")
            return True

        except Exception as e:
            logger.warning(f"[DEFERRED_NOTIF] Error checking monitor availability: {e}")
            # Start anyway on error (fail-open)
            self._is_running = True
            self._monitor_task = asyncio.create_task(self._run_monitor_loop())
            return True

    async def stop_monitoring(self) -> None:
        """Stop the background monitoring task gracefully."""
        self._is_running = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            logger.info("[DEFERRED_NOTIF] Monitoring stopped")

        if self._redis:
            await self._redis.close()
            self._redis = None

    async def _run_monitor_loop(self) -> None:
        """Main monitoring loop - polls queue for ready notifications."""
        logger.info("[DEFERRED_NOTIF] Monitor loop started")
        await self._init_redis()

        try:
            while self._is_running:
                await asyncio.sleep(self.POLL_INTERVAL)

                # Distributed lock - only one worker processes at a time
                lock_key = "global:deferred_notification_monitor:lock"
                lock = self._redis.lock(
                    lock_key,
                    timeout=self.POLL_INTERVAL + 10,
                    blocking_timeout=0  # Non-blocking
                )

                try:
                    acquired = await lock.acquire()
                    if not acquired:
                        continue  # Another worker is processing

                    try:
                        await self._process_ready_notifications()
                    finally:
                        try:
                            await lock.release()
                        except Exception:
                            pass  # Lock may have expired

                except Exception as e:
                    logger.error(f"[DEFERRED_NOTIF] Error in monitor cycle: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("[DEFERRED_NOTIF] Monitor loop cancelled")
            raise

    async def _process_ready_notifications(self) -> None:
        """Process all notifications that are ready to be sent."""
        try:
            # Get notifications ready for retry
            notifications = await self.notification_redis.get_ready_notifications(limit=50)

            if not notifications:
                return

            logger.info(f"[DEFERRED_NOTIF] Processing {len(notifications)} ready notifications")

            for notification in notifications:
                await self._process_single_notification(notification)

        except Exception as e:
            logger.error(f"[DEFERRED_NOTIF] Error processing notifications: {e}")

    async def _process_single_notification(self, notification: Dict[str, Any]) -> None:
        """Process a single deferred notification."""
        notification_id = notification.get("notification_id")
        rfq_data = notification.get("rfq_data", {})
        seller = notification.get("seller", {})
        attempt = notification.get("attempt", 1)

        phone_number = seller.get("phone_number")
        seller_name = seller.get("seller_name", "Unknown")
        rfq_id = rfq_data.get("rfq_id", "unknown")

        if not phone_number:
            logger.warning(f"[DEFERRED_NOTIF] No phone number for notification {notification_id}")
            await self.notification_redis.remove_notification(notification_id)
            return

        try:
            # Check if seller is still in an active workflow
            is_blocked, workflow_type, workflow_stage = await self.check_seller_workflow_status(
                phone_number
            )

            if is_blocked:
                # Still blocked - reschedule with exponential backoff
                if attempt >= self.MAX_RETRY_ATTEMPTS:
                    logger.warning(
                        f"[DEFERRED_NOTIF] Max retries ({self.MAX_RETRY_ATTEMPTS}) reached for "
                        f"{notification_id}, removing from queue"
                    )
                    await self.notification_redis.remove_notification(notification_id)
                    return

                # Calculate next retry time with exponential backoff
                delay = self.BASE_RETRY_DELAY * (2 ** (attempt - 1))  # 5min, 10min, 20min, 40min, 80min
                next_retry = time.time() + delay

                logger.info(
                    f"[DEFERRED_NOTIF] Seller {seller_name} still in workflow {workflow_type}, "
                    f"rescheduling attempt {attempt + 1} in {delay}s"
                )

                await self.notification_redis.update_retry_time(
                    notification_id,
                    next_retry,
                    attempt + 1
                )
                return

            # Seller is free - send the notification
            logger.info(
                f"[DEFERRED_NOTIF] Sending deferred RFQ {rfq_id} notification to "
                f"{seller_name} ({phone_number}) - attempt {attempt}"
            )

            success = await self._send_deferred_notification(rfq_data, seller)

            if success:
                logger.info(
                    f"[DEFERRED_NOTIF] Successfully sent deferred notification {notification_id}"
                )
                await self.notification_redis.remove_notification(notification_id)
            else:
                # Send failed - retry with backoff
                if attempt >= self.MAX_RETRY_ATTEMPTS:
                    logger.error(
                        f"[DEFERRED_NOTIF] Failed to send {notification_id} after "
                        f"{self.MAX_RETRY_ATTEMPTS} attempts, removing"
                    )
                    await self.notification_redis.remove_notification(notification_id)
                else:
                    delay = self.BASE_RETRY_DELAY * (2 ** (attempt - 1))
                    next_retry = time.time() + delay

                    logger.warning(
                        f"[DEFERRED_NOTIF] Send failed for {notification_id}, "
                        f"retrying in {delay}s (attempt {attempt + 1})"
                    )

                    await self.notification_redis.update_retry_time(
                        notification_id,
                        next_retry,
                        attempt + 1
                    )

        except Exception as e:
            logger.error(
                f"[DEFERRED_NOTIF] Error processing notification {notification_id}: {e}",
                exc_info=True
            )

    async def _send_deferred_notification(
        self,
        rfq_data: Dict[str, Any],
        seller: Dict[str, Any]
    ) -> bool:
        """
        Send a deferred RFQ notification to the seller.

        Returns:
            True if sent successfully
        """
        try:
            phone_number = seller.get("phone_number")
            rfq_id = rfq_data.get("rfq_id", "unknown")

            # Format message
            message_body = self.format_rfq_message(rfq_data)
            buttons = self._get_rfq_buttons(str(rfq_id))

            # Send via WhatsApp
            response: MessageResponse = await self.whatsapp_service.send_configurable_buttons(
                recipient_id=phone_number,
                body=message_body,
                buttons_config=buttons,
                header="New RFQ Opportunity",
                footer="Select an option to proceed"
            )

            return response.success

        except Exception as e:
            logger.error(f"[DEFERRED_NOTIF] Error sending notification: {e}")
            return False


# Singleton instance for background monitoring
_seller_notification_service: Optional[SellerNotificationService] = None


def get_seller_notification_service() -> SellerNotificationService:
    """Get singleton instance of SellerNotificationService."""
    global _seller_notification_service
    if _seller_notification_service is None:
        _seller_notification_service = SellerNotificationService()
    return _seller_notification_service
