"""
Seller Notification Service

Handles sending RFQ notifications to matched sellers via WhatsApp.
"""

import logging
from typing import Dict, List, Any
from datetime import datetime

from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.config import get_settings

logger = logging.getLogger(__name__)


class SellerNotificationService:
    """
    Service for sending RFQ notifications to sellers.
    """

    # Button IDs for handling responses
    BUTTON_CHECK_DETAILS = "rfq_check_details"
    BUTTON_INTERESTED = "rfq_interested"

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
        lines = []
        lines.append("*New RFQ Opportunity*")
        lines.append("")

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

    async def send_rfq_notifications(
        self,
        rfq_data: Dict[str, Any],
        sellers: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Send RFQ notifications to a list of sellers with interactive buttons.

        Args:
            rfq_data: RFQ information dictionary
            sellers: List of seller dictionaries with phone_number field

        Returns:
            Dictionary with success count, failure count, and details
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
                "results": []
            }

        # Format the message once (same for all sellers)
        message_body = self.format_rfq_message(rfq_data)
        buttons = self._get_rfq_buttons(str(rfq_id))

        logger.info(f"Sending RFQ {rfq_id} notification to {len(sellers)} sellers")

        results = []
        sent_count = 0
        failed_count = 0

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

        logger.info(f"RFQ {rfq_id} notifications complete: {sent_count} sent, {failed_count} failed")

        return {
            "success": failed_count == 0,
            "rfq_id": rfq_id,
            "total": len(sellers),
            "sent": sent_count,
            "failed": failed_count,
            "results": results
        }
