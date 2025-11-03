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
                 entity_service, chat_summary_service, products_array_handler, session_manager):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.entity_service = entity_service
        self.chat_summary_service = chat_summary_service
        self.products_array_handler = products_array_handler
        self.session_manager = session_manager
    
    async def handle_purchase_intent(self, user: User, session: ConversationSession,
                                   message: str, intent_result: Dict[str, Any] = None,
                                   should_use_summary_aware_extraction_func=None) -> Dict[str, Any]:
        """Handle purchase intent with data model driven orchestration."""
        try:
            # DEFENSIVE CLEANUP: Remove any lingering session_archive from timeout
            # This prevents old RFQ data from leaking into new workflows
            if session.workflow_state and 'session_archive' in session.workflow_state:
                logger.warning(f"[CLEANUP] Removing lingering session_archive from workflow_state")
                del session.workflow_state['session_archive']

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
            
            # Check if modification intent was detected but clarification is needed
            if entity_result.get("modification_intent_detected") and entity_result.get("requires_clarification"):
                return await self._handle_modification_clarification(
                    user, session, message, entity_result, chat_summaries
                )
            
            if "products" in entity_result and entity_result["products"]:
                print(f"PurchaseIntentHandler: Taking PRODUCTS ARRAY path with {len(entity_result['products'])} products")
                logger.info(f"Taking PRODUCTS ARRAY path with {len(entity_result['products'])} products")
                # Products array detected - process all products and create RFQs
                products = entity_result["products"]
                date_validation_error = entity_result.get("date_validation_error", False)
                return await self.products_array_handler.handle_products_array(user, session, message, products, chat_summaries, date_validation_error)
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
        
        # --- Generate clarification message for modification context ---
        # Don't show collected information, just show modification instructions
        clarification_questions = (
            "If you'd like to make any updates, please use these keywords:\n"
            "• New – to add new items (e.g., New 10 motors)\n"
            "• Remove – to delete an item (e.g., Remove desktop)\n"
            "• Change – to modify quantity, delivery date, or location (e.g., Change laptops to 20, Change date to 30 Dec)\n\n"
            "If everything looks good, just click Continue to proceed."
        )

        await self.whatsapp_service.send_configurable_buttons(
            recipient_id=user.phone_number,
            body=clarification_questions,
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
            await self.whatsapp_service.send_message(user.phone_number, helpful_message)
            await self.session_manager.save_session(session, WorkflowType.general_inquiry)
            return {
                "status": "modification_request_no_data",
                "message": "Sent helpful message for modification request with no data"
            }

        # If no new entities but not a modification request, send general clarification
        clarification_message = (
            "Please share the items for your RFQ with name, brand/specs (if any), and quantity — you can add multiple items together in one message.\n\n"
            "📝 Example:\n"
            "Laptop Dell Inspiron - 5, Printer HP LaserJet - 2, Desktop HP 17\" - 10"
        )
        await self.whatsapp_service.send_message(user.phone_number, clarification_message)
        await self.session_manager.save_session(session, WorkflowType.rfq_creation)

        return {
            "status": "no_new_entities",
            "message": "Requested more details from user"
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