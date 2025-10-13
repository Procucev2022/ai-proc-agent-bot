"""
RFQ message formatting utilities.

Common functions for formatting RFQ-related messages that show extracted entities
and ask for missing details in a consistent format across the application.
"""

from typing import Dict, Any, List


def format_rfq_entities_message(extracted_entities: List[Dict[str, Any]], missing_fields: List[str]) -> str:
    """Format RFQ message showing extracted entities and asking for missing details.
    
    Args:
        extracted_entities: List of extracted product entities
        missing_fields: List of missing field descriptions
        
    Returns:
        Formatted message string in the new format
    """
    message_parts = ["Here's what I found from your request:"]
    message_parts.append("")
    
    # Show captured entities if any
    if extracted_entities:
        message_parts.append("✅ **Correctly Identified**")
        
        for i, entity in enumerate(extracted_entities, 1):
            if not isinstance(entity, dict):
                continue
            
            item_label = f"Item{i}" if len(extracted_entities) > 1 else "Item"
            message_parts.append(f"• **{item_label}:** {entity.get('description', 'Item')}")
            
            if entity.get('brand'):
                message_parts.append(f"• **Brand:** {entity.get('brand')}")
            
            if entity.get('quantity'):
                message_parts.append(f"• **Quantity:** {entity.get('quantity')}")
            
            if entity.get('unitofMeasures'):
                message_parts.append(f"• **Unit:** {entity.get('unitofMeasures')}")
            
            if entity.get('remarks'):
                message_parts.append(f"• **Specifications:** {entity.get('remarks')}")
            
            if len(extracted_entities) > 1 and i < len(extracted_entities):
                message_parts.append("")
        
        message_parts.append("")  # Empty line
    
    # Separate error messages from missing fields
    error_messages = []
    actual_missing_fields = []
    
    for field in missing_fields:
        if "date" in field.lower() and ("past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower()):
            error_messages.append(field)
        else:
            actual_missing_fields.append(field)
    
    # Show error messages if any
    if error_messages:
        message_parts.append("❌ **Invalid Details**")
        for error in error_messages:
            message_parts.append(f"• {error}")
        message_parts.append("")
    
    # Show missing information if any
    if actual_missing_fields:
        message_parts.append("⚠️ **Missing Details**")
        
        field_mapping = {
            "How many items do you need (quantity)?": "• Quantity: —",
            "What is the required delivery date?": "• Delivery Date: —",
            "What is the delivery state?": "• Delivery State: —",
            "What is the delivery city?": "• Delivery City: —", 
            "What is the delivery pincode?": "• Pin Code / ZIP: —",
            "Where should the items be delivered?": "• Delivery City: —\n• Delivery State: —\n• Pin Code / ZIP: —"
        }
        
        for field in actual_missing_fields:
            if field in field_mapping:
                message_parts.append(field_mapping[field])
            else:
                if "quantity" in field.lower():
                    message_parts.append("• Quantity: —")
                elif "delivery date" in field.lower():
                    message_parts.append("• Delivery Date: —")
                elif "state" in field.lower():
                    message_parts.append("• Delivery State: —")
                elif "city" in field.lower():
                    message_parts.append("• Delivery City: —")
                elif "pincode" in field.lower():
                    message_parts.append("• Pin Code / ZIP: —")
                elif "delivery" in field.lower() or "location" in field.lower():
                    message_parts.append("• Delivery City: —")
                    message_parts.append("• Delivery State: —")
                    message_parts.append("• Pin Code / ZIP: —")
                else:
                    message_parts.append(f"• {field}: —")
        message_parts.append("")
    
    if error_messages or actual_missing_fields:
        message_parts.append("Please provide the correct or missing information so I can continue with your request.")
        message_parts.append("")
        message_parts.append("What would you like to update?")
        
        # Add action buttons based on missing fields
        action_buttons = []
        for field in missing_fields:
            if "quantity" in field.lower():
                action_buttons.append("🔢 Add Quantity")
            elif "delivery date" in field.lower():
                action_buttons.append("📅 Update Delivery Date")
            elif "state" in field.lower():
                action_buttons.append("📍 Add Delivery State")
            elif "city" in field.lower():
                action_buttons.append("📍 Add Delivery City")
            elif "pincode" in field.lower():
                action_buttons.append("🔢 Add Pin Code")
            elif "delivery" in field.lower() or "location" in field.lower():
                action_buttons.append("📍 Add Delivery Location")
                action_buttons.append("🔢 Add Pin Code")
        
        # Remove duplicates and add to message
        unique_buttons = list(dict.fromkeys(action_buttons))
        message_parts.extend(unique_buttons)
    
    return "\n".join(message_parts)


def format_rfq_entities_with_global_fields(extracted_entities: List[Dict[str, Any]], 
                                         global_fields: Dict[str, Any], 
                                         missing_fields: List[str]) -> str:
    """Format RFQ message showing extracted entities with global fields and missing details.
    
    Args:
        extracted_entities: List of extracted product entities
        global_fields: Dict of global fields (deliveryDate, state, city, pincode)
        missing_fields: List of missing field descriptions
        
    Returns:
        Formatted message string in the new format
    """
    message_parts = ["Here's what I found from your request:"]
    message_parts.append("")
    
    # Show captured entities if any
    if extracted_entities or global_fields:
        message_parts.append("✅ **Correctly Identified**")
        
        # Show product entities
        for i, entity in enumerate(extracted_entities, 1):
            if not isinstance(entity, dict):
                continue
            
            item_label = f"Item{i}" if len(extracted_entities) > 1 else "Item"
            message_parts.append(f"• **{item_label}:** {entity.get('description', 'Item')}")
            
            if entity.get('brand'):
                message_parts.append(f"• **Brand:** {entity.get('brand')}")
            
            if entity.get('quantity'):
                message_parts.append(f"• **Quantity:** {entity.get('quantity')}")
            
            if entity.get('unitofMeasures'):
                message_parts.append(f"• **Unit:** {entity.get('unitofMeasures')}")
            
            if entity.get('remarks'):
                message_parts.append(f"• **Specifications:** {entity.get('remarks')}")
            
            if len(extracted_entities) > 1 and i < len(extracted_entities):
                message_parts.append("")
        
        # Show global fields if available
        if global_fields:
            if global_fields.get('deliveryDate'):
                message_parts.append(f"• **Delivery Date:** {global_fields['deliveryDate']}")
            
            if global_fields.get('city'):
                message_parts.append(f"• **Delivery City:** {global_fields['city']}")
            
            if global_fields.get('state'):
                message_parts.append(f"• **Delivery State:** {global_fields['state']}")
            
            if global_fields.get('pincode'):
                message_parts.append(f"• **Pin Code:** {global_fields['pincode']}")
        
        message_parts.append("")  # Empty line
    
    # Separate error messages from missing fields
    error_messages = []
    actual_missing_fields = []
    
    for field in missing_fields:
        if "date" in field.lower() and ("past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower()):
            error_messages.append(field)
        else:
            actual_missing_fields.append(field)
    
    # Show error messages if any
    if error_messages:
        message_parts.append("❌ **Invalid Details**")
        for error in error_messages:
            message_parts.append(f"• {error}")
        message_parts.append("")
    
    # Show missing information if any
    if actual_missing_fields:
        message_parts.append("⚠️ **Missing Details**")
        
        field_mapping = {
            "How many items do you need (quantity)?": "• Quantity: —",
            "What is the required delivery date?": "• Delivery Date: —",
            "What is the delivery state?": "• Delivery State: —",
            "What is the delivery city?": "• Delivery City: —",
            "What is the delivery pincode?": "• Pin Code / ZIP: —",
            "Where should the items be delivered?": "• Delivery City: —\n• Delivery State: —\n• Pin Code / ZIP: —"
        }
        
        for field in actual_missing_fields:
            if field in field_mapping:
                message_parts.append(field_mapping[field])
            else:
                if "quantity" in field.lower():
                    message_parts.append("• Quantity: —")
                elif "delivery date" in field.lower():
                    message_parts.append("• Delivery Date: —")
                elif "state" in field.lower():
                    message_parts.append("• Delivery State: —")
                elif "city" in field.lower():
                    message_parts.append("• Delivery City: —")
                elif "pincode" in field.lower():
                    message_parts.append("• Pin Code / ZIP: —")
                elif "delivery" in field.lower() or "location" in field.lower():
                    message_parts.append("• Delivery City: —")
                    message_parts.append("• Delivery State: —")
                    message_parts.append("• Pin Code / ZIP: —")
                else:
                    message_parts.append(f"• {field}: —")
        message_parts.append("")
    
    if error_messages or actual_missing_fields:
        message_parts.append("Please provide the correct or missing information so I can continue with your request.")
        message_parts.append("")
        message_parts.append("What would you like to update?")
        
        # Add action buttons
        action_buttons = []
        for field in missing_fields:
            if "quantity" in field.lower():
                action_buttons.append("🔢 Add Quantity")
            elif "delivery date" in field.lower():
                action_buttons.append("📅 Update Delivery Date")
            elif "state" in field.lower():
                action_buttons.append("📍 Add Delivery State")
            elif "city" in field.lower():
                action_buttons.append("📍 Add Delivery City")
            elif "pincode" in field.lower():
                action_buttons.append("🔢 Add Pin Code")
            elif "delivery" in field.lower() or "location" in field.lower():
                action_buttons.append("📍 Add Delivery Location")
                action_buttons.append("🔢 Add Pin Code")
        
        unique_buttons = list(dict.fromkeys(action_buttons))
        message_parts.extend(unique_buttons)
    
    return "\n".join(message_parts)


def format_simple_missing_fields_message(missing_fields: List[str]) -> str:
    """Format simple message asking for missing fields.
    
    Args:
        missing_fields: List of missing field descriptions
        
    Returns:
        Formatted message string
    """
    if not missing_fields:
        return "Thank you for the information!"
    
    questions_text = "\n".join(f"• {field}" for field in missing_fields)
    return f"Please provide the following details:\n\n{questions_text}"


# Example usage:
# 
# from app.utils.rfq_message_formatter import format_rfq_entities_message
# 
# # Example 1: Basic usage with extracted entities and missing fields
# extracted_entities = [
#     {
#         "description": "Laptops", 
#         "quantity": "2",
#         "unitofMeasures": "pieces"
#     }
# ]
# missing_fields = ["Delivery date", "Delivery location"]
# 
# message = format_rfq_entities_message(extracted_entities, missing_fields)
# # Output:
# # Great! I've captured:
# # ✓ Product: Laptops (Qty: 2) - pieces
# # 
# # Missing information:
# # - Delivery date
# # - Delivery location
# # 
# # Please share these details to continue.
# 
# # Example 2: Multiple products
# extracted_entities = [
#     {"description": "Office Chairs", "quantity": "10"},
#     {"description": "Desks", "quantity": "5", "unitofMeasures": "units"}
# ]
# missing_fields = ["Delivery date"]
# 
# message = format_rfq_entities_message(extracted_entities, missing_fields)
# 
# # Example 3: Only missing fields (no extracted entities)
# message = format_simple_missing_fields_message(["Product description", "Quantity", "Delivery date"])