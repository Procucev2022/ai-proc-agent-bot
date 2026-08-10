"""
Purchase Intent Handler.

Handles purchase intent workflow orchestration including entity extraction,
context building, and routing to appropriate processors.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any
from app.models import WorkflowType, User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.services.helpers.response_helpers import ResponseHelpers
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class PurchaseIntentHandler:
    """Handles purchase intent workflow orchestration."""

    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers,
                 entity_service, chat_summary_service, products_array_handler, session_manager,
                 confirmation_handler=None, attachment_decision_handler=None):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.entity_service = entity_service
        self.chat_summary_service = chat_summary_service
        self.products_array_handler = products_array_handler
        self.session_manager = session_manager
        self.confirmation_handler = confirmation_handler
        self.attachment_decision_handler = attachment_decision_handler
    
    async def handle_purchase_intent(self, user: User, session: ConversationSession,
                                   message: str, intent_result: Dict[str, Any] = None,
                                   should_use_summary_aware_extraction_func=None) -> Dict[str, Any]:
        """Handle purchase intent with data model driven orchestration."""
        try:
            # Normalize message if passed as a dict (e.g. interactive button reply)
            if isinstance(message, dict):
                if "button_reply" in message:
                    message = message["button_reply"].get("title", "") or message["button_reply"].get("id", "")
                elif "text" in message:
                    txt = message.get("text", "")
                    message = txt.get("body", "") if isinstance(txt, dict) else str(txt)
                elif "content" in message:
                    message = str(message.get("content", ""))
                else:
                    message = str(message)
            elif not isinstance(message, str):
                message = str(message or "")

            # DEFENSIVE CLEANUP: Remove any lingering session_archive from timeout
            # This prevents old RFQ data from leaking into new workflows
            if session.workflow_state and 'session_archive' in session.workflow_state:
                logger.warning(f"[CLEANUP] Removing lingering session_archive from workflow_state")
                del session.workflow_state['session_archive']

            # CHECK FOR SECTIONED RFQ WORKFLOW (Track 3)
            # Check global configuration flag
            from app.config import get_settings
            settings = get_settings()

            # If sectioned RFQ is already active OR global flag is enabled
            if WorkflowManager.is_sectioned_rfq_active(session) or settings.use_sectioned_rfq:
                logger.info(f"[SECTIONED_RFQ] Routing to sectioned RFQ handler (global flag: {settings.use_sectioned_rfq}, already active: {WorkflowManager.is_sectioned_rfq_active(session)})")

                # Initialize sectioned RFQ if not already active
                if not WorkflowManager.is_sectioned_rfq_active(session):
                    logger.info(f"[SECTIONED_RFQ] Initializing sectioned RFQ workflow")
                    WorkflowManager.initialize_sectioned_rfq(session)
                    WorkflowManager.set_sectioned_rfq_section(session, "date_location")
                    if "sectioned_rfq" in session.workflow_state:
                        session.workflow_state["sectioned_rfq"]["active"] = True

                # Get or create sectioned RFQ handler (lazy initialization)
                from app.services.handlers.sectioned_rfq_creation_handler import SectionedRFQCreationHandler
                if not hasattr(self, 'sectioned_rfq_handler'):
                    from app.services.cancel_service import CancelService
                    cancel_service = CancelService(self.whatsapp_service, self.session_manager)
                    self.sectioned_rfq_handler = SectionedRFQCreationHandler(
                        entity_service=self.entity_service,
                        whatsapp_service=self.whatsapp_service,
                        cancel_service=cancel_service,
                        session_manager=self.session_manager,
                        confirmation_handler=self.confirmation_handler,
                        attachment_decision_handler=self.attachment_decision_handler
                    )

                # Route to sectioned RFQ handler
                return await self.sectioned_rfq_handler.handle_sectioned_rfq(user, session, message, attachments=[])

            # 1. Extract entities using EntityService (focused service)
            # Include both existing entities and incomplete products in context
            existing_entities = session.workflow_state.get("extracted_entities", [])
            incomplete_products = session.workflow_state.get("incomplete_products", [])
            
            # Also check for pending confirmations that might need modification
            pending_combined = session.workflow_state.get("pending_combined_rfq")
            pending_single = session.workflow_state.get("pending_rfq")
            
            if pending_combined:
                pending_confirmations = pending_combined.get("products", [])
            elif pending_single:
                pending_confirmations = [pending_single]
            else:
                pending_confirmations = []
            
            print(f"PurchaseIntentHandler: handle_purchase_intent - pending_confirmations: {len(pending_confirmations)} items")
            if pending_confirmations:
                print(f"PurchaseIntentHandler: Found pending confirmations, this might be a modification request")
            
            # If we have incomplete products, use them as the base entities
            if incomplete_products:
                context_entities = [prod["entities"] for prod in incomplete_products]
                print(f"PurchaseIntentHandler: Using incomplete products as context: {len(context_entities)} products")
            else:
                context_entities = existing_entities
                
            # Load user's recent summaries for enhanced context
            chat_summaries = await self.chat_summary_service.load_user_context(user.phone_number)
            print(f"PurchaseIntentHandler: Loaded {len(chat_summaries)} chat summaries for context")
            
            # Build comprehensive context for EntityService including pending confirmations, intent result, and summaries
            entity_context = {
                "extracted_entities": context_entities,
                "workflow_state": session.workflow_state,
                "session_metadata": {
                    "session_id": session.session_id,
                    "workflow_type": session.workflow_type
                },
                "intent_result": intent_result,  # Pass intent result to avoid duplicate OpenAI calls
                "chat_summaries": chat_summaries,  # Add summaries for smart entity extraction
            }
            logger.info(f"entity extraction results in handle purchase intent:{entity_context}")
            print(f"PurchaseIntentHandler: Passing context to EntityService - has pending confirmations: {bool(session.workflow_state.get('pending_combined_rfq'))}")
            print(f"PurchaseIntentHandler: Debug session.workflow_state keys: {list(session.workflow_state.keys()) if session.workflow_state else 'None'}")
            print(f"PurchaseIntentHandler: Debug pending_combined_rfq: {session.workflow_state.get('pending_combined_rfq') if session.workflow_state else 'No workflow_state'}")
            
            # Determine workflow type based on intent result
            workflow_type = "buy_something"  # default
            if intent_result and intent_result.get("intent") == "modification_request":
                workflow_type = "modification_request"
            elif intent_result and intent_result.get("intent") == "rfq_status_check":
                workflow_type = "rfq_status_check"
            
            print(f"PurchaseIntentHandler: Using workflow_type: {workflow_type}")
            
            # Use summary-aware entity extraction if we have summaries, otherwise use standard extraction
            if chat_summaries and should_use_summary_aware_extraction_func and should_use_summary_aware_extraction_func(message):
                print(f"PurchaseIntentHandler: Using summary-aware entity extraction")
                logger.info(f"PurchaseIntentHandler: Using summary aware entity extraction data to be passed for entity extraction message={message}, context={entity_context}, workflow_type={workflow_type} ")

                entity_result = await self.entity_service.extract_entities_with_summary_context(message, context=entity_context, workflow_type=workflow_type)
            else:
                print(f"PurchaseIntentHandler: Using standard entity extraction")
                logger.info(f"PurchaseIntentHandler: Using standard entity extraction data to be passed for entity extraction message={message}, context={entity_context}, workflow_type={workflow_type} ")
                entity_result = await self.entity_service.extract_entities(message, context=entity_context, workflow_type=workflow_type)
            logger.info(f"EntityService result: {entity_result}")
            
            # Debug: Check which path we're taking
            print(f"PurchaseIntentHandler debug: entity_result keys = {entity_result.keys()}")
            print(f"PurchaseIntentHandler debug: entity_result = {entity_result}")

            # Check for non-procurable items FIRST
            if entity_result.get("error_type") == "non_procurable":
                logger.info(f"Non-procurable items detected: {entity_result.get('non_procurable_items')}")
                return await self._handle_non_procurable_items(user, session, entity_result)

            # Check for quantity limit violations
            if entity_result.get("error_type") == "quantity_limit":
                logger.info(f"Quantity limit violations detected: {entity_result.get('quantity_violations')}")
                return await self._handle_quantity_limit_violations(user, session, entity_result)

            # Check if modification intent was detected but clarification is needed
            if entity_result.get("modification_intent_detected") and entity_result.get("requires_clarification"):
                return await self._handle_modification_clarification(
                    user, session, message, entity_result, chat_summaries
                )

            # Handle products array path OR supplementary data only (e.g., just delivery date)
            if "products" in entity_result:
                products = entity_result["products"]
                global_supplementary_fields = entity_result.get("global_supplementary_fields")

                # Call handler if we have products OR global supplementary fields to store
                if products or global_supplementary_fields:
                    print(f"PurchaseIntentHandler: Taking PRODUCTS ARRAY path with {len(products)} products and global fields: {global_supplementary_fields}")
                    logger.info(f"Taking PRODUCTS ARRAY path with {len(products)} products")
                    date_validation_error = entity_result.get("date_validation_error", False)
                    return await self.products_array_handler.handle_products_array(
                        user, session, message, products, chat_summaries, date_validation_error, global_supplementary_fields
                    )
            elif "entities" in entity_result:
                print(f"PurchaseIntentHandler: Taking BACKWARD COMPATIBILITY path with entities: {entity_result['entities']}")
                logger.info(f"Taking BACKWARD COMPATIBILITY path with entities: {entity_result['entities']}")
            else:
                print(f"PurchaseIntentHandler: Taking NO ENTITIES path")
                logger.info(f"Taking NO ENTITIES path")
            
            # 3. Handle single product (backward compatibility)
            return await self._handle_single_product_entities(user, session, message, entity_result, chat_summaries)
                
        except Exception as e:
            return await self._handle_error_response(e, user.phone_number)
    
    async def _handle_modification_clarification(self, user: User, session: ConversationSession,
                                               _message: str, _entity_result: Dict, _chat_summaries: list) -> Dict[str, Any]:
        """Handle modification intent that requires clarification."""
        print(f"PurchaseIntentHandler: Modification intent detected but missing values, generating clarification")
        
        # Show captured information first, then modification instructions
        captured_info_message = self._format_captured_info_for_modification(session)
        modification_instructions = (
            "If you'd like to make any updates, please use these keywords:\n"
            "• New – to add new items (e.g., New 10 motors)\n"
            "• Remove – to delete an item (e.g., Remove desktop)\n"
            "• Change – to modify quantity, delivery date, or location (e.g., Change laptops to 20, Change date to 30 Dec)\n\n"
            "If everything looks good, just click Continue to proceed."
        )
        
        full_message = f"{captured_info_message}\n\n{modification_instructions}"

        await self.whatsapp_service.send_configurable_buttons(
            recipient_id=user.phone_number,
            body=full_message,
            buttons_config=[{"id": "confirm_no_changes", "title": "Continue"}]
        )

        await self.session_manager.save_session(session, WorkflowType.rfq_creation)
        
        return {
            "status": "modification_clarification_sent",
            "message": "Asked for clarification on modification details"
        }
    
    async def _handle_single_product_entities(self, user: User, session: ConversationSession,
                                            message: str, entity_result: Dict, chat_summaries: list) -> Dict[str, Any]:
        """Handle single product entities (backward compatibility)."""
        current_entities = session.workflow_state.get("extracted_entities", [])
        new_entities = entity_result.get("entities", {})

        # Convert single entity to array format
        if new_entities and not isinstance(current_entities, list):
            current_entities = [current_entities] if current_entities else []

        # Add new entities as a product
        if new_entities:
            current_entities.append(new_entities)
            session.workflow_state["extracted_entities"] = current_entities

            # Track categories from extracted entities in product_items
            category = new_entities.get('category') or new_entities.get('description')
            if category:
                if not session.product_items:
                    session.product_items = []
                # Update or add category info to product_items
                product_info = {
                    'category': category,
                    'description': new_entities.get('description'),
                    'added_at': utc_now().isoformat()
                }
                session.product_items.append(product_info)

            # Process this as a single product array
            date_validation_error = entity_result.get("date_validation_error", False)
            return await self.products_array_handler.handle_products_array(user, session, message, current_entities, chat_summaries, date_validation_error)

        # If no new entities extracted - check if this is a modification request with no data to modify
        # Check workflow_state for pending confirmations or existing products
        has_pending_products = bool(
            session.workflow_state.get("pending_combined_rfq") or
            session.workflow_state.get("pending_rfq") or
            session.workflow_state.get("pending_optional_combined_rfq") or
            session.workflow_state.get("pending_optional_rfq") or
            current_entities
        )

        # If workflow is modification but no data exists, send helpful message
        workflow_type = session.workflow_state.get("workflow_type") or session.workflow_type
        if workflow_type == "modification_request" and not has_pending_products:
            logger.warning(f"[BUG#2_FIX] Modification request detected but no data to modify for session {session.session_id}")
            helpful_message = "I couldn't find any product details to modify. Could you please tell me what product you'd like to purchase? For example, 'I need 5 laptops'."
            await self.whatsapp_service.send_message(user.phone_number, helpful_message, session_id=session)
            await self.session_manager.save_session(session, WorkflowType.general_inquiry)
            return {
                "status": "modification_request_no_data",
                "message": "Sent helpful message for modification request with no data"
            }

        # If no new entities but not a modification request, send general clarification
        clarification_message = (
            "Please share your RFQ items in this format:\n"
            "*Quantity – Item Details – [Brand/Specs] – [UOM] – [Other Details]*\n\n"
            "Examples:\n"
            "• 15 – Laptop – HP – pieces – 10\" display, i7 processor, blue\n"
            "• 25 – Cable – 10 m roll, 10 mm thickness\n"
            "• 6 – Book – A4 size, 200 pages\n\n"
            "You can leave Brand or other Details empty if you don’t have them.\n\n"
            "Alternatively, you can bulk upload an Excel file (Max 49 items) with all the above fields."
        )

        await self.whatsapp_service.send_message(user.phone_number, clarification_message, session_id=session)
        await self.session_manager.save_session(session, WorkflowType.rfq_creation)

        return {
            "status": "no_new_entities",
            "message": "Requested more details from user"
        }
    
    async def _handle_quantity_limit_violations(self, user: User, session: ConversationSession,
                                                entity_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle quantity limit violations by informing user and asking them to adjust.

        Args:
            user: User object
            session: Current conversation session
            entity_result: Entity extraction result with quantity_violations

        Returns:
            Dict with status and details
        """
        try:
            violations = entity_result.get("quantity_violations", [])
            logger.info(f"Handling quantity limit violations for user {user.phone_number}: {violations}")

            # Format the violations for the message
            if len(violations) == 1:
                violation = violations[0]
                items_text = (
                    f'"{violation["description"]}" with quantity {int(violation["quantity"]):,}'
                )
            else:
                items_list = []
                for v in violations:
                    items_list.append(f'"{v["description"]}" ({int(v["quantity"]):,})')
                items_text = ', '.join(items_list[:-1]) + f' and {items_list[-1]}'

            # Send informative message
            info_message = (
                f"I'm sorry, but the quantity for {items_text} exceeds our limit.\n\n"
                f"Please limit quantities to 1,00,00,000 pieces/units per product. "
                f"You can adjust the quantities and try again."
            )
            await self.whatsapp_service.send_message(user.phone_number, info_message, session_id=session)

            logger.info(f"Quantity limit violation message sent to user {user.phone_number}")

            return {
                "status": "quantity_limit_violation",
                "violations": violations,
                "message": "User informed about quantity limit violations"
            }

        except Exception as e:
            logger.error(f"Error handling quantity limit violations: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_non_procurable_items(self, user: User, session: ConversationSession,
                                          entity_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle detection of non-procurable items by informing user and canceling the request.

        Args:
            user: User object
            session: Current conversation session
            entity_result: Entity extraction result with non_procurable_items

        Returns:
            Dict with status and cancellation details
        """
        try:
            non_procurable_items = entity_result.get("non_procurable_items", [])
            logger.info(f"Handling non-procurable items for user {user.phone_number}: {non_procurable_items}")

            # Format the items list for the message
            if len(non_procurable_items) == 1:
                items_text = f'"{non_procurable_items[0]}"'
            else:
                items_text = ', '.join(f'"{item}"' for item in non_procurable_items[:-1])
                items_text += f' and "{non_procurable_items[-1]}"'

            # Send informative message about non-procurable items first
            info_message = (
                f"I'm sorry, but {items_text} cannot be procured through our standard RFQ system.\n\n"
                f"Our system handles tangible physical products like equipment, materials, supplies, and goods that can be purchased through standard procurement channels."
            )
            await self.whatsapp_service.send_message(user.phone_number, info_message, session_id=session)

            # Clear the workflow state and send action buttons using cancel service
            from app.services.cancel_service import CancelService

            cancel_service = CancelService(
                whatsapp_service=self.whatsapp_service,
                session_manager=self.session_manager
            )

            # Clear workflow state without confirmation (automatic cancellation)
            await cancel_service._clear_workflow_state(session)

            # Get user type and send cancellation message with buttons
            user_role = user.role.value if hasattr(user.role, 'value') else user.role
            user_type = "buyer" if user_role == "buyer" else "seller"

            # Use cancel service's method to send the appropriate message with buttons
            await cancel_service._send_cancellation_message(user.phone_number, user_type)

            logger.info(f"Workflow cancelled for user {user.phone_number} due to non-procurable items")

            return {
                "status": "non_procurable_cancelled",
                "non_procurable_items": non_procurable_items,
                "message": "Request cancelled due to non-procurable items"
            }

        except Exception as e:
            logger.error(f"Error handling non-procurable items: {e}")
            return {
                "status": "error",
                "error": str(e)
            }

    async def _handle_error_response(self, error: Exception, phone_number: str) -> Dict[str, Any]:
        """Handle error response."""
        logger.error(f"Error in purchase intent handler: {error}")
        # For now, return a simple error response
        # In the future, this could use response_helpers for better error handling
        return {
            "status": "error",
            "error": "Could you tell me more about what you need?"
        }
    
    def _format_captured_info_for_modification(self, session: ConversationSession) -> str:
        """Format captured RFQ information for modification display."""
        try:
            from app.utils.rfq_message_formatter import format_rfq_response_message
            
            # Extract entities and global fields from session
            extracted_entities = []
            global_fields = {}
            
            # Check for pending RFQ data
            if session.workflow_state.get("pending_combined_rfq"):
                combined_data = session.workflow_state["pending_combined_rfq"]
                # Extract entities from combined RFQ products
                for product in combined_data.get("products", []):
                    if "entities" in product:
                        extracted_entities.append(product["entities"])
                
                # Extract global fields from combined schema
                combined_schema = combined_data.get("combined_schema", {})
                global_fields = {
                    "deliveryDate": combined_schema.get("delivery_date"),
                    "city": combined_schema.get("delivery_locations", [{}])[0].get("city") if combined_schema.get("delivery_locations") else None,
                    "state": combined_schema.get("delivery_locations", [{}])[0].get("state") if combined_schema.get("delivery_locations") else None,
                    "pincode": combined_schema.get("delivery_locations", [{}])[0].get("pincode") if combined_schema.get("delivery_locations") else None
                }
                
            elif session.workflow_state.get("pending_rfq"):
                product_info = session.workflow_state["pending_rfq"]
                entities = product_info.get("entities", {})
                extracted_entities = [entities]
                
                # Extract global fields from entities
                global_fields = {
                    "deliveryDate": entities.get("deliveryDate"),
                    "city": entities.get("city"),
                    "state": entities.get("state"),
                    "pincode": entities.get("pincode")
                }
            
            # Use format_rfq_response_message with show_only_collected=True to display captured info
            if extracted_entities:
                return format_rfq_response_message(
                    extracted_entities=extracted_entities,
                    global_fields=global_fields,
                    missing_fields=[],  # No missing fields for modification display
                    show_only_collected=True
                )
            else:
                return "*Here's what I have captured so far:*\n\nNo product information found."
                
        except Exception as e:
            logger.error(f"Error formatting captured info for modification: {e}")
            return "*Here's what I have captured so far:*\n\nUnable to display captured information."