"""
Products Array Handler.

Handles processing of product arrays (single or multiple products) including
validation, completeness checking, and workflow routing.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any, List
from app.models import User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class ProductsArrayHandler:
    """Handles products array processing."""
    
    def __init__(self, whatsapp_service: WhatsAppService, openai_service: OpenAIService,
                 response_helpers: ResponseHelpers, session_manager):
        self.whatsapp_service = whatsapp_service
        self.openai_service = openai_service
        self.response_helpers = response_helpers
        self.session_manager = session_manager
    
    async def handle_products_array(self, user: User, session: ConversationSession, 
                                  message: str, products: list, chat_summaries: list = None) -> Dict[str, Any]:
        """Handle products array (single or multiple products)."""
        try:
            print(f"ProductsArrayHandler: Processing {len(products)} products")
            logger.info(f"Handling {len(products)} products from message")
            
            # Track categories from all products in product_items
            await self._track_product_categories(session, products)
            
            # Check completeness for each product and identify which ones need more info
            incomplete_products, complete_products = await self._categorize_products_by_completeness(products)
            
            # If any product is incomplete, collect all questions from data model
            if incomplete_products:
                return await self._handle_incomplete_products(
                    user, session, message, products, incomplete_products, complete_products, chat_summaries
                )
            else:
                print(f"ProductsArrayHandler: All {len(complete_products)} products are complete!")
                return await self._handle_complete_products(
                    user, session, message, complete_products, chat_summaries
                )
                    
        except Exception as e:
            logger.error(f"Error in products array handler: {e}")
            return {
                "status": "error",
                "error": f"Could you tell me more about what you need? ({str(e)})"
            }
    
    async def _track_product_categories(self, session: ConversationSession, products: list):
        """Track categories from all products in product_items."""
        for product_entities in products:
            if isinstance(product_entities, dict):
                category = product_entities.get('category') or product_entities.get('description')
                if category:
                    if not session.product_items:
                        session.product_items = []
                    # Add product info with category to product_items
                    product_info = {
                        'category': category,
                        'description': product_entities.get('description'),
                        'added_at': utc_now().isoformat()
                    }
                    session.product_items.append(product_info)
    
    async def _categorize_products_by_completeness(self, products: list) -> tuple:
        """Categorize products into complete and incomplete based on mandatory fields."""
        incomplete_products = []
        complete_products = []
        
        for i, product_entities in enumerate(products):
            try:
                # Transform entities to schema format
                rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_entities, self.openai_service)
                
                # Check if this product has all mandatory fields
                missing_mandatory = rfq_schema.get_missing_mandatory_fields()
                
                # Debug logging
                logger.info(f"Product {i+1} entities: {product_entities}")
                logger.info(f"Product {i+1} missing mandatory: {missing_mandatory}")
                
                if len(missing_mandatory) == 0:
                    complete_products.append({
                        "index": i + 1,
                        "entities": product_entities
                    })
                else:
                    incomplete_products.append({
                        "index": i + 1,
                        "entities": product_entities,
                        "missing_fields": missing_mandatory
                    })
                    
            except Exception as e:
                logger.warning(f"Error validating product {i+1}: {e}")
                incomplete_products.append({
                    "index": i + 1,
                    "entities": product_entities,
                    "missing_fields": ["project_desc", "delivery_date", "division"]
                })
        
        return incomplete_products, complete_products
    
    async def _handle_incomplete_products(self, user: User, session: ConversationSession,
                                        message: str, products: list, incomplete_products: list,
                                        complete_products: list, chat_summaries: list) -> Dict[str, Any]:
        """Handle incomplete products by generating clarification questions."""
        print(f"ProductsArrayHandler: Found {len(incomplete_products)} incomplete products")
        
        all_questions, all_missing_fields = await self._generate_clarification_questions(incomplete_products)
        
        # Calculate overall completeness
        total_mandatory_fields = sum(len(prod["missing_fields"]) for prod in incomplete_products)
        filled_fields = len(products) * 5 - total_mandatory_fields  # Rough estimate
        completeness = max(10, (filled_fields / (len(products) * 5)) * 100)
        
        # Store incomplete products for follow-up (serialize datetime objects)
        session.workflow_state["incomplete_products"] = ChatServiceHelpers.serialize_products_for_session(incomplete_products)
        session.workflow_state["complete_products"] = ChatServiceHelpers.serialize_products_for_session(complete_products)
        await self.session_manager.save_session(session, 'rfq_creation')
        
        # Generate and send clarification response directly with our specific questions
        clarification_message = "\n".join(all_questions)
        print(f"  Final clarification message: {clarification_message}")
        
        # Build context and send response directly
        context = ChatServiceHelpers.build_context("clarification", message, {}, completeness,
            missing_fields=all_missing_fields,
            total_products=len(products),
            incomplete_products=len(incomplete_products)
        )
        
        # Add products to context for date validation error extraction
        context["products"] = products
        
        response = await self.response_helpers.generate_clarification_response([clarification_message], completeness, context, chat_summaries)
        await self.whatsapp_service.send_message(user.phone_number, response)
        
        return {
            "status": "products_incomplete",
            "total_products": len(products),
            "incomplete_products": len(incomplete_products)
        }
    
    async def _generate_clarification_questions(self, incomplete_products: list) -> tuple:
        """Generate clarification questions for incomplete products."""
        all_questions = []
        all_missing_fields = []
        
        # Group missing fields across all products to avoid repetition
        common_missing_fields = set()
        for prod in incomplete_products:
            common_missing_fields.update(prod["missing_fields"])
        
        # Check if all products have the same missing fields
        all_same_missing = True
        first_missing = set(incomplete_products[0]["missing_fields"])
        print(f"  First product missing fields: {first_missing}")
        
        for prod in incomplete_products[1:]:
            prod_missing = set(prod["missing_fields"])
            print(f"  Product {prod['index']} missing fields: {prod_missing}")
            if prod_missing != first_missing:
                all_same_missing = False
                break
        
        print(f"  All products have same missing fields: {all_same_missing}")
        
        if all_same_missing and len(incomplete_products) > 1:
            # All products missing the same fields - ask once for all
            await self._generate_combined_questions(incomplete_products, all_questions, all_missing_fields)
        else:
            # Products have different missing fields - ask individually
            await self._generate_individual_questions(incomplete_products, all_questions, all_missing_fields)
        
        print(f"  All questions to ask: {all_questions}")
        return all_questions, all_missing_fields
    
    async def _generate_combined_questions(self, incomplete_products: list, all_questions: list, all_missing_fields: list):
        """Generate combined questions for products with same missing fields."""
        rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(incomplete_products[0]["entities"], self.openai_service)
        combined_questions = rfq_schema.get_combined_questions()
        
        if combined_questions["has_mandatory"] or combined_questions["has_optional"]:
            product_names = [prod["entities"].get("description", f"Product {prod['index']}") for prod in incomplete_products]
            all_questions.append(f"For all products ({', '.join(product_names)}):")
            
            # Add mandatory fields first
            if combined_questions["has_mandatory"]:
                # all_questions.append("*Required information:*")
                all_questions.extend(combined_questions["mandatory"])
            

                
            all_missing_fields.extend(incomplete_products[0]["missing_fields"])
    
    async def _generate_individual_questions(self, incomplete_products: list, all_questions: list, all_missing_fields: list):
        """Generate individual questions for products with different missing fields."""
        for prod in incomplete_products:
            print(f"  Processing incomplete product {prod['index']}: {prod['entities'].get('description', 'Unknown')}")
            print(f"    Missing fields: {prod['missing_fields']}")
            
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(prod["entities"], self.openai_service)
            
            # Get combined questions from data model
            combined_questions = rfq_schema.get_combined_questions()
            product_desc = prod["entities"].get("description", f"Product {prod['index']}")
            
            print(f"    Combined questions: mandatory={len(combined_questions['mandatory'])}, optional={len(combined_questions['optional'])}")
            
            # Format questions for this product
            if combined_questions["has_mandatory"] or combined_questions["has_optional"]:
                if len(incomplete_products) > 1:
                    all_questions.append(f"For {product_desc}:")
                
                # Add mandatory fields first
                if combined_questions["has_mandatory"]:
                    # all_questions.append("*Required information:*")
                    all_questions.extend(combined_questions["mandatory"])
                

                    
                all_missing_fields.extend(prod["missing_fields"])
    
    async def _handle_complete_products(self, user: User, session: ConversationSession,
                                      message: str, complete_products: list, chat_summaries: list) -> Dict[str, Any]:
        """Handle complete products by checking optional fields or sending confirmation."""
        if len(complete_products) == 1:
            return await self._handle_single_complete_product(user, session, message, complete_products[0], chat_summaries)
        else:
            return await self._handle_multiple_complete_products(user, session, message, complete_products, chat_summaries)
    
    async def _handle_single_complete_product(self, user: User, session: ConversationSession,
                                            message: str, product_info: dict, chat_summaries: list) -> Dict[str, Any]:
        """Handle single complete product."""
        # Recreate schema from entities
        rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(product_info["entities"])
        
        # Check if user wants to provide optional information
        optional_questions = rfq_schema.get_optional_questions()
        
        # Check if this is a response to optional questions (look for specific workflow state)
        if optional_questions and not session.workflow_state.get("optional_fields_asked"):
            # Ask about optional fields first
            optional_intro = "Would you like to provide any additional details such as:"
            optional_text = "\n".join(f"• {q}" for q in optional_questions)
            optional_message = f"{optional_intro}\n\n{optional_text}\n\n You may send the details now or reply “No” to continue."
            
            await self.whatsapp_service.send_message(user.phone_number, optional_message)
            
            # Mark that we've asked about optional fields
            session.workflow_state["optional_fields_asked"] = True
            session.workflow_state["pending_optional_rfq"] = ChatServiceHelpers.serialize_products_for_session({
                "index": product_info["index"],
                "entities": product_info["entities"],
                "schema_data": rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else {}
            })
            
            await self.session_manager.save_session(session, 'rfq_creation')
            
            return {
                "status": "optional_fields_inquiry",
                "total_products": 1
            }
        
        # Generate confirmation (either optional fields were completed or user declined)
        summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
            "user_message": message,
            "extracted_entities": product_info["entities"]
        }, chat_summaries)
        await self.whatsapp_service.send_message(user.phone_number, summary_response)
        
        # Store for confirmation (serialize schema to dict)
        product_info_serializable = {
            "index": product_info["index"],
            "entities": product_info["entities"],
            "schema_data": rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else {}
        }
        session.workflow_state["pending_rfq"] = ChatServiceHelpers.serialize_products_for_session(product_info_serializable)
        
        # Clear incomplete products since we're now in confirmation phase
        self._clear_workflow_state(session)
        await self.session_manager.save_session(session, 'rfq_creation')
        
        return {
            "status": "single_product_confirmation",
            "total_products": 1
        }
    
    async def _handle_multiple_complete_products(self, user: User, session: ConversationSession,
                                               message: str, complete_products: list, chat_summaries: list) -> Dict[str, Any]:
        """Handle multiple complete products."""
        # Create a single combined RFQ schema instead of separate ones
        combined_schema = ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products(complete_products)
        
        if not combined_schema:
            return {"success": False, "message": "Failed to create RFQ schema"}
        
        # Check if we should ask about optional fields
        optional_questions = combined_schema.get_optional_questions()
        
        # Check if this is a response to optional questions
        if optional_questions and not session.workflow_state.get("optional_fields_asked"):
            # Use OpenAI to generate a natural optional fields message
            optional_context = {
                "conversation_stage": "optional_fields_inquiry",
                "user_message": message,
                "extracted_entities": [prod["entities"] for prod in complete_products],
                "total_products": len(complete_products),
                "product_descriptions": [prod["entities"].get("description", f"Product {i+1}") for i, prod in enumerate(complete_products)],
                "available_optional_fields": optional_questions,
                "action_needed": "Ask about optional fields for multiple products in a natural, user-friendly way"
            }
            
            optional_message = await self.response_helpers.generate_contextual_response(
                optional_context, 
                optional_questions,
                "optional_fields_inquiry",
                chat_summaries
            )
            
            await self.whatsapp_service.send_message(user.phone_number, optional_message)
            
            # Mark that we've asked about optional fields
            session.workflow_state["optional_fields_asked"] = True
            session.workflow_state["pending_optional_combined_rfq"] = {
                "combined_schema": combined_schema.model_dump() if hasattr(combined_schema, 'model_dump') else combined_schema.dict(),
                "products": ChatServiceHelpers.serialize_products_for_session(complete_products)
            }
            
            await self.session_manager.save_session(session, 'rfq_creation')
            
            return {
                "status": "optional_fields_inquiry",
                "total_products": len(complete_products)
            }
        
        # Generate confirmation for the combined RFQ
        summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(
            combined_schema, 
            {
                "user_message": message,
                "extracted_entities": [prod["entities"] for prod in complete_products],
                "total_products": len(complete_products)
            },
            chat_summaries
        )
        await self.whatsapp_service.send_message(user.phone_number, summary_response)
        
        # Store for confirmation (single combined RFQ)
        session.workflow_state["pending_combined_rfq"] = {
            "combined_schema": combined_schema.model_dump() if hasattr(combined_schema, 'model_dump') else combined_schema.dict(),
            "products": ChatServiceHelpers.serialize_products_for_session(complete_products)
        }
        
        # Clear incomplete products since we're now in confirmation phase
        self._clear_workflow_state(session)
        await self.session_manager.save_session(session, 'rfq_creation')
        
        return {
            "status": "combined_rfq_confirmation",
            "total_products": len(complete_products)
        }
    
    def _clear_workflow_state(self, session: ConversationSession):
        """Clear workflow state fields."""
        fields_to_clear = ["incomplete_products", "complete_products", "optional_fields_asked"]
        for field in fields_to_clear:
            if field in session.workflow_state:
                del session.workflow_state[field]