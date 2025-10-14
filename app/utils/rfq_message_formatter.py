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

    # Check for validation errors in entities
    error_messages_set = set()  # Use set to avoid duplicates
    for entity in extracted_entities:
        if entity.get("date_validation_error"):
            error_messages_set.add(entity["date_validation_error"])
        if entity.get("pincode_validation_error"):
            error_messages_set.add(entity["pincode_validation_error"])
    
    error_messages = list(error_messages_set)

    for field in missing_fields:
        if (
            "date" in field.lower() and (
                "past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower()
            )
        ) or (
            "pincode" in field.lower() and (
                "could not find" in field.lower() or "invalid" in field.lower()
            )
        ):
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

def format_rfq_response_message(
    extracted_entities: List[Dict[str, Any]],
    global_fields: Dict[str, Any],
    missing_fields: List[str],
    include_optional: bool = False
) -> str:
    """
    Generate a conversational RFQ response message without angle brackets for qty/items.
    Shows brand inline, remarks as a paragraph, and missing details as bullet points.
    """

    items = []
    remarks_list = []

    # --- Collect entities ---
    for entity in extracted_entities:
        desc = entity.get('description', '').strip()
        qty = entity.get('quantity')
        unit = entity.get('unitofMeasures', '')
        brand = entity.get('brand')
        remarks = entity.get('remarks')

        # Combine brand inline with item name
        if desc:
            desc_text = desc.capitalize()
            if brand:
                desc_text += f" ({brand} brand)"
            if qty:
                if unit:
                    items.append(f"{qty} {unit} {desc_text}")
                else:
                    items.append(f"{qty} {desc_text}")
            else:
                items.append(desc_text)

        if remarks:
            remarks_list.append(f"{remarks}")

    # --- Base message ---
    if not items:
        message = "I understand you want to raise an RFQ."
    elif len(items) == 1:
        message = f"I understand you require {items[0]}."
    else:
        message = f"I understand you require {', '.join(items[:-1])}, and {items[-1]}."

    # --- Add delivery date ---
    if global_fields.get("deliveryDate"):
        date_str = global_fields["deliveryDate"]
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            date_str = date_obj.strftime("%d %b %Y")
        except:
            pass
        message = message.replace(".", f" on {date_str}.")

    # --- Add delivery location ---
    if any(global_fields.get(f) for f in ["city", "state", "pincode"]):
        location = ", ".join(
            str(global_fields.get(f)) for f in ["city", "state", "pincode"] if global_fields.get(f)
        )
        message = message.replace(".", f" for delivery at {location}.")

    # --- Append remarks paragraph ---
    if remarks_list:
        message += "\n\nRemark: " + " ".join(remarks_list)

    # --- Check for validation errors first ---
    validation_errors = set()  # Use set to avoid duplicates
    for entity in extracted_entities:
        if entity.get("date_validation_error"):
            validation_errors.add(entity["date_validation_error"])
        if entity.get("pincode_validation_error"):
            validation_errors.add(entity["pincode_validation_error"])
    
    # Add validation errors from missing_fields (for backward compatibility)
    for field in missing_fields:
        if (
            "date" in field.lower() and (
                "past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower()
            )
        ) or (
            "pincode" in field.lower() and (
                "could not find" in field.lower() or "invalid" in field.lower()
            )
        ):
            validation_errors.add(field)
    
    # Convert set back to list
    validation_errors = list(validation_errors)
    
    # --- Determine missing questions ---
    questions = []
    has_quantity = any(e.get('quantity') for e in extracted_entities)
    has_date_error = any(e.get('date_validation_error') for e in extracted_entities)
    has_pincode_error = any(e.get('pincode_validation_error') for e in extracted_entities)

    if len(items) == 1 and not has_quantity:
        questions.append("How much quantity do you need for each item?")
    elif len(items) > 1 and not all(e.get('quantity') for e in extracted_entities):
        questions.append("How much quantity do you need for all the items?")

    # Only ask for delivery date if no date error and no date provided
    if not global_fields.get("deliveryDate") and not has_date_error:
        questions.append("What is the delivery date you expect?")
    
    # Only ask for location if no pincode error and location not complete
    # if not all(global_fields.get(k) for k in ["city", "state", "pincode"]) and not has_pincode_error:
    #     questions.append("What is the delivery location?")
    # Extract global fields
    city = global_fields.get("city")
    state = global_fields.get("state")
    pincode = global_fields.get("pincode")

    # Check what's missing
    missing_city = not city
    missing_state = not state
    missing_pincode = not pincode

    # Only ask if no pincode error
    if not has_pincode_error:

        # If all missing → ask for overall location
        if missing_city and missing_state and missing_pincode:
            questions.append("What is the delivery location (city, state, and pincode)?")

        # Otherwise, ask for specific missing fields
        elif missing_pincode:
            questions.append("What is the delivery pincode?")
        elif missing_city:
            questions.append("What is the delivery city?")
        elif missing_state:
            questions.append("What is the delivery state?")

    # Combine validation errors and questions
    all_issues = validation_errors + questions

    # --- Add validation errors and questions with bullet points ---
    if all_issues:
        message += "\n\nHowever I need the following information to proceed.\n"
        for issue in all_issues:
            message += f"\n•  {issue}"
        message += "\n\nPlease provide this information so I can continue with your request."
    elif include_optional and missing_fields:
        # If no mandatory issues but flag is set, add optional questions
        message += "\n\n" + "\n".join(missing_fields)

    return message

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

    # Check for validation errors in entities
    error_messages_set = set()  # Use set to avoid duplicates
    for entity in extracted_entities:
        if entity.get("date_validation_error"):
            error_messages_set.add(entity["date_validation_error"])
        if entity.get("pincode_validation_error"):
            error_messages_set.add(entity["pincode_validation_error"])
    
    error_messages = list(error_messages_set)

    for field in missing_fields:
        if (
            "date" in field.lower()
            and ("past" in field.lower() or "invalid" in field.lower() or "kindly" in field.lower())
        ) or (
            "pincode" in field.lower()
            and ("could not find" in field.lower() or "invalid" in field.lower())
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