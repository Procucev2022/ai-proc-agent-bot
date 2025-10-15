"""
Entity extraction service for parsing structured data from user messages.

This service focuses solely on extracting entities from user messages using OpenAI.
It does not handle completeness checking or question generation - that's handled
by the data model and orchestration layer.
"""

import json
import os
from datetime import datetime
from typing import Dict

from ..services.openai_service import OpenAIService
from ..utils.datetime_utils import format_date_display
from ..utils.pincode_lookup import get_location_from_pincode_async


class EntityService:
    """Entity extraction service using OpenAI function calling."""

    def __init__(self, openai_service=None):
        self.openai_service = openai_service or OpenAIService()

    async def extract_entities(self, message: str, context: dict = None, workflow_type: str = "buy_something") -> dict:
        """
        Extract entities using OpenAI function calling with modification context awareness and reference detection.
        
        Args:
            message: User message to extract entities from
            context: Optional context with existing entities, pending confirmations, and user history
            workflow_type: Type of workflow (buy_something, etc.)
            
        Returns:
            Dict containing extracted entities, potentially merged with existing data for modifications,
            or historical options for reference phrases
        """
        try:
            # Check if intent service already detected a reference request
            intent_context = context.get("intent_result") if context else None
            if intent_context and intent_context.get("intent") == "reference_request":
                reference_details = intent_context.get("context_analysis", {}).get("reference_details", {})
                if reference_details.get("reference_type") and reference_details.get("has_history"):
                    print(f"EntityService: Using intent-detected reference: {reference_details}")
                    return await self._handle_reference_extraction(message, context, {
                        "reference_type": reference_details.get("reference_type"),
                        "confidence": intent_context.get("confidence", 0),
                        "reasoning": intent_context.get("reasoning", ""),
                        "detected_phrases": []  # Not available from intent service
                    })
            
            # Check if this is a modification request based on workflow_type (set by intent classification)
            if workflow_type == "modification_request":
                print(f"EntityService: Workflow type is modification_request - handling as modification")
                return await self._handle_modification_extraction(message, context, workflow_type)
            else:
                # Standard entity extraction for other workflow types
                print(f"EntityService: Using standard extraction for workflow_type: {workflow_type}")
                return await self._handle_standard_extraction(message, context, workflow_type)

        except Exception as e:
            print(f"Entity extraction error: {e}")
            return {"products": [], "confidence": 0, "success": False}
    
    async def _handle_registration_extraction(self, message: str, context: dict = None, workflow_type: str = "buyer_registration") -> dict:
        """Handle entity extraction for registration data with context awareness."""
        try:
            
            # Build context from conversation history
            conversation_context = ""
            if context and context.get("conversation_history"):
                conversation_context = context["conversation_history"]
                logger.info(f"EntityService: Built conversation context: {conversation_context}")
            
            user_type = workflow_type.replace("_registration", "")
            existing_entities = context.get("registration_entities", {}) if context else {}
            
            logger.info(f"EntityService: User type: {user_type}")
            logger.info(f"EntityService: Existing entities: {existing_entities}")
            
            # Extract registration entities using OpenAI
            logger.info(f"EntityService: Calling OpenAI extract_registration_entities")
            response = await self.openai_service.extract_registration_entities(
                message=message,
                conversation_context=conversation_context,
                user_type=user_type,
                existing_entities=existing_entities
            )
            
            logger.info(f"EntityService: OpenAI response: {response}")
            
            result = {
                "entities": response.get("entities", {}),
                "confidence": response.get("confidence", 0),
                "success": response.get("success", True),
                "extracted_fields": response.get("extracted_fields", []),
                "reasoning": response.get("reasoning", "")
            }
            
            logger.info(f"EntityService: Final result: {result}")
            return result
            
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Registration entity extraction error: {e}", exc_info=True)
            return {"entities": {}, "confidence": 0, "success": False}
    
    async def _handle_standard_extraction(self, message: str, context: dict = None, workflow_type: str = "buy_something") -> dict:
        """Handle standard entity extraction for new requests."""
        # Build prompt for entity extraction
        prompt = f"Extract entities from: '{message}'"

        # Check for existing context to merge with
        existing_context = []

        # First priority: incomplete_products (ongoing data collection)
        if context and context.get("workflow_state"):
            incomplete_products = context["workflow_state"].get("incomplete_products", [])
            if incomplete_products:
                print(f"EntityService: Found {len(incomplete_products)} incomplete products in context")
                # Extract entities from incomplete products for context
                for prod in incomplete_products:
                    if isinstance(prod, dict) and "entities" in prod:
                        existing_context.append(prod["entities"])
                    elif isinstance(prod, dict):
                        existing_context.append(prod)

        # Second priority: extracted_entities (fallback)
        if not existing_context and context and context.get("extracted_entities"):
            extracted_entities = context["extracted_entities"]
            if isinstance(extracted_entities, list):
                existing_context = extracted_entities
            elif isinstance(extracted_entities, dict):
                existing_context = [extracted_entities]

        # Add existing context to prompt if found
        if existing_context:
            prompt += f"\n\nExisting products context (merge new information with these):\n{existing_context}"
            print(f"EntityService: Added {len(existing_context)} existing products to extraction context")

        # Call OpenAI extract_entities method
        response = await self.openai_service.extract_entities(
            message=prompt,
            workflow_type=workflow_type
        )

        # Handle both old single entity format and new multi-product format
        if "products" in response:
            # New multi-product format with global + item structure
            products = response.get("products", [])
            global_fields = {
                "deliveryDate": response.get("deliveryDate"),
                "state": response.get("state"),
                "city": response.get("city"),
                "pincode": response.get("pincode")
            }

            # Merge global fields into each product for backward compatibility
            merged_products = self._merge_global_fields_into_products(products, global_fields)

            # If we have existing context and the new extraction returned data, merge them intelligently
            if existing_context and merged_products:
                print(f"EntityService: Merging {len(merged_products)} newly extracted products with {len(existing_context)} existing products")
                merged_products = self._merge_new_extraction_with_existing_products(existing_context, merged_products, message)
            elif existing_context and not merged_products:
                # User only provided supplementary data (no product descriptions)
                # Apply the global fields to existing products
                print(f"EntityService: No new products extracted, applying supplementary data to {len(existing_context)} existing products")
                merged_products = self._apply_supplementary_data_to_existing_products(existing_context, global_fields)

            validated_products, has_date_validation_error = await self._validate_dates_in_products(merged_products, message)
            # Auto-fill city and state from pincode
            validated_products = await self._auto_fill_location_from_pincode(validated_products)

            print(f"EntityService: Final products array with {len(validated_products)} products")
            print(f"EntityService: Global fields: {global_fields}")
            for i, product in enumerate(validated_products):
                print(f"  Product {i+1}: {product}")
            return {
                "products": validated_products,
                "confidence": response.get("confidence", 0),
                "success": response.get("success", True),
                "date_validation_error": has_date_validation_error
            }
        else:
            # Backward compatibility for old single entity format - validate date
            entities = response.get("entities", {})
            validated_entities, has_date_validation_error = await self._validate_date_in_entity(entities, message)
            
            print(f"EntityService: Using backward compatibility with entities: {validated_entities}")
            return {
                "entities": validated_entities,
                "confidence": response.get("confidence", 0),
                "success": response.get("success", True),
                "date_validation_error": has_date_validation_error
            }
    
    async def _handle_modification_extraction(self, message: str, context: dict, workflow_type: str = "buy_something") -> dict:
        """Handle entity extraction for modification requests with existing pending confirmations."""
        print(f"EntityService: Processing modification request: '{message}'")
        
        # Get existing pending confirmation data
        workflow_state = context.get("workflow_state", {})
        
        # Get pending products from any workflow state (confirmations, optional fields, or incomplete products)
        pending_combined = workflow_state.get("pending_combined_rfq")
        pending_single = workflow_state.get("pending_rfq")
        pending_optional_combined = workflow_state.get("pending_optional_combined_rfq")
        pending_optional_single = workflow_state.get("pending_optional_rfq")
        incomplete_products = workflow_state.get("incomplete_products")
        extracted_entities = workflow_state.get("extracted_entities", [])
        
        if pending_combined:
            pending_products = pending_combined.get("products", [])
        elif pending_single:
            pending_products = [pending_single]
        elif pending_optional_combined:
            pending_products = pending_optional_combined.get("products", [])
        elif pending_optional_single:
            pending_products = [pending_optional_single]
        elif incomplete_products:
            pending_products = incomplete_products
        elif extracted_entities:
            # Convert extracted_entities to proper format
            if isinstance(extracted_entities, list) and extracted_entities:
                pending_products = [{"entities": entity} for entity in extracted_entities]
            else:
                pending_products = []
        else:
            pending_products = []
        
        # Debug: Print pending products info
        print(f"EntityService: Found {len(pending_products)} pending products for modification")
        if pending_products:
            for i, product in enumerate(pending_products):
                entities = product.get("entities", {})
                desc = entities.get("description", f"Product {i+1}")
                qty = entities.get("quantity", "unknown")
                print(f"  Pending Product {i+1}: {desc} (quantity: {qty})")
        
        if not pending_products:
            print(f"EntityService: No pending products found, falling back to standard extraction")
            return await self._handle_standard_extraction(message, context, workflow_type)
        
        # Extract modification details from the message
        modification_prompt = f"""
        Extract ONLY the NEW modification values from: '{message}'
        
        Existing products context:
        {self._format_existing_products_for_prompt(pending_products)}
        
        IMPORTANT: 
        - Only extract NEW values that the user wants to change
        - Do NOT include existing values or copy over current data
        - If no new values are provided, return empty/null fields
        - Focus only on what the user explicitly wants to modify
        
        Example: If user says "change delivery address" without specifying new address, 
        return null for state/city/pincode fields.
        """
        
        # Call OpenAI to extract modification details
        response = await self.openai_service.extract_entities(
            message=modification_prompt,
            workflow_type=workflow_type
        )
        
        print(f"EntityService: Modification extraction response: {response}")
        
        # Handle new modification extraction format
        if response.get("is_modification_extraction"):
            has_new_values = response.get("has_new_values", False)
            modifications = response.get("modifications", [])
            
            if has_new_values and modifications:
                print(f"EntityService: User provided new values, applying {len(modifications)} modifications")
                # Convert modifications to products format for existing logic
                converted_products = self._convert_modifications_to_products_format(modifications)
                # Convert modifications to products format for existing logic
                converted_products = self._convert_modifications_to_products_format(modifications)
                # Validate dates in converted products
                validated_products, has_date_validation_error = await self._validate_dates_in_products(converted_products, message)
                # Auto-fill city and state from pincode
                validated_products = await self._auto_fill_location_from_pincode(validated_products)
                modified_products = self._apply_modifications_to_existing_products(
                    pending_products, validated_products, message
                )
                print(f"EntityService: Applied modifications, returning {len(modified_products)} updated products")
                return {
                    "products": modified_products,
                    "confidence": response.get("confidence", 0),
                    "success": True,
                    "is_modification": True
                }
            else:
                print(f"EntityService: Modification intent detected but no new values provided")
                modification_intent = response.get("modification_intent", "unknown field")
                return {
                    "modification_intent_detected": True,
                    "requires_clarification": True,
                    "existing_products": pending_products,
                    "user_message": message,
                    "modification_intent": modification_intent,
                    "confidence": response.get("confidence", 0),
                    "success": False,
                    "message": f"User wants to modify {modification_intent} but didn't provide new values"
                }
        
        # Fallback for old format (shouldn't happen with modification_request workflow_type)
        elif "products" in response and response["products"]:
            # Check if the extracted products contain meaningful modification values
            has_meaningful_modifications = self._has_meaningful_modification_values(response["products"], message)
            
            if has_meaningful_modifications:
                # Validate dates in modification products
                validated_products, has_date_validation_error = await self._validate_dates_in_products(response["products"], message)
                # Auto-fill city and state from pincode
                validated_products = await self._auto_fill_location_from_pincode(validated_products)
                modified_products = self._apply_modifications_to_existing_products(
                    pending_products, validated_products, message
                )
                print(f"EntityService: Applied modifications, returning {len(modified_products)} updated products")
                return {
                    "products": modified_products,
                    "confidence": response.get("confidence", 0),
                    "success": True,
                    "is_modification": True
                }
            else:
                print(f"EntityService: Modification intent detected but no meaningful new values provided")
                return {
                    "modification_intent_detected": True,
                    "requires_clarification": True,
                    "existing_products": pending_products,
                    "user_message": message,
                    "confidence": response.get("confidence", 0),
                    "success": False,
                    "message": "Modification intent detected but missing new values"
                }
        else:
            print(f"EntityService: No modifications extracted")
            return {
                "modification_intent_detected": True,
                "requires_clarification": True,
                "existing_products": pending_products,
                "user_message": message,
                "confidence": response.get("confidence", 0),
                "success": False,
                "message": "No modification values provided"
            }
    
    def _format_existing_products_for_prompt(self, pending_products: list) -> str:
        """Format existing products for modification prompt context."""
        formatted = []
        for i, product_info in enumerate(pending_products):
            entities = product_info.get("entities", {})
            description = entities.get("description", f"Product {i+1}")
            quantity = entities.get("quantity", "unknown")
            division = entities.get("division", "unknown")
            delivery_date = entities.get("deliveryDate", "unknown")
            formatted.append(f"- {description}: {quantity} units for {division}, delivery: {delivery_date}")
        return "\n".join(formatted)
    
    def _apply_modifications_to_existing_products(self, existing_products: list, modifications: list, original_message: str) -> list:
        """Apply modification details to existing products and return updated product list."""
        print(f"EntityService: Applying {len(modifications)} modifications to {len(existing_products)} existing products")
        
        # Create a copy of existing products to modify
        updated_products = []
        for product_info in existing_products:
            # Extract entities from the product info structure
            if "entities" in product_info:
                product_copy = product_info["entities"].copy()
            else:
                # If it's already an entity dict, use it directly
                product_copy = product_info.copy()
            
            # Clear any existing date validation errors when starting modifications
            if "date_validation_error" in product_copy:
                del product_copy["date_validation_error"]
                print(f"EntityService: Cleared existing date validation error from product")
            
            updated_products.append(product_copy)
        
        # Apply each modification
        for modification in modifications:
            print(f"EntityService: Processing modification: {modification}")
            
            # Find which existing product this modification applies to
            target_product_index = self._find_matching_product(updated_products, modification, original_message)
            
            if target_product_index is not None:
                print(f"EntityService: Updating product {target_product_index}: {updated_products[target_product_index].get('description', 'Unknown')}")
                # Update the matching product with modification data
                for key, value in modification.items():
                    if value is not None:  # Only update non-null values
                        updated_products[target_product_index][key] = value
                        print(f"  Updated {key}: {value}")
            else:
                print(f"EntityService: No matching product found for modification, treating as new product")
                # If no match found, add as new product (shouldn't happen in modification context)
                updated_products.append(modification)
        
        return updated_products
    
    def _find_matching_product(self, existing_products: list, modification: dict, original_message: str) -> int:
        """Find which existing product the modification applies to."""
        modification_description = modification.get("description", "").lower()
        message_lower = original_message.lower()
        
        print(f"EntityService: Looking for product matching '{modification_description}' in message '{original_message}'")
        
        # Try to match by description
        for i, product in enumerate(existing_products):
            product_description = product.get("description", "").lower()
            
            # Direct description match
            if modification_description and product_description and modification_description in product_description:
                print(f"  Found match by description: {product_description}")
                return i
            
            # Reverse match - product description in modification
            if modification_description and product_description and product_description in modification_description:
                print(f"  Found reverse match by description: {product_description}")
                return i
            
            # Message contains product description (e.g., "change chairs to 53")
            if product_description and product_description in message_lower:
                print(f"  Found match by message content: {product_description}")
                return i
        
        # If no description match, try by category or other fields
        for i, product in enumerate(existing_products):
            category = product.get("category", "").lower()
            if modification_description and category and (modification_description in category or category in modification_description):
                print(f"  Found match by category: {category}")
                return i
        
        # For modification requests, if no specific match found and we have only one product,
        # default to updating that product (common case for delivery address changes, etc.)
        if len(existing_products) == 1:
            print(f"  No specific match found, but only one product exists - defaulting to update product 0")
            return 0
        
        print(f"  No matching product found")
        return None

    def _convert_modifications_to_products_format(self, modifications: list) -> list:
        """
        Convert the new modification format to the old products format for compatibility.
        
        Args:
            modifications: List of modification objects from new format
            
        Returns:
            List of product-like objects compatible with existing logic
        """
        converted_products = []
        
        for modification in modifications:
            product = {}
            
            # Map new field names to old field names
            field_mapping = {
                "new_project_desc": "projectDesc",
                "new_description": "description", 
                "new_quantity": "quantity",
                "new_unit_of_measures": "unitofMeasures",
                "new_delivery_date": "deliveryDate",
                "new_division": "division",
                "new_brand": "brand",
                "new_state": "state",
                "new_city": "city", 
                "new_pincode": "pincode",
                "new_remarks": "remarks"
            }
            
            # Convert fields
            for new_field, old_field in field_mapping.items():
                value = modification.get(new_field)
                if value is not None:
                    product[old_field] = value
                    print(f"EntityService: Converting {new_field} -> {old_field}: {value}")
            
            if product:  # Only add if we have some fields
                converted_products.append(product)
        
        print(f"EntityService: Converted {len(modifications)} modifications to {len(converted_products)} products")
        return converted_products

    async def _validate_dates_in_products(self, products: list, original_message: str) -> tuple:
        """Validate delivery dates in products list.

        Returns:
            tuple: (validated_products, has_date_validation_error)
        """
        validated_products = []
        has_date_validation_error = False

        # Collect unique delivery dates to avoid redundant API calls
        unique_dates = {}
        date_validation_cache = {}

        for product in products:
            delivery_date = product.get("deliveryDate")
            if delivery_date:
                unique_dates[delivery_date] = unique_dates.get(delivery_date, 0) + 1

        # Validate each unique date only once
        for date in unique_dates.keys():
            if date not in date_validation_cache:
                print(f"EntityService: Validating unique delivery date: {date} (appears in {unique_dates[date]} products)")
                validation_result = await self.openai_service.validate_delivery_date(
                    raw_date_input=date,
                    extracted_date=date
                )
                
                # Additional programmatic check for AI-returned date
                if validation_result.get("is_valid") and validation_result.get("normalized_date"):
                    normalized_date = validation_result.get("normalized_date")
                    current_date = datetime.now().date()
                    
                    try:
                        ai_date = datetime.strptime(normalized_date, "%Y-%m-%d").date()
                        if ai_date < current_date:
                            validation_result["is_valid"] = False
                            extracted_date = format_date_display(datetime.strptime(normalized_date, "%Y-%m-%d"))
                            validation_result["user_friendly_message"] = f"The date {extracted_date} is in the past. Kindly share a valid delivery date from today onward."
                            print(f"EntityService: AI date validation override - date {extracted_date} is before current date {current_date}")
                    except ValueError as e:
                        validation_result["is_valid"] = False
                        validation_result["user_friendly_message"] = "Invalid date format. Kindly share a valid delivery date."
                        print(f"EntityService: Invalid date format from AI: {normalized_date}, error: {e}")
                
                date_validation_cache[date] = validation_result

        # Apply validation results to all products
        for product in products:
            validated_product = product.copy()
            delivery_date = product.get("deliveryDate")

            if delivery_date and delivery_date in date_validation_cache:
                validation_result = date_validation_cache[delivery_date]

                if validation_result.get("is_valid"):
                    validated_product["deliveryDate"] = validation_result.get("normalized_date")
                    # Clear any existing date validation error when date is valid
                    if "date_validation_error" in validated_product:
                        del validated_product["date_validation_error"]
                else:
                    # Mark as invalid and add validation message
                    validated_product["deliveryDate"] = None
                    validated_product["date_validation_error"] = validation_result.get("user_friendly_message")
                    has_date_validation_error = True
                    print(f"EntityService: Invalid date '{delivery_date}': {validation_result.get('user_friendly_message')}")
            else:
                # If no delivery date provided, preserve any existing validation error
                pass

            validated_products.append(validated_product)
        
        return validated_products, has_date_validation_error
    
    async def _validate_date_in_entity(self, entities: dict, original_message: str) -> tuple:
        """Validate delivery date in single entity.
        
        Returns:
            tuple: (validated_entities, has_date_validation_error)
        """
        validated_entities = entities.copy()
        has_date_validation_error = False
        delivery_date = entities.get("deliveryDate")
        
        if delivery_date:
            validation_result = await self.openai_service.validate_delivery_date(
                raw_date_input=delivery_date,
                extracted_date=delivery_date
            )
            
            # Additional programmatic check for AI-returned date
            if validation_result.get("is_valid") and validation_result.get("normalized_date"):
                normalized_date = validation_result.get("normalized_date")
                current_date = datetime.now().date()
                
                try:
                    ai_date = datetime.strptime(normalized_date, "%Y-%m-%d").date()
                    if ai_date < current_date:
                        validation_result["is_valid"] = False
                        extracted_date = format_date_display(datetime.strptime(normalized_date, "%Y-%m-%d"))
                        validation_result["user_friendly_message"] = f"The date {extracted_date} is in the past. Kindly share a valid delivery date from today onward."
                        print(f"EntityService: AI date validation override - date {extracted_date} is before current date {current_date}")
                except ValueError as e:
                    validation_result["is_valid"] = False
                    validation_result["user_friendly_message"] = "Invalid date format. Kindly share a valid delivery date."
                    print(f"EntityService: Invalid date format from AI: {normalized_date}, error: {e}")
            
            if validation_result.get("is_valid"):
                validated_entities["deliveryDate"] = validation_result.get("normalized_date")
            else:
                # Mark as invalid and add validation message
                validated_entities["deliveryDate"] = None
                validated_entities["date_validation_error"] = validation_result.get("user_friendly_message")
                has_date_validation_error = True
                print(f"EntityService: Invalid date '{delivery_date}': {validation_result.get('user_friendly_message')}")
        
        return validated_entities, has_date_validation_error

    def _has_meaningful_modification_values(self, products: list, message: str) -> bool:
        """
        Check if the extracted products contain actual new values for modification.
        
        Simply check if any non-null values were extracted (excluding remarks).
        
        Args:
            products: List of extracted product modifications
            message: Original user message
            
        Returns:
            True if products contain modification values, False if just modification intent
        """
        for product in products:
            new_values_count = 0
            
            for key, value in product.items():
                # Skip remarks as it typically contains intent statements
                if key == "remarks":
                    continue
                    
                # Check if this field has a non-null, non-empty value
                if value is not None and str(value).strip() and str(value).strip().lower() not in ["none", "null", ""]:
                    new_values_count += 1
                    print(f"EntityService: Found modification value for {key}: {value}")
            
            print(f"EntityService: Product has {new_values_count} modification values")
            
            # If we have any values, consider it a valid modification
            if new_values_count >= 1:
                return True
        
        return False

    async def _handle_reference_extraction(self, message: str, context: dict, reference_detection: dict) -> dict:
        """
        Handle entity extraction when reference phrases are detected using intelligent analysis.
        
        Args:
            message: User message
            context: Context with user history
            reference_detection: OpenAI reference detection results
            
        Returns:
            Dict with intelligently extracted historical options for user selection
        """
        reference_type = reference_detection.get("reference_type")
        user_context = context.get("user_context", {}) if context else {}
        chat_history = user_context.get("chat_history", [])
        
        print(f"EntityService: Handling intelligent reference for entity type: {reference_type}")
        print(f"EntityService: Detection confidence: {reference_detection.get('confidence', 0)}%")
        print(f"EntityService: Available chat history: {len(chat_history)} sessions")
        
        if not chat_history:
            print(f"EntityService: No chat history available, falling back to standard extraction")
            return await self._handle_standard_extraction(message, context, "buy_something")
        
        # Use OpenAI to intelligently extract historical options
        historical_result = self.openai_service.extract_historical_options(
            entity_type=reference_type,
            chat_history=chat_history,
            current_message=message,
            current_context=context.get("workflow_state", {})
        )
        
        if historical_result.get("success") and historical_result.get("options"):
            return {
                "reference_detected": True,
                "entity_type": reference_type,
                "detected_phrases": reference_detection.get("detected_phrases", []),
                "reference_confidence": reference_detection.get("confidence", 0),
                "reasoning": reference_detection.get("reasoning", ""),
                "historical_options": historical_result.get("options", []),
                "analysis_summary": historical_result.get("summary", ""),
                "recommendations": historical_result.get("recommendations", {}),
                "requires_user_selection": True,
                "success": True,
                "message": f"Found {len(historical_result.get('options', []))} relevant options for {reference_type}"
            }
        else:
            print(f"EntityService: No relevant historical options found, falling back to standard extraction")
            return await self._handle_standard_extraction(message, context, "buy_something")

    async def extract_entities_with_summary_context(self, message: str, context: dict = None, workflow_type: str = "buy_something") -> dict:
        """
        Extract entities using historical context from chat summaries.
        
        This method uses AI to resolve references like "same as last time", "usual address"
        by analyzing previous conversation summaries and replacing references with actual values.
        It also handles modification requests when workflow_type is "modification_request".
        
        Args:
            message: User message to extract entities from
            context: Context dictionary containing chat_summaries and other data
            workflow_type: Type of workflow (buy_something, modification_request, etc.)
            
        Returns:
            Dict containing extracted products with resolved references and metadata
        """
        try:
            # Check if intent service already detected a reference request
            intent_context = context.get("intent_result") if context else None
            if intent_context and intent_context.get("intent") == "reference_request":
                reference_details = intent_context.get("context_analysis", {}).get("reference_details", {})
                if reference_details.get("reference_type") and reference_details.get("has_history"):
                    print(f"EntityService: Using intent-detected reference: {reference_details}")
                    return await self._handle_reference_extraction(message, context, {
                        "reference_type": reference_details.get("reference_type"),
                        "confidence": intent_context.get("confidence", 0),
                        "reasoning": intent_context.get("reasoning", ""),
                        "detected_phrases": []  # Not available from intent service
                    })
            
            # Check if this is a modification request based on workflow_type (set by intent classification)
            if workflow_type == "modification_request":
                print(f"EntityService: Workflow type is modification_request - handling as modification with summary context")
                return await self._handle_modification_extraction(message, context, workflow_type)
            
            # Check if we have chat summaries for context
            chat_summaries = context.get("chat_summaries", []) if context else []
            
            if not chat_summaries:
                print("EntityService: No chat summaries available, falling back to standard extraction")
                return await self._handle_standard_extraction(message, context, workflow_type)
            
            print(f"EntityService: Using summary-aware extraction with {len(chat_summaries)} summaries")
            
            # Use new OpenAI method with summaries
            response = await self.openai_service.extract_entities_with_summary_context(
                message=message,
                chat_summaries=chat_summaries,
                workflow_type="rfq_creation"
            )
            
            print(f"EntityService: Summary-aware extraction response: {response}")
            
            # Log resolved references for debugging
            resolved_refs = response.get("resolved_references", [])
            if resolved_refs:
                print(f"EntityService: Resolved {len(resolved_refs)} references:")
                for ref in resolved_refs:
                    print(f"  - '{ref.get('reference_phrase')}' -> {ref.get('resolved_field')}: {ref.get('resolved_value')}")
                
                # Use AI to intelligently apply resolved references to product entities
                products = response.get("products", [])
                updated_products = await self._apply_resolved_references_intelligently(products, resolved_refs, message)
                response["products"] = updated_products
                print(f"EntityService: Applied resolved references to {len(updated_products)} products using AI")
            
            # Validate dates in the final products
            if "products" in response:
                validated_products, has_date_validation_error = await self._validate_dates_in_products(response["products"], message)
                # Auto-fill city and state from pincode
                validated_products = await self._auto_fill_location_from_pincode(validated_products)
                response["products"] = validated_products
                response["date_validation_error"] = has_date_validation_error
                print(f"EntityService: Validated dates in {len(validated_products)} products from summary-aware extraction")
            
            return response
            
        except Exception as e:
            print(f"EntityService: Summary-aware extraction error: {e}")
            # Fallback to standard extraction on error
            return await self._handle_standard_extraction(message, context, workflow_type)

    async def _apply_resolved_references_intelligently(self, products: list, resolved_refs: list, original_message: str) -> list:
        """
        Use AI to intelligently apply resolved references to product entities.
        
        Instead of hardcoding field mappings, this uses AI to determine how to merge
        the resolved reference data into the appropriate product entity fields.
        
        Args:
            products: List of product entities
            resolved_refs: List of resolved reference objects
            original_message: Original user message for context
            
        Returns:
            Updated products list with resolved references applied intelligently
        """
        try:
            if not resolved_refs or not products:
                return products
            
            # Use OpenAI to intelligently merge the resolved references
            merge_result = await self.openai_service.merge_resolved_references_with_entities(
                products=products,
                resolved_references=resolved_refs,
                original_message=original_message
            )
            
            if merge_result.get("success") and merge_result.get("updated_products"):
                return merge_result["updated_products"]
            else:
                print(f"EntityService: AI merge failed, returning original products")
                return products
                
        except Exception as e:
            print(f"EntityService: Error in AI reference merging: {e}")
            return products
    
    def _is_date_future_or_today(self, date_str: str) -> bool:
        """Check if date string is today or in the future."""
        try:
            if not date_str:
                return False
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            current_date = datetime.now().date()
            return date_obj >= current_date
        except:
            return False

    def _merge_global_fields_into_products(self, products: list, global_fields: dict) -> list:
        """
        Merge global fields into each product for backward compatibility.

        TEMPORARY SOLUTION: This method merges global fields (deliveryDate, state, city,
        pincode) into each product entity to maintain backward compatibility with existing
        downstream code.

        TODO: Refactor downstream code (chat_service_helpers.py, products_array_handler.py,
        confirmation_handler.py) to understand and handle the two-level structure natively.
        This will eliminate the need for this transformation layer.

        Args:
            products: List of product entities with item-specific fields only
            global_fields: Dict of global fields that apply to all products

        Returns:
            List of products with global fields merged in
        """
        merged_products = []

        for product in products:
            merged_product = product.copy()

            # Merge each global field into the product if it has a value
            for field_name, field_value in global_fields.items():
                if field_value is not None:
                    # Only add if product doesn't already have this field
                    if field_name not in merged_product or merged_product.get(field_name) is None:
                        merged_product[field_name] = field_value

            merged_products.append(merged_product)

        print(f"EntityService: Merged global fields into {len(merged_products)} products")
        return merged_products

    def _merge_new_extraction_with_existing_products(self, existing_products: list, new_products: list, message: str) -> list:
        """
        Merge newly extracted products with existing incomplete products.

        Logic:
        - If new products have descriptions that match existing ones, merge the data
        - If new products are genuinely new, add them to the list
        - Preserve all existing products
        - If new product has no description (None), it's supplementary data for all existing products

        Args:
            existing_products: List of existing product entities from incomplete_products
            new_products: List of newly extracted product entities
            message: Original user message for context

        Returns:
            Merged list of products
        """
        # Get descriptions from existing products for matching
        existing_descriptions = {}
        for i, existing_prod in enumerate(existing_products):
            desc = existing_prod.get("description")
            if desc and isinstance(desc, str):
                desc_lower = desc.lower().strip()
                if desc_lower:
                    existing_descriptions[desc_lower] = i

        # Start with copies of existing products
        merged_products = [prod.copy() for prod in existing_products]

        # Process each new product
        for new_prod in new_products:
            new_desc = new_prod.get("description")

            # Skip NO_PRODUCTS_MENTIONED entries - they should be filtered out earlier
            if new_desc == "NO_PRODUCTS_MENTIONED":
                print(f"EntityService: Skipping NO_PRODUCTS_MENTIONED entry in merge")
                continue

            # Handle None or non-string descriptions
            if new_desc is None or not isinstance(new_desc, str):
                # This product has no description - it's supplementary data for ALL existing products
                print(f"EntityService: Product has no description - treating as supplementary data for all existing products")
                for i in range(len(merged_products)):
                    for key, value in new_prod.items():
                        if value is not None and key != "description":  # Don't copy None description
                            # Only update if field is missing or None
                            if key not in merged_products[i] or merged_products[i].get(key) is None:
                                merged_products[i][key] = value
                                print(f"  Applied {key}={value} to product: {merged_products[i].get('description', 'unnamed')}")
                continue

            new_desc_lower = new_desc.lower().strip()

            if new_desc_lower and new_desc_lower in existing_descriptions:
                # This is a re-extraction of an existing product - merge the data
                existing_index = existing_descriptions[new_desc_lower]
                print(f"EntityService: Merging data for existing product: {new_desc_lower}")

                # Merge non-None fields from new product into existing
                for key, value in new_prod.items():
                    if value is not None:
                        merged_products[existing_index][key] = value
                        print(f"  Updated {key}={value}")
            elif new_desc_lower:
                # This is a genuinely new product with a description - add it
                print(f"EntityService: Adding new product: {new_desc_lower}")
                merged_products.append(new_prod)
            # else: empty string description, skip

        return merged_products

    def _apply_supplementary_data_to_existing_products(self, existing_products: list, global_fields: dict) -> list:
        """
        Apply supplementary data (global fields) to all existing products.

        Used when user provides only supplementary information (like delivery date, location)
        without mentioning specific products.

        Args:
            existing_products: List of existing product entities
            global_fields: Dict of global fields to apply (deliveryDate, state, city, pincode)

        Returns:
            Updated list of products with supplementary data applied
        """
        updated_products = []

        for product in existing_products:
            updated_product = product.copy()

            # Apply each global field if it has a value
            for field_name, field_value in global_fields.items():
                if field_value is not None:
                    # Only update if the field is missing or None in the product
                    if field_name not in updated_product or updated_product.get(field_name) is None:
                        updated_product[field_name] = field_value
                        print(f"EntityService: Applied {field_name}={field_value} to product: {product.get('description', 'unnamed')}")

            updated_products.append(updated_product)

        return updated_products



    def _get_schema(self, workflow_type: str) -> dict:
        """Load schema from JSON file for reference."""
        schema_files = {
            "buy_something": "entity_extraction_rfq_creation.json",
        }

        filename = schema_files.get(workflow_type)
        if not filename:
            return {}

        schema_path = os.path.join(os.path.dirname(__file__), "..", "tools", filename)

        try:
            with open(schema_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Schema file not found: {schema_path}")
            return {}

    async def _auto_fill_location_from_pincode(self, products: list) -> list:
        """
        Auto-fill city and state from pincode using direct API lookup.
        
        Args:
            products: List of product entities
            
        Returns:
            Updated products list with city and state filled from pincode lookup
        """
        updated_products = []
        
        for product in products:
            updated_product = product.copy()
            pincode = product.get("pincode")
            
            # Only lookup if pincode exists and city/state are missing
            if pincode and (not product.get("city") or not product.get("state")):
                try:
                    # Validate pincode format first
                    clean_pincode = str(pincode).strip()
                    if not clean_pincode.isdigit() or len(clean_pincode) != 6:
                        print(f"EntityService: Invalid pincode format: {pincode}")
                        updated_products.append(updated_product)
                        continue
                    
                    location_data = await get_location_from_pincode_async(clean_pincode)
                    if location_data:
                        city = location_data.get("city", "")
                        state = location_data.get("state", "")
                        
                        if not updated_product.get("city") and city:
                            updated_product["city"] = city
                            print(f"EntityService: Auto-filled city '{city}' from pincode {pincode}")
                        
                        if not updated_product.get("state") and state:
                            updated_product["state"] = state
                            print(f"EntityService: Auto-filled state '{state}' from pincode {pincode}")
                    else:
                        print(f"EntityService: No location data returned for pincode {pincode}")
                        # Set pincode, city, and state to None when no location found
                        updated_product["pincode"] = None
                        updated_product["city"] = None
                        updated_product["state"] = None
                        updated_product["pincode_validation_error"] = f"Could not find location for pincode {pincode}. Please enter valid pincode"
                        print(f"EntityService: Set pincode, city, and state to None for invalid pincode {pincode}")
                except Exception as e:
                    print(f"EntityService: Error fetching location for pincode {pincode}: {e}")
            
            updated_products.append(updated_product)
        
        return updated_products