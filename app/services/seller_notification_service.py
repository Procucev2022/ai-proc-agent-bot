"""
Seller Notification Service

Handles sending RFQ notifications to matched sellers via WhatsApp.
Includes workflow state checking to avoid interrupting active seller conversations.
Sellers in active workflows are skipped and will be reconsidered in the next task run.

Workflow Check Logic:
- Query conversation_sessions table for today's session
- SKIP if last_activity_at < 15 minutes ago (seller is active)
- SAFE if no session today OR last_activity_at >= 15 minutes ago
"""

import logging
from typing import Dict, List, Any, Optional, Tuple

from datetime import datetime, timedelta
from sqlalchemy import text

from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.database import get_db_session
from app.config import get_settings

logger = logging.getLogger(__name__)

# Inactivity threshold - sellers inactive for this long are safe to notify
WORKFLOW_INACTIVITY_THRESHOLD_MINUTES = 15


class SellerNotificationService:
    """
    Service for sending RFQ notifications to sellers.

    Features:
    - Workflow state checking via conversation_sessions table
    - Sellers active in last 15 minutes are skipped (will be reconsidered next task run)
    """

    # Button IDs for handling responses
    BUTTON_INTERESTED = "rfq_interested"
    # Intermediate step buttons (shown after clicking "I'm Interested")
    BUTTON_CHECK_DETAILS = "rfq_check_details"
    BUTTON_REQUEST_RFQ = "rfq_request"

    def __init__(self):
        self.whatsapp_service = WhatsAppService()
        self.settings = get_settings()

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

    def _get_rfq_buttons(self, rfq_id: str, seller_id: str = None) -> List[Dict[str, str]]:
        """
        Get button configuration for RFQ notification.

        Args:
            rfq_id: The RFQ ID to include in button IDs
            seller_id: The seller ID to include in button IDs (for authentication flow)

        Returns:
            List of button configurations

        Note:
            Button ID format: {button_type}_{rfq_id}_{seller_id}
            This allows the button handler to identify which seller account
            should be authenticated when the button is clicked.
        """
        # Include seller_id if provided, otherwise use legacy format for backwards compatibility
        if seller_id:
            return [
                {
                    "id": f"{self.BUTTON_INTERESTED}_{rfq_id}_{seller_id}",
                    "title": "I'm Interested"
                }
            ]
        else:
            # Legacy format without seller_id
            return [
                {
                    "id": f"{self.BUTTON_INTERESTED}_{rfq_id}",
                    "title": "I'm Interested"
                }
            ]

    def get_intermediate_rfq_buttons(self, rfq_id: str, seller_id: str) -> List[Dict[str, str]]:
        """
        Get intermediate step button configuration after "I'm Interested" is clicked.

        Shows two options:
        - Check Details: Opens Procucev website to view RFQ details
        - Request RFQ: Proceeds with seller authentication flow

        Args:
            rfq_id: The RFQ ID
            seller_id: The seller ID for authentication flow

        Returns:
            List of button configurations for Check Details and Request RFQ
        """
        return [
            {
                "id": f"{self.BUTTON_CHECK_DETAILS}_{rfq_id}_{seller_id}",
                "title": "Check Details"
            },
            {
                "id": f"{self.BUTTON_REQUEST_RFQ}_{rfq_id}_{seller_id}",
                "title": "Request RFQ"
            }
        ]

    def check_seller_workflow_status(
        self,
        phone_number: str
    ) -> Tuple[bool, Optional[str], Optional[int]]:
        """
        Check if a seller is currently active and should not be interrupted.

        Queries the conversation_sessions table to check if the seller has been
        active in the last 15 minutes. If so, they should not receive notifications.

        Args:
            phone_number: Seller's phone number

        Returns:
            Tuple of (is_active, workflow_type, minutes_since_activity)
            - is_active: True if seller should NOT receive notification now
            - workflow_type: Current workflow type if active, None otherwise
            - minutes_since_activity: Minutes since last activity, None if no session
        """
        db = None
        try:
            db = get_db_session()

            # Query for today's session with activity in last 15 minutes
            query = text("""
                SELECT
                    workflow_type,
                    last_activity_at,
                    TIMESTAMPDIFF(MINUTE, last_activity_at, NOW()) as minutes_inactive
                FROM conversation_sessions
                WHERE external_user_id = :phone_number
                AND DATE(created_at) = CURDATE()
                ORDER BY last_activity_at DESC
                LIMIT 1
            """)

            result = db.execute(query, {'phone_number': phone_number}).fetchone()

            if not result:
                # No session today - safe to notify
                logger.debug(f"[WORKFLOW_CHECK] {phone_number}: No session today - SAFE")
                return (False, None, None)

            workflow_type = result[0]
            last_activity = result[1]
            minutes_inactive = result[2] if result[2] is not None else 0

            # Check if active in last 15 minutes
            if minutes_inactive < WORKFLOW_INACTIVITY_THRESHOLD_MINUTES:
                logger.info(
                    f"[WORKFLOW_CHECK] {phone_number}: Active {minutes_inactive}min ago "
                    f"(workflow: {workflow_type}) - SKIP"
                )
                return (True, workflow_type, minutes_inactive)

            # Inactive for 15+ minutes - safe to notify
            logger.debug(
                f"[WORKFLOW_CHECK] {phone_number}: Inactive {minutes_inactive}min "
                f"(>= {WORKFLOW_INACTIVITY_THRESHOLD_MINUTES}min threshold) - SAFE"
            )
            return (False, workflow_type, minutes_inactive)

        except Exception as e:
            logger.error(f"[WORKFLOW_CHECK] {phone_number}: Error checking status - {e}")
            # On error, default to allowing notification (fail-open)
            return (False, None, None)
        finally:
            if db:
                db.close()

    async def send_rfq_notifications(
        self,
        rfq_data: Dict[str, Any],
        sellers: List[Dict[str, Any]],
        skip_workflow_check: bool = False
    ) -> Dict[str, Any]:
        """
        Send RFQ notifications to a list of sellers with interactive buttons.

        Checks each seller's workflow state before sending to avoid interrupting
        active conversations. Sellers in active workflows are skipped and will
        be reconsidered in the next task run.

        Args:
            rfq_data: RFQ information dictionary
            sellers: List of seller dictionaries with phone_number field
            skip_workflow_check: If True, skip workflow checking (default False)

        Returns:
            Dictionary with success count, failure count, skipped count, and details
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
                "skipped": 0,
                "results": []
            }

        # Format the message once (same for all sellers)
        message_body = self.format_rfq_message(rfq_data)

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
        skipped_count = 0

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
                is_active, workflow_type, minutes_inactive = self.check_seller_workflow_status(
                    phone_number
                )

                if is_active:
                    # Skip seller - they will be reconsidered in the next task run
                    skipped_count += 1
                    results.append({
                        "seller_id": seller_id,
                        "seller_name": seller_name,
                        "phone_number": phone_number,
                        "success": False,
                        "skipped": True,
                        "reason": f"Active {minutes_inactive}min ago",
                        "workflow_type": workflow_type,
                        "minutes_inactive": minutes_inactive
                    })
                    continue

            try:
                # Generate buttons per-seller with seller_id for authentication flow
                buttons = self._get_rfq_buttons(str(rfq_id), str(seller_id))

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
            f"{failed_count} failed, {skipped_count} skipped (in workflow)"
        )

        return {
            "success": failed_count == 0,
            "rfq_id": rfq_id,
            "total": len(sellers),
            "sent": sent_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "results": results
        }


# Singleton instance
_seller_notification_service: Optional[SellerNotificationService] = None


def get_seller_notification_service() -> SellerNotificationService:
    """Get singleton instance of SellerNotificationService."""
    global _seller_notification_service
    if _seller_notification_service is None:
        _seller_notification_service = SellerNotificationService()
    return _seller_notification_service
