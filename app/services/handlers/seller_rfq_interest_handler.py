"""
Seller RFQ Interest Handler.

Handles seller authentication flow when clicking 'I'm Interested' on RFQ notification.
This handler manages the seller_rfq_intimation workflow type which can ONLY be
initiated from the "I'm Interested" button handler.

Flow:
1. Seller clicks "I'm Interested" -> Show intermediate buttons (Check Details / Request RFQ)
2. If "Check Details" -> Send Procucev website link
3. If "Request RFQ" -> Proceed with seller authentication flow

Uses SellerAuthMixin for shared authentication logic with BFSSellerBidHandler.
"""

import logging
from typing import Dict, Any
from app.models import WorkflowType, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.redis_db import get_auth_redis_service
from app.config import get_settings
from app.services.handlers.seller_auth_mixin import SellerAuthMixin

logger = logging.getLogger(__name__)


class SellerRFQInterestHandler(SellerAuthMixin):
    """
    Handles seller authentication flow when clicking 'I'm Interested' on RFQ notification.

    IMPORTANT: This handler manages the seller_rfq_intimation workflow type.
    This workflow can ONLY be initiated from the button handler in interactive_message_processor.

    Uses SellerAuthMixin for shared authentication logic:
    - Auth state checking
    - Account switch prompting (1/2 choice)
    - Seller email auto-lookup
    - OTP flow initiation
    """

    def __init__(self, whatsapp_service: WhatsAppService, authentication_service=None,
                 session_manager=None, otp_service=None):
        self.whatsapp_service = whatsapp_service
        self.authentication_service = authentication_service
        self.session_manager = session_manager
        self.otp_service = otp_service
        self.auth_redis_service = get_auth_redis_service()
        self.settings = get_settings()

        # Initialize mixin dependencies
        self.__init_auth_mixin__()

    async def handle_rfq_interest_click(self, user_phone: str, rfq_id: str,
                                        seller_id: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Main entry point - orchestrate the complete seller RFQ interest flow.

        This is the ONLY place where seller_rfq_intimation workflow can be initiated.

        Args:
            user_phone: User's phone number
            rfq_id: RFQ ID that the seller is interested in
            seller_id: Target seller ID that should be authenticated
            session: Current conversation session

        Returns:
            Dictionary with flow status and any messages sent
        """
        logger.info(f"Handling RFQ interest click for phone={user_phone}, rfq_id={rfq_id}, seller_id={seller_id}")

        # 1. Set workflow type (ONLY place this workflow can be initiated)
        WorkflowManager.set_workflow_type(session, WorkflowType.seller_rfq_intimation,
                                         caller="seller_rfq_interest_handler")
        session.workflow_state = {
            "rfq_id": rfq_id,
            "target_seller_id": seller_id,
            "auth_stage": "checking"
        }

        # 2. Check current authentication state (from mixin)
        auth_state = await self.check_seller_auth_state(user_phone, seller_id)
        logger.info(f"Auth state for {user_phone}: {auth_state}")

        # 3. Route based on auth state
        if auth_state["state"] == "correct_seller":
            # Already authenticated as correct seller - show intermediate buttons
            return await self._show_intermediate_buttons(user_phone, rfq_id, seller_id, session)

        elif auth_state["state"] == "not_auth":
            # Not authenticated - start OTP auth for target seller (from mixin)
            return await self.initiate_seller_auth(user_phone, session, seller_id)

        else:
            # Authenticated as wrong account (buyer or different seller) - prompt switch (from mixin)
            return await self.prompt_seller_account_switch(
                user_phone, session, auth_state.get("current_user"),
                action_description="express interest in this RFQ"
            )

    # Authentication methods (check_seller_auth_state, prompt_seller_account_switch,
    # handle_seller_switch_response, initiate_seller_auth, get_seller_info)
    # are inherited from SellerAuthMixin

    async def handle_switch_response(self, user_phone: str, session: ConversationSession,
                                     message: str) -> Dict[str, Any]:
        """
        Process user's response to account switch prompt.
        Delegates to mixin with RFQ-specific decline message.
        """
        decline_message = (
            "No problem! You can click the \"I'm Interested\" button again "
            "when you're ready to respond as a Seller."
        )

        return await self.handle_seller_switch_response(
            user_phone, session, message, decline_message=decline_message
        )

    async def _show_intermediate_buttons(self, user_phone: str, rfq_id: str,
                                          seller_id: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Show intermediate buttons after successful authentication.

        Shows two options:
        - Check Details: Opens Procucev website to view RFQ details
        - Request RFQ: Proceeds with seller workflow to request the RFQ

        Args:
            user_phone: User's phone number
            rfq_id: RFQ ID the seller is interested in
            seller_id: Seller's ID
            session: Current conversation session
        """
        logger.info(f"Showing intermediate buttons to {user_phone} for RFQ {rfq_id}")

        # Import here to avoid circular imports
        from app.services.seller_notification_service import SellerNotificationService

        # Get intermediate buttons configuration
        notification_service = SellerNotificationService()
        buttons = notification_service.get_intermediate_rfq_buttons(rfq_id, seller_id)

        # Send message with intermediate buttons
        message = (
            f"*RFQ ID:* {rfq_id}\n\n"
            "What would you like to do?\n\n"
            "*Check Details* - View RFQ details on the Procucev website\n"
            "*Request RFQ* - Request this RFQ to be sent to your email"
        )

        response = await self.whatsapp_service.send_configurable_buttons(
            recipient_id=user_phone,
            body=message,
            buttons_config=buttons,
            header="RFQ Options",
            footer="Select an option to proceed"
        )

        # Store rfq_id and seller_id in session for handling button clicks
        session.workflow_state = session.workflow_state or {}
        session.workflow_state["rfq_id"] = rfq_id
        session.workflow_state["target_seller_id"] = seller_id
        session.workflow_state["auth_stage"] = "intermediate_selection"

        if self.session_manager:
            await self.session_manager.save_session(session)

        if response.success:
            logger.info(f"Sent intermediate buttons to {user_phone} for RFQ {rfq_id}")
            return {
                "status": "intermediate_buttons_sent",
                "rfq_id": rfq_id,
                "seller_id": seller_id,
                "message_id": response.message_id
            }
        else:
            logger.error(f"Failed to send intermediate buttons: {response.error}")
            return {
                "status": "error",
                "error": f"Failed to send options: {response.error}"
            }

    async def handle_check_details_click(self, user_phone: str, rfq_id: str,
                                         seller_id: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Handle "Check Details" button click - send Procucev website link and return to seller menu.

        Args:
            user_phone: User's phone number
            rfq_id: RFQ ID
            seller_id: Seller ID
            session: Current conversation session

        Returns:
            Dictionary with flow status
        """
        logger.info(f"Handling Check Details click for phone={user_phone}, rfq_id={rfq_id}")

        # Get Procucev website URL from config
        website_url = self.settings.procucev_rfq_details_url

        # Send URL message with Request RFQ button
        message = (
            f"You can view the RFQ details on the Procucev website:\n\n"
            f"{website_url}"
        )
        buttons = [
            {"id": f"rfq_request_{rfq_id}_{seller_id}", "title": "Request RFQ"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            recipient_id=user_phone,
            body=message,
            buttons_config=buttons
        )

        # Clear workflow - user remains authenticated as seller but exits current workflow
        from app.utils.datetime_utils import utc_now
        session.workflow_type = None
        session.workflow_state = {"last_activity_at": utc_now().isoformat()}
        if self.session_manager:
            await self.session_manager.save_session(session)

        return {
            "status": "check_details_sent",
            "rfq_id": rfq_id,
            "website_url": website_url
        }

    async def handle_request_rfq_click(self, user_phone: str, rfq_id: str,
                                       seller_id: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Handle "Request RFQ" button click - proceed with seller workflow.

        Uses the same flow as "I'm Interested" button - calls _redirect_to_seller_flow.

        Args:
            user_phone: User's phone number
            rfq_id: RFQ ID the seller is interested in
            seller_id: Seller's ID
            session: Current conversation session

        Returns:
            Dictionary with flow status and seller workflow result
        """
        logger.info(f"Handling Request RFQ click for phone={user_phone}, rfq_id={rfq_id}")

        return await self._redirect_to_seller_flow(user_phone, rfq_id, seller_id, session)

    async def _redirect_to_seller_flow(self, user_phone: str, rfq_id: str,
                                       seller_id: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Redirect to seller service flow after successful authentication.

        Args:
            user_phone: User's phone number
            rfq_id: RFQ ID the seller is interested in
            seller_id: Seller's ID
            session: Current conversation session
        """
        from app.services.seller_service import SellerService

        logger.info(f"Redirecting {user_phone} to seller flow for RFQ {rfq_id}")

        # Get authenticated user data from Redis (already a User object from schemas)
        normalized_phone = user_phone.lstrip('+')
        user = await self.auth_redis_service.retrieve(normalized_phone)

        if not user:
            logger.error(f"No authenticated user data found for {user_phone}")
            await self.whatsapp_service.send_message(
                user_phone,
                "Sorry, there was an error processing your request. Please try again."
            )
            return {"status": "error", "error": "User data not found"}

        # Set up the workflow state for RFQ selection - this ensures the seller service
        # routes to _handle_rfq_selection_response which processes the specific RFQ
        WorkflowManager.set_workflow_type(session, WorkflowType.seller_rfq_view, caller="seller_rfq_interest_handler")
        session.workflow_state = {
            "seller_workflow_state": "awaiting_rfq_selection"
        }

        # Initialize seller service and trigger the workflow with the specific RFQ ID
        seller_service = SellerService(
            whatsapp_service=self.whatsapp_service,
            session_manager=self.session_manager
        )

        # Trigger seller workflow with the specific RFQ ID as the message
        # The _handle_rfq_selection_response will extract and process this RFQ
        result = await seller_service.handle_seller_workflow(
            user=user,
            session=session,
            message=rfq_id
        )

        # Send the message if not already sent by the seller service
        if result.get("message") and not result.get("message_already_sent"):
            await self.whatsapp_service.send_message(user_phone, result["message"])

        return {
            "status": "redirected_to_seller_flow",
            "rfq_id": rfq_id,
            "seller_flow_result": result
        }

    async def handle_otp_validated(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Called after successful OTP validation to show intermediate buttons.

        This method is called from authentication_service.handle_email_otp_validation
        when the workflow type is seller_rfq_intimation.
        """
        rfq_id = session.workflow_state.get("rfq_id")
        seller_id = session.workflow_state.get("target_seller_id")

        if not rfq_id:
            logger.error(f"No rfq_id found in session for {user_phone}")
            return {"status": "error", "error": "RFQ ID not found in session"}

        return await self._show_intermediate_buttons(user_phone, rfq_id, seller_id, session)
