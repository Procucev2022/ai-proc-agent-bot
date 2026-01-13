"""
BFS Search Handler

Handles BFS (Buy From Stock) search workflow.
Simplified flow: extract products -> categorize -> call API directly.
"""

import logging
from typing import Dict, Any, List
from app.models import User, ConversationSession
from app.services.openai_service import OpenAIService
from app.services.whatsapp_service import WhatsAppService
from app.services.auto_categorization_service import get_auto_categorization_service
from app.procucev_apis.bfs_apis import get_bfs_api_service
from app.utils.bfs_bid_format_parser import (
    generate_bid_format,
    parse_bid_format,
    generate_bid_summary,
    MAX_BID_RETRY_ATTEMPTS
)

logger = logging.getLogger(__name__)


class BFSSearchHandler:
    """Handler for BFS stock search workflow."""

    def __init__(self, whatsapp_service: WhatsAppService, session_manager):
        self.whatsapp_service = whatsapp_service
        self.session_manager = session_manager
        self.openai_service = OpenAIService()
        self.auto_categorization_service = get_auto_categorization_service()
        self.bfs_api_service = get_bfs_api_service()

    # ==================== Button Handler ====================

    async def handle_button(
        self,
        user: User,
        session: ConversationSession,
        button_id: str
    ) -> Dict[str, Any]:
        """
        Handle all BFS-related button clicks.

        Returns status dict. Some statuses require further handling by chat_service:
        - "bfs_activate_rfq": chat_service should call _activate_sectioned_rfq
        - "bfs_send_cancel_message": chat_service should call cancel_service
        """
        if button_id == "search_bfs":
            # Prompt user to describe what they want to search
            await self.whatsapp_service.send_message(
                user.phone_number,
                "What products are you looking for?",
                session_id=session
            )

            if not session.workflow_state:
                session.workflow_state = {}
            session.workflow_state["bfs_search_pending"] = True
            await self.session_manager.save_session(session, persist_to_db=False)

            return {"status": "bfs_awaiting_product_description"}

        elif button_id in ("bfs_negotiate", "bfs_place_bid"):
            logger.info(f"[BFS] {button_id} button clicked")
            return await self.initiate_bid_flow(user, session)

        elif button_id == "bfs_raise_rfq":
            logger.info(f"[BFS] Raising RFQ from BFS search")
            # Prepare session state for RFQ
            searched_products = session.workflow_state.get("bfs_searched_products", []) if session.workflow_state else []

            if session.workflow_state:
                session.workflow_state.pop("bfs_results", None)
                session.workflow_state.pop("bfs_searched_products", None)

            if searched_products:
                if not session.workflow_state:
                    session.workflow_state = {}
                session.workflow_state["bfs_rfq_products"] = searched_products
                await self.session_manager.save_session(session, persist_to_db=False)

            # Signal chat_service to activate RFQ workflow
            return {"status": "bfs_activate_rfq"}

        elif button_id == "bfs_cancel":
            logger.info(f"[BFS] Cancelling BFS workflow")

            if session.workflow_state:
                session.workflow_state.pop("bfs_results", None)
                session.workflow_state.pop("bfs_search_pending", None)
                session.workflow_state.pop("bfs_searched_products", None)
                await self.session_manager.save_session(session, persist_to_db=False)

            # Signal chat_service to send cancellation message
            return {"status": "bfs_send_cancel_message"}

        elif button_id == "bfs_bid_cancel":
            logger.info(f"[BFS] Cancelling BFS bid flow")
            await self._clear_bid_state(session, clear_bfs_search=True)
            return {"status": "bfs_send_cancel_message"}

        elif button_id == "bfs_restart":
            logger.info(f"[BFS] Restart button clicked - clearing state and returning to menu")
            await self._clear_bid_state(session, clear_bfs_search=True)
            return {"status": "bfs_send_cancel_message"}

        return {"status": "unknown_bfs_button", "button_id": button_id}

    # ==================== BFS Search Methods ====================

    async def handle_bfs_search(
        self,
        user: User,
        session: ConversationSession,
        message: str,
        suppress_raise_rfq_on_no_results: bool = False,
    ) -> Dict[str, Any]:
        """
        Main entry point for BFS search.
        Extracts products, categorizes them, and calls the API directly.
        """
        logger.info(f"[BFS] Handling BFS search for user {user.phone_number}")

        # Extract product descriptions from message
        products = await self._extract_entities(message)

        if not products:
            logger.warning(f"[BFS] No products extracted from message")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "I couldn't identify any products to search. Please describe what you're looking for.",
                session_id=session
            )
            return {"status": "no_products_found"}

        # Store original searched products in session for RFQ pre-population
        if not session.workflow_state:
            session.workflow_state = {}
        session.workflow_state["bfs_searched_products"] = [p.get("description", "") for p in products]
        await self.session_manager.save_session(session, persist_to_db=False)

        # Build API payload with categorization
        payload = await self._build_api_payload(products, str(user.id), session.session_id)

        # Log the payload for debugging
        logger.info(f"[BFS] API payload: {payload}")

        # Call BFS API
        api_response = await self.bfs_api_service.search_bfs_items(payload)

        if api_response.get("success"):
            # Format and send results
            await self._send_bfs_results(
                user,
                session,
                api_response.get("data"),
                suppress_raise_rfq_on_no_results=suppress_raise_rfq_on_no_results,
            )
            return {"status": "bfs_search_completed", "data": api_response.get("data")}
        else:
            # Handle API error
            error_msg = api_response.get("error", "Unknown error")
            logger.error(f"[BFS] API error: {error_msg}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Sorry, we couldn't complete the stock search at this time. Please try again later.",
                session_id=session
            )
            return {"status": "bfs_search_failed", "error": error_msg}

    async def _extract_entities(self, message: str) -> List[Dict]:
        """Extract product entities from message (description only)."""
        try:
            result = await self.openai_service.extract_entities(
                message=message,
                workflow_type="bfs"
            )

            if result.get("success") and result.get("products"):
                # Only keep description from extracted products
                products = [{"description": p.get("description", "")} for p in result["products"]]
                # Filter out empty descriptions
                return [p for p in products if p.get("description")]

            # Fallback - create single product from message
            return [{"description": message}]

        except Exception as e:
            logger.error(f"[BFS] Entity extraction failed: {e}")
            return [{"description": message}]

    async def _build_api_payload(
        self,
        products: List[Dict],
        user_id: str,
        session_id: str
    ) -> List[Dict]:
        """Build API payload with categorization (description array format)."""
        payload = []

        for product in products:
            description = product.get("description", "")

            # Build description array with original description and category
            description_array = [description]
            try:
                cat_result = await self.auto_categorization_service.categorize_item(
                    item_description=description,
                    user_id=user_id,
                    session_id=session_id
                )
                logger.info(f"[BFS] Categorization result for '{description}': {cat_result}")
                if cat_result.get("success"):
                    # Add category as synonym
                    category = cat_result.get("category", "")
                    if category and category.lower() != description.lower():
                        description_array.append(category)
                        logger.info(f"[BFS] Added category '{category}' to description array")
                    else:
                        logger.info(f"[BFS] Category '{category}' same as description or empty, not added")
                else:
                    logger.warning(f"[BFS] Categorization not successful: {cat_result.get('reason', 'unknown')}")
            except Exception as e:
                logger.warning(f"[BFS] Categorization failed: {e}")

            payload.append({
                "description": description_array
            })

        return payload

    async def _send_bfs_results(
        self,
        user: User,
        session: ConversationSession,
        data: Any,
        suppress_raise_rfq_on_no_results: bool = False,
    ) -> None:
        """Format and send BFS search results to user with action buttons."""
        try:
            if not data or (isinstance(data, list) and len(data) == 0):
                if suppress_raise_rfq_on_no_results:
                    await self.whatsapp_service.send_message(
                        user.phone_number,
                        "Item not available in BFS.",
                        session_id=session
                    )
                    return

                # Send message with Create new RFQ and Cancel buttons when no products found
                no_results_message = "No items found in stock matching your search."

                buttons = [
                    {"id": "bfs_raise_rfq", "title": "Create new RFQ"},
                    {"id": "bfs_cancel", "title": "Cancel"}
                ]

                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    no_results_message,
                    buttons,
                    header="No Stock Available",
                    footer="",
                    session_id=session
                )
                return

            # Store results in session for later use
            if not session.workflow_state:
                session.workflow_state = {}
            session.workflow_state["bfs_results"] = data
            await self.session_manager.save_session(session, persist_to_db=False)

            # Format results compactly (max 1024 chars)
            if isinstance(data, list):
                result_message = f"*{len(data)} item(s) in stock:*\n"

                for idx, item in enumerate(data[:5], 1):
                    if isinstance(item, dict):
                        desc = (item.get("description") or "N/A")[:25]
                        spec = item.get("specification")
                        qty = int(item.get("availableQuantity") or 0)
                        age = item.get("ageOfAsset")
                        price = item.get("sellPrice") or 0

                        # Build details with available info (tab indented)
                        result_message += f"\n{idx}. *{desc}*"
                        if spec:
                            result_message += f"\n\t{spec[:15]}"
                        result_message += f"\n\t*Qty:* {qty}"
                        if age:
                            result_message += f"\n\tAge: {age}yr"
                        result_message += f"\n\t*₹{price:,.0f}*"

                buttons = [
                    {"id": "bfs_negotiate", "title": "Place Bid"},
                    {"id": "bfs_cancel", "title": "Cancel"}
                ]

                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    result_message,
                    buttons,
                    header="Stock Available",
                    footer="",
                    session_id=session
                )
            else:
                await self.whatsapp_service.send_message(
                    user.phone_number,
                    "Stock search completed.",
                    session_id=session
                )

        except Exception as e:
            logger.error(f"[BFS] Error sending results: {e}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Error displaying results. Please try again.",
                session_id=session
            )

    # ==================== BFS Bid Workflow Methods ====================

    async def initiate_bid_flow(
        self,
        user: User,
        session: ConversationSession
    ) -> Dict[str, Any]:
        """
        Initiate BFS bid flow - show items in editable format.
        Called when user clicks "Place Bid" button.
        """
        logger.info(f"[BFS Bid] Initiating bid flow for user {user.phone_number}")

        # Get BFS results from session
        bfs_results = session.workflow_state.get("bfs_results", []) if session.workflow_state else []

        if not bfs_results:
            await self.whatsapp_service.send_message(
                user.phone_number,
                "No items available to bid on. Please search again.",
                session_id=session
            )
            return {"status": "bfs_no_items_to_bid"}

        # Generate bid format
        bid_format = generate_bid_format(bfs_results)

        # Set session state for bid flow
        if not session.workflow_state:
            session.workflow_state = {}
        session.workflow_state["bfs_bid_stage"] = "format_input"
        session.workflow_state["bfs_bid_retry_count"] = 0
        await self.session_manager.save_session(session, persist_to_db=False)

        # Send format message with Cancel button
        message = (
            "*Place Bid*\n\n"
            "Copy the format below, modify the prices, and remove items you don't want to bid on:\n\n"
            f"{bid_format}"
        )

        buttons = [{"id": "bfs_bid_cancel", "title": "Cancel"}]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons,
            header="Place Your Bid",
            footer="Edit prices below, then send",
            session_id=session
        )

        logger.info(f"[BFS Bid] Sent bid format with {len(bfs_results)} items")
        return {"status": "bfs_awaiting_bid_format"}

    async def handle_bid_format_input(
        self,
        user: User,
        session: ConversationSession,
        message: str
    ) -> Dict[str, Any]:
        """
        Handle user's bid format input.
        Parses the format and proceeds to OTP if valid.
        """
        logger.info(f"[BFS Bid] Processing bid format input: {message[:50]}...")

        # Get original BFS results for validation
        bfs_results = session.workflow_state.get("bfs_results", []) if session.workflow_state else []

        if not bfs_results:
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Session expired. Please search again.",
                session_id=session
            )
            await self._clear_bid_state(session)
            return {"status": "bfs_bid_session_expired"}

        # Parse the bid format
        parse_result = parse_bid_format(message, bfs_results)

        if "error" in parse_result:
            # Invalid format - increment retry count
            retry_count = session.workflow_state.get("bfs_bid_retry_count", 0) + 1
            session.workflow_state["bfs_bid_retry_count"] = retry_count

            if retry_count >= MAX_BID_RETRY_ATTEMPTS:
                # Max retries reached - cancel flow
                return await self._cancel_bid_after_max_retries(user, session)

            # Send error message with Restart button
            error_msg = (
                f"{parse_result['error']}\n\n"
                f"Attempt {retry_count}/{MAX_BID_RETRY_ATTEMPTS}\n\n"
                "_Please copy and paste the format again with correct prices._"
            )
            buttons = [{"id": "bfs_restart", "title": "Restart"}]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons,
                header="Invalid Format",
                footer="",
                session_id=session
            )
            await self.session_manager.save_session(session, persist_to_db=False)
            return {"status": "bfs_bid_format_invalid", "retry_count": retry_count}

        # Valid format - store bids and proceed to OTP
        bid_items = parse_result["bids"]
        session.workflow_state["bfs_bid_items"] = bid_items
        session.workflow_state["bfs_bid_retry_count"] = 0  # Reset for OTP
        await self.session_manager.save_session(session, persist_to_db=False)

        logger.info(f"[BFS Bid] Parsed {len(bid_items)} valid bids, proceeding to OTP")
        return await self._send_bid_otp(user, session, bid_items)

    async def _send_bid_otp(
        self,
        user: User,
        session: ConversationSession,
        bid_items: List[Dict]
    ) -> Dict[str, Any]:
        """
        Send OTP to user's email for bid confirmation.
        Skips OTP if user has validated within the last 30 minutes.
        """
        from app.services.otp_service import OTPService
        from app.procucev_apis.register_apis import RegisterAPIService
        from app.services.support_notification_service import SupportNotificationService
        from app.redis_db import get_auth_redis_service
        import time

        # Get user data from Redis
        auth_redis = get_auth_redis_service()
        normalized_phone = user.phone_number.lstrip('+')
        user_data = await auth_redis.retrieve(normalized_phone)

        # Check if OTP was validated recently (within 30 minutes) - skip OTP if so
        if user_data and user_data.otp_validated_at:
            elapsed_time = time.time() - user_data.otp_validated_at
            if elapsed_time < 1800:  # 30 minutes
                logger.info(f"[BFS Bid] OTP validated {elapsed_time:.0f}s ago, skipping OTP for {len(bid_items)} bids")
                return await self._submit_bids_to_api(user, session)

        logger.info(f"[BFS Bid] Sending OTP for {len(bid_items)} bid items")

        if not user_data or not user_data.email:
            logger.error(f"[BFS Bid] No email found for user {user.phone_number}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Unable to verify your account. Please contact support.",
                session_id=session
            )
            await self._clear_bid_state(session)
            return {"status": "bfs_bid_no_email"}

        user_email = user_data.email

        # Initialize OTP service and send OTP (without default notification - we send our own)
        otp_service = OTPService(
            register_api_service=RegisterAPIService(),
            whatsapp_service=self.whatsapp_service,
            support_notification_service=SupportNotificationService()
        )

        otp_result = await otp_service.send_otp(user.phone_number, user_email, session, send_notification=False)

        if otp_result.get("status") == "otp_sent":
            # Update session state
            session.workflow_state["bfs_bid_stage"] = "otp_pending"
            session.workflow_state["otp_email"] = user_email
            await self.session_manager.save_session(session, persist_to_db=False)

            # Generate bid summary for display
            bid_summary = generate_bid_summary(bid_items)

            # Send single OTP message with bid summary and Cancel button
            otp_message = (
                "*Verify your bids:*\n\n"
                f"{bid_summary}\n\n"
                f"OTP sent to {user_email}.\n"
                "Please enter the OTP to confirm your bids."
            )
            buttons = [{"id": "bfs_bid_cancel", "title": "Cancel"}]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                otp_message,
                buttons,
                header="",
                footer="",
                session_id=session
            )

            logger.info(f"[BFS Bid] OTP sent to {user_email}")
            return {"status": "bfs_bid_otp_sent", "email": user_email}
        else:
            # OTP send failed
            logger.error(f"[BFS Bid] OTP send failed: {otp_result}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Failed to send OTP. Please try again later.",
                session_id=session
            )
            await self._clear_bid_state(session)
            return {"status": "bfs_bid_otp_failed"}

    async def handle_bid_otp_input(
        self,
        user: User,
        session: ConversationSession,
        message: str
    ) -> Dict[str, Any]:
        """
        Handle OTP input for bid confirmation.
        """
        from app.services.otp_service import OTPService
        from app.procucev_apis.register_apis import RegisterAPIService
        from app.services.support_notification_service import SupportNotificationService
        from datetime import datetime, timedelta
        import time

        logger.info(f"[BFS Bid] Processing OTP input")

        # Initialize OTP service
        otp_service = OTPService(
            register_api_service=RegisterAPIService(),
            whatsapp_service=self.whatsapp_service,
            support_notification_service=SupportNotificationService()
        )

        # Handle RESEND request
        if message.strip().upper() == "RESEND":
            email = session.workflow_state.get("otp_email")
            if email:
                return await otp_service.send_otp(user.phone_number, email, session)

        # Check if OTP has been validated in the last 30 min
        from app.redis_db import get_auth_redis_service
        auth_redis_service = get_auth_redis_service()

        normalized_phone = user.phone_number.lstrip('+')
        stored_user = await auth_redis_service.retrieve(normalized_phone)

        if stored_user and stored_user.otp_validated_at:
            elapsed_time = time.time() - stored_user.otp_validated_at

            if elapsed_time < 1800:
                return await self._submit_bids_to_api(user, session)

        # Validate OTP
        otp_result = await otp_service.validate_otp(user.phone_number, session, message)

        if otp_result.get("status") == "otp_valid":
            # Store OTP validation timestamp in Redis
            current_time = time.time()
            stored_user = await auth_redis_service.retrieve(normalized_phone)

            if stored_user is None:
                logger.warning(f"[BFS Bid] No existing user data found for {normalized_phone}, skipping OTP timestamp storage")
            else:
                stored_user.otp_validated_at = current_time
                await auth_redis_service.store(normalized_phone, stored_user.model_dump())
            
            # OTP validated - submit bids
            logger.info(f"[BFS Bid] OTP validated, submitting bids")
            return await self._submit_bids_to_api(user, session)

        elif otp_result.get("status") == "max_otp_exceeded":
            # Max OTP retries exceeded
            logger.warning(f"[BFS Bid] Max OTP retries exceeded")
            await self._clear_bid_state(session)
            return {"status": "bfs_bid_max_otp_retries"}

        else:
            # Invalid OTP - message already sent by OTPService
            logger.info(f"[BFS Bid] Invalid OTP: {otp_result}")
            return {"status": "bfs_bid_otp_invalid", "result": otp_result}

    async def _submit_bids_to_api(
        self,
        user: User,
        session: ConversationSession
    ) -> Dict[str, Any]:
        """
        Submit bids to the backend API.
        Calls the BFS request item API for each bid.
        """
        from app.redis_db import get_auth_redis_service

        logger.info(f"[BFS Bid] Submitting bids to API")

        # Get bid items from session
        bid_items = session.workflow_state.get("bfs_bid_items", [])

        if not bid_items:
            logger.error(f"[BFS Bid] No bid items found in session")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Session expired. Please try again.",
                session_id=session
            )
            await self._clear_bid_state(session)
            return {"status": "bfs_bid_no_items"}

        # Get user data from Redis for org_id and user_id
        auth_redis = get_auth_redis_service()
        normalized_phone = user.phone_number.lstrip('+')
        user_data = await auth_redis.retrieve(normalized_phone)

        if not user_data or not user_data.org_id or not user_data.id:
            logger.error(f"[BFS Bid] Missing user data for {user.phone_number}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Unable to verify your account. Please contact support.",
                session_id=session
            )
            await self._clear_bid_state(session)
            return {"status": "bfs_bid_missing_user_data"}

        org_id = user_data.org_id
        user_id = user_data.id
        buyer_phone = user.phone_number

        # Submit each bid to the API
        successful_bids = []
        failed_bids = []

        for bid in bid_items:
            original_item = bid.get("original_item", {})
            item_id = original_item.get("id")
            buy_price = original_item.get("sellPrice", 0)
            ask_price = bid.get("price", 0)
            quantity = 1  # Default quantity per bid

            if not item_id:
                logger.warning(f"[BFS Bid] Skipping bid with missing item_id: {bid}")
                failed_bids.append({"bid": bid, "error": "Missing item ID"})
                continue

            logger.info(f"[BFS Bid] Submitting bid for item {item_id}: ask_price={ask_price}, buy_price={buy_price}")

            api_result = await self.bfs_api_service.request_bfs_item(
                item_id=item_id,
                buy_price=buy_price,
                ask_price=ask_price,
                quantity=quantity,
                org_id=org_id,
                user_id=user_id,
                buyer_phone=buyer_phone
            )

            if api_result.get("success"):
                successful_bids.append({"bid": bid, "response": api_result})
                logger.info(f"[BFS Bid] Successfully submitted bid for item {item_id}")
            else:
                failed_bids.append({"bid": bid, "error": api_result.get("error")})
                logger.error(f"[BFS Bid] Failed to submit bid for item {item_id}: {api_result.get('error')}")

        # Determine overall result
        if successful_bids:
            api_result = {"success": True, "successful_count": len(successful_bids), "failed_count": len(failed_bids)}
        else:
            api_result = {"success": False, "error": "All bids failed"}

        if api_result.get("success"):
            # Generate success summary
            bid_summary = generate_bid_summary(bid_items)

            # Clear all BFS state (bid + search) to reset for next interaction
            await self._clear_bid_state(session, clear_bfs_search=True)

            # Build success message with menu buttons
            success_msg = (
                "*Bids Placed Successfully!*\n\n"
                f"{bid_summary}\n\n"
                "You will be notified about the bid status."
            )

            # Get appropriate menu buttons based on role
            user_role = user.role.value if hasattr(user.role, 'value') else user.role
            if user_role == "buyer":
                buttons = [
                    {"id": "create_rfq", "title": "Create new RFQ"},
                    {"id": "rfq_status", "title": "Check RFQ Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]
            else:
                buttons = [
                    {"id": "view_rfqs", "title": "View Open RFQs"},
                    {"id": "quote_status", "title": "Check Quote Status"},
                    {"id": "search_bfs", "title": "Search Stocks"}
                ]

            # Send single message with buttons attached
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                success_msg,
                buttons,
                header="",
                footer="",
                session_id=session
            )

            logger.info(f"[BFS Bid] Bids submitted successfully")
            return {"status": "bfs_bid_submitted", "bid_count": len(bid_items)}
        else:
            # API error
            error_msg = (
                "Sorry, we couldn't place your bids at this time.\n"
                "Please try again later or contact support."
            )
            buttons = [{"id": "bfs_restart", "title": "Restart"}]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                error_msg,
                buttons,
                header="Bid Failed",
                footer="",
                session_id=session
            )
            return {"status": "bfs_bid_api_error", "error": api_result.get("error")}

    async def _clear_bid_state(self, session: ConversationSession, clear_bfs_search: bool = False) -> None:
        """
        Clear all BFS bid related state from session.

        Args:
            session: The conversation session
            clear_bfs_search: If True, also clears BFS search state (bfs_results, etc.)
        """
        bid_keys = [
            "bfs_bid_stage",
            "bfs_bid_items",
            "bfs_bid_retry_count",
            "otp_email",
            "otp_retry_count",
        ]

        # Also clear BFS search state if requested (e.g., after successful bid)
        if clear_bfs_search:
            bid_keys.extend([
                "bfs_results",
                "bfs_searched_products",
            ])

        if session.workflow_state:
            for key in bid_keys:
                session.workflow_state.pop(key, None)
        await self.session_manager.save_session(session, persist_to_db=False)
        logger.info(f"[BFS Bid] Cleared bid state from session (clear_bfs_search={clear_bfs_search})")

    async def _cancel_bid_flow(
        self,
        user: User,
        session: ConversationSession,
        reason: str
    ) -> Dict[str, Any]:
        """Cancel bid flow and clear state. Returns signal for cancel_service."""
        logger.info(f"[BFS Bid] Cancelling bid flow: {reason}")
        await self._clear_bid_state(session, clear_bfs_search=True)
        return {"status": "bfs_send_cancel_message", "reason": reason}

    async def _cancel_bid_after_max_retries(
        self,
        user: User,
        session: ConversationSession
    ) -> Dict[str, Any]:
        """Cancel bid after max retry attempts."""
        logger.warning(f"[BFS Bid] Max retries ({MAX_BID_RETRY_ATTEMPTS}) reached")
        await self._clear_bid_state(session)

        message = (
            f"Maximum attempts ({MAX_BID_RETRY_ATTEMPTS}) reached.\n"
            "Please restart to try again."
        )
        buttons = [{"id": "bfs_restart", "title": "Restart"}]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            message,
            buttons,
            header="Max Attempts Reached",
            footer="",
            session_id=session
        )
        return {"status": "bfs_bid_max_retries"}

    async def _send_post_bid_menu(
        self,
        user: User,
        session: ConversationSession,
        role: str
    ) -> None:
        """
        Send post-bid action menu with standard buyer/seller options.
        Same menu as shown after RFQ creation.
        """
        if role == "buyer":
            buttons = [
                {"id": "create_rfq", "title": "Create new RFQ"},
                {"id": "rfq_status", "title": "Check RFQ Status"},
                {"id": "search_bfs", "title": "Search Stocks"}
            ]
        else:
            # Seller menu
            buttons = [
                {"id": "view_rfqs", "title": "View Open RFQs"},
                {"id": "quote_status", "title": "Check Quote Status"},
                {"id": "search_bfs", "title": "Search Stocks"}
            ]

        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            "What would you like to do next?",
            buttons,
            header="",
            footer="",
            session_id=session
        )
