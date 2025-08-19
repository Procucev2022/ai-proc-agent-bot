"""
Seller Service for managing seller registration, RFQ access, and credit management.

This service handles the complete seller workflow including:
- Seller identification and email confirmation
- New seller registration with OTP verification
- Active RFQ fetching by category
- Credit balance management
- RFQ email sending
- Subscription plan management
"""

import logging
from typing import Dict, Any
from app.models import ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.gmt_api_service import GMTAPIService
from app.config import get_settings
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers

logger = logging.getLogger(__name__)


class SellerService:
    """
    Service for managing seller operations and RFQ access.

    Handles seller identification, registration, RFQ fetching,
    credit management, and email notifications.
    """

    def __init__(self):
        self.whatsapp_service = WhatsAppService()
        self.gmt_api_service = GMTAPIService()
        self.settings = get_settings()
        self.openai_service = OpenAIService()
        self.response_helpers = ResponseHelpers(self.openai_service)

    async def _generate_fallback_message(self, workflow_step: str, context: Dict[str, Any]) -> str:
        """Generate a fallback message when contextual response generation fails.

        Args:
            workflow_step: The current workflow step
            context: Dictionary containing context data for message generation

        Returns:
            Formatted fallback message string
        """
        fallback_messages = {
            "show_rfqs_unregistered": (
                f"We have {context.get('total_count', 0)} RFQs in your category. "
                "Here are the latest 3:\n\n{rfq_list}\n\n"
                "Is there anything specific you are looking to sell today? Please share the details. "
                "You can also search for RFQs against your category.\n\n"
                "To register and get full access, please contact support@procucev.com"
            ),
            "show_rfqs_with_plans": (
                f"You have {context.get('total_count', 0)} live RFQs in your category. "
                "Here are the latest 3:\n\n{rfq_list}\n\n"
                "Here are our subscription plans:\n{plans_list}\n\n"
                "Would you like to subscribe to access more RFQs?"
            ),
            "show_rfqs_with_credits": (
                f"You have {context.get('total_count', 0)} live RFQs in your category. "
                "Here are the latest 3:\n\n{rfq_list}\n\n"
                f"You have {context.get('credits_available', 0)} request credits available. "
                "Type the RFQ IDs that you want to request."
            )
        }

        # Generate RFQ list
        rfq_list = "\n".join([
            f"RFQ {rfq.get('rfq_id')} - {rfq.get('location', 'N/A')} - {rfq.get('submission_date', 'N/A')}"
            for rfq in context.get('latest_rfqs', [])
        ])

        # Generate plans list if needed
        plans_list = ""
        if workflow_step == "show_rfqs_with_plans":
            plans_list = "\n".join([
                f"{plan.get('name')} – ₹{plan.get('price')} for {plan.get('rfq_count')} RFQs"
                for plan in context.get('plans', [])
            ])

        # Format the message with the appropriate variables
        message_template = fallback_messages.get(workflow_step, "")
        return message_template.format(
            rfq_list=rfq_list,
            plans_list=plans_list
        )

    async def identify_seller(self, phone_number: str) -> Dict[str, Any]:
        """
        Identify seller by phone number and return associated information.

        Args:
            phone_number: WhatsApp phone number with country code

        Returns:
            Dict containing seller information or None if not found
        """
        try:
                return {
                    "is_auth": False,
                    "multiple_emails": False,
                }


        except Exception as e:
            logger.error(f"Error identifying seller: {e}")
            return {"found": False, "error": str(e)}


    async def fetch_active_rfqs_by_category(self, category: str, limit: int = get_settings().rfq_fetch_limit) -> Dict[str, Any]:
        """
        Fetch active RFQs based on seller's category.

        Args:
            category: Product/service category
            limit: Number of RFQs to fetch

        Returns:
            Dict containing RFQ list and count
        """
        try:
            # Call GMT API to fetch active RFQs
            rfq_response = await self.gmt_api_service.fetch_active_rfqs(
                category=category,
                limit=limit
            )

            if rfq_response.get("success"):
                rfqs = rfq_response.get("rfqs", [])
                total_count = rfq_response.get("total_count", 0)

                return {
                    "success": True,
                    "rfqs": rfqs,
                    "total_count": total_count,
                    "latest_rfqs": rfqs[:get_settings().rfq_fetch_limit]  # Latest 3 RFQs
                }
            else:
                return {
                    "success": False,
                    "error": rfq_response.get("error", "Failed to fetch RFQs")
                }

        except Exception as e:
            logger.error(f"Error fetching active RFQs: {e}")
            return {"success": False, "error": str(e)}

    async def check_seller_credit_balance(self, seller_id: str) -> Dict[str, Any]:
        """
        Check seller's RFQ request credit balance.

        Args:
            seller_id: Unique seller identifier

        Returns:
            Dict containing credit balance information
        """
        try:
            # Call GMT API to check credit balance
            credit_response = await self.gmt_api_service.check_seller_credits(seller_id)

            if credit_response.get("success"):
                return {
                    "success": True,
                    "credits_available": credit_response.get("credits_available", 0),
                    "subscription_status": credit_response.get("subscription_status", "unsubscribed"),
                    "seller_status": credit_response.get("seller_status", "new")
                }
            else:
                return {
                    "success": False,
                    "error": credit_response.get("error", "Failed to check credits")
                }

        except Exception as e:
            logger.error(f"Error checking seller credit balance: {e}")
            return {"success": False, "error": str(e)}

    async def send_rfq_email(self, rfq_id: str, seller_email: str, seller_id: str) -> Dict[str, Any]:
        """
        Send RFQ details to seller via email.

        Args:
            rfq_id: RFQ identifier
            seller_email: Seller's email address
            seller_id: Seller identifier

        Returns:
            Dict containing email sending status
        """
        try:
            # Call GMT API to send RFQ email
            email_response = await self.gmt_api_service.send_rfq_email(
                rfq_id=rfq_id,
                seller_email=seller_email,
                seller_id=seller_id
            )

            if email_response.get("success"):
                # Update RFQ-Seller sent flag
                await self.gmt_api_service.update_rfq_seller_sent_flag(
                    rfq_id=rfq_id,
                    seller_id=seller_id
                )

                return {
                    "success": True,
                    "email_sent": True,
                    "message": f"RFQ {rfq_id} has been emailed to you"
                }
            else:
                return {
                    "success": False,
                    "error": email_response.get("error", "Failed to send RFQ email")
                }

        except Exception as e:
            logger.error(f"Error sending RFQ email: {e}")
            return {"success": False, "error": str(e)}

    async def get_subscription_plans(self) -> Dict[str, Any]:
        """
        Get available subscription plans for sellers.

        Returns:
            Dict containing subscription plans
        """
        try:
            # Call GMT API to get subscription plans
            plans_response = await self.gmt_api_service.get_subscription_plans()

            if plans_response.get("success"):
                return {
                    "success": True,
                    "plans": plans_response.get("plans", [])
                }
            else:
                return {
                    "success": False,
                    "error": plans_response.get("error", "Failed to fetch plans")
                }

        except Exception as e:
            logger.error(f"Error fetching subscription plans: {e}")
            return {"success": False, "error": str(e)}

    async def generate_payment_link(self, plan_id: str, seller_id: str) -> Dict[str, Any]:
        """
        Generate Razorpay payment link for subscription plan.

        Args:
            plan_id: Subscription plan identifier
            seller_id: Seller identifier

        Returns:
            Dict containing payment link
        """
        try:
            # Call GMT API to generate payment link
            payment_response = await self.gmt_api_service.generate_payment_link(
                plan_id=plan_id,
                seller_id=seller_id
            )

            if payment_response.get("success"):
                return {
                    "success": True,
                    "payment_link": payment_response.get("payment_link"),
                    "message": "Payment link generated successfully"
                }
            else:
                return {
                    "success": False,
                    "error": payment_response.get("error", "Failed to generate payment link")
                }

        except Exception as e:
            logger.error(f"Error generating payment link: {e}")
            return {"success": False, "error": str(e)}

    async def handle_seller_workflow(self, user, message: str) -> Dict[str, Any]:
        """
        Main seller workflow handler based on the flow document.

        Args:
            phone_number: Seller's WhatsApp number
            message: Incoming message
            session: Current conversation session

        Returns:
            Dict containing workflow response and next steps
        """
        try:
            # Step 1: Identify seller
            seller_info = await self.identify_seller(user)

            if not seller_info.get("is_auth"):
                # New seller - start registration flow
                return await self._handle_registration_seller_flow(seller_info, message)

            # Existing seller - check if multiple emails
            if seller_info.get("multiple_email", True):
                # Email confirmation flow
                return await self._handle_email_confirmation_flow(seller_info, message)

            # Regular seller flow
            return await self._handle_existing_seller_flow(seller_info)

        except Exception as e:
            logger.error(f"Error in seller workflow: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": "We encountered an issue. Please contact support@procucev.com"
            }

    async def _handle_registration_seller_flow(self, seller_info: Dict[str, Any], message: str) -> Dict[str, Any]:
        """Handle new seller registration flow."""

        if "register" in message.lower() or "sign up" in message.lower():
            # Step 1: TODO : Check if user wants to register or just see RFQs

            # call the fetch category API and show the list of RFQ also mention you will get one free rfq
            category = seller_info.get("seller_category")
            rfq_info = await self.fetch_active_rfqs_by_category(category, limit=get_settings().rfq_fetch_limit)

            success = rfq_info.get("success")

            context = {
                "workflow_step": "registration_completed_show_rfqs" if success else "error_fetching_data",
                "total_count": rfq_info.get("total_count", 0),
                "latest_rfqs": rfq_info.get("latest_rfqs", []),
                "free_rfq": True,  # flag to tell AI we want to mention the free RFQ
                "error_message": None if success else "Failed to fetch RFQ's. Please contact support@procucev.com",
            }
            try:
                response_message = await self.response_helpers.generate_seller_contextual_response(context)
            except Exception as e:
                logger.error(f"generate seller contextual response  failed: {str(e)}")
                response_message = await self._generate_fallback_message(context["workflow_step"], context)

            return {
                "workflow_step": context["workflow_step"],
                "success": success,
                "message": response_message,
                "rfq_ids": [rfq.get("rfq_id") for rfq in context["latest_rfqs"] if rfq.get("rfq_id")],
                "total_count": context["total_count"],
                "error": context.get("error_message"),
            }

        else:
            # Show available RFQs without registration
            return await self._show_available_rfqs_without_registration(seller_info,message)

    async def _handle_email_confirmation_flow(self, seller_info: Dict[str, Any], message: str,session: ConversationSession) -> Dict[str, Any]:
        """Handle email confirmation for existing sellers."""
        seller_name = seller_info.get("seller_name", "Seller")
        email = seller_info.get("email")

        if seller_info.get("multiple_emails"):
            # Handle multiple emails selection
            return {
                "workflow_step": "email_selection",
                "message": f"Welcome, {seller_name}. We found multiple records. Please choose the correct email to start:\n" +
                           "\n".join(
                               [f"{i + 1}. {email}" for i, email in enumerate(seller_info.get("multiple_emails", []))])
            }
        else:
            # Single email confirmation
            return {
                "workflow_step": "email_confirmation",
                "message": f"Welcome, {seller_name}. Before we begin, could you please confirm your email address: {email}?"
            }



    async def _handle_existing_seller_flow(self, seller_info: Dict[str, Any]) -> Dict[str, Any]:
        """Handle existing seller RFQ access flow."""

        category = seller_info.get("seller_category")

        rfq_info = await self.fetch_active_rfqs_by_category(category, limit=get_settings().rfq_fetch_limit)

        success = rfq_info.get("success")

        context = {
            "workflow_step": "show_rfqs_to_existing_sellers" if success else "error_fetching_data",
            "total_count": rfq_info.get("total_count", 0),
            "latest_rfqs": rfq_info.get("latest_rfqs", []),
            "rfq_success": rfq_info.get("success", False),
            "error_message": None if success else "Failed to fetch RFQ's. Please contact support@procucev.com",
        }
        try:
            response_message = await self.response_helpers.generate_seller_contextual_response(context)
        except Exception as e:
            logger.error(f"generate seller contextual response  failed: {str(e)}")
            response_message = await self._generate_fallback_message(context["workflow_step"], context)

        return {
            "workflow_step": context["workflow_step"],
            "success": success,
            "message": response_message,
            "rfq_ids": [rfq.get("rfq_id") for rfq in context["latest_rfqs"] if rfq.get("rfq_id")],
            "total_count": context["total_count"],
            "error": context.get("error_message"),
        }



    async def _show_available_rfqs_without_registration(self) -> Dict[str, Any]:
        """Show available RFQs to unregistered users."""

        rfq_info = await self.fetch_active_rfqs_by_category(limit=get_settings().rfq_fetch_limit)

        if rfq_info.get("success"):
            total_count = rfq_info.get("total_count", 0)
            latest_rfqs = rfq_info.get("latest_rfqs", [])

            # Build AI contextual response like RFQ status flow
            try:
                context = {
                    "workflow_step": "show_rfqs_unregistered",
                    "total_count": total_count,
                    "latest_rfqs": latest_rfqs,
                }

                response_message = await self.response_helpers.generate_seller_contextual_response(context)
            except Exception as e:
                logger.error(f"generate seller contextual response  failed: {str(e)}")
                response_message = await self._generate_fallback_message("show_rfqs_unregistered", context)

            return {
                "workflow_step": "show_rfqs_unregistered",
                "success": True,
                "message": response_message
            }
        else:
            return {
                "success": False,
                "error": rfq_info.get("error"),
                "message": "Unable to fetch RFQs. Please contact support@procucev.com"
            }

    async def _show_rfqs_with_subscription_plans(self, seller_info: Dict[str, Any]) -> Dict[str, Any]:
        """Show RFQs with subscription plan options."""
        category= seller_info.get("seller_category")

        rfq_info = await self.fetch_active_rfqs_by_category(category, limit=get_settings().rfq_fetch_limit)
        plans_info = await self.get_subscription_plans()

        print("rfq info", rfq_info, "plans_info",plans_info)

        success = rfq_info.get("success") and plans_info.get("success")

        context = {
            "workflow_step": "show_rfqs_with_plans" if success else "error_fetching_data",
            "total_count": rfq_info.get("total_count", 0),
            "latest_rfqs": rfq_info.get("latest_rfqs", []),
            "plans": plans_info.get("plans", []),
            "rfq_success": rfq_info.get("success", False),
            "plans_success": plans_info.get("success", False),
            "error_message": None if success else "Failed to fetch RFQs or subscription plans. Please contact support@procucev.com",
        }
        try:
            response_message = await self.response_helpers.generate_seller_contextual_response(context)
        except Exception as e:
            logger.error(f"generate seller contextual response  failed: {str(e)}")
            response_message = await self._generate_fallback_message("show_rfqs_with_plans", context)

        return {
            "workflow_step": context["workflow_step"],
            "success": success,
            "message": response_message,
            "rfq_ids": [rfq.get("rfq_id") for rfq in context["latest_rfqs"] if rfq.get("rfq_id")],
            "total_count": context["total_count"],
            "error": context.get("error_message"),
        }


    async def _show_rfqs_with_credits(self, seller_info: Dict[str, Any], credits_available: int) -> Dict[str, Any]:
        """Show RFQs to sellers with available credits."""
        category = seller_info.get("seller_category")
        rfq_info = await self.fetch_active_rfqs_by_category(category, limit=3)

        if rfq_info.get("success"):
            total_count = rfq_info.get("total_count", 0)
            latest_rfqs = rfq_info.get("latest_rfqs", [])
            # Build AI contextual response like RFQ status flow
            try:
                context = {
                    "workflow_step": "show_rfqs_with_credits",
                    "total_count": total_count,
                    "latest_rfqs": latest_rfqs,
                    "credits_available": credits_available
                }

                response_message = await self.response_helpers.generate_seller_contextual_response(context)
            except Exception as e:
                logger.error(f"generate seller contextual response  failed: {str(e)}")
                response_message = await self._generate_fallback_message("show_rfqs_with_credits", context)


            return {
                "workflow_step": "show_rfqs_with_credits",
                "success": True,
                "message": response_message,
                "rfq_ids": [rfq.get("rfq_id") for rfq in latest_rfqs if rfq.get("rfq_id")],
                "total_count": total_count
            }
        else:
            return {
                "success": False,
                "error": rfq_info.get("error"),
                "message": "Unable to fetch RFQs. Please contact support@procucev.com"
            }