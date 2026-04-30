"""
Seller Authentication Mixin.

Shared authentication logic for seller notification handlers.
Used by both SellerRFQInterestHandler and BFSSellerBidHandler.

This mixin provides:
- Auth state checking (correct seller, wrong account, not authenticated)
- Account switch prompting with numbered choice (1/2)
- Switch response handling
- Seller email auto-lookup
- OTP flow initiation
"""

import logging
from typing import Dict, Any, Optional, Tuple

from app.services.openai_service import OpenAIService
from app.redis_db import get_auth_redis_service

logger = logging.getLogger(__name__)


class SellerAuthMixin:
    """
    Mixin providing seller authentication methods.

    Handlers using this mixin must have these attributes:
    - whatsapp_service
    - authentication_service
    - session_manager
    - otp_service

    And should implement:
    - _get_action_description() -> str: Returns action name for messages (e.g., "accept", "express interest")
    """

    def __init_auth_mixin__(self):
        """Initialize mixin dependencies. Call this in the handler's __init__."""
        self.openai_service = OpenAIService()
        self.auth_redis_service = get_auth_redis_service()

    async def check_seller_auth_state(self, user_phone: str, target_seller_id: str) -> Dict[str, Any]:
        """
        Check if user is authenticated and as the correct seller account.

        Args:
            user_phone: User's phone number
            target_seller_id: Target seller ID (org UUID) that should be authenticated

        Returns:
            Dict with:
                - state: "not_auth" | "correct_seller" | "wrong_account"
                - current_user: User object if authenticated, None otherwise
                - account_type: "buyer" | "seller" if wrong_account
        """
        try:
            normalized_phone = user_phone.lstrip('+')
            user_data = await self.auth_redis_service.retrieve(normalized_phone)

            if not user_data:
                return {"state": "not_auth", "current_user": None}

            is_self_client = user_data.self_client if hasattr(user_data, 'self_client') else False

            if is_self_client:
                # Currently authenticated as buyer
                return {
                    "state": "wrong_account",
                    "current_user": user_data,
                    "account_type": "buyer"
                }

            # Check if it's the correct seller
            current_org_id = user_data.org_id if hasattr(user_data, 'org_id') else None
            current_user_id = user_data.id if hasattr(user_data, 'id') else None

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
            return {"state": "not_auth", "current_user": None}

    async def prompt_seller_account_switch(
        self,
        user_phone: str,
        session,
        current_user: Any,
        action_description: str = "respond to this"
    ) -> Dict[str, Any]:
        """
        Ask user if they want to switch to the seller account.
        Uses numbered choice (1/2) pattern.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            current_user: Current authenticated User object
            action_description: Description of the action (e.g., "accept this bid")
        """
        logger.info(f"Prompting account switch for {user_phone}")

        # Update session state
        session.workflow_state["auth_stage"] = "switch_prompt"
        # Store as dict for session serialization
        session.workflow_state["current_user"] = current_user.dict() if hasattr(current_user, 'dict') else current_user

        # Determine current account type for message
        is_self_client = current_user.self_client if hasattr(current_user, 'self_client') else False
        current_account_type = "Buyer" if is_self_client else "Seller"
        current_email = current_user.email if hasattr(current_user, 'email') and current_user.email else "your current account"

        # Send switch prompt message with numbered choice
        switch_message = (
            f"You're currently logged in as a {current_account_type} ({current_email}).\n"
            f"To {action_description}, you need to use your Seller account.\n\n"
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
            "auth_stage": "switch_prompt"
        }

    async def handle_seller_switch_response(
        self,
        user_phone: str,
        session,
        message: str,
        decline_message: str = "No problem! You can try again when you're ready."
    ) -> Dict[str, Any]:
        """
        Process user's response to account switch prompt.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            message: User's response message
            decline_message: Message to show if user chooses to stay

        Returns:
            Dict with status. If "switch", caller should call initiate_seller_auth next.
        """
        logger.info(f"Handling switch response for {user_phone}: {message}")

        # Parse user's choice
        choice = await self._parse_switch_choice(message)
        logger.info(f"Parsed switch choice: {choice}")

        if choice == "switch":
            # User wants to switch - clear current auth token
            target_seller_id = session.workflow_state.get("target_seller_id")

            if self.authentication_service:
                await self.authentication_service.clear_user_token(user_phone)

            # Initiate seller auth
            return await self.initiate_seller_auth(user_phone, session, target_seller_id)

        elif choice == "stay":
            # User wants to stay with current account
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
        stay_patterns = ["2", "stay", "no", "current", "keep", "cancel", "quit"]
        for pattern in stay_patterns:
            if pattern in message_lower:
                return "stay"

        # Use AI for complex responses
        try:
            response = await self.openai_service.generate_response(
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

    async def initiate_seller_auth(
        self,
        user_phone: str,
        session,
        target_seller_id: str
    ) -> Dict[str, Any]:
        """
        Start OTP authentication for the specific seller account.
        Auto-looks up seller email - no need to ask the user.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            target_seller_id: Target seller ID to authenticate
        """
        logger.info(f"Initiating seller auth for {user_phone}, seller_id={target_seller_id}")

        # Update session state
        session.workflow_state["auth_stage"] = "otp"

        # Get seller email and user data
        seller_email, seller_user = await self.get_seller_info(user_phone, target_seller_id)

        if not seller_email:
            error_message = (
                "Sorry, we couldn't find the seller account associated with this notification. "
                "Please contact support for assistance."
            )
            # Reset workflow and show menu options
            await self._reset_workflow_with_menu(
                user_phone, session, error_message
            )

            return {
                "status": "seller_not_found",
                "error": "Could not find seller email",
                "workflow_reset": True
            }

        # Store seller info in session for OTP validation and post-OTP authentication
        session.workflow_state["target_seller_email"] = seller_email
        session.workflow_state["otp_email"] = seller_email
        session.workflow_state["target_seller_user"] = seller_user

        # Send OTP using existing OTP service
        if self.otp_service:
            otp_result = await self.otp_service.send_otp(user_phone, seller_email, session)

            if otp_result.get("status") == "otp_sent":
                if self.session_manager:
                    await self.session_manager.save_session(session)

                return {
                    "status": "otp_sent",
                    "auth_stage": "otp",
                    "seller_email": seller_email
                }
            else:
                error_message = (
                    "Sorry, we couldn't send the verification code. "
                    "Please try again later."
                )
                # Reset workflow and show menu options
                await self._reset_workflow_with_menu(
                    user_phone, session, error_message
                )

                return {
                    "status": "otp_send_failed",
                    "error": otp_result.get("error", "Unknown error"),
                    "workflow_reset": True
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
                "auth_stage": "otp"
            }

    async def get_seller_info(self, user_phone: str, target_seller_id: str) -> Tuple[Optional[str], Optional[Dict]]:
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

            # Find the exact seller matching target_seller_id
            # target_seller_id is the organization UUID (orgId), not user id
            for user in seller_users:
                user_id = user.get("id") or user.get("user_id")
                org_id = user.get("orgId") or user.get("org_id")

                # Match against both user id and org id
                if str(org_id) == str(target_seller_id) or str(user_id) == str(target_seller_id):
                    email = user.get("username") or user.get("email")
                    if email:
                        logger.info(f"Found seller email by orgId/ID match: {email} (orgId={org_id}, userId={user_id})")
                        return email, user

            logger.warning(f"No seller account matching orgId/ID {target_seller_id} found for phone {user_phone}")
            return None, None

        except Exception as e:
            logger.error(f"Error getting seller email for {user_phone}: {e}")
            return None, None

    async def _reset_workflow_with_menu(
        self,
        user_phone: str,
        session,
        message: str
    ) -> None:
        """
        Reset workflow and show appropriate menu options based on user type.

        Args:
            user_phone: User's phone number
            session: Current conversation session
            message: Message to display before menu options
        """
        # Get user type from current auth state
        user_type = await self._get_current_user_type(user_phone)

        # Clear workflow
        session.workflow_type = None
        session.workflow_state = {}

        if self.session_manager:
            await self.session_manager.save_session(session)

        # Send message with appropriate menu buttons
        if user_type == "buyer":
            buttons_config = [
                {"id": "create_rfq", "title": "Create new RFQ"},
                {"id": "rfq_status", "title": "Check RFQ Status"},
                {"id": "search_bfs", "title": "Search Ready Stocks"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                recipient_id=user_phone,
                body=message,
                buttons_config=buttons_config
            )
        elif user_type == "seller":
            buttons_config = [
                {"id": "view_rfqs", "title": "Other Active RFQs"},
                {"id": "rfq_status", "title": "My quoTe Status"},
                {"id": "contact_support", "title": "Contact Support"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                recipient_id=user_phone,
                body=message,
                buttons_config=buttons_config
            )
        else:
            # Unknown user type - just send message without buttons
            await self.whatsapp_service.send_message(user_phone, message)

        logger.info(f"Workflow reset with menu for {user_phone} (user_type={user_type})")

    async def _get_current_user_type(self, user_phone: str) -> Optional[str]:
        """
        Get current user type from Redis auth token.

        Returns:
            "buyer" or "seller" or None
        """
        try:
            normalized_phone = user_phone.lstrip('+')
            user_data = await self.auth_redis_service.retrieve(normalized_phone)

            if user_data:
                is_self_client = user_data.self_client if hasattr(user_data, 'self_client') else False
                return "buyer" if is_self_client else "seller"

            return None

        except Exception as e:
            logger.error(f"Error getting user type for {user_phone}: {e}")
            return None
