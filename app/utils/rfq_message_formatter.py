"""
RFQ message formatting utilities.

Common functions for formatting RFQ-related messages that show extracted entities
and ask for missing details in a consistent format across the application.
"""

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
            questions.append("What is the delivery pincode?")

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


