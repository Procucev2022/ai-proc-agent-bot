"""
RFQ message formatting utilities.

Common functions for formatting RFQ-related messages that show extracted entities
and ask for missing details in a consistent format across the application.
"""

from typing import Dict, Any, List


from typing import List, Dict, Any
from datetime import datetime

def format_rfq_entities_message(extracted_entities: List[Dict[str, Any]], missing_fields: List[str], are_required: bool = True) -> str:
    """
    Format RFQ message for WhatsApp showing extracted entities and missing details.
    (No markdown or asterisks; clean, emoji-friendly formatting)
    """
    message_parts = ["Thanks!", "Here's what I've got so far:"]
    message_parts.append("")

    # --- Captured Entities ---
    if extracted_entities:
        message_parts.append("✅ *Items Requested:*")

        # Detect common fields (delivery details common to all products)
        common_fields = {}
        if len(extracted_entities) > 1:
            first_entity = extracted_entities[0] if isinstance(extracted_entities[0], dict) else {}
            for field in ['deliveryDate', 'state', 'city', 'pincode']:
                value = first_entity.get(field)
                if value and all(
                    entity.get(field) == value for entity in extracted_entities if isinstance(entity, dict)
                ):
                    common_fields[field] = value

        # --- Item-wise Display ---
        for i, entity in enumerate(extracted_entities, 1):
            if not isinstance(entity, dict):
                continue

            description = entity.get('description', 'Item')
            description = description[:1].upper() + description[1:] if description else 'Item'
            quantity = entity.get('quantity', '')
            unit = entity.get('unitofMeasures', '')
            brand = entity.get('brand', '')
            specs = entity.get('remarks', '')

            # Main product line
            line = f"• {description}"
            if quantity:
                line += f" – {quantity}"
            if unit:
                line += f" {unit}"
            message_parts.append(line)

            # Nested details
            if brand:
                message_parts.append(f"   • Brand: {brand}")
            if specs:
                message_parts.append(f"   • Specifications: {specs}")

            # Delivery date (only if not common)
            if entity.get('deliveryDate') and 'deliveryDate' not in common_fields:
                delivery_date = entity.get('deliveryDate')
                try:
                    if isinstance(delivery_date, str) and len(delivery_date) == 10:
                        date_obj = datetime.strptime(delivery_date, '%Y-%m-%d')
                        delivery_date = date_obj.strftime('%d %b %Y')
                except:
                    pass
                message_parts.append(f"   • Delivery Date: {delivery_date}")

        # --- Common Fields (for all items) ---
        if common_fields:
            message_parts.append("")
            if len(extracted_entities) > 1:
                message_parts.append("*For all items:*")

            if common_fields.get('deliveryDate'):
                delivery_date = common_fields['deliveryDate']
                try:
                    if isinstance(delivery_date, str) and len(delivery_date) == 10:
                        date_obj = datetime.strptime(delivery_date, '%Y-%m-%d')
                        delivery_date = date_obj.strftime('%d %b %Y')
                except:
                    pass
                message_parts.append(f"• Delivery Date: {delivery_date}")

            if common_fields.get('city'):
                message_parts.append(f"• Delivery City: {common_fields['city']}")
            if common_fields.get('state'):
                message_parts.append(f"• Delivery State: {common_fields['state']}")
            if common_fields.get('pincode'):
                message_parts.append(f"• Pin Code: {common_fields['pincode']}")

        message_parts.append("")

    # --- Error & Missing Info Sections ---
    error_messages = []
    actual_missing_fields = []

    for field in missing_fields:
        if ("date" in field.lower() and (
            "past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower())) or \
           ("pincode" in field.lower() and ("could not find" in field.lower() or "invalid" in field.lower())):
            error_messages.append(field)
        else:
            actual_missing_fields.append(field)

    # Invalid details section
    if error_messages:
        message_parts.append("❌ *Invalid Details:*")
        for error in error_messages:
            message_parts.append(f"• {error}")
        message_parts.append("")

    # Missing details section
    if actual_missing_fields:
        message_parts.append("⚠️ *Need More Information:*")

        field_mapping = {
            "How many items do you need (quantity)?": "• Quantity",
            "What is the required delivery date?": "• Delivery Date",
            "What is the delivery state?": "• Delivery State",
            "What is the delivery city?": "• Delivery City",
            "What is the delivery pincode?": "• Pin Code / ZIP",
            "Where should the items be delivered?": "• Delivery City\n• Delivery State\n• Pin Code / ZIP"
        }

        for field in actual_missing_fields:
            if field in field_mapping:
                message_parts.append(field_mapping[field])
            else:
                field_lower = field.lower()
                if "quantity" in field_lower:
                    message_parts.append("• Quantity")
                elif "delivery date" in field_lower:
                    message_parts.append("• Delivery Date")
                elif "state" in field_lower:
                    message_parts.append("• Delivery State")
                elif "city" in field_lower:
                    message_parts.append("• Delivery City")
                elif "pincode" in field_lower:
                    message_parts.append("• Pin Code / ZIP")
                elif "delivery" in field_lower or "location" in field_lower:
                    message_parts.append("• Delivery City")
                    message_parts.append("• Delivery State")
                    message_parts.append("• Pin Code / ZIP")
                else:
                    message_parts.append(f"• {field}")
        message_parts.append("")

    if error_messages or actual_missing_fields:
        if are_required:
            message_parts.append("Please share the missing or invalid details to continue.")

    return "\n".join(message_parts)


from typing import List, Dict, Any
from datetime import datetime

def format_rfq_entities_with_global_fields(
    extracted_entities: List[Dict[str, Any]],
    global_fields: Dict[str, Any],
    missing_fields: List[str],
    are_required: bool = True
) -> str:
    """
    Format RFQ message for WhatsApp showing extracted entities with global fields and missing details.
    (No Markdown or bold text — clean mobile-friendly format)
    """
    message_parts = ["Thanks!", "Here's what I've got so far:"]
    message_parts.append("")

    # --- Display Captured Entities ---
    if extracted_entities or global_fields:
        message_parts.append("✅ *Items Requested:*")
        message_parts.append("")

        # Show individual product entities
        for i, entity in enumerate(extracted_entities, 1):
            if not isinstance(entity, dict):
                continue

            description = entity.get("description", "Item")
            description = description[:1].upper() + description[1:] if description else 'Item'
            quantity = entity.get("quantity")
            unit = entity.get("unitofMeasures")
            brand = entity.get("brand")
            specs = entity.get("remarks")

            # Product line
            line = f"• {description}"
            if quantity:
                line += f" – {quantity}"
            if unit:
                line += f" {unit}"
            message_parts.append(line)

            # Details under item
            if brand:
                message_parts.append(f"   • Brand: {brand}")
            if specs:
                message_parts.append(f"   • Specifications: {specs}")

        # --- Global Fields (apply to all items) ---
        if global_fields and any(global_fields.values()):
            message_parts.append("")
            if len(extracted_entities) > 1:
                message_parts.append("*For all items:*")

            if global_fields.get("deliveryDate"):
                delivery_date = global_fields["deliveryDate"]
                try:
                    if isinstance(delivery_date, str) and len(delivery_date) == 10:
                        date_obj = datetime.strptime(delivery_date, "%Y-%m-%d")
                        delivery_date = date_obj.strftime("%d %b %Y")
                except:
                    pass
                message_parts.append(f"• Delivery Date: {delivery_date}")

            if global_fields.get("city"):
                message_parts.append(f"• Delivery City: {global_fields['city']}")
            if global_fields.get("state"):
                message_parts.append(f"• Delivery State: {global_fields['state']}")
            if global_fields.get("pincode"):
                message_parts.append(f"• Pin Code: {global_fields['pincode']}")

        message_parts.append("")

    # --- Handle Missing and Invalid Fields ---
    error_messages = []
    actual_missing_fields = []

    for field in missing_fields:
        if (
            "date" in field.lower()
            and ("past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower())
        ):
            error_messages.append(field)
        else:
            actual_missing_fields.append(field)

    # Invalid details
    if error_messages:
        message_parts.append("❌ *Invalid Details:*")
        for error in error_messages:
            message_parts.append(f"• {error}")
        message_parts.append("")

    # Missing details
    if actual_missing_fields:
        message_parts.append("⚠️ *Need More Information:*")

        field_mapping = {
            "How many items do you need (quantity)?": "• Quantity",
            "What is the required delivery date?": "• Delivery Date",
            "What is the delivery state?": "• Delivery State",
            "What is the delivery city?": "• Delivery City",
            "What is the delivery pincode?": "• Pin Code / ZIP",
            "Where should the items be delivered?": "• Delivery City\n• Delivery State\n• Pin Code / ZIP",
        }

        for field in actual_missing_fields:
            if field in field_mapping:
                message_parts.append(field_mapping[field])
            else:
                f_lower = field.lower()
                if "quantity" in f_lower:
                    message_parts.append("• Quantity")
                elif "delivery date" in f_lower:
                    message_parts.append("• Delivery Date")
                elif "state" in f_lower:
                    message_parts.append("• Delivery State")
                elif "city" in f_lower:
                    message_parts.append("• Delivery City")
                elif "pincode" in f_lower:
                    message_parts.append("• Pin Code / ZIP")
                elif "delivery" in f_lower or "location" in f_lower:
                    message_parts.append("• Delivery City")
                    message_parts.append("• Delivery State")
                    message_parts.append("• Pin Code / ZIP")
                else:
                    message_parts.append(f"• {field}")
        message_parts.append("")

    if error_messages or actual_missing_fields:
        if are_required:
            message_parts.append("Please share the missing or invalid details to continue.")

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