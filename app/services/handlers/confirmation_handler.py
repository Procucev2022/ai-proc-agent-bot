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
from app.services.gmt_api_service import GMTAPIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.rfq_processing_helpers import (
    run_auto_categorization_for_rfqs,
    run_seller_recommendation_for_rfqs
)
from app.services.auto_categorization_service import AutoCategorizationService
from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
from app.services.seller_recommendation_service import SellerRecommendationService
from app.services.enhanced_seller_matching_service import EnhancedSellerMatchingService
from app.schemas.rfq import RFQValidationSchema
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class ConfirmationHandler:
    """Handles RFQ confirmation workflow."""
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers,
                 auto_categorization_service: AutoCategorizationService,
                 enhanced_auto_categorization_service: EnhancedAutoCategorizationService,
                 seller_recommendation_service: SellerRecommendationService,
                 enhanced_seller_matching_service: EnhancedSellerMatchingService):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.auto_categorization_service = auto_categorization_service
        self.enhanced_auto_categorization_service = enhanced_auto_categorization_service
        self.seller_recommendation_service = seller_recommendation_service
        self.enhanced_seller_matching_service = enhanced_seller_matching_service
    
    async def handle_confirmation_button(self, user: User, session: ConversationSession, 
                                       button_id: str) -> Dict[str, Any]:
        """Handle Confirm/Modify confirmation button responses."""
        logger.info(f"Confirmation button response from {user.phone_number}: {button_id}")
        
        if button_id == "confirm_rfq":
            # User clicked "Confirm" - proceed with RFQ acceptance
            return await self._handle_rfq_acceptance(user, session, "Confirm")
            
        elif button_id == "no_rfq":
            # User clicked "Modify" - handle exactly like current "No" response
            return await self._handle_rfq_modification(user, session, "No")
        
        return {"status": "unknown_button", "button_id": button_id}
    
    async def handle_pending_confirmations(self, user: User, session: ConversationSession, 
                                         message: str, intent_result: Dict[str, Any]) -> Dict[str, Any]:
        """Handle pending confirmation responses."""
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
        """Handle optional field responses."""
        # Check if user wants to skip optional fields
        if any(keyword in message.lower() for keyword in ["no", "skip", "proceed", "continue", "next"]):
            # User wants to skip optional fields, proceed to confirmation
            return await self._proceed_to_confirmation_from_optional(user, session, message)
        else:
            # User provided optional information, process it and then proceed to confirmation
            return {"status": "continue_with_purchase_intent"}
    
    async def _handle_rfq_acceptance(self, user: User, session: ConversationSession, 
                                   message: str) -> Dict[str, Any]:
        """Handle RFQ acceptance and submission."""
        # Handle combined RFQ or single RFQ confirmations
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
            schema_data = product_info.get("schema_data", {})
            if schema_data:
                rfq_schema = RFQValidationSchema(**schema_data)
            else:
                rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"], None)
            
            gmt_result = await self._submit_rfq_to_backend(rfq_schema, user)
            rfq_results = [gmt_result]
            successful_count = 1 if gmt_result.get("success") else 0
        else:
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
        session.outcome = 'completed'
        session.completed_at = utc_now().replace(tzinfo=None)
        
        # Update core tracking fields (user type, categories, RFQ IDs)
        rfq_ids = []
        for result in rfq_results:
            if result.get("success") and result.get("rfq_id"):
                rfq_ids.append(result["rfq_id"])
        
        if rfq_ids:
            session.rfq_ids = rfq_ids
            # Set user type as buyer (since they're creating RFQs)
            session.user_type = 'buyer'
            # Calculate averages based on session data
            from app.services.helpers.session_helpers import SessionHelpers
            session = SessionHelpers.calculate_session_averages(session)
        
        # Clear session AFTER summarization data is captured
        session.workflow_state = {"extracted_entities": []}
        
        return {"status": "multiple_rfqs_created", "successful_count": successful_count}
    
    async def _handle_rfq_modification(self, user: User, session: ConversationSession, 
                                     message: str) -> Dict[str, Any]:
        """Handle RFQ modification requests."""
        logger.info("User declined or has conditions - treating as modification request")
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
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"], None)
            
            # Load summaries for enhanced response generation
            chat_summaries = []  # Could load from chat summary service if needed
            
            summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
                "user_message": message,
                "extracted_entities": product_info["entities"]
            }, chat_summaries)
            
            # Send confirmation message with buttons combined
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                "RFQ Confirmation", 
                summary_response,
                buttons_config
            )
            
            # Move to confirmation state
            session.workflow_state["pending_rfq"] = product_info
            del session.workflow_state["pending_optional_rfq"]
            
        elif session.workflow_state.get("pending_optional_combined_rfq"):
            # Combined RFQ with multiple items
            combined_data = session.workflow_state["pending_optional_combined_rfq"]
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
            
            # Send confirmation message with buttons combined
            buttons_config = [
                {"id": "confirm_rfq", "title": "Confirm"},
                {"id": "no_rfq", "title": "Modify"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                "RFQ Confirmation",
                summary_response,
                buttons_config
            )
            
            # Move to confirmation state
            session.workflow_state["pending_combined_rfq"] = combined_data
            del session.workflow_state["pending_optional_combined_rfq"]
        
        return {"status": "optional_fields_skipped"}
    
    async def _submit_rfq_to_backend(self, rfq_schema, user) -> dict:
        """Submit RFQ directly to backend via GMT API service."""
        try:
            gmt_service = GMTAPIService()
            
            # Convert schema to dict using the newer method
            schema_dict = rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else rfq_schema.dict()
            
            # Extract attachments from schema
            attachments = schema_dict.get("attachments", [])
            
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
                "product_name": schema_dict.get("project_desc", "Unknown Product"),
                "quantity": schema_dict.get("items", [{}])[0].get("quantity", 1) if schema_dict.get("items") else 1,
                "unit_of_measure": schema_dict.get("items", [{}])[0].get("unit_of_measures", "pcs") if schema_dict.get("items") else "pcs",
                "division": schema_dict.get("division", "Admin & IT"),
                "specifications": schema_dict.get("items", [{}])[0].get("description", "") if schema_dict.get("items") else "",
                "preferred_brand": schema_dict.get("preferred_brand", ""),
                "delivery_state": schema_dict.get("delivery_locations", [{}])[0].get("state", "Karnataka") if schema_dict.get("delivery_locations") else "Karnataka",
                "delivery_city": schema_dict.get("delivery_locations", [{}])[0].get("city", "Bangalore") if schema_dict.get("delivery_locations") else "Bangalore",
                "delivery_pincode": schema_dict.get("delivery_locations", [{}])[0].get("pincode", "560001") if schema_dict.get("delivery_locations") else "560001",
                "deadline": schema_dict.get("delivery_date"),
                "remarks": schema_dict.get("remarks", "Created via AI Procurement WhatsApp Bot"),
                "attachments": attachments  # Include attachments in the data passed to GMT API
            }
            
            # Submit to backend via GMT API
            logger.info(f"Creating RFQ with user_id={user.id}, org_id={user.org_id}")
            result = await gmt_service.create_rfq(rfq_data, user_id=user.id, org_id=user.org_id)
            
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
        rfq_ids = []
        for result in rfq_results:
            if result.get("success") and result.get("rfq_id"):
                rfq_ids.append(result["rfq_id"])
        if rfq_ids:
            # Single RFQ case (matches your example format)
            if successful_count == 1:
                response = f"Thank you! Your RFQ has been created successfully.\n\nRFQ ID: {rfq_ids[0]}\nUse this reference number to track your request."
            # Multiple RFQs case
            else:
                rfq_ids_text = "\n".join([f"RFQ ID: {rfq_id}" for rfq_id in rfq_ids])
                response = f"Thank you! All {successful_count} RFQs have been created successfully.\n\n{rfq_ids_text}\n\nUse these reference numbers to track your requests."
        else:
            response = f"Thank you! All {successful_count} RFQs have been created successfully."

        # Add closing message (optional)
        response += "\n\nIf there is anything else I can assist you with, please let me know."

        await self.whatsapp_service.send_message(user.phone_number, response)
    
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