"""
Seller API Service.

This service handles seller-specific operations including RFQ fetching,
credit checking, email sending, and subscription management.
"""

import logging
from typing import Dict, Any, List

from app.procucev_apis.procucev_api_client import get_procucev_api_client

logger = logging.getLogger(__name__)

class SellerAPIService:
    """
    Service for handling seller-specific operations.
    Provides methods for RFQ management, credits, emails, and subscriptions.
    """

    def __init__(self):
        self.api_client = get_procucev_api_client()
        
    async def fetch_active_rfqs(self, org_id: str) -> Dict[str, Any]:
        """Fetch active RFQs based on seller's category."""
        try:
            endpoint = "/rest/gmt/getRfqByCategory"
            data = {"id": org_id}

            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="fetch_active_rfqs"
            )
            
            if response.get('success'):
                result = response.get('data', {})
                rfqs_raw = result.get("rfqs", [])

                # ✅ LIMIT TO TOP 5 RFQs
                rfqs_raw = rfqs_raw[:5]
                transformed_rfqs = []

                for rfq in rfqs_raw:
                    # Get delivery locations
                    delivery_locations = rfq.get("clientdeliverylocationrfq", [])
                    location_state = None
                    if delivery_locations and len(delivery_locations) > 0:
                        location_state = delivery_locations[0].get("state")

                    # Get unique categories from rfqItem list
                    rfq_items = rfq.get("rfqItem", [])
                    categories = list(set(item.get("category") for item in rfq_items if item.get("category")))
                    categories_str = ", ".join(categories) if categories else ""

                    # Get delivery date and format it
                    delivery_date = rfq.get("deliveryDate", "")
                    formatted_date = ""
                    if delivery_date:
                        try:
                            from datetime import datetime
                            date_obj = datetime.fromisoformat(delivery_date.replace('Z', '+00:00'))
                            formatted_date = date_obj.strftime("%d-%b-%Y")  # e.g., "28-Jan-2025"
                        except:
                            formatted_date = delivery_date

                    # Get project description
                    project_desc = rfq.get("projectDesc", "")

                    transformed_rfqs.append({
                        "rfq_id": rfq.get("rfqId"),
                        "categories": categories_str,
                        "delivery_date": formatted_date,
                        "project_description": project_desc,
                        "location": location_state
                    })

                return {
                        "success": True,
                        "rfqs": transformed_rfqs,
                        "total_count": result.get("count", 0),
                    }
            else:
                return {"success": False, "error": response.get('message', 'Failed to fetch active RFQs')}

        except Exception as e:
            logger.error(f"Error fetching active RFQs: {e}")
            return {"success": False, "error": str(e)}

    async def check_seller_credits(self, seller_org_id: str) -> Dict[str, Any]:
        """Check seller's RFQ request credit balance."""
        try:
            endpoint = "/rest/gmt/getSellerRfqCredits"
            data = {"id": seller_org_id}

            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="check_seller_credits"
            )
            
            if response.get('success'):
                result = response.get('data', {})
                return {
                    "success": True,
                    "credits_available": result.get("creditsAvailable", 0)
                }
            else:
                return {"success": False, "error": response.get('message', 'Failed to check seller credits')}

        except Exception as e:
            logger.error(f"Error checking seller credits: {e}")
            return {"success": False, "error": str(e)}

    async def send_rfq_email(self, rfq_ids: List[str], seller_email: str, seller_id: str) -> Dict[str, Any]:
        """Send RFQ details to seller via email."""
        try:
            endpoint = "/rest/gmt/forwardRfqsToVendor"
            
            data = {
                "rfqIds": rfq_ids,
                "email": seller_email,
                "sellerId": seller_id
            }

            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="send_rfq_email"
            )

            # Mock successful response for now
            if response.get('status_code') == 200:
                return {"success": True, "email_sent": True, "data": response}
            else:
                return {"success": False, "error": response.get('message', 'Failed to send RFQ email')}

        except Exception as e:
            logger.error(f"Error sending RFQ email: {e}")
            return {"success": False, "error": str(e)}

    async def update_rfq_seller_sent_flag(self, rfq_id: str, seller_id: str) -> Dict[str, Any]:
        """Update RFQ-Seller sent flag after email is sent."""
        try:
            endpoint = "/rest/seller/updateRFQSentFlag"
            
            data = {
                "rfqId": rfq_id,
                "sellerId": seller_id
            }

            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="update_rfq_seller_sent_flag"
            )
            
            # Mock successful response for now
            if response.get('status_code') != 200:
                result = {
                    "success": True,
                    "flag_updated": True,
                    "data": {
                        "message": "RFQ sent flag updated successfully",
                        "rfqId": rfq_id,
                        "sellerId": seller_id,
                        "status": "sent",
                        "updatedAt": "2025-08-24T12:32:10Z"
                    }
                }
                return {"success": True, "flag_updated": True, "data": result}
            else:
                return {"success": False, "error": response.get('message', 'Failed to update RFQ sent flag')}

        except Exception as e:
            logger.error(f"Error updating RFQ sent flag: {e}")
            return {"success": False, "error": str(e)}

    async def get_subscription_plans(self) -> Dict[str, Any]:
        """Get available subscription plans for sellers."""
        try:
            endpoint = "/rest/gmt/getSubscriptionPlans"

            response = await self.api_client.get(
                endpoint=endpoint,
                require_auth=True,
                api_title="get_subscription_plans"
            )
            
            if response.get('success'):
                result = response.get('data', {})
                return {
                    "success": True,
                    "plans": result.get("plans", [])
                }
            else:
                return {"success": False, "error": response.get('message', 'Failed to fetch subscription plans')}

        except Exception as e:
            logger.error(f"Error fetching subscription plans: {e}")
            return {"success": False, "error": str(e)}

    async def generate_payment_link(self, plan_id: str, seller_id: str) -> Dict[str, Any]:
        """Generate application portal link for subscription plan."""
        try:
            # Return static portal link instead of payment link
            portal_link = "https://p2pdevuiindia.azurewebsites.net/"
            
            return {
                "success": True,
                "payment_link": portal_link
            }
            
        except Exception as e:
            logger.error(f"Error generating portal link: {e}")
            return {"success": False, "error": str(e)}

    async def fetch_seller_open_rfqs_for_reminder(self, seller_id: str) -> Dict[str, Any]:
        """Fetch open RFQs where seller has not submitted bids yet for end-of-flow reminder."""
        try:
            endpoint = "/rest/gmt/getOpenRfqs"
            
            payload = {
                "seller_id": seller_id
            }

            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=payload,
                require_auth=True,
                api_title="fetch_seller_open_rfqs_for_reminder"
            )
            
            # Mock successful response for now
            if response.get('status_code') == 200:
                return {
                    "success": True,
                    "open_rfqs": response.get("open_rfqs", []),
                    "total_count": response.get("total_count", 0)
                }
            else:
                return {"success": False, "error": response.get('message', 'Failed to fetch seller open RFQs')}

        except Exception as e:
            logger.error(f"Exception in fetch_seller_open_rfqs_for_reminder: {e}")
            return {"success": False, "error": str(e)}