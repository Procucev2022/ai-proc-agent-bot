"""
Entity extraction service for parsing structured data from user messages.

This service focuses solely on extracting entities from user messages using OpenAI.
It does not handle completeness checking or question generation - that's handled
by the data model and orchestration layer.
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict

from ..services.openai_service import OpenAIService
from ..utils.datetime_utils import format_date_display
from ..utils.pincode_lookup import get_location_from_pincode_async


logger = logging.getLogger(__name__)

# Sentinel description the extraction prompt emits to signal "the user mentioned no
# products at all" (see app/prompts/entity_extraction/_get_entity_system_prompt_rfq_creation.txt).
# It is a signal, never a product line, so it is stripped before anything downstream
# can store it or render it back to the user.
NO_PRODUCTS_SENTINEL = "NO_PRODUCTS_MENTIONED"


def strip_no_products_sentinel(products: list) -> list:
    """
    Drop the extractor's "no products mentioned" sentinel entries from a product list.

    The prompt contract represents "zero products" as a single synthetic product whose
    description is NO_PRODUCTS_SENTINEL and whose other fields are null. Treating that
    as a real product made the bot ask the user for the missing quantity of an item
    called "NO_PRODUCTS_MENTIONED", so it is removed here, at the boundary where the
    model response first enters the application.

    Args:
        products: Raw product dicts from the extraction response

    Returns:
        The list without sentinel entries. An all-sentinel list becomes empty, which is
        what every caller already treats as "no products yet".
    """
    if not products:
        return products

    kept = []
    for product in products:
        description = product.get("description") if isinstance(product, dict) else None
        if isinstance(description, str) and description.strip() == NO_PRODUCTS_SENTINEL:
            logger.debug("EntityService: Dropped %s sentinel product entry", NO_PRODUCTS_SENTINEL)
            continue
        kept.append(product)
    return kept


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
            # Strip the "no products mentioned" sentinel here so no downstream consumer
            # can mistake it for a real item. Global fields below are read from the
            # response directly, so a date/pincode-only message still keeps its data.
            products = strip_no_products_sentinel(response.get("products", []))

            # Get newly extracted global fields
            newly_extracted_global_fields = {
                "deliveryDate": response.get("deliveryDate"),
                "state": response.get("state"),
                "city": response.get("city"),
                "pincode": response.get("pincode")
            }

            # Retrieve existing global supplementary fields from workflow_state
            existing_global_fields = {}
            if context and context.get("workflow_state"):
                existing_global_fields = context["workflow_state"].get("global_supplementary_fields", {})
                if existing_global_fields:
                    print(f"EntityService: Found existing global fields: {existing_global_fields}")

            # Merge newly extracted global fields with existing ones (new values override old)
            # Only use new values if they are not None and not empty string
            global_fields = {**existing_global_fields, **{k: v for k, v in newly_extracted_global_fields.items() if v is not None and v != ''}}
            print(f"EntityService: Accumulated global fields: {global_fields}")

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

            # Check for non-procurable items BEFORE cleaning invalid descriptions
            non_procurable_items = self._detect_non_procurable_items(validated_products)
            if non_procurable_items:
                print(f"EntityService: Detected non-procurable items: {non_procurable_items}")
                return {
                    "products": [],
                    "confidence": response.get("confidence", 0),
                    "success": False,
                    "non_procurable_items": non_procurable_items,
                    "error_type": "non_procurable"
                }

            # Check for quantity limit violations (100,000 max)
            quantity_violations = self._check_quantity_limits(validated_products)
            if quantity_violations:
                print(f"EntityService: Detected quantity limit violations: {quantity_violations}")
                return {
                    "products": [],
                    "confidence": response.get("confidence", 0),
                    "success": False,
                    "quantity_violations": quantity_violations,
                    "error_type": "quantity_limit"
                }

            # Clean up invalid descriptions (units of measure, generic terms, etc.)
            validated_products = self._clean_invalid_descriptions(validated_products)
            # Auto-fill city and state from pincode
            validated_products = await self._auto_fill_location_from_pincode(validated_products)

            # Update global fields with corrected location from pincode lookup
            # Pincode is authoritative - if products have updated city/state from pincode, use those
            if validated_products:
                first_product = validated_products[0]
                if first_product.get("city"):
                    global_fields["city"] = first_product["city"]
                if first_product.get("state"):
                    global_fields["state"] = first_product["state"]

            return {
                "products": validated_products,
                "deliveryDate": global_fields.get("deliveryDate"),
                "state": global_fields.get("state"),
                "city": global_fields.get("city"),
                "pincode": global_fields.get("pincode"),
                "global_supplementary_fields": global_fields,
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
        
        # Get pending products from any workflow state (confirmations, optional fields, incomplete products, or complete products)
        pending_combined = workflow_state.get("pending_combined_rfq")
        pending_single = workflow_state.get("pending_rfq")
        pending_optional_combined = workflow_state.get("pending_optional_combined_rfq")
        pending_optional_single = workflow_state.get("pending_optional_rfq")
        incomplete_products = workflow_state.get("incomplete_products")
        complete_products = workflow_state.get("complete_products", [])
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
        elif complete_products:
            # Use complete_products if no pending products found
            pending_products = complete_products
        elif extracted_entities:
            # Convert extracted_entities to proper format
            if isinstance(extracted_entities, list) and extracted_entities:
                pending_products = [{"entities": entity} for entity in extracted_entities]
            else:
                pending_products = []
        else:
            pending_products = []

        # Preserve attachments from extracted_entities when they're not in pending_products
        # This ensures attachments added after product entry are preserved during modification
        if pending_products and extracted_entities:
            # Get attachments from extracted_entities (usually index 0 for single product RFQ)
            extracted_attachments = []
            if isinstance(extracted_entities, list):
                for entity in extracted_entities:
                    if isinstance(entity, dict) and entity.get("attachments"):
                        extracted_attachments = entity.get("attachments", [])
                        break  # Use attachments from first entity that has them

            # Add attachments to pending_products if they don't already have them
            for product in pending_products:
                if isinstance(product, dict):
                    entities = product.get("entities", {})
                    if isinstance(entities, dict):
                        # If this product doesn't have attachments but extracted_entities has some, add them
                        if not entities.get("attachments") and extracted_attachments:
                            entities["attachments"] = extracted_attachments
                            print(f"EntityService: Merged {len(extracted_attachments)} attachments into pending product")

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

            # Extract global delivery fields from response
            global_delivery_fields = {}
            if response.get("deliveryDate") and response.get("deliveryDate").strip():
                global_delivery_fields["deliveryDate"] = response.get("deliveryDate")
            if response.get("state") and response.get("state").strip():
                global_delivery_fields["state"] = response.get("state")
            if response.get("city") and response.get("city").strip():
                global_delivery_fields["city"] = response.get("city")
            if response.get("pincode") and response.get("pincode").strip():
                global_delivery_fields["pincode"] = response.get("pincode")

            # Check if we have remove operations even if has_new_values is False
            has_remove_operation = any(mod.get("operation_type") == "remove" for mod in modifications)

            if (has_new_values and (modifications or global_delivery_fields)) or has_remove_operation:
                print(f"EntityService: User provided new values, applying {len(modifications)} modification operations")
                if has_remove_operation and not has_new_values:
                    print(f"EntityService: WARNING - has_new_values=False but remove operation detected, proceeding anyway")
                if global_delivery_fields:
                    print(f"EntityService: Global delivery fields to apply: {list(global_delivery_fields.keys())}")

                # Apply modifications directly - they contain operation_type, target_product_index, and new values
                modified_products = self._apply_modifications_to_existing_products(
                    pending_products, modifications, message, global_delivery_fields
                )
                # Validate dates in final products
                validated_products, has_date_validation_error = await self._validate_dates_in_products(modified_products, message)
                # Clean up invalid descriptions
                validated_products = self._clean_invalid_descriptions(validated_products)
                # Auto-fill city and state from pincode
                validated_products = await self._auto_fill_location_from_pincode(validated_products)

                print(f"EntityService: Applied modifications, returning {len(validated_products)} updated products")
                return {
                    "products": validated_products,
                    "confidence": response.get("confidence", 0),
                    "success": True,
                    "is_modification": True,
                    "date_validation_error": has_date_validation_error
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
        """Format existing products for modification prompt context with index numbers."""
        formatted = []
        for i, product_info in enumerate(pending_products):
            entities = product_info.get("entities", {})
            description = entities.get("description", f"Product {i}")
            quantity = entities.get("quantity", "unknown")
            division = entities.get("division", "unknown")
            delivery_date = entities.get("deliveryDate", "unknown")
            project_desc = entities.get("projectDesc", "")
            brand = entities.get("brand", "")
            city = entities.get("city", "")
            state = entities.get("state", "")
            pincode = entities.get("pincode", "")

            # Build comprehensive product details
            details = f"Index {i}: {description} (Quantity: {quantity}"
            if division != "unknown":
                details += f", Division: {division}"
            if delivery_date != "unknown":
                details += f", Delivery: {delivery_date}"
            if project_desc:
                details += f", Project: {project_desc}"
            if brand:
                details += f", Brand: {brand}"
            if city or state or pincode:
                location = ", ".join(filter(None, [city, state, pincode]))
                details += f", Location: {location}"
            details += ")"

            formatted.append(details)
        return "\n".join(formatted)
    
    def _apply_modifications_to_existing_products(self, existing_products: list, modifications: list, original_message: str, global_delivery_fields: dict = None) -> list:
        """
        Apply modification operations to existing products based on AI's analysis.

        This method trusts the AI's determination of:
        - operation_type: "modify", "add", or "remove"
        - target_product_index: which product to operate on

        Args:
            existing_products: List of existing product info dicts with "entities" key
            modifications: List of modification operations from AI
            original_message: Original user message (for logging)
            global_delivery_fields: Dict of global delivery fields (deliveryDate, state, city, pincode) to apply to ALL products

        Returns:
            Updated list of product entities
        """
        print(f"EntityService: Applying {len(modifications)} modification operations to {len(existing_products)} existing products")
        if global_delivery_fields:
            print(f"EntityService: Will apply global delivery fields: {list(global_delivery_fields.keys())}")

        # Create a copy of existing products to work with
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

        # Extract common fields from existing products to inherit for new products
        common_fields = self._extract_common_fields_from_products(updated_products)
        if common_fields:
            print(f"EntityService: Extracted common fields for inheritance: {list(common_fields.keys())}")

        # Track indices to remove (process after all modifications)
        indices_to_remove = set()

        # Track global field modifications (apply to all products)
        # Start with the global delivery fields passed in (from new schema format)
        global_field_updates = {}
        if global_delivery_fields:
            global_field_updates.update(global_delivery_fields)
            print(f"EntityService: Initialized global_field_updates with delivery fields: {list(global_delivery_fields.keys())}")

        # Apply each modification operation
        for modification in modifications:
            operation_type = modification.get("operation_type")
            target_index = modification.get("target_product_index")
            target_desc = modification.get("target_product_description", "unknown")
            reasoning = modification.get("reasoning", "")

            print(f"EntityService: Operation '{operation_type}' on product index {target_index} ({target_desc})")
            if reasoning:
                print(f"  Reasoning: {reasoning}")

            if operation_type == "modify":
                # MODIFY: Update an existing product
                if target_index is None:
                    print(f"  ERROR: 'modify' operation requires target_product_index, skipping")
                    continue

                if target_index < 0 or target_index >= len(updated_products):
                    print(f"  ERROR: Invalid target_product_index {target_index}, skipping")
                    continue

                # Apply the modification to the target product
                print(f"  Modifying product at index {target_index}: {updated_products[target_index].get('description', 'Unknown')}")
                for key, value in modification.items():
                    # Skip metadata fields
                    if key in ["operation_type", "target_product_index", "target_product_description", "field_type", "reasoning"]:
                        continue

                    # Map new_* fields to actual field names
                    actual_key = key.replace("new_", "") if key.startswith("new_") else key

                    # Handle special field name mappings
                    if actual_key == "unit_of_measures":
                        actual_key = "unitofMeasures"
                    elif actual_key == "delivery_date":
                        actual_key = "deliveryDate"
                    elif actual_key == "project_desc":
                        actual_key = "projectDesc"

                    if value is not None:  # Only update non-null values
                        updated_products[target_index][actual_key] = value
                        print(f"    Updated {actual_key}: {value}")

                        # Track global field updates (delivery date/location changes)
                        if actual_key in ["deliveryDate", "city", "state", "pincode"]:
                            if actual_key not in global_field_updates:
                                global_field_updates[actual_key] = value

            elif operation_type == "add":
                # ADD: Create a new product and inherit common fields
                print(f"  Adding new product: {target_desc}")
                new_product = {}

                # First, inherit common fields from existing products
                if common_fields:
                    for field_name, field_value in common_fields.items():
                        new_product[field_name] = field_value
                        print(f"    Inherited {field_name}: {field_value}")

                # Then, apply explicitly provided fields from modification
                for key, value in modification.items():
                    # Skip metadata fields
                    if key in ["operation_type", "target_product_index", "target_product_description", "field_type", "reasoning"]:
                        continue

                    # Map new_* fields to actual field names
                    actual_key = key.replace("new_", "") if key.startswith("new_") else key

                    # Handle special field name mappings
                    if actual_key == "unit_of_measures":
                        actual_key = "unitofMeasures"
                    elif actual_key == "delivery_date":
                        actual_key = "deliveryDate"
                    elif actual_key == "project_desc":
                        actual_key = "projectDesc"

                    if value is not None:
                        new_product[actual_key] = value
                        print(f"    Set {actual_key}: {value}")

                        # Track global field updates
                        if actual_key in ["deliveryDate", "city", "state", "pincode"]:
                            global_field_updates[actual_key] = value

                if new_product:  # Only add if we have some fields
                    updated_products.append(new_product)
                    print(f"  Successfully added new product at index {len(updated_products) - 1}")
                else:
                    print(f"  WARNING: No fields provided for new product, skipping")

            elif operation_type == "remove":
                # REMOVE: Delete an existing product
                if target_index is None:
                    print(f"  ERROR: 'remove' operation requires target_product_index, skipping")
                    continue

                if target_index < 0 or target_index >= len(updated_products):
                    print(f"  ERROR: Invalid target_product_index {target_index}, skipping")
                    continue

                # Mark for removal (we'll remove after processing all operations)
                indices_to_remove.add(target_index)
                print(f"  Marked product at index {target_index} for removal: {updated_products[target_index].get('description', 'Unknown')}")

            else:
                print(f"  ERROR: Unknown operation_type '{operation_type}', skipping")

        # Remove products marked for deletion (in reverse order to preserve indices)
        if indices_to_remove:
            print(f"EntityService: Removing {len(indices_to_remove)} products")
            for index in sorted(indices_to_remove, reverse=True):
                removed_product = updated_products.pop(index)
                print(f"  Removed product at index {index}: {removed_product.get('description', 'Unknown')}")

        # Apply global field updates to ALL products
        if global_field_updates:
            print(f"EntityService: Applying global field updates to all {len(updated_products)} products: {list(global_field_updates.keys())}")
            for product in updated_products:
                for field_name, field_value in global_field_updates.items():
                    product[field_name] = field_value
                    print(f"  Updated {product.get('description', 'Unknown')}.{field_name}: {field_value}")

        print(f"EntityService: Final product count: {len(updated_products)}")
        return updated_products

    def _extract_common_fields_from_products(self, products: list) -> dict:
        """
        Extract common fields (delivery location, date) from existing products to inherit for new products.

        Args:
            products: List of product entities

        Returns:
            Dict of common fields that are shared across all products
        """
        if not products:
            return {}

        # Fields that should be inherited by new products
        inheritable_fields = ["deliveryDate", "city", "state", "pincode", "unitofMeasures"]

        common_fields = {}

        # For each inheritable field, check if it's common across all products
        for field_name in inheritable_fields:
            # Get all values for this field from products
            values = []
            for product in products:
                value = product.get(field_name)
                if value is not None:
                    values.append(value)

            # If all products have the same value for this field, consider it common
            if values and all(v == values[0] for v in values):
                common_fields[field_name] = values[0]

        return common_fields

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

                # Check if AI returned validation issues even if is_valid is true (AI bug)
                if validation_result.get("validation_issues") and len(validation_result.get("validation_issues", [])) > 0:
                    validation_result["is_valid"] = False
                    print(f"EntityService: Overriding is_valid to False due to validation_issues: {validation_result.get('validation_issues')}")

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
            
            # Check if AI returned validation issues even if is_valid is true (AI bug)
            if validation_result.get("validation_issues") and len(validation_result.get("validation_issues", [])) > 0:
                validation_result["is_valid"] = False
                print(f"EntityService: Overriding is_valid to False due to validation_issues: {validation_result.get('validation_issues')}")
            
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

            # Strip the "no products mentioned" sentinel before any further processing,
            # exactly as the standard extraction path does.
            if "products" in response:
                response["products"] = strip_no_products_sentinel(response["products"])

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
                # Clean up invalid descriptions
                validated_products = self._clean_invalid_descriptions(validated_products)
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
        - If existing products lack descriptions and new products have them, use positional matching
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
        # Identify existing products without valid descriptions
        products_without_desc = []
        products_with_desc = {}

        for i, existing_prod in enumerate(existing_products):
            desc = existing_prod.get("description")
            if desc and isinstance(desc, str):
                desc_lower = desc.lower().strip()
                if desc_lower:
                    products_with_desc[desc_lower] = i
                else:
                    products_without_desc.append(i)
            else:
                products_without_desc.append(i)

        # Check if we should use positional matching
        # Condition 1: existing products without descriptions, new products with descriptions, same count
        # Condition 2: same number of existing and new products (suggests supplementary data for same items)
        new_products_with_desc = [p for p in new_products if p.get("description")]
        use_positional_matching = (
            (len(products_without_desc) > 0 and
             len(new_products_with_desc) > 0 and
             len(products_without_desc) == len(new_products_with_desc) and
             len(new_products) == len(new_products_with_desc)) or  # All new products have descriptions
            (len(existing_products) == len(new_products) and len(new_products) > 1)  # Same count, multiple items
        )

        if use_positional_matching:
            print(f"EntityService: Using positional matching - merging {len(new_products)} new products with {len(existing_products)} existing products by position")
            # Positional merge: match by index order
            merged_products = [prod.copy() for prod in existing_products]

            # If matching products without descriptions with new products
            if len(products_without_desc) == len(new_products_with_desc):
                for existing_idx, new_prod in zip(products_without_desc, new_products_with_desc):
                    print(f"EntityService: Positionally merging new product '{new_prod.get('description')}' into existing product at index {existing_idx}")
                    for key, value in new_prod.items():
                        if value is not None and value != '':
                            # Don't overwrite existing UOM with default "unit(s)"
                            if key == "unitofMeasures" and value == "unit(s)":
                                existing_uom = merged_products[existing_idx].get("unitofMeasures")
                                if existing_uom and existing_uom != "unit(s)":
                                    print(f"  Preserving existing UOM '{existing_uom}' (not overwriting with default)")
                                    continue
                            merged_products[existing_idx][key] = value
                            print(f"  Updated {key}={value}")
            else:
                # Same count of existing and new products - merge by position
                for i, new_prod in enumerate(new_products):
                    if i < len(merged_products):
                        print(f"EntityService: Positionally merging new product at index {i}")
                        for key, value in new_prod.items():
                            if value is not None and value != '':
                                # Don't overwrite existing UOM with default "unit(s)"
                                if key == "unitofMeasures" and value == "unit(s)":
                                    existing_uom = merged_products[i].get("unitofMeasures")
                                    if existing_uom and existing_uom != "unit(s)":
                                        print(f"  Preserving existing UOM '{existing_uom}' (not overwriting with default)")
                                        continue
                                merged_products[i][key] = value
                                print(f"  Updated {key}={value}")

            return merged_products

        # Standard merge logic (description-based matching)
        existing_descriptions = products_with_desc

        # Start with copies of existing products
        merged_products = [prod.copy() for prod in existing_products]

        # Process each new product
        for new_prod in new_products:
            new_desc = new_prod.get("description")

            # Belt and braces: strip_no_products_sentinel already removes these at
            # ingestion, but existing_products may have been persisted by an older build.
            if new_desc == NO_PRODUCTS_SENTINEL:
                print(f"EntityService: Skipping {NO_PRODUCTS_SENTINEL} entry in merge")
                continue

            # Handle None, non-string, or empty string descriptions
            if new_desc is None or not isinstance(new_desc, str) or not new_desc.strip():
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

                # Merge non-None and non-empty fields from new product into existing
                for key, value in new_prod.items():
                    # Only update if value is not None and not empty string
                    # This prevents overwriting existing good data with empty values
                    if value is not None and value != '':
                        # Don't overwrite existing UOM with default "unit(s)"
                        if key == "unitofMeasures" and value == "unit(s)":
                            existing_uom = merged_products[existing_index].get("unitofMeasures")
                            if existing_uom and existing_uom != "unit(s)":
                                print(f"  Preserving existing UOM '{existing_uom}' (not overwriting with default)")
                                continue
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

    def _detect_non_procurable_items(self, products: list) -> list:
        """
        Detect non-procurable items marked by the LLM during extraction.

        The LLM is instructed to set description="NON_PROCURABLE" and put the actual
        item name in remarks when it detects non-procurable items.

        Returns list of non-procurable item names found, or empty list if all items are procurable.
        """
        non_procurable = []

        for product in products:
            desc = product.get("description", "")

            # Check if LLM marked this as non-procurable
            if desc and desc.strip() == "NON_PROCURABLE":
                # Get the actual item name from remarks
                item_name = product.get("remarks", "unknown item")
                non_procurable.append(item_name)
                print(f"EntityService: Detected non-procurable item marked by LLM: {item_name}")

        return non_procurable

    def _check_quantity_limits(self, products: list) -> list:
        """
        Check if any product quantities exceed the maximum limit of 1,00,00,000,000 units.

        Returns list of dicts with product info for items that violate the limit,
        or empty list if all quantities are within limits.
        """
        MAX_QUANTITY = 10000000000
        violations = []

        for product in products:
            quantity = product.get("quantity")
            if quantity is not None:
                try:
                    # Convert to float for comparison
                    qty_value = float(quantity) if isinstance(quantity, str) else quantity
                    if qty_value > MAX_QUANTITY:
                        violations.append({
                            "description": product.get("description", "unknown product"),
                            "quantity": qty_value,
                            "max_allowed": MAX_QUANTITY
                        })
                        print(f"EntityService: Quantity limit violation - {product.get('description')}: {qty_value} > {MAX_QUANTITY}")
                except (ValueError, TypeError):
                    # Skip if quantity can't be converted to number
                    pass

        return violations

    def _clean_invalid_descriptions(self, products: list) -> list:
        """
        Remove descriptions that are actually units of measure, generic terms, or otherwise invalid.

        This ensures that invalid descriptions extracted by OpenAI are cleared so they can be
        properly requested from the user during validation.

        Args:
            products: List of product entities

        Returns:
            Updated products list with invalid descriptions set to None
        """
        # Same invalid description sets as in RFQValidationSchema for consistency
        UNIT_KEYWORDS = {
            'pcs', 'pc', 'pieces', 'piece',
            'kg', 'kgs', 'kilogram', 'kilograms',
            'g', 'grams', 'gram',
            'liters', 'liter', 'l', 'lt',
            'meters', 'meter', 'm', 'mt',
            'boxes', 'box',
            'sets', 'set',
            'units', 'unit',
            'sqft', 'sqm'
        }

        GENERIC_TERMS = {
            'item', 'items',
            'product', 'products',
            'thing', 'things',
            'stuff',
            'something'
        }

        cleaned_products = []

        for product in products:
            cleaned_product = product.copy()
            desc = product.get("description", "")

            if desc:
                desc_lower = desc.lower().strip()

                # Check if description is invalid
                is_invalid = (
                    desc_lower.isdigit() or  # Just a number
                    desc_lower in UNIT_KEYWORDS or  # Unit of measure
                    desc_lower in GENERIC_TERMS  # Too generic
                )

                if is_invalid:
                    cleaned_product["description"] = None
                    print(f"EntityService: Cleared invalid description '{desc}' (unit of measure or generic term)")

            cleaned_products.append(cleaned_product)

        return cleaned_products

    async def _auto_fill_location_from_pincode(self, products: list) -> list:
        """
        Auto-fill city and state from pincode using direct API lookup.

        IMPORTANT: Pincode is the authoritative source for city/state.
        User-provided city/state values are OVERRIDDEN by pincode lookup results
        to ensure data accuracy (e.g., user says "Mumbai 411005" but 411005 is Pune).

        Pincode is a global field (one per RFQ), so we fetch location once
        and apply to all products.

        Args:
            products: List of product entities

        Returns:
            Updated products list with city and state filled from pincode lookup
        """
        if not products:
            return products

        # Get pincode from first product (pincode is global - same for all products)
        pincode = next((p.get("pincode") for p in products if p.get("pincode")), None)
        if not pincode:
            return products

        # Fetch location data once (outside the loop)
        clean_pincode = str(pincode).strip()
        location_data = None
        validation_error = None

        if not clean_pincode.isdigit() or len(clean_pincode) != 6:
            validation_error = f"Invalid pincode format: {pincode}. Please enter a valid 6-digit pincode."
        else:
            try:
                location_data = await get_location_from_pincode_async(clean_pincode)
                if not location_data:
                    validation_error = f"Could not find location for pincode {pincode}. Please enter valid pincode"
            except Exception as e:
                logger.error(f"Error fetching location for pincode {pincode}: {e}")

        # Apply to all products
        updated_products = []
        for product in products:
            updated_product = product.copy()

            if location_data:
                if location_data.get("city"):
                    updated_product["city"] = location_data["city"]
                if location_data.get("state"):
                    updated_product["state"] = location_data["state"]
            elif validation_error:
                updated_product["pincode"] = None
                updated_product["city"] = None
                updated_product["state"] = None
                updated_product["pincode_validation_error"] = validation_error

            updated_products.append(updated_product)

        return updated_products