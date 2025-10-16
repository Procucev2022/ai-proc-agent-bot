"""
Products Array Handler.

Handles processing of product arrays (single or multiple products) including
validation, completeness checking, and workflow routing.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any, List
from app.models import WorkflowType, User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.utils.datetime_utils import utc_now
from app.utils.rfq_message_formatter import format_rfq_response_message

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
                                  message: str, products: list, chat_summaries: list = None,
                                  date_validation_error: bool = False) -> Dict[str, Any]:
        """Handle products array (single or multiple products)."""
        try:
            print(f"ProductsArrayHandler: Processing {len(products)} products")
            logger.info(f"Handling {len(products)} products from message")

            # Check if we have existing incomplete products that need to be merged with new data
            existing_incomplete = session.workflow_state.get("incomplete_products", [])
            if existing_incomplete:
                print(f"ProductsArrayHandler: Found {len(existing_incomplete)} existing incomplete products, merging with new data")
                products = await self._merge_with_existing_incomplete_products(existing_incomplete, products)
                print(f"ProductsArrayHandler: After merging, processing {len(products)} total products")

            # Track categories from all products in product_items
            await self._track_product_categories(session, products)

            # Check completeness for each product and identify which ones need more info
            incomplete_products, complete_products = await self._categorize_products_by_completeness(products)
            
            # If any product is incomplete, collect all questions from data model
            if incomplete_products:
                return await self._handle_incomplete_products(
                    user, session, message, products, incomplete_products, complete_products, 
                    chat_summaries, date_validation_error
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
                                        complete_products: list, chat_summaries: list,
                                        date_validation_error: bool = False) -> Dict[str, Any]:
        """Handle incomplete products by generating clarification questions."""
        print(f"ProductsArrayHandler: Found {len(incomplete_products)} incomplete products")

        # Check if no products are mentioned (all fields are None)
        # Only show intro message if there are truly no products AND no existing incomplete products
        # This prevents the check from blocking supplementary data (like delivery info) from being applied
        existing_incomplete = session.workflow_state.get("incomplete_products", [])
        if self._no_products_mentioned(products) and not existing_incomplete:
            no_products_message = (
                "To proceed with your request, we will create a Request for Quotation (RFQ).\n\n"
                "To create your RFQ, please provide:\n"
                "• Items with quantities, brand & specifications (type here or attach an Excel)\n"
                "• Delivery date\n"
                "• Delivery location (Please just give the pincode directly)\n\n"
                "Once I have these details, I can help raise the RFQ and ensure timely processing."
            )
            await self.whatsapp_service.send_message(user.phone_number, no_products_message)
            return {
                "status": "no_products_mentioned",
                "total_products": 0
            }
        
        all_questions, all_missing_fields = await self._generate_clarification_questions(incomplete_products)
        
        # Calculate overall completeness
        total_mandatory_fields = sum(len(prod["missing_fields"]) for prod in incomplete_products)
        filled_fields = len(products) * 5 - total_mandatory_fields  # Rough estimate
        completeness = max(10, (filled_fields / (len(products) * 5)) * 100)
        
        # Store incomplete products for follow-up (serialize datetime objects)
        session.workflow_state["incomplete_products"] = ChatServiceHelpers.serialize_products_for_session(incomplete_products)
        session.workflow_state["complete_products"] = ChatServiceHelpers.serialize_products_for_session(complete_products)

        # Store ALL products (both complete and incomplete) in extracted_entities for full context
        # This ensures that entity extraction and modification requests have access to all products
        all_products_entities = []
        for prod in complete_products:
            all_products_entities.append(prod["entities"])
        for prod in incomplete_products:
            all_products_entities.append(prod["entities"])

        session.workflow_state["extracted_entities"] = ChatServiceHelpers.serialize_products_for_session(all_products_entities)
        print(f"ProductsArrayHandler: Stored {len(all_products_entities)} total products in extracted_entities ({len(complete_products)} complete, {len(incomplete_products)} incomplete)")

        await self.session_manager.save_session(session, WorkflowType.rfq_creation)
        
        # Keep questions as a list for proper bullet formatting
        print(f"  Final clarification questions: {all_questions}")
        
        # Build context and send response directly
        context = ChatServiceHelpers.build_context("clarification", message, {}, completeness,
            missing_fields=all_missing_fields,
            total_products=len(products),
            incomplete_products=len(incomplete_products)
        )
        
        # Add products to context for date validation error extraction
        context["products"] = products
        context["date_validation_error"] = date_validation_error
        
        # Add extracted entities to context for enhanced formatting
        context["extracted_entities"] = [prod["entities"] for prod in incomplete_products]
        
        response = await self.response_helpers.generate_clarification_response(all_questions, completeness, context, chat_summaries)
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

        # Check if any product has date validation error
        has_date_error = any(prod["entities"].get("date_validation_error") for prod in incomplete_products)
        
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
            await self._generate_combined_questions(incomplete_products, all_questions, all_missing_fields, has_date_error)
        else:
            # Products have different missing fields - ask individually
            await self._generate_individual_questions(incomplete_products, all_questions, all_missing_fields, has_date_error)

        # Remove duplicate questions while preserving order
        all_questions = list(dict.fromkeys(all_questions))
        print(f"  All questions to ask: {all_questions}")
        return all_questions, all_missing_fields
    
    async def _generate_combined_questions(self, incomplete_products: list, all_questions: list, all_missing_fields: list, has_date_error: bool = False):
        """Generate combined questions for products with same missing fields."""
        rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(incomplete_products[0]["entities"], self.openai_service)
        combined_questions = rfq_schema.get_combined_questions()

        if combined_questions["has_mandatory"] or combined_questions["has_optional"]:
            # Safely generate product names with fallbacks
            product_names = []
            for i, prod in enumerate(incomplete_products):
                description = prod["entities"].get("description")
                if description and description.strip():
                    product_names.append(description)
                else:
                    # Use index from prod dict, or fallback to list index
                    index = prod.get("index", i + 1)
                    product_names.append(f"Product {index}")

            # Add mandatory fields first - filter out None values and delivery date if there's a date error
            if combined_questions["has_mandatory"]:
                mandatory_questions = [q for q in combined_questions["mandatory"] if q is not None and str(q).strip()]
                if has_date_error:
                    # Filter out delivery date question when there's a date validation error
                    mandatory_questions = [q for q in mandatory_questions if "delivery date" not in q.lower()]
                all_questions.extend(mandatory_questions)

            all_missing_fields.extend(incomplete_products[0]["missing_fields"])
    
    async def _generate_individual_questions(self, incomplete_products: list, all_questions: list, all_missing_fields: list, has_date_error: bool = False):
        """Generate individual questions for products with different missing fields."""
        # First, identify delivery fields that should always be asked for all products
        delivery_fields = {'delivery_date', 'delivery_location_0_state', 'delivery_location_0_city', 'delivery_location_0_pincode'}

        # Collect all delivery questions needed across all products
        all_delivery_missing = set()
        product_specific_questions = []

        for prod in incomplete_products:
            prod_missing = set(prod["missing_fields"])
            delivery_missing = prod_missing & delivery_fields
            product_specific_missing = prod_missing - delivery_fields

            all_delivery_missing.update(delivery_missing)

            if product_specific_missing:
                product_specific_questions.append({
                    "product": prod,
                    "missing_fields": list(product_specific_missing)
                })

        # Ask delivery questions once for all products (if any)
        if all_delivery_missing:
            # Safely generate product names with fallbacks
            product_names = []
            for i, prod in enumerate(incomplete_products):
                description = prod["entities"].get("description")
                if description and description.strip():
                    product_names.append(description)
                else:
                    # Use index from prod dict, or fallback to list index
                    index = prod.get("index", i + 1)
                    product_names.append(f"Product {index}")

            # Don't add any prefix for delivery questions - they apply to all products by default

            # Get delivery question text from schema
            sample_schema = ChatServiceHelpers.create_rfq_schema_from_entities(incomplete_products[0]["entities"], self.openai_service)
            combined_questions = sample_schema.get_combined_questions()

            # Filter to only include delivery-related questions - filter out None values and delivery date if there's a date error
            for question in combined_questions.get("mandatory", []):
                if question is not None and str(question).strip():
                    if has_date_error and "delivery date" in question.lower():
                        continue  # Skip delivery date question when there's a date validation error
                    all_questions.append(question)

            all_missing_fields.extend(list(all_delivery_missing))

        # Ask product-specific questions individually
        for item in product_specific_questions:
            prod = item["product"]
            missing_fields = item["missing_fields"]

            product_desc = prod["entities"].get("description", f"Product {prod['index']}")
            rfq_schema = ChatServiceHelpers.create_rfq_schema_from_entities(prod["entities"], self.openai_service)
            combined_questions = rfq_schema.get_combined_questions()

            print(f"  Processing product-specific questions for {product_desc}: {missing_fields}")

            if combined_questions["has_mandatory"]:
                # Only add product prefix if there are multiple products
                if len(incomplete_products) > 1:
                    all_questions.append(f"For {product_desc}:")
                # Filter out None values from mandatory questions and delivery date if there's a date error
                mandatory_questions = [q for q in combined_questions["mandatory"] if q is not None and str(q).strip()]
                if has_date_error:
                    mandatory_questions = [q for q in mandatory_questions if "delivery date" not in q.lower()]
                all_questions.extend(mandatory_questions)
                all_missing_fields.extend(missing_fields)
    
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

            # Show extracted details using RFQ message formatter
            global_fields = {
                'deliveryDate': product_info["entities"].get('deliveryDate'),
                'state': product_info["entities"].get('state'),
                'city': product_info["entities"].get('city'),
                'pincode': product_info["entities"].get('pincode')
            }
            formatted_message = format_rfq_response_message([product_info["entities"]], global_fields, optional_questions, include_optional=True)
          
            optional_message = f"{formatted_message}\n\nIf yes, please upload them now — or click on ‘Continue’ to proceed."
            
            # Send message with Continue button
            buttons_config = [
                {"id": "continue_rfq", "title": "Continue"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                optional_message,
                buttons_config,
                "Optional Information"
            )
            
            # Mark that we've asked about optional fields
            session.workflow_state["optional_fields_asked"] = True
            session.workflow_state["pending_optional_rfq"] = ChatServiceHelpers.serialize_products_for_session({
                "index": product_info["index"],
                "entities": product_info["entities"],
                "schema_data": rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else {}
            })

            # Store complete product in extracted_entities for full context
            session.workflow_state["extracted_entities"] = ChatServiceHelpers.serialize_products_for_session([product_info["entities"]])
            print(f"ProductsArrayHandler: Stored 1 complete product in extracted_entities (optional fields stage)")

            await self.session_manager.save_session(session, WorkflowType.rfq_creation)
            
            return {
                "status": "optional_fields_inquiry",
                "total_products": 1
            }
        
        # Generate confirmation (either optional fields were completed or user declined)
        summary_response = await self.response_helpers.generate_rfq_summary_and_confirmation(rfq_schema, {
            "user_message": message,
            "extracted_entities": product_info["entities"]
        }, chat_summaries)

        summary_response += (
            "\n\nPlease review the above details carefully. "
            'If everything is correct, kindly click "Confirm" to proceed with the RFQ creation. '
            'If you wish to make any changes, click "Add or Modify."'
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
        
        # Store for confirmation (serialize schema to dict)
        product_info_serializable = {
            "index": product_info["index"],
            "entities": product_info["entities"],
            "schema_data": rfq_schema.model_dump() if hasattr(rfq_schema, 'model_dump') else {}
        }
        session.workflow_state["pending_rfq"] = ChatServiceHelpers.serialize_products_for_session(product_info_serializable)

        # Store complete product in extracted_entities for full context
        session.workflow_state["extracted_entities"] = ChatServiceHelpers.serialize_products_for_session([product_info["entities"]])
        print(f"ProductsArrayHandler: Stored 1 complete product in extracted_entities")

        # Clear incomplete products since we're now in confirmation phase
        self._clear_workflow_state(session)
        await self.session_manager.save_session(session, WorkflowType.rfq_creation)
        
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
            # Show extracted details using RFQ message formatter
            all_products_entities = [prod["entities"] for prod in complete_products]

            # Extract global fields from first product
            global_fields = {}
            if all_products_entities:
                first_entity = all_products_entities[0]
                global_fields = {
                    'deliveryDate': first_entity.get('deliveryDate'),
                    'state': first_entity.get('state'),
                    'city': first_entity.get('city'),
                    'pincode': first_entity.get('pincode')
                }
            formatted_message = format_rfq_response_message(all_products_entities, global_fields, optional_questions, include_optional=True)

            optional_message = f"{formatted_message}\n\n If yes, please upload them now — or click on ‘Continue’ to proceed."
            
            # Send message with Continue button
            buttons_config = [
                {"id": "continue_rfq", "title": "Continue"}
            ]
            await self.whatsapp_service.send_configurable_buttons(
                user.phone_number,
                optional_message,
                buttons_config,
                "Optional Information"
            )

            # Mark that we've asked about optional fields
            session.workflow_state["optional_fields_asked"] = True
            session.workflow_state["pending_optional_combined_rfq"] = {
                "combined_schema": combined_schema.model_dump() if hasattr(combined_schema, 'model_dump') else combined_schema.dict(),
                "products": ChatServiceHelpers.serialize_products_for_session(complete_products)
            }

            # Store all complete products in extracted_entities for full context
            session.workflow_state["extracted_entities"] = ChatServiceHelpers.serialize_products_for_session(all_products_entities)
            print(f"ProductsArrayHandler: Stored {len(all_products_entities)} complete products in extracted_entities (optional fields stage)")

            # Clear incomplete products now that all products are complete and we're asking for optional fields
            if "incomplete_products" in session.workflow_state:
                del session.workflow_state["incomplete_products"]

            await self.session_manager.save_session(session, WorkflowType.rfq_creation)
            
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
        
        # Store for confirmation (single combined RFQ)
        session.workflow_state["pending_combined_rfq"] = {
            "combined_schema": combined_schema.model_dump() if hasattr(combined_schema, 'model_dump') else combined_schema.dict(),
            "products": ChatServiceHelpers.serialize_products_for_session(complete_products)
        }

        # Store all complete products in extracted_entities for full context
        all_products_entities = [prod["entities"] for prod in complete_products]
        session.workflow_state["extracted_entities"] = ChatServiceHelpers.serialize_products_for_session(all_products_entities)
        print(f"ProductsArrayHandler: Stored {len(all_products_entities)} complete products in extracted_entities")

        # Clear incomplete products since we're now in confirmation phase
        self._clear_workflow_state(session)
        await self.session_manager.save_session(session, WorkflowType.rfq_creation)
        
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

    def _no_products_mentioned(self, products: list) -> bool:
        """Check if no products are mentioned using OpenAI's special indicator."""
        if not products:
            return True

        for product in products:
            # Check for OpenAI's special indicator
            description = product.get('description')
            if description == "NO_PRODUCTS_MENTIONED":
                return True

            # Fallback: check for generic descriptions (backward compatibility)
            generic_descriptions = {'general product', 'general purchase', 'product', 'item', 'items', 'something',
                                    'purchase'}
            if description and str(description).strip().lower() not in generic_descriptions:
                return False

            # Check other meaningful fields
            meaningful_fields = ['projectDesc', 'quantity', 'brand']
            for field in meaningful_fields:
                value = product.get(field)
                if value is not None and str(value).strip():
                    return False

        return True

    async def _merge_with_existing_incomplete_products(self, existing_incomplete: list, new_products: list) -> list:
        """
        Merge newly extracted product data with existing incomplete products.

        Logic:
        1. If new products contain only supplementary info (delivery location, date),
           merge this info into all existing products that need it
        2. If new products contain actual new product descriptions,
           add them as additional products
        3. Return the merged list of all products
        """
        try:
            print(f"ProductsArrayHandler: Merging {len(existing_incomplete)} existing with {len(new_products)} new products")

            # Extract existing product entities
            existing_entities = []
            for existing_prod in existing_incomplete:
                if isinstance(existing_prod, dict) and "entities" in existing_prod:
                    existing_entities.append(existing_prod["entities"])
                else:
                    existing_entities.append(existing_prod)

            # Check if new products are re-extractions of existing products or genuinely new ones
            new_products_with_descriptions = []
            supplementary_data = {}

            # Get existing product descriptions for comparison
            existing_descriptions = set()
            for existing_entity in existing_entities:
                desc = existing_entity.get("description") or existing_entity.get("projectDesc")
                if desc:
                    existing_descriptions.add(desc.lower().strip())

            for new_product in new_products:
                has_description = bool(new_product.get("description") or new_product.get("projectDesc"))

                if has_description:
                    # Check if this is a re-extraction of an existing product
                    new_desc = (new_product.get("description") or new_product.get("projectDesc", "")).lower().strip()

                    if new_desc in existing_descriptions:
                        # This is a re-extraction of an existing product, treat as supplementary data for that product
                        print(f"ProductsArrayHandler: Detected re-extraction of existing product: {new_desc}")
                        # Instead of adding as new product, we'll merge this data with the existing product later
                        continue
                    else:
                        # This is genuinely a new product
                        new_products_with_descriptions.append(new_product)
                        print(f"ProductsArrayHandler: Found genuinely new product: {new_desc}")
                else:
                    # This is supplementary data (location, date, etc.) that should be applied to existing products
                    for field in ["state", "city", "pincode", "deliveryDate", "division", "brand", "remarks"]:
                        if new_product.get(field):
                            supplementary_data[field] = new_product[field]

            print(f"ProductsArrayHandler: Found {len(new_products_with_descriptions)} new products with descriptions")
            print(f"ProductsArrayHandler: Found supplementary data: {list(supplementary_data.keys())}")

            # Create a map of re-extracted products by description for merging
            reextracted_products_map = {}
            for new_product in new_products:
                desc = (new_product.get("description") or new_product.get("projectDesc", "")).lower().strip()
                if desc and desc in existing_descriptions:
                    reextracted_products_map[desc] = new_product

            # Merge data into existing products
            merged_products = []
            for existing_entity in existing_entities:
                merged_entity = existing_entity.copy()

                # Check if we have a re-extracted version of this product with new data
                existing_desc_raw = existing_entity.get("description") or existing_entity.get("projectDesc") or ""
                existing_desc = existing_desc_raw.lower().strip() if existing_desc_raw else ""
                if existing_desc and existing_desc in reextracted_products_map:
                    reextracted_product = reextracted_products_map[existing_desc]
                    print(f"ProductsArrayHandler: Merging re-extracted data for product: {existing_desc}")

                    # Merge all non-None fields from re-extracted product
                    for field, value in reextracted_product.items():
                        if value is not None and (merged_entity.get(field) is None or merged_entity.get(field) == ""):  # Only fill if field is None or empty
                            merged_entity[field] = value
                            print(f"ProductsArrayHandler: Applied {field}={value} to {existing_desc}")
                    
                    # Handle date validation error updates
                    if "date_validation_error" in reextracted_product:
                        # Only update if there's actually an error message
                        if reextracted_product["date_validation_error"]:
                            merged_entity["date_validation_error"] = reextracted_product["date_validation_error"]
                            print(f"ProductsArrayHandler: Updated date_validation_error for {existing_desc}")
                        else:
                            # Empty error message means clear the error
                            if "date_validation_error" in merged_entity:
                                del merged_entity["date_validation_error"]
                                print(f"ProductsArrayHandler: Cleared empty date_validation_error for {existing_desc}")
                    
                    # Clear date validation error if delivery date is now valid and no error in new extraction
                    if reextracted_product.get("deliveryDate") and not reextracted_product.get("date_validation_error"):
                        if "date_validation_error" in merged_entity:
                            del merged_entity["date_validation_error"]
                            print(f"ProductsArrayHandler: Cleared date_validation_error for {existing_desc} (valid date provided)")
                    
                    # Handle pincode validation error updates
                    if "pincode_validation_error" in reextracted_product:
                        # Only update if there's actually an error message
                        if reextracted_product["pincode_validation_error"]:
                            merged_entity["pincode_validation_error"] = reextracted_product["pincode_validation_error"]
                            print(f"ProductsArrayHandler: Updated pincode_validation_error for {existing_desc}")
                        else:
                            # Empty error message means clear the error
                            if "pincode_validation_error" in merged_entity:
                                del merged_entity["pincode_validation_error"]
                                print(f"ProductsArrayHandler: Cleared empty pincode_validation_error for {existing_desc}")
                    
                    # Clear pincode validation error if valid location data is provided
                    if reextracted_product.get("pincode") and reextracted_product.get("city") and reextracted_product.get("state") and not reextracted_product.get("pincode_validation_error"):
                        if "pincode_validation_error" in merged_entity:
                            del merged_entity["pincode_validation_error"]
                            print(f"ProductsArrayHandler: Cleared pincode_validation_error for {existing_desc} (valid location provided)")

                # Apply global supplementary data to fields that are missing or None
                for field, value in supplementary_data.items():
                    if merged_entity.get(field) is None or merged_entity.get(field) == "":  # Only fill if field is None or empty
                        merged_entity[field] = value
                        print(f"ProductsArrayHandler: Applied supplementary {field}={value} to existing product")
                


                # Final cleanup: Clear validation errors if valid data exists
                if merged_entity.get("deliveryDate") and "date_validation_error" in merged_entity:
                    del merged_entity["date_validation_error"]
                    print(f"ProductsArrayHandler: Final cleanup - cleared date_validation_error for valid delivery date")
                
                if merged_entity.get("pincode") and merged_entity.get("city") and merged_entity.get("state") and "pincode_validation_error" in merged_entity:
                    del merged_entity["pincode_validation_error"]
                    print(f"ProductsArrayHandler: Final cleanup - cleared pincode_validation_error for valid location data")
                
                merged_products.append(merged_entity)

            # Add any new products with descriptions
            merged_products.extend(new_products_with_descriptions)

            print(f"ProductsArrayHandler: Final merged result: {len(merged_products)} total products")
            return merged_products

        except Exception as e:
            logger.error(f"Error merging products: {e}")
            # Fallback: return existing + new
            return existing_entities + new_products if 'existing_entities' in locals() else new_products