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
    MAX_ITEMS_TO_SHOW = 5  # Show only first 5 items
    MAX_REMARKS_LENGTH = 200  # Maximum total length for remarks

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

        if remarks and remarks.strip():
            remarks_list.append(f"{remarks.strip()}")

    # Start with acknowledgment
    message = "*Got it!*"

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
        ) or (
            "rows" in field.lower() and "50" in field.lower()
        ) or (
            "merged cells" in field.lower()
        ):
            validation_errors.add(field)
    
    # Convert set back to list
    validation_errors = list(validation_errors)
    
    # --- Determine missing questions ---
    questions = []
    has_quantity = any(e.get('quantity') for e in extracted_entities)
    has_date_error = any(e.get('date_validation_error') for e in extracted_entities)
    has_pincode_error = any(e.get('pincode_validation_error') for e in extracted_entities)
    
    # Check if this is from Excel upload (has multiple items) and missing quantities
    is_excel_upload = len(extracted_entities) > 3  # Assume Excel if more than 3 items
    missing_quantities = [e for e in extracted_entities if not e.get('quantity')]
    
    if missing_quantities:
        if is_excel_upload:
            # For Excel uploads with missing quantities, suggest re-upload
            missing_count = len(missing_quantities)
            if missing_count == 1:
                questions.append(f"Your Excel file is missing quantity for 1 item. Please add the missing quantity and reupload the file.")
            else:
                questions.append(f"Your Excel file is missing quantities for {missing_count} items. Please add the missing quantities and reupload the file.")
        else:
            # For manual input, ask for quantities
            if len(items) == 1 and not has_quantity:
                questions.append("Quantity")
            elif len(items) > 1:
                questions.append("Quantity?")

    # Only ask for delivery date if no date error and no date provided
    if not global_fields.get("deliveryDate") and not has_date_error:
        questions.append("Delivery Date")
    
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
            questions.append("Delivery Pincode?")

        # Otherwise, ask for specific missing fields
        elif missing_pincode:
            questions.append("Delivery Pincode?")
        elif missing_city:
            questions.append("Delivery City?")
        elif missing_state:
            questions.append("Delivery State?")

    # Combine validation errors and questions
    all_issues = validation_errors + questions

    # --- Add validation errors and questions with bullet points ---
    if all_issues:
        # Check if any issues are Excel validation errors
        excel_errors = [issue for issue in all_issues if 
                       ("50 rows" in issue and "allowed" in issue) or 
                       "merged cells" in issue.lower() or
                       ("Excel file" in issue and ("missing" in issue or "reupload" in issue))]
        
        if excel_errors:
            # For Excel validation errors, show them prominently and stop processing
            message += "\n\n"
            for error in excel_errors:
                if "50 rows" in error and "allowed" in error:
                    message += "[ERROR] Your Excel file has more than 50 rows. Please reduce to 50 rows and reupload the file.\n"
                elif "merged cells" in error.lower():
                    message += "[ERROR] Your Excel file contains merged cells. Please unmerge all cells and reupload the file.\n"
                elif "Excel file" in error and "missing" in error:
                    message += f"[ERROR] {error}\n"
                else:
                    message += f"[ERROR] {error}\n"
        else:
            # For other validation issues, show as questions
            message += " To proceed, please share the following information:\n\n"
            for issue in all_issues:
                message += f"•  {issue}\n"
            message += "\nOnce I have this, I can continue with your request."
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

def format_excel_validation_error_message(error_message: str) -> str:
    """Format Excel validation error message for user.
    
    Args:
        error_message: The validation error message
        
    Returns:
        Formatted error message string
    """
    if "50 rows" in error_message and "allowed" in error_message:
        return "[ERROR] Your Excel file has more than 50 rows. Please reduce to 50 rows and reupload the file."
    elif "merged cells" in error_message.lower():
        return "[ERROR] Your Excel file contains merged cells. Please unmerge all cells and reupload the file."
    else:
        return f"[ERROR] {error_message}"


