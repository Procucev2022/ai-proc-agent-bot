"""
Seller RFQ Interest Handler.

Handles seller authentication flow when clicking 'I'm Interested' on RFQ notification.
This handler manages the seller_rfq_intimation workflow type which can ONLY be
initiated from the "I'm Interested" button handler.
"""

import logging
from typing import Dict, Any, Optional, Tuple
from app.models import WorkflowType, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.services.openai_service import OpenAIService
from app.redis_db import get_auth_redis_service

logger = logging.getLogger(__name__)

# Portal URL for RFQ details
RFQ_PORTAL_BASE_URL = "https://p2pdevuiindia.azurewebsites.net"


class SellerRFQInterestHandler:
    """
    Handles seller authentication flow when clicking 'I'm Interested' on RFQ notification.

    IMPORTANT: This handler manages the seller_rfq_intimation workflow type.
    This workflow can ONLY be initiated from the button handler in interactive_message_processor.
    """

    def __init__(self, whatsapp_service: WhatsAppService, authentication_service=None,
                 session_manager=None, otp_service=None):
        self.whatsapp_service = whatsapp_service
        self.authentication_service = authentication_service
        self.session_manager = session_manager
        self.otp_service = otp_service
        self.openai_service = OpenAIService()
        self.auth_redis_service = get_auth_redis_service()

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

        # 2. Check current authentication state
        auth_state = await self._check_auth_state(user_phone, seller_id)
        logger.info(f"Auth state for {user_phone}: {auth_state}")

        # 3. Route based on auth state
        if auth_state["state"] == "correct_seller":
            # Already authenticated as correct seller - send portal link directly
            return await self._send_portal_link(user_phone, rfq_id, session)

        elif auth_state["state"] == "not_auth":
            # Not authenticated - start OTP auth for target seller
            return await self._initiate_seller_auth(user_phone, session, seller_id, rfq_id)

        else:
            # Authenticated as wrong account (buyer or different seller) - prompt switch
            return await self._prompt_account_switch(user_phone, session, seller_id,
                                                     auth_state.get("current_user"))

    async def _check_auth_state(self, user_phone: str, target_seller_id: str) -> Dict[str, Any]:
        """
        Check if user is authenticated and as the correct seller account.

        Returns:
            Dict with:
                - state: "not_auth" | "correct_seller" | "wrong_account"
                - current_user: User object if authenticated, None otherwise
        """
        try:
            # Check Redis for existing auth token
            normalized_phone = user_phone.lstrip('+')
            user_data = await self.auth_redis_service.retrieve(normalized_phone)

            if not user_data:
                return {"state": "not_auth", "current_user": None}

            # user_data is a User Pydantic object, access attributes directly
            is_self_client = user_data.self_client

            if is_self_client:
                # Currently authenticated as buyer
                return {
                    "state": "wrong_account",
                    "current_user": user_data,
                    "account_type": "buyer"
                }

            # Check if it's the correct seller (compare seller_id with org_id)
            # The seller_id from button is the organization UUID
            current_user_id = user_data.id
            current_org_id = user_data.org_id

            # Note: target_seller_id is the organization UUID, match against org_id
            if current_org_id == target_seller_id or current_user_id == target_seller_id:
                return {"state": "correct_seller", "current_user": user_data}

            # Different seller account
            return {
                "state": "wrong_account",
                "current_user": user_data,
                "account_type": "seller"
            }

        except Exception as e:
            logger.error(f"Error checking auth state for {user_phone}: {e}")
            # On error, assume not authenticated to be safe
            return {"state": "not_auth", "current_user": None}

    async def _prompt_account_switch(self, user_phone: str, session: ConversationSession,
                                     target_seller_id: str, current_user: Any) -> Dict[str, Any]:
        """
        Ask user if they want to switch to the seller account.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            target_seller_id: Target seller ID to switch to
            current_user: Current authenticated User object
        """
        logger.info(f"Prompting account switch for {user_phone}")

        # Update session state
        session.workflow_state["auth_stage"] = "switch_prompt"
        # Store as dict for session serialization
        session.workflow_state["current_user"] = current_user.dict() if hasattr(current_user, 'dict') else current_user

        # Determine current account type for message (current_user is a User Pydantic object)
        is_self_client = current_user.self_client if hasattr(current_user, 'self_client') else False
        current_account_type = "Buyer" if is_self_client else "Seller"
        current_email = current_user.email if hasattr(current_user, 'email') and current_user.email else "your current account"

        # Send switch prompt message
        switch_message = (
            f"You're currently logged in as a {current_account_type} ({current_email}).\n"
            f"To express interest in this RFQ, you need to use your Seller account.\n\n"
            f"1. Switch to Seller account\n"
            f"2. Stay with current account\n\n"
            f"Reply 1 or 2:"
        )

        await self.whatsapp_service.send_message(user_phone, switch_message)

        # Save session
        if self.session_manager:
            await self.session_manager.save_session(session)

        return {
            "status": "switch_prompt_sent",
            "auth_stage": "switch_prompt",
            "rfq_id": session.workflow_state.get("rfq_id")
        }

    async def handle_switch_response(self, user_phone: str, session: ConversationSession,
                                     message: str) -> Dict[str, Any]:
        """
        Process user's response to account switch prompt.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            message: User's response message
        """
        logger.info(f"Handling switch response for {user_phone}: {message}")

        # Parse user's choice
        choice = await self._parse_switch_choice(message)
        logger.info(f"Parsed switch choice: {choice}")

        if choice == "switch":
            # User wants to switch - initiate seller auth
            target_seller_id = session.workflow_state.get("target_seller_id")
            rfq_id = session.workflow_state.get("rfq_id")

            # Clear current auth token before switching
            if self.authentication_service:
                await self.authentication_service.clear_user_token(user_phone)

            return await self._initiate_seller_auth(user_phone, session, target_seller_id, rfq_id)

        elif choice == "stay":
            # User wants to stay with current account - clear workflow and do nothing
            decline_message = (
                "No problem! You can click the \"I'm Interested\" button again "
                "when you're ready to respond as a Seller."
            )
            await self.whatsapp_service.send_message(user_phone, decline_message)

            # Clear workflow
            session.workflow_type = None
            session.workflow_state = {}

            if self.session_manager:
                await self.session_manager.save_session(session)

            return {
                "status": "switch_declined",
                "workflow_cleared": True
            }

        else:
            # Unclear response - ask again
            retry_message = (
                "I didn't understand your response. Please reply with:\n"
                "1 - to switch to your Seller account\n"
                "2 - to stay with your current account"
            )
            await self.whatsapp_service.send_message(user_phone, retry_message)

            return {
                "status": "switch_response_unclear",
                "auth_stage": "switch_prompt"
            }

    async def _parse_switch_choice(self, message: str) -> str:
        """
        Parse user's switch choice from their message.

        Returns:
            "switch" - User wants to switch accounts
            "stay" - User wants to stay with current account
            "unclear" - Cannot determine user's choice
        """
        message_lower = message.strip().lower()

        # Check for switch indicators
        switch_patterns = ["1", "switch", "yes", "seller", "change"]
        for pattern in switch_patterns:
            if pattern in message_lower:
                return "switch"

        # Check for stay indicators
        stay_patterns = ["2", "stay", "no", "current", "keep"]
        for pattern in stay_patterns:
            if pattern in message_lower:
                return "stay"

        # Use AI for complex responses
        try:
            response = self.openai_service.generate_response(
                context={
                    "message": message,
                    "options": "1. Switch to Seller account, 2. Stay with current account"
                },
                query_results=[],
                prompt_file="auth/switch_choice_parsing"
            )
            response_lower = response.strip().lower()
            if "switch" in response_lower or "1" in response_lower:
                return "switch"
            elif "stay" in response_lower or "2" in response_lower:
                return "stay"
        except Exception as e:
            logger.warning(f"AI parsing failed for switch choice: {e}")

        return "unclear"

    async def _initiate_seller_auth(self, user_phone: str, session: ConversationSession,
                                    target_seller_id: str, rfq_id: str) -> Dict[str, Any]:
        """
        Start OTP authentication for the specific seller account.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            target_seller_id: Target seller ID to authenticate
            rfq_id: RFQ ID for post-auth redirect
        """
        logger.info(f"Initiating seller auth for {user_phone}, seller_id={target_seller_id}")

        # Update session state
        session.workflow_state["auth_stage"] = "otp"

        # Get seller email and user data from database or API
        seller_email, seller_user = await self._get_seller_info(user_phone, target_seller_id)

        if not seller_email:
            error_message = (
                "Sorry, we couldn't find the seller account associated with this notification. "
                "Please contact support for assistance."
            )
            await self.whatsapp_service.send_message(user_phone, error_message)

            # Clear workflow
            session.workflow_type = None
            session.workflow_state = {}

            return {
                "status": "seller_not_found",
                "error": "Could not find seller email"
            }

        # Store seller info in session for OTP validation and post-OTP authentication
        session.workflow_state["target_seller_email"] = seller_email
        session.workflow_state["otp_email"] = seller_email
        session.workflow_state["target_seller_user"] = seller_user  # Store user data for authentication after OTP

        # Send OTP using existing OTP service
        if self.otp_service:
            otp_result = await self.otp_service.send_otp(user_phone, seller_email, session)

            if otp_result.get("status") == "otp_sent":
                if self.session_manager:
                    await self.session_manager.save_session(session)

                return {
                    "status": "otp_sent",
                    "auth_stage": "otp",
                    "rfq_id": rfq_id,
                    "seller_email": seller_email
                }
            else:
                error_message = (
                    "Sorry, we couldn't send the verification code. "
                    "Please try again later."
                )
                await self.whatsapp_service.send_message(user_phone, error_message)

                return {
                    "status": "otp_send_failed",
                    "error": otp_result.get("error", "Unknown error")
                }
        else:
            # No OTP service available - send OTP message manually
            otp_message = (
                f"To verify your identity as a seller, please enter the verification code "
                f"sent to {seller_email}."
            )
            await self.whatsapp_service.send_message(user_phone, otp_message)

            if self.session_manager:
                await self.session_manager.save_session(session)

            return {
                "status": "otp_instruction_sent",
                "auth_stage": "otp",
                "rfq_id": rfq_id
            }

    async def _get_seller_info(self, user_phone: str, target_seller_id: str) -> Tuple[Optional[str], Optional[Dict]]:
        """
        Get seller email and user data for the target seller ID.

        This method attempts to find the seller by:
        1. Looking up user profiles by phone number
        2. Finding the seller profile matching target_seller_id

        Returns:
            Tuple of (email, user_data) - both None if not found
        """
        try:
            if not self.authentication_service:
                logger.warning("No authentication service available for seller email lookup")
                return None, None

            # Get all users for this phone number
            auth_result = await self.authentication_service.user_authenticate(
                user_phone, "", None, intent="sell_something"
            )

            if not auth_result.get("success"):
                logger.warning(f"Could not authenticate user {user_phone} for email lookup")
                return None, None

            raw_users = auth_result.get("response", [])

            # Filter to sellers (selfClient = False)
            seller_users = [
                user for user in raw_users
                if not (user.get("selfClient") or user.get("self_client"))
            ]

            if not seller_users:
                logger.warning(f"No seller accounts found for phone {user_phone}")
                return None, None

            # Find the exact seller that received the RFQ notification
            # target_seller_id is the organization UUID (orgId), not user id
            for user in seller_users:
                user_id = user.get("id") or user.get("user_id")
                org_id = user.get("orgId") or user.get("org_id")

                # Match against both user id and org id (seller_id from button is typically orgId)
                if str(org_id) == str(target_seller_id) or str(user_id) == str(target_seller_id):
                    email = user.get("username") or user.get("email")
                    if email:
                        logger.info(f"Found seller email by orgId/ID match: {email} (orgId={org_id}, userId={user_id})")
                        return email, user

            # No matching seller found - return None (will trigger error message)
            logger.warning(f"No seller account matching orgId/ID {target_seller_id} found for phone {user_phone}")
            return None, None

        except Exception as e:
            logger.error(f"Error getting seller email for {user_phone}: {e}")
            return None, None

    async def _send_portal_link(self, user_phone: str, rfq_id: str,
                                session: ConversationSession) -> Dict[str, Any]:
        """
        Send portal link message after successful authentication.

        Args:
            user_phone: User's phone number
            rfq_id: RFQ ID to include in portal link
            session: Current conversation session
        """

        success_message = (
            f"View full details and submit your quote:\n{RFQ_PORTAL_BASE_URL}"
        )

        await self.whatsapp_service.send_message(user_phone, success_message)

        # Clear workflow after sending link
        session.workflow_type = None
        session.workflow_state = {}

        if self.session_manager:
            await self.session_manager.save_session(session)

        return {
            "status": "portal_link_sent",
            "rfq_id": rfq_id,
            "portal_url": RFQ_PORTAL_BASE_URL,
            "workflow_cleared": True
        }

    async def handle_otp_validated(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Called after successful OTP validation to send portal link.

        This method is called from authentication_service.handle_email_otp_validation
        when the workflow type is seller_rfq_intimation.
        """
        rfq_id = session.workflow_state.get("rfq_id")

        if not rfq_id:
            logger.error(f"No rfq_id found in session for {user_phone}")
            return {"status": "error", "error": "RFQ ID not found in session"}

        return await self._send_portal_link(user_phone, rfq_id, session)
