"""
Entity extraction service for parsing structured data from user messages.

This service focuses solely on extracting entities from user messages using OpenAI.
It does not handle completeness checking or question generation - that's handled
by the data model and orchestration layer.
"""

import json
import os
from typing import Dict

from ..services.openai_service import OpenAIService


class EntityService:
    """Entity extraction service using OpenAI function calling."""

    def __init__(self, openai_service=None):
        self.openai_service = openai_service or OpenAIService()

    def extract_entities(self, message: str, context: dict = None, workflow_type: str = "buy_something") -> dict:
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
                    return self._handle_reference_extraction(message, context, {
                        "reference_type": reference_details.get("reference_type"),
                        "confidence": intent_context.get("confidence", 0),
                        "reasoning": intent_context.get("reasoning", ""),
                        "detected_phrases": []  # Not available from intent service
                    })
            
            # Check if this is a modification request with pending confirmations
            has_pending_confirmations = context and (
                context.get("workflow_state", {}).get("pending_multiple_rfqs") or
                context.get("workflow_state", {}).get("pending_rfq")
            )
            
            if has_pending_confirmations:
                print(f"EntityService: Detected modification context with pending confirmations")
                return self._handle_modification_extraction(message, context, workflow_type)
            else:
                # Standard entity extraction for new requests
                return self._handle_standard_extraction(message, context, workflow_type)

        except Exception as e:
            print(f"Entity extraction error: {e}")
            return {"products": [], "confidence": 0, "success": False}
    
    def _handle_standard_extraction(self, message: str, context: dict = None, workflow_type: str = "buy_something") -> dict:
        """Handle standard entity extraction for new requests."""
        # Build prompt for entity extraction
        prompt = f"Extract entities from: '{message}'"
        if context and context.get("extracted_entities"):
            prompt += f"\nExisting entities: {context['extracted_entities']}"

        # Call OpenAI extract_entities method
        response = self.openai_service.extract_entities(
            message=prompt,
            workflow_type=workflow_type
        )

        # Handle both old single entity format and new multi-product format
        if "products" in response:
            # New multi-product format
            print(f"EntityService: Found products array with {len(response.get('products', []))} products")
            for i, product in enumerate(response.get('products', [])):
                print(f"  Product {i+1}: {product}")
            return {
                "products": response.get("products", []),
                "confidence": response.get("confidence", 0),
                "success": response.get("success", True)
            }
        else:
            # Backward compatibility for old single entity format
            print(f"EntityService: Using backward compatibility with entities: {response.get('entities', {})}")
            return {
                "entities": response.get("entities", {}),
                "confidence": response.get("confidence", 0),
                "success": response.get("success", True)
            }
    
    def _handle_modification_extraction(self, message: str, context: dict, workflow_type: str = "buy_something") -> dict:
        """Handle entity extraction for modification requests with existing pending confirmations."""
        print(f"EntityService: Processing modification request: '{message}'")
        
        # Get existing pending confirmation data
        workflow_state = context.get("workflow_state", {})
        
        # Get pending products with proper handling
        pending_multiple = workflow_state.get("pending_multiple_rfqs")
        pending_single = workflow_state.get("pending_rfq")
        
        if pending_multiple:
            pending_products = pending_multiple
        elif pending_single:
            pending_products = [pending_single]
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
            return self._handle_standard_extraction(message, context, workflow_type)
        
        # Extract modification details from the message
        modification_prompt = f"""
        Extract modification details from: '{message}'
        
        Context: User is modifying existing RFQ products:
        {self._format_existing_products_for_prompt(pending_products)}
        
        Focus on identifying WHAT is being modified and the NEW VALUES only.
        """
        
        # Call OpenAI to extract modification details
        response = self.openai_service.extract_entities(
            message=modification_prompt,
            workflow_type=workflow_type
        )
        
        print(f"EntityService: Modification extraction response: {response}")
        
        # Apply modifications to existing products
        if "products" in response and response["products"]:
            modified_products = self._apply_modifications_to_existing_products(
                pending_products, response["products"], message
            )
            print(f"EntityService: Applied modifications, returning {len(modified_products)} updated products")
            return {
                "products": modified_products,
                "confidence": response.get("confidence", 0),
                "success": True,
                "is_modification": True
            }
        else:
            print(f"EntityService: No clear modification detected, falling back to standard extraction")
            return self._handle_standard_extraction(message, context, workflow_type)
    
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
                updated_products.append(product_info["entities"].copy())
            else:
                # If it's already an entity dict, use it directly
                updated_products.append(product_info.copy())
        
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
        
        print(f"  No matching product found")
        return None

    def _handle_reference_extraction(self, message: str, context: dict, reference_detection: dict) -> dict:
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
            return self._handle_standard_extraction(message, context, "buy_something")
        
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
            return self._handle_standard_extraction(message, context, "buy_something")

    def extract_entities_with_summary_context(self, message: str, context: dict = None) -> dict:
        """
        Extract entities using historical context from chat summaries.
        
        This method uses AI to resolve references like "same as last time", "usual address"
        by analyzing previous conversation summaries and replacing references with actual values.
        
        Args:
            message: User message to extract entities from
            context: Context dictionary containing chat_summaries and other data
            
        Returns:
            Dict containing extracted products with resolved references and metadata
        """
        try:
            # Check if we have chat summaries for context
            chat_summaries = context.get("chat_summaries", []) if context else []
            
            if not chat_summaries:
                print("EntityService: No chat summaries available, falling back to standard extraction")
                return self._handle_standard_extraction(message, context, "buy_something")
            
            print(f"EntityService: Using summary-aware extraction with {len(chat_summaries)} summaries")
            
            # Use new OpenAI method with summaries
            response = self.openai_service.extract_entities_with_summary_context(
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
                updated_products = self._apply_resolved_references_intelligently(products, resolved_refs, message)
                response["products"] = updated_products
                print(f"EntityService: Applied resolved references to {len(updated_products)} products using AI")
            
            return response
            
        except Exception as e:
            print(f"EntityService: Summary-aware extraction error: {e}")
            # Fallback to standard extraction on error
            return self._handle_standard_extraction(message, context, "buy_something")

    def _apply_resolved_references_intelligently(self, products: list, resolved_refs: list, original_message: str) -> list:
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
            merge_result = self.openai_service.merge_resolved_references_with_entities(
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