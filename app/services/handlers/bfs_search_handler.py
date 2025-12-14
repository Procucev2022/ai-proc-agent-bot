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

logger = logging.getLogger(__name__)


class BFSSearchHandler:
    """Handler for BFS stock search workflow."""

    def __init__(self, whatsapp_service: WhatsAppService, session_manager):
        self.whatsapp_service = whatsapp_service
        self.session_manager = session_manager
        self.openai_service = OpenAIService()
        self.auto_categorization_service = get_auto_categorization_service()
        self.bfs_api_service = get_bfs_api_service()

    async def handle_bfs_search(
        self,
        user: User,
        session: ConversationSession,
        message: str
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
            await self._send_bfs_results(user, session, api_response.get("data"))
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
        data: Any
    ) -> None:
        """Format and send BFS search results to user with action buttons."""
        try:
            if not data or (isinstance(data, list) and len(data) == 0):
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
                    {"id": "bfs_place_bid", "title": "Place Bid"},
                    {"id": "bfs_raise_rfq", "title": "Raise RFQ"},
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
