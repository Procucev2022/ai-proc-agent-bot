"""
Seller Notification Service

Handles sending notifications to sellers via WhatsApp:
1. RFQ notifications - When buyers create RFQs matching seller categories
2. BFS bid notifications - When buyers place bids on seller's stock items

Includes workflow state checking to avoid interrupting active seller conversations.
Sellers in active workflows are skipped and will be reconsidered in the next task run.

Workflow Check Logic:
- Query conversation_sessions table for today's session
- SKIP if last_activity_at < 15 minutes ago (seller is active)
- SAFE if no session today OR last_activity_at >= 15 minutes ago
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Any, Optional, Tuple

from datetime import datetime, timedelta
from sqlalchemy import text

from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.database import get_db_session
from app.config import get_settings

logger = logging.getLogger(__name__)

# Inactivity threshold - sellers inactive for this long are safe to notify
WORKFLOW_INACTIVITY_THRESHOLD_MINUTES = 15

# ==================== Dedicated Notification Logger ====================
# Creates a separate log file for notification events for easier tracking

def _setup_notification_logger() -> logging.Logger:
    """
    Set up a dedicated logger for notification events.
    Writes to logs/notifications_YYYY-MM-DD.log with rotation.
    """
    notification_logger = logging.getLogger("notification_events")

    # Avoid adding duplicate handlers
    if notification_logger.handlers:
        return notification_logger

    notification_logger.setLevel(logging.INFO)
    notification_logger.propagate = False  # Don't propagate to root logger

    # Create logs directory if it doesn't exist
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)

    # Date-based log file
    log_file = os.path.join(log_dir, f"notifications_{datetime.now().strftime('%Y-%m-%d')}.log")

    # File handler with rotation (10MB max, keep 30 backups)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,
        backupCount=30,
        encoding='utf-8'
    )

    # Human-readable format
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    notification_logger.addHandler(file_handler)

    return notification_logger

# Initialize the dedicated notification logger
notification_logger = _setup_notification_logger()


class SellerNotificationService:
    """
    Service for sending RFQ notifications to sellers.

    Features:
    - Workflow state checking via conversation_sessions table
    - Sellers active in last 15 minutes are skipped (will be reconsidered next task run)
    """

    # Button IDs for RFQ responses
    BUTTON_INTERESTED = "rfq_interested"
    # Intermediate step buttons (shown after clicking "I'm Interested")
    BUTTON_CHECK_DETAILS = "rfq_check_details"
    BUTTON_REQUEST_RFQ = "rfq_request"

    # Button IDs for BFS bid responses
    BUTTON_BFS_ACCEPT = "bfs_seller_accept"
    BUTTON_BFS_REJECT = "bfs_seller_reject"

    def __init__(self):
        self.whatsapp_service = WhatsAppService()
        self.settings = get_settings()

        # Template names for 24hr+ inactive users
        self.rfq_template_name = self.settings.WHATSAPP_TEMPLATE_RFQ_NOTIFICATION
        self.bfs_template_name = self.settings.WHATSAPP_TEMPLATE_BFS_BID_NOTIFICATION

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

    def _build_rfq_template_parameters(self, rfq_data: Dict[str, Any], seller: Dict[str, Any]) -> List[str]:
        """
        Build template parameters for RFQ notification template.

        Template structure (sellers_for_rfq_yes_or_no):
            An RFQ is available on Procucev Portal (QUA AI).
            *RFQ ID:* {{1}}
            *Delivery Date:* {{2}}
            *Delivery Location:* {{3}}
            *Description:* {{4}}
            Click on the provided Procucev Portal {{5}} link to view or submit your quote.
            (T&C Apply)

        Args:
            rfq_data: RFQ information dictionary
            seller: Seller information dictionary

        Returns:
            List of parameter values in order:
                [rfq_id, delivery_date, delivery_location, description, portal_link]
        """
        logger.info(f"rfq_data:{rfq_data}")
        # {{1}} - RFQ ID
        rfq_id = str(rfq_data.get('rfq_id', 'N/A'))

        # {{2}} - Delivery Date (formatted)
        delivery_date = rfq_data.get('delivery_date')
        formatted_date = self._format_date(delivery_date) if delivery_date else 'N/A'

        # {{3}} - Delivery Location (city, state)
        delivery_location = rfq_data.get('delivery_location', {})
        city = delivery_location.get('city', '')
        state = delivery_location.get('state', '')
        location_str = ", ".join(filter(None, [city, state])) or 'N/A'

        # {{4}} - Description
        description = rfq_data.get('description', '').strip() or 'N/A'

        # {{5}} - Portal link
        portal_link = self.settings.PROCUCEV_PORTAL_URL or 'N/A'

        return [rfq_id, formatted_date, location_str, description, portal_link]

    def _build_bfs_template_parameters(self, bid_data: Dict[str, Any], seller_id: str) -> List[str]:
        """
        Build template parameters for BFS bid notification template.

        Template structure (bfs_bid_notification_for_sellers):
            Hello {{1}},
            Here is a New Bid from the buyer for the stocks listed by you.
            *Item:* {{2}}
            *Your Listed Price:* {{3}}
            *Buyer's Offer:* {{4}}
            *Quantity:* {{5}}
            Click on the provided Procucev Portal {{6}} link to accept or reject the bid.

        Args:
            bid_data: Bid information dictionary
            seller_id: Seller's organization UUID

        Returns:
            List of parameter values in order:
                [seller_name, item_description, listed_price, offer_price, quantity, portal_link]
        """
        # {{1}} - Seller name (recipient of the message)
        seller_name = bid_data.get('seller_name', 'Seller')

        # {{2}} - Item description
        item_description = bid_data.get('item_description', 'N/A')

        # {{3}} - Listed price (seller's price)
        buy_price = bid_data.get('buy_price', 0)
        listed_price = f"{buy_price:,.0f}" if buy_price else "N/A"

        # {{4}} - Buyer's offer (ask price)
        ask_price = bid_data.get('ask_price', 0)
        offer_price = f"{ask_price:,.0f}" if ask_price else "N/A"

        # {{5}} - Quantity
        quantity = str(bid_data.get('quantity', 1))

        # {{6}} - Portal link
        portal_link = self.settings.PROCUCEV_PORTAL_URL or 'N/A'

        return [seller_name, item_description, listed_price, offer_price, quantity, portal_link]

    async def _send_rfq_template_notification(
        self,
        phone_number: str,
        rfq_data: Dict[str, Any],
        seller: Dict[str, Any]
    ) -> MessageResponse:
        """
        Send RFQ notification using WhatsApp template message.

        Used for sellers who haven't been active in the last 24 hours,
        requiring a template message to re-initiate conversation.

        Args:
            phone_number: Seller's phone number
            rfq_data: RFQ information dictionary
            seller: Seller information dictionary

        Returns:
            MessageResponse with success status
        """
        parameters = self._build_rfq_template_parameters(rfq_data, seller)

        return await self.whatsapp_service.send_template_message(
            recipient_id=phone_number,
            template_name=self.rfq_template_name,
            parameters=parameters
        )

    async def _send_bfs_template_notification(
        self,
        phone_number: str,
        bid_data: Dict[str, Any],
        seller_id: str
    ) -> MessageResponse:
        """
        Send BFS bid notification using WhatsApp template message.

        Used for sellers who haven't been active in the last 24 hours,
        requiring a template message to re-initiate conversation.

        Args:
            phone_number: Seller's phone number
            bid_data: Bid information dictionary
            seller_id: Seller's organization UUID

        Returns:
            MessageResponse with success status
        """
        parameters = self._build_bfs_template_parameters(bid_data, seller_id)

        return await self.whatsapp_service.send_template_message(
            recipient_id=phone_number,
            template_name=self.bfs_template_name,
            parameters=parameters
        )

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
            # Normalize phone number - strip + prefix to match external_user_id format in DB
            normalized_phone = phone_number.lstrip('+')

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

            result = db.execute(query, {'phone_number': normalized_phone}).fetchone()

            if not result:
                # No session today - safe to notify
                logger.debug(f"[WORKFLOW_CHECK] {normalized_phone}: No session today - SAFE")
                return (False, None, None)

            workflow_type = result[0]
            last_activity = result[1]
            minutes_inactive = result[2] if result[2] is not None else 0

            # Check if active in last 15 minutes
            if minutes_inactive < WORKFLOW_INACTIVITY_THRESHOLD_MINUTES:
                notification_logger.info(
                    f"HELD | Phone: {normalized_phone} | "
                    f"Reason: User active {minutes_inactive}min ago (workflow: {workflow_type}) | "
                    f"Will retry in next task run"
                )
                return (True, workflow_type, minutes_inactive)

            # Inactive for 15+ minutes - safe to notify
            notification_logger.info(
                f"READY | Phone: {normalized_phone} | "
                f"User inactive for {minutes_inactive}min (>= {WORKFLOW_INACTIVITY_THRESHOLD_MINUTES}min threshold)"
            )
            return (False, workflow_type, minutes_inactive)

        except Exception as e:
            logger.error(f"[WORKFLOW_CHECK] {normalized_phone}: Error checking status - {e}")
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
                    notification_logger.info(
                        f"RFQ HELD | RFQ: {rfq_id} | Seller: {seller_name} | "
                        f"Phone: {phone_number} | Reason: User active {minutes_inactive}min ago | "
                        f"Will retry in next task run"
                    )
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
                # Check if we should use template message (user inactive 24hr+)
                use_template = seller.get('use_template_message', False)
                message_type = "template" if use_template else "interactive"

                if use_template:
                    # Send template message for inactive users (24hr+ since last activity)
                    response: MessageResponse = await self._send_rfq_template_notification(
                        phone_number=phone_number,
                        rfq_data=rfq_data,
                        seller=seller
                    )
                else:
                    # Send interactive message for active users (within 24hr window)
                    buttons = self._get_rfq_buttons(str(rfq_id), str(seller_id))
                    response: MessageResponse = await self.whatsapp_service.send_configurable_buttons(
                        recipient_id=phone_number,
                        body=message_body,
                        buttons_config=buttons,
                        header="New RFQ Opportunity",
                        footer="Select an option to proceed"
                    )

                if response.success:
                    notification_logger.info(
                        f"RFQ SENT | RFQ: {rfq_id} | Seller: {seller_name} | "
                        f"Phone: {phone_number} | Type: {message_type} | Message ID: {response.message_id}"
                    )
                    sent_count += 1
                    results.append({
                        "seller_id": seller_id,
                        "seller_name": seller_name,
                        "phone_number": phone_number,
                        "success": True,
                        "message_id": response.message_id,
                        "message_type": message_type
                    })
                else:
                    notification_logger.error(
                        f"RFQ FAILED | RFQ: {rfq_id} | Seller: {seller_name} | "
                        f"Phone: {phone_number} | Type: {message_type} | Error: {response.error}"
                    )
                    failed_count += 1
                    results.append({
                        "seller_id": seller_id,
                        "seller_name": seller_name,
                        "phone_number": phone_number,
                        "success": False,
                        "error": response.error,
                        "message_type": message_type
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

        notification_logger.info(
            f"RFQ SUMMARY | RFQ: {rfq_id} | "
            f"Total: {len(sellers)} | Sent: {sent_count} | Failed: {failed_count} | "
            f"Held: {skipped_count} (will retry next run)"
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

    # ==================== BFS Bid Notification Methods ====================

    def format_bfs_bid_message(self, bid_data: Dict[str, Any]) -> str:
        """
        Format BFS bid data into a WhatsApp message for sellers.

        Args:
            bid_data: Dictionary containing bid information:
                - item_description: Item name/description
                - buy_price: Seller's listed price
                - ask_price: Buyer's bid/offer price
                - quantity: Quantity requested

        Returns:
            Formatted message string
        """
        item_description = bid_data.get('item_description', 'N/A')
        buy_price = bid_data.get('buy_price', 0)
        ask_price = bid_data.get('ask_price', 0)
        quantity = bid_data.get('quantity', 1)

        lines = []

        # Item details
        lines.append(f"*Item:* {item_description}")

        # Price details
        if buy_price:
            lines.append(f"*Your Listed Price:* ₹{buy_price:,.0f}")
        lines.append(f"*Buyer's Offer:* ₹{ask_price:,.0f}")

        lines.append(f"*Quantity:* {quantity}")

        return "\n".join(lines)

    def _get_bfs_buttons(self, bfs_user_uuid: str, seller_id: str) -> List[Dict[str, str]]:
        """
        Get button configuration for BFS bid notification.

        Args:
            bfs_user_uuid: The bfs_users record UUID for Accept/Reject API calls
            seller_id: The seller's organization UUID for account verification

        Returns:
            List of button configurations with Accept and Reject options

        Note:
            Button ID format: {button_type}_{bfs_user_uuid}_{seller_id}
            This allows the handler to verify seller account and call correct API.
        """
        return [
            {
                "id": f"{self.BUTTON_BFS_ACCEPT}_{bfs_user_uuid}_{seller_id}",
                "title": "Accept Bid"
            },
            {
                "id": f"{self.BUTTON_BFS_REJECT}_{bfs_user_uuid}_{seller_id}",
                "title": "Reject Bid"
            }
        ]

    async def send_bfs_bid_notification(
        self,
        seller_phone: str,
        bid_data: Dict[str, Any],
        bfs_user_uuid: str,
        seller_id: str,
        skip_workflow_check: bool = False,
        use_template_message: bool = False
    ) -> Dict[str, Any]:
        """
        Send a BFS bid notification to a seller.

        Args:
            seller_phone: Seller's phone number
            bid_data: Bid information dictionary
            bfs_user_uuid: The bfs_users UUID for button callbacks
            seller_id: The seller's organization UUID for account verification
            skip_workflow_check: If True, skip workflow checking
            use_template_message: If True, send template message instead of interactive

        Returns:
            Dictionary with success status and notification details
        """
        if not seller_phone:
            return {
                "success": False,
                "bfs_user_uuid": bfs_user_uuid,
                "error": "No phone number provided"
            }

        if not seller_id:
            return {
                "success": False,
                "bfs_user_uuid": bfs_user_uuid,
                "error": "No seller ID provided"
            }

        # Check if seller is in an active workflow
        if not skip_workflow_check:
            is_active, workflow_type, minutes_inactive = self.check_seller_workflow_status(
                seller_phone
            )
            if is_active:
                item_desc = bid_data.get('item_description', 'N/A')
                notification_logger.info(
                    f"BFS HELD | Item: {item_desc} | Phone: {seller_phone} | "
                    f"Reason: User active {minutes_inactive}min ago | Will retry in next task run"
                )
                return {
                    "success": False,
                    "bfs_user_uuid": bfs_user_uuid,
                    "seller_phone": seller_phone,
                    "skipped": True,
                    "reason": f"Active {minutes_inactive}min ago",
                    "workflow_type": workflow_type
                }

        try:
            item_desc = bid_data.get('item_description', 'N/A')
            ask_price = bid_data.get('ask_price', 0)
            message_type = "template" if use_template_message else "interactive"

            if use_template_message:
                # Send template message for inactive users (24hr+ since last activity)
                response: MessageResponse = await self._send_bfs_template_notification(
                    phone_number=seller_phone,
                    bid_data=bid_data,
                    seller_id=seller_id
                )
            else:
                # Send interactive message for active users (within 24hr window)
                message_body = self.format_bfs_bid_message(bid_data)
                buttons = self._get_bfs_buttons(bfs_user_uuid, seller_id)

                response: MessageResponse = await self.whatsapp_service.send_configurable_buttons(
                    recipient_id=seller_phone,
                    body=message_body,
                    buttons_config=buttons,
                    header="New Bid Received",
                    footer="Tap to respond"
                )

            if response.success:
                notification_logger.info(
                    f"BFS SENT | Item: {item_desc} | Bid: ₹{ask_price:,.0f} | "
                    f"Phone: {seller_phone} | Type: {message_type} | Message ID: {response.message_id}"
                )
                return {
                    "success": True,
                    "seller_phone": seller_phone,
                    "bfs_user_uuid": bfs_user_uuid,
                    "message_id": response.message_id,
                    "message_type": message_type
                }
            else:
                notification_logger.error(
                    f"BFS FAILED | Item: {item_desc} | Phone: {seller_phone} | "
                    f"Type: {message_type} | Error: {response.error}"
                )
                return {
                    "success": False,
                    "seller_phone": seller_phone,
                    "bfs_user_uuid": bfs_user_uuid,
                    "error": response.error,
                    "message_type": message_type
                }

        except Exception as e:
            notification_logger.error(
                f"BFS FAILED | Phone: {seller_phone} | Exception: {e}"
            )
            return {
                "success": False,
                "seller_phone": seller_phone,
                "bfs_user_uuid": bfs_user_uuid,
                "error": str(e)
            }

    async def send_bfs_bid_notifications_batch(
        self,
        notifications: List[Dict[str, Any]],
        skip_workflow_check: bool = False
    ) -> Dict[str, Any]:
        """
        Send BFS bid notifications to multiple sellers.

        Args:
            notifications: List of notification dictionaries, each containing:
                - seller_phone: Seller's phone number
                - bid_data: Bid information (item_description, ask_price, etc.)
                - bfs_user_uuid: Record UUID for button callbacks
                - seller_id: Seller's organization UUID for account verification
                - use_template_message: If True, send template instead of interactive
            skip_workflow_check: If True, skip workflow checking for all

        Returns:
            Dictionary with batch results summary
        """
        if not notifications:
            logger.warning("[BFS_NOTIFY] No notifications to send")
            return {
                "success": True,
                "total": 0,
                "sent": 0,
                "failed": 0,
                "skipped": 0,
                "results": []
            }

        notification_logger.info(f"BFS BATCH START | Processing {len(notifications)} bid notifications")

        results = []
        sent_count = 0
        failed_count = 0
        skipped_count = 0

        for notification in notifications:
            result = await self.send_bfs_bid_notification(
                seller_phone=notification['seller_phone'],
                bid_data=notification['bid_data'],
                bfs_user_uuid=notification['bfs_user_uuid'],
                seller_id=notification['seller_id'],
                skip_workflow_check=skip_workflow_check,
                use_template_message=notification.get('use_template_message', False)
            )

            results.append(result)

            if result.get('success'):
                sent_count += 1
            elif result.get('skipped'):
                skipped_count += 1
            else:
                failed_count += 1

        notification_logger.info(
            f"BFS SUMMARY | Total: {len(notifications)} | "
            f"Sent: {sent_count} | Failed: {failed_count} | Held: {skipped_count} (will retry next run)"
        )

        return {
            "success": failed_count == 0,
            "total": len(notifications),
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
