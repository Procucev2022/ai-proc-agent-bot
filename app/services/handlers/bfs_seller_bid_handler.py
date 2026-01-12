"""
BFS Seller Bid Handler.

Handles seller authentication and bid Accept/Reject flow when clicking buttons
on BFS bid notifications.

Flow:
1. Seller clicks "Accept Bid" or "Reject Bid"
2. Check if authenticated as the correct seller account
3. If not authenticated -> initiate seller auth (auto-lookup email, send OTP)
4. If wrong account -> prompt to switch (numbered choice 1/2)
5. If correct account -> call Accept/Reject API

Uses SellerAuthMixin for shared authentication logic with SellerRFQInterestHandler.
"""

import logging
from typing import Dict, Any

from app.models import WorkflowType, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.procucev_apis.bfs_apis import get_bfs_api_service
from app.config import get_settings
from app.services.handlers.seller_auth_mixin import SellerAuthMixin

logger = logging.getLogger(__name__)


class BFSSellerBidHandler(SellerAuthMixin):
    """
    Handles seller bid Accept/Reject flow for BFS notifications.

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
        self.settings = get_settings()
        self.bfs_api_service = get_bfs_api_service()

        # Initialize mixin dependencies
        self.__init_auth_mixin__()

    async def handle_accept_bid_click(
        self,
        user_phone: str,
        bfs_user_uuid: str,
        seller_id: str,
        session: ConversationSession
    ) -> Dict[str, Any]:
        """
        Handle seller clicking 'Accept Bid' on BFS notification.

        Args:
            user_phone: User's phone number
            bfs_user_uuid: The bfs_users record UUID for API call
            seller_id: Target seller ID that should be authenticated
            session: Current conversation session

        Returns:
            Dictionary with flow status and messages sent
        """
        logger.info(f"Handling BFS Accept Bid: phone={user_phone}, bfs_uuid={bfs_user_uuid}, seller_id={seller_id}")
        return await self._handle_bid_action(user_phone, bfs_user_uuid, seller_id, session, is_accept=True)

    async def handle_reject_bid_click(
        self,
        user_phone: str,
        bfs_user_uuid: str,
        seller_id: str,
        session: ConversationSession
    ) -> Dict[str, Any]:
        """
        Handle seller clicking 'Reject Bid' on BFS notification.

        Args:
            user_phone: User's phone number
            bfs_user_uuid: The bfs_users record UUID for API call
            seller_id: Target seller ID that should be authenticated
            session: Current conversation session

        Returns:
            Dictionary with flow status and messages sent
        """
        logger.info(f"Handling BFS Reject Bid: phone={user_phone}, bfs_uuid={bfs_user_uuid}, seller_id={seller_id}")
        return await self._handle_bid_action(user_phone, bfs_user_uuid, seller_id, session, is_accept=False)

    async def _handle_bid_action(
        self,
        user_phone: str,
        bfs_user_uuid: str,
        seller_id: str,
        session: ConversationSession,
        is_accept: bool
    ) -> Dict[str, Any]:
        """
        Common handler for both Accept and Reject actions.

        Args:
            user_phone: User's phone number
            bfs_user_uuid: The bfs_users record UUID
            seller_id: Target seller ID
            session: Current conversation session
            is_accept: True for Accept, False for Reject

        Returns:
            Dictionary with flow status
        """
        action_name = "accept" if is_accept else "reject"

        # Validate inputs
        if not bfs_user_uuid or not seller_id:
            logger.error(f"Invalid BFS bid button - bfs_uuid={bfs_user_uuid}, seller_id={seller_id}")
            await self.whatsapp_service.send_message(
                recipient_id=user_phone,
                message="Sorry, there was an error processing your request. Please contact support."
            )
            return {"status": "error", "error": "Invalid button format"}

        # Set workflow state for tracking
        WorkflowManager.set_workflow_type(session, WorkflowType.bfs_seller_bid,
                                          caller="bfs_seller_bid_handler")
        session.workflow_state = {
            "bfs_user_uuid": bfs_user_uuid,
            "target_seller_id": seller_id,
            "action": action_name,
            "auth_stage": "checking"
        }

        # Check current authentication state (from mixin)
        auth_state = await self.check_seller_auth_state(user_phone, seller_id)
        logger.info(f"Auth state for {user_phone}: {auth_state}")

        if auth_state["state"] == "correct_seller":
            # Authenticated as correct seller - proceed with API call
            return await self._execute_bid_action(user_phone, bfs_user_uuid, is_accept, session)

        elif auth_state["state"] == "not_auth":
            # Not authenticated - initiate seller auth (from mixin)
            return await self.initiate_seller_auth(user_phone, session, seller_id)

        else:
            # Authenticated as wrong account - prompt switch (from mixin)
            return await self.prompt_seller_account_switch(
                user_phone, session, auth_state.get("current_user"),
                action_description=f"{action_name} this bid"
            )

    async def _execute_bid_action(
        self,
        user_phone: str,
        bfs_user_uuid: str,
        is_accept: bool,
        session: ConversationSession
    ) -> Dict[str, Any]:
        """
        Execute the Accept or Reject API call.
        """
        action_name = "accept" if is_accept else "reject"

        try:
            if is_accept:
                result = await self.bfs_api_service.accept_bid_by_seller(bfs_user_uuid)
            else:
                result = await self.bfs_api_service.reject_bid_by_seller(bfs_user_uuid)

            if result.get('success'):
                if is_accept:
                    message = "*Bid Accepted!*\n\nYou have accepted the buyer's offer. The buyer will be notified and you can proceed with the transaction."
                else:
                    message = "*Bid Rejected*\n\nYou have declined this offer. The buyer will be notified."

                await self.whatsapp_service.send_message(
                    recipient_id=user_phone,
                    message=message
                )

                # Clear workflow state
                session.workflow_type = None
                session.workflow_state = {}
                if self.session_manager:
                    await self.session_manager.save_session(session, session.workflow_type)

                logger.info(f"BFS bid {action_name} successful: {bfs_user_uuid}")
                return {"status": f"bfs_bid_{action_name}ed", "bfs_user_uuid": bfs_user_uuid}

            else:
                error_msg = result.get('error', 'Unknown error')
                await self.whatsapp_service.send_message(
                    recipient_id=user_phone,
                    message=f"Could not {action_name} the bid: {error_msg}\n\nPlease try again or contact support."
                )
                logger.error(f"BFS bid {action_name} failed: {error_msg}")
                return {"status": "error", "error": error_msg}

        except Exception as e:
            logger.error(f"Exception in BFS bid {action_name}: {e}")
            await self.whatsapp_service.send_message(
                recipient_id=user_phone,
                message="Sorry, an error occurred. Please try again later."
            )
            return {"status": "error", "error": str(e)}

    async def handle_switch_response(
        self,
        user_phone: str,
        session: ConversationSession,
        message: str
    ) -> Dict[str, Any]:
        """
        Process user's response to account switch prompt.
        Delegates to mixin with BFS-specific decline message.
        """
        action_name = session.workflow_state.get("action", "respond to")
        decline_message = (
            f"No problem! You can click the \"{action_name.title()} Bid\" button again "
            "when you're ready to respond as a Seller."
        )

        return await self.handle_seller_switch_response(
            user_phone, session, message, decline_message=decline_message
        )

    async def handle_otp_validated(self, user_phone: str, session: ConversationSession) -> Dict[str, Any]:
        """
        Called after successful OTP validation to execute the bid action.

        This method is called from chat_service when the workflow type is bfs_seller_bid
        and OTP has been validated.
        """
        bfs_user_uuid = session.workflow_state.get("bfs_user_uuid")
        action = session.workflow_state.get("action", "accept")
        is_accept = action == "accept"

        if not bfs_user_uuid:
            logger.error(f"No bfs_user_uuid found in session for {user_phone}")
            return {"status": "error", "error": "BFS user UUID not found in session"}

        return await self._execute_bid_action(user_phone, bfs_user_uuid, is_accept, session)
