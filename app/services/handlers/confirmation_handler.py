"""
Confirmation Handler.

Handles RFQ confirmation workflow including optional fields,
user confirmation responses, and RFQ submission.
Extracted from ChatService to reduce complexity.
"""

import logging
import asyncio
from typing import Dict, Any, List
from app.models import User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.procucev_apis.rfq_apis import RFQAPIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.schemas.rfq import RFQValidationSchema
from app.utils.datetime_utils import utc_now
from app.tools.confirmation_tool import ConfirmationTool
from app.services.confirmation_service import ConfirmationService
from app.services.openai_service import OpenAIService

logger = logging.getLogger(__name__)


class ConfirmationHandler:
    """Handles RFQ confirmation workflow."""
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        
        # Initialize confirmation service

        
        openai_service = OpenAIService()
        confirmation_tool = ConfirmationTool(openai_service)
        self.confirmation_service = ConfirmationService(confirmation_tool)
    
    async def handle_confirmation_button(self, user: User, session: ConversationSession, 
                                       button_id: str) -> Dict[str, Any]:
        """Handle Confirm/Modify confirmation button responses using confirmation service."""
        logger.info(f"Confirmation button response from {user.phone_number}: {button_id}")
        
        if button_id == "confirm_rfq":
            # User clicked "Confirm" - proceed with RFQ acceptance
            logger.info(f"Confirmation service: Button 'confirm_rfq' treated as 'yes' from {user.phone_number}")
            return await self._handle_rfq_acceptance(user, session, "Confirm")
            
        elif button_id == "no_rfq":
            # User clicked "Modify" - handle exactly like current "No" response
            logger.info(f"Confirmation service: Button 'no_rfq' treated as 'no' from {user.phone_number}")
            return await self._handle_rfq_modification(user, session, "No")
        
        elif button_id == "continue_rfq":
            # User clicked "Continue" from optional fields - proceed to confirmation
            logger.info(f"Confirmation service: Button 'continue_rfq' - proceeding to confirmation from {user.phone_number}")
            return await self._proceed_to_confirmation_from_optional(user, session, "Continue")

        elif button_id == "confirm_no_changes":
            # User clicked "Continue" from modification clarification - treat as acceptance
            logger.info(f"Confirmation service: Button 'confirm_no_changes' - user confirmed no changes needed from {user.phone_number}")
            return await self._handle_rfq_acceptance(user, session, "Continue")

        return {"status": "unknown_button", "button_id": button_id}
    
    async def handle_pending_confirmations(self, user: User, session: ConversationSession, 
                                         message: str, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle pending confirmation responses using confirmation service."""
        # First try to parse confirmation using the confirmation service
        confirmation_result = await self.confirmation_service.parse_confirmation(message)
        
        if confirmation_result == "yes":
            # User confirmed - create RFQs
            logger.info(f"Confirmation service detected 'yes' response from {user.phone_number}")
            return await self._handle_rfq_acceptance(user, session, message)
        elif confirmation_result == "no":
            # User declined - handle modification
            logger.info(f"Confirmation service detected 'no' response from {user.phone_number}")
            return await self._handle_rfq_modification(user, session, message)
        
        # Fallback to intent-based analysis if confirmation service couldn't parse
        confirmation_intent_type = intent_result.get('intent')
        confirmation_confidence = intent_result.get('confidence', 0)
        context_analysis = intent_result.get('context_analysis', {})
        
        print(f"Confirmation stage - Intent: {confirmation_intent_type}, Confidence: {confirmation_confidence}%, Context: {context_analysis}")
        
        if confirmation_intent_type == "confirmation_response" and confirmation_confidence > 0.7:
            response_type = context_analysis.get('confirmation_details', {}).get('response_type')
            has_conditions = context_analysis.get('confirmation_details', {}).get('has_conditions', False)
            
            if response_type == "accept" and not has_conditions:
                # User confirmed - create RFQs
                return await self._handle_rfq_acceptance(user, session, message)
            else:
                # User declined or has conditions - treat as modification request
                return await self._handle_rfq_modification(user, session, message)
                
        elif confirmation_intent_type == "modification_request" and confirmation_confidence > 0.7:
            # User wants to modify - process the modification
            logger.info("User requesting modification during confirmation stage")
            return await self._handle_rfq_modification(user, session, message)
        else:
            # Unclear response - ask for clarification while keeping context
            return await self._handle_confirmation_clarification(user, session, message)
    
    async def handle_optional_fields_response(self, user: User, session: ConversationSession,
                                            message: str) -> Dict[str, Any]:
        """Handle optional field responses using confirmation service."""
        #TODO: use ai based but from where can i use context to determine the response
        if any(keyword in message.lower() for keyword in [
            # Generic skip words
            "no", "nope", "nah", "not now", "none", "nothing",
            # Skip intent
            "skip", "skip it", "skip this", "skip for now",
            "don’t want", "don't want", "do not want",
            "not required", "not needed", "no need", "no thanks",
            # Proceed / continue intents
            "proceed", "continue", "next", "go ahead", "move ahead",
            "go next", "carry on", "let's go", "lets go",
            "ok continue", "okay continue", "ok proceed", "okay proceed",
            "continue to process", "continue to proceed", "proceed ahead",
            # Confirmation style responses
            "yes proceed", "yes continue", "yeah proceed", "yep continue",
            # Short positive words with intent to move on
            "ok", "okay", "alright", "fine", "sure", "done", "ready"
        ]):
            logger.info(f"Confirmation service detected skip request from {user.phone_number}")
            return await self._proceed_to_confirmation_from_optional(user, session, message)
        # For above TODO, Add a elif logic here
        else:
            logger.info(f"Confirmation service detected user wants to provide optional info from {user.phone_number}")
            return await self._merge_optional_fields_and_confirm(user, session, message)

    async def _handle_rfq_acceptance(self, user: User, session: ConversationSession,
                                   message: str) -> Dict[str, Any]:
        """Handle RFQ acceptance and submission."""
        # Debug: Log current workflow state
        logger.info(f"RFQ acceptance - workflow_state keys: {list(session.workflow_state.keys())}")
        logger.info(f"Has pending_combined_rfq: {bool(session.workflow_state.get('pending_combined_rfq'))}")
        logger.info(f"Has pending_rfq: {bool(session.workflow_state.get('pending_rfq'))}")
        logger.info(f"Has pending_optional_combined_rfq: {bool(session.workflow_state.get('pending_optional_combined_rfq'))}")
        logger.info(f"Has pending_optional_rfq: {bool(session.workflow_state.get('pending_optional_rfq'))}")

        # Check if user is still in optional fields phase - move to confirmation automatically
        if session.workflow_state.get("pending_optional_combined_rfq"):
            logger.warning(f"User clicked Confirm while in optional fields phase (combined) - auto-moving to confirmation")
            combined_data = session.workflow_state["pending_optional_combined_rfq"]
            session.workflow_state["pending_combined_rfq"] = combined_data
            del session.workflow_state["pending_optional_combined_rfq"]
            logger.info(f"Moved to confirmation phase - pending_combined_rfq now set")
        elif session.workflow_state.get("pending_optional_rfq"):
            logger.warning(f"User clicked Confirm while in optional fields phase (single) - auto-moving to confirmation")
            product_info = session.workflow_state["pending_optional_rfq"]
            session.workflow_state["pending_rfq"] = product_info
            del session.workflow_state["pending_optional_rfq"]
            logger.info(f"Moved to confirmation phase - pending_rfq now set")

        # Handle combined RFQ or single RFQ confirmations

        logger.info(f"pending combined rfq:{session.workflow_state.get("pending_combined_rfq")}")
        logger.info(f"pending combined rfq:{session.workflow_state.get("pending_rfq")}")
        if session.workflow_state.get("pending_combined_rfq"):
            # Combined RFQ format (single RFQ with multiple items)
            combined_data = session.workflow_state["pending_combined_rfq"]
            combined_schema = RFQValidationSchema(**combined_data["combined_schema"])

            gmt_result = await self._submit_rfq_to_backend(combined_schema, user)
            rfq_results = [gmt_result]
            successful_count = 1 if gmt_result.get("success") else 0

        elif session.workflow_state.get("pending_rfq"):
            # Single RFQ format
            product_info = session.workflow_state["pending_rfq"]
            entities = product_info["entities"]

            # Merge attachments from extracted_entities into entities
            if session.workflow_state.get("extracted_entities") and session.workflow_state["extracted_entities"]:
                extracted_attachments = session.workflow_state["extracted_entities"][0].get("attachments", [])
                if extracted_attachments:
                    entities["attachments"] = extracted_attachments
                    logger.info(f"Merged {len(extracted_attachments)} attachments from extracted_entities into entities")

            # Debug: Log entities to see if attachments are present
            logger.info(f"Entities keys: {list(entities.keys())}")
            logger.info(f"Entities attachments: {entities.get('attachments', 'MISSING')}")

            # Always rebuild from entities to ensure attachments are included
            # (schema_data might be stale and not include recently added attachments)
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(entities, None)

            logger.info(f"rfq_schema:{rfq_schema}")

            gmt_result = await self._submit_rfq_to_backend(rfq_schema, user)
            logger.info(f"gmt_result:{gmt_result}")
            rfq_results = [gmt_result]
            logger.info(f"rfq results:{rfq_results}")
            successful_count = 1 if gmt_result.get("success") else 0
            logger.info(f"success count:{successful_count}")
        else:
            logger.error(f"No pending RFQ found! workflow_state keys: {list(session.workflow_state.keys())}")
            rfq_results = []
            successful_count = 0
        
        # Generate completion response with RFQ IDs
        await self._send_completion_response(user, rfq_results, successful_count)
        
        # Run auto-categorization for each successful RFQ (offline process)
        # auto_cat = await run_auto_categorization_for_rfqs(
        #     rfq_results, 
        #     self.auto_categorization_service, 
        #     self.enhanced_auto_categorization_service
        # )
        # await self.whatsapp_service.send_message(user.phone_number, auto_cat)
        
        # # Run seller recommendation for each successful RFQ (offline process)
        # seller_match = await run_seller_recommendation_for_rfqs(
        #     rfq_results, 
        #     self.seller_recommendation_service,
        #     self.enhanced_seller_matching_service
        # )
        # await self.whatsapp_service.send_message(user.phone_number, seller_match)

        # # Check BFS availability after successful RFQ creation
        # await self._check_bfs_availability(user.phone_number)
        
        # Mark session as completed
        from app.models import ConversationOutcome
        session.outcome = ConversationOutcome.completed
        logger.info(f"session outcome is:{session.outcome}")
        session.completed_at = utc_now().replace(tzinfo=None)
        
        # Update core tracking fields (user type, categories, RFQ IDs)
        rfq_ids = []
        for result in rfq_results:
            if result.get("success") and result.get("rfq_id"):
                rfq_ids.append(result["rfq_id"])

        logger.info(f"rfqids:{rfq_ids}")
        
        if rfq_ids:
            session.rfq_ids = rfq_ids
            logger.info(f"session.rfq_ids is:{rfq_ids}")
            # Set user type as buyer (since they're creating RFQs)
            from app.models import UserType
            session.user_type = UserType.buyer
            # Calculate averages based on session data
            from app.services.helpers.session_helpers import SessionHelpers
            session = SessionHelpers.calculate_session_averages(session)
        
        # Clear session AFTER summarization data is captured
        logger.info(f"successful count is :{successful_count}")

        from app.models import ConversationOutcome
        if successful_count > 0:
            session.outcome = ConversationOutcome.completed
            session.workflow_type = None
            session.workflow_state = {}
            logger.info(f"Cleared workflow_type and workflow_state after successful RFQ creation")
        else:
            # Clear workflow state after failure (error message already sent to user)
            session.outcome = ConversationOutcome.abandoned
            session.workflow_type = None
            session.workflow_state = {}
            logger.warning(f"RFQ creation failed, cleared workflow_type and workflow_state after sending error message to user")

        return {"status": "multiple_rfqs_created", "successful_count": successful_count}
    
    async def _handle_rfq_modification(self, user: User, session: ConversationSession,
                                     message: str) -> Dict[str, Any]:
        """Handle RFQ modification requests."""
        logger.info("User declined or has conditions - treating as modification request")

        # Extract captured information and show it to user
        captured_info_message = self._format_captured_info_for_modification(session)
        
        # Add modification instructions
        modification_instructions = (
            "If you'd like to make any updates, please use these keywords:\n"
            "• New – to add new items (e.g., New 10 motors)\n"
            "• Remove – to delete an item (e.g., Remove desktop)\n"
            "• Change – to modify quantity, delivery date, or location (e.g., Change laptops to 20, Change date to 30 Dec)\n\n"
            "If everything looks good, just click Continue to proceed."
        )
        
        # Combine captured info with instructions
        full_message = f"{captured_info_message}\n\n{modification_instructions}"

        buttons_config = [
            {"id": "confirm_rfq", "title": "Continue"},
            {"id": "no_rfq", "title": "Add or Modify"}
        ]
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            full_message,
            buttons_config,
            "Confirmation Required"
        )

        # Don't delete pending confirmations - let the modification flow handle it
        return {"status": "modification_requested", "continue_with_purchase_intent": True}
    
    async def _handle_confirmation_clarification(self, user: User, session: ConversationSession, 
                                               message: str) -> Dict[str, Any]:
        """Handle unclear confirmation responses."""
        decline_context = {
            "conversation_stage": "confirmation_clarification",
            "user_message": message
        }
        
        response = await self.response_helpers.generate_contextual_response(
            decline_context,
            ["I didn't quite understand. Please say 'yes' to confirm the RFQs or tell me what you'd like to change."],
            "clarification_request"
        )
        
        await self.whatsapp_service.send_message(user.phone_number, response)
        return {"status": "confirmation_clarification_requested"}

    async def _proceed_to_confirmation_from_optional(self, user: User, session: ConversationSession,
                                                     message: str) -> Dict[str, Any]:
        """Proceed from optional fields to confirmation."""
        if session.workflow_state.get("pending_optional_rfq"):
            # Single product
            product_info = session.workflow_state["pending_optional_rfq"]
            entities = product_info["entities"]

            # Apply attachment caption as remarks if present
            attachment_caption = session.workflow_state.get("attachment_caption")
            if attachment_caption and not entities.get("remarks"):
                logger.info(f"Applying attachment caption to confirmation: {attachment_caption}")
                entities["remarks"] = attachment_caption
                # Clear the caption after applying
                del session.workflow_state["attachment_caption"]

            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(entities, None)

            # Load summaries for enhanced response generation
            chat_summaries = []  # Could load from chat summary service if needed

            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
                "user_message": message,
                "extracted_entities": product_info["entities"]
            }, chat_summaries)
            
            # Send confirmation message with buttons
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Add or Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                summary_response,
                buttons_config,
                "Confirmation Required"
            )

            # Move to confirmation state
            session.workflow_state["pending_rfq"] = product_info
            del session.workflow_state["pending_optional_rfq"]

        elif session.workflow_state.get("pending_optional_combined_rfq"):
            # Combined RFQ with multiple items
            combined_data = session.workflow_state["pending_optional_combined_rfq"]

            # Apply attachment caption as remarks to all products if present
            attachment_caption = session.workflow_state.get("attachment_caption")
            if attachment_caption:
                logger.info(f"Applying attachment caption to combined RFQ: {attachment_caption}")
                for product in combined_data.get("products", []):
                    entities = product.get("entities", {})
                    if not entities.get("remarks"):
                        entities["remarks"] = attachment_caption
                # Update the combined schema with the new remarks
                # Note: combined_schema is already built, so we need to update it
                if "remarks" in combined_data["combined_schema"] and not combined_data["combined_schema"]["remarks"]:
                    combined_data["combined_schema"]["remarks"] = attachment_caption
                # Clear the caption after applying
                del session.workflow_state["attachment_caption"]

            combined_schema = RFQValidationSchema(**combined_data["combined_schema"])

            # Load summaries for enhanced response generation
            chat_summaries = []

            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                combined_schema,
                {
                    "user_message": message,
                    "extracted_entities": [prod["entities"] for prod in combined_data["products"]],
                    "total_products": len(combined_data["products"])
                },
                chat_summaries
            )
            
            # Send confirmation message with buttons
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Add or Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                summary_response,
                buttons_config,
                "Confirmation Required"
            )

            # Move to confirmation state
            session.workflow_state["pending_combined_rfq"] = combined_data
            del session.workflow_state["pending_optional_combined_rfq"]

        return {"status": "optional_fields_skipped"}
    
    async def _submit_rfq_to_backend(self, rfq_schema, user) -> dict:
        """Submit RFQ directly to backend via GMT API service."""
        try:
            rfq_service = RFQAPIService()
            
            # Convert schema to dict using the newer method
            schema_dict = rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else rfq_schema.dict()
            
            # Extract attachments from schema
            attachments = schema_dict.get("attachments", [])

            # Debug: Log schema_dict to see what's available
            logger.info(f"Schema dict keys: {list(schema_dict.keys())}")
            logger.info(f"Schema dict attachments field: {schema_dict.get('attachments', 'MISSING')}")

            # Log attachment information
            if attachments:
                logger.info(f"RFQ contains {len(attachments)} attachment(s):")
                for i, attachment in enumerate(attachments):
                    filename = attachment.get("file_name", "unknown")
                    file_type = attachment.get("file_type", "unknown")
                    file_size = len(attachment.get("file_content", "")) if attachment.get("file_content") else 0
                    logger.info(f"  Attachment {i+1}: {filename} ({file_type}, {file_size} bytes)")
            else:
                logger.info("RFQ contains no attachments")
            
            # Transform to format expected by GMT API service
            rfq_data = {
                "project_desc": schema_dict.get("project_desc", "Unknown Product"),
                "items": schema_dict.get("items", []),
                "division": schema_dict.get("division", "Admin & IT"),
                "preferred_brand": schema_dict.get("preferred_brand", ""),
                "delivery_state": schema_dict.get("delivery_locations", [{}])[0].get("state", "Karnataka") if schema_dict.get("delivery_locations") else "Karnataka",
                "delivery_city": schema_dict.get("delivery_locations", [{}])[0].get("city", "Bangalore") if schema_dict.get("delivery_locations") else "Bangalore",
                "delivery_pincode": schema_dict.get("delivery_locations", [{}])[0].get("pincode", "560001") if schema_dict.get("delivery_locations") else "560001",
                "deadline": schema_dict.get("delivery_date"),
                "remarks": schema_dict.get("remarks", "Created via AI Procurement WhatsApp Bot"),
                "attachments": attachments,
                "product_name": schema_dict.get("project_desc", "Unknown Product"),
                "quantity": schema_dict.get("items", [{}])[0].get("quantity", 1) if schema_dict.get("items") else 1,
                "unit_of_measure": schema_dict.get("items", [{}])[0].get("unit_of_measures", "pcs") if schema_dict.get("items") else "pcs",
                "specifications": schema_dict.get("items", [{}])[0].get("description", "") if schema_dict.get("items") else ""
            }
            
            # Submit to backend via GMT API
            logger.info(f"Creating RFQ with user_id={user.id}, org_id={user.org_id}, rfq_data={rfq_data}")
            result = await rfq_service.create_rfq(rfq_data, user_id=user.id, org_id=user.org_id)
            
            # Log the GMT API response for debugging
            logger.info(f"GMT API Response: {result}")
            print(f"GMT API Response: {result}")
            
            # Include original RFQ data for auto-categorization
            if result.get("success"):
                # Extract all items from the schema for auto-categorization
                items_data = []
                if schema_dict.get("items"):
                    # Combined RFQ with multiple items
                    for item in schema_dict.get("items", []):
                        items_data.append({
                            "description": item.get("description", ""),
                            "product_name": item.get("description", ""),
                            "quantity": item.get("quantity", 1),
                            "unit_of_measure": item.get("unit_of_measures", "pcs"),
                            "division": schema_dict.get("division", ""),
                            "preferred_brand": item.get("brand", "")
                        })
                else:
                    # Fallback for single item (legacy compatibility)
                    items_data.append({
                        "description": rfq_data.get("specifications", ""),
                        "product_name": rfq_data.get("product_name", ""),
                        "quantity": rfq_data.get("quantity", 1),
                        "unit_of_measure": rfq_data.get("unit_of_measure", "pcs"),
                        "division": rfq_data.get("division", ""),
                        "preferred_brand": rfq_data.get("preferred_brand", "")
                    })
                
                result["rfq_data"] = {
                    "items": items_data
                }
            
            return result
            
        except Exception as e:
            logger.error(f"Error submitting RFQ to backend: {e}")
            return {
                "success": False,
                "error": f"Failed to submit RFQ: {str(e)}"
            }

    async def _send_completion_response(self, user: User, rfq_results: List[Dict], successful_count: int):
        """Send completion response to user."""
        if successful_count == 0:
            # Send error message to user when RFQ creation fails
            error_messages = []
            for result in rfq_results:
                if not result.get("success"):
                    error_msg = result.get("error", "Unknown error occurred")
                    error_messages.append(error_msg)

            error_details = "\n".join(error_messages) if error_messages else "Unable to create RFQ at this time."
            response = (
                "❌ *RFQ Creation Failed*\n\n"
                f"We encountered an issue while creating your RFQ:\n{error_details}\n\n"
                "Please try again or contact support if the issue persists."
            )
            await self.whatsapp_service.send_message(user.phone_number, response)
            return

        rfq_ids = []
        for result in rfq_results:
            if result.get("success") and result.get("rfq_id"):
                rfq_ids.append(result["rfq_id"])
        if rfq_ids:
            # Single RFQ case (matches your example format)
            if successful_count == 1:
                response = (
                    "✅ *RFQ created successfully!*\n\n"
                    f"*ID:* {rfq_ids[0]}\n"
                    "(You can use this ID anytime to track your request.)\n\n"
                    "What would you like to do next?\n"
                    "You can choose from the options below or simply type what you’d like to do next."
                )

            # Multiple RFQs case
            else:
                rfq_ids_text = "\n".join([f"ID: {rfq_id}" for rfq_id in rfq_ids])
                response = f"✅ All {successful_count} RFQs created successfully!\n\n{rfq_ids_text}\n(You can use these IDs anytime to track your requests.)\n\nWhat would you like to do next?"
        else:
            response = f"✅ All {successful_count} RFQs created successfully!\n\nWhat would you like to do next?"

        # Send message with interactive buttons (WhatsApp limit: 3 buttons max)

        buttons_config = [
            {"id": "new_rfq", "title": "Create new RFQ"},
            {"id": "rfq_status", "title": "Check RFQs Status"},
            {"id": "search_bfs", "title": "Search Stocks"}
        ]
        
        await self.whatsapp_service.send_configurable_buttons(
            user.phone_number,
            response,
            buttons_config,
        )

    async def _merge_optional_fields_and_confirm(self, user: User, session: ConversationSession,
                                               message: str) -> Dict[str, Any]:
        """Merge optional field specifications with existing product and proceed to confirmation."""
        try:
            # Extract specifications from the user message using entity extraction
            from app.services.openai_service import OpenAIService
            openai_service = OpenAIService()

            # Extract only specifications from the message
            extraction_result = await openai_service.extract_entities(message, "buy_something")
            extracted_product = extraction_result.get("products", [{}])[0] if extraction_result.get("products") else {}

            # Get the existing complete product from session
            if session.workflow_state.get("pending_optional_rfq"):
                # Single product case
                existing_product_info = session.workflow_state["pending_optional_rfq"]
                existing_entities = existing_product_info["entities"]

                # Merge specifications into existing product
                self._merge_specifications_into_product(existing_entities, extracted_product, message)

                # Create updated schema
                rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(existing_entities, openai_service)

                # Generate confirmation with merged data
                summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
                    "user_message": message,
                    "extracted_entities": existing_entities
                }, [])

                # Send confirmation message with buttons
                buttons_config = [
                    {"id": "confirm_rfq", "title": "Confirm"},
                    {"id": "no_rfq", "title": "Add or Modify"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    summary_response,
                    buttons_config,
                    "Confirmation Required"
                )

                # Move to confirmation state
                session.workflow_state["pending_rfq"] = existing_product_info
                del session.workflow_state["pending_optional_rfq"]

            elif session.workflow_state.get("pending_optional_combined_rfq"):
                # Combined RFQ case - merge specs into all products
                combined_data = session.workflow_state["pending_optional_combined_rfq"]

                # For combined RFQs, merge specs into the first product (or could be made smarter)
                if combined_data.get("products"):
                    first_product = combined_data["products"][0]
                    existing_entities = first_product["entities"]
                    self._merge_specifications_into_product(existing_entities, extracted_product, message)

                # Generate confirmation with merged data
                combined_schema = RFQValidationSchema(**combined_data["combined_schema"])
                summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
                    combined_schema,
                    {
                        "user_message": message,
                        "extracted_entities": [prod["entities"] for prod in combined_data["products"]],
                        "total_products": len(combined_data["products"])
                    },
                    []
                )

                # Send confirmation message with buttons
                buttons_config = [
                    {"id": "confirm_rfq", "title": "Confirm"},
                    {"id": "no_rfq", "title": "Add or Modify"}
                ]
                await self.whatsapp_service.send_configurable_buttons(
                    user.phone_number,
                    summary_response,
                    buttons_config,
                    "Confirmation Required"
                )

                # Move to confirmation state
                session.workflow_state["pending_combined_rfq"] = combined_data
                del session.workflow_state["pending_optional_combined_rfq"]

            return {"status": "optional_fields_merged_confirmation_sent"}

        except Exception as e:
            logger.error(f"Error merging optional fields: {e}")
            # Fallback to original behavior
            return {"status": "continue_with_purchase_intent"}

    def _merge_specifications_into_product(self, existing_entities: dict, extracted_product: dict, message: str):
        """Merge specifications from extracted product into existing product entities."""
        # Merge brand if provided
        if extracted_product.get("brand"):
            existing_entities["brand"] = extracted_product["brand"]

        # Merge remarks/specifications
        extracted_remarks = extracted_product.get("remarks")
        if extracted_remarks:
            # If existing remarks exist, append new ones
            if existing_entities.get("remarks"):
                existing_entities["remarks"] += f", {extracted_remarks}"
            else:
                existing_entities["remarks"] = extracted_remarks
        elif not existing_entities.get("remarks"):
            # If no remarks in extracted but user provided message, use the message as remarks
            existing_entities["remarks"] = message.strip()

        # Merge other optional fields if they don't exist in existing product
        optional_fields = ["projectDesc", "division"]
        for field in optional_fields:
            if extracted_product.get(field) and not existing_entities.get(field):
                existing_entities[field] = extracted_product[field]

        logger.info(f"Merged specifications into existing product: {existing_entities.get('description', 'Unknown')}")

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

    # async def _check_bfs_availability(self, user_phone: str) -> None:
    #     """Check BFS availability after successful RFQ creation."""
    #     try:
    #         # Send initial checking message
    #         checking_message = "Checking our inventory for immediate availability..."
    #         await self.whatsapp_service.send_message(user_phone, checking_message)
            
    #         # Send placeholder message
    #         placeholder_message = "BFS inventory check feature is in progress."
    #         await self.whatsapp_service.send_message(user_phone, placeholder_message)
            
    #         logger.info(f"Sent BFS availability placeholder to {user_phone}")
    #     except Exception as e:
    #         logger.error(f"Error sending BFS availability placeholder: {e}")