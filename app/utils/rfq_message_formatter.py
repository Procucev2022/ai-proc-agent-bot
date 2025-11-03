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
    include_optional: bool = False,
    show_only_collected: bool = False,
    excel_source: bool = False
) -> str:
    """
    Generate a conversational RFQ response message without angle brackets for qty/items.
    Shows brand inline, remarks as a paragraph, and missing details as bullet points.

    Args:
        extracted_entities: List of product entities
        global_fields: Global delivery information
        missing_fields: List of missing field descriptions
        include_optional: Whether to include optional questions
        show_only_collected: If True, only show collected info without asking for missing fields (for modification clarification)
        excel_source: If True, this data came from an Excel upload (shows Excel-specific error messages)
    """

    items = []
    remarks_list = []
    MAX_ITEMS_TO_SHOW = 5  # Show only first 5 items
    MAX_REMARKS_LENGTH = 200  # Maximum total length for remarks

    # --- Collect entities ---
    for entity in extracted_entities:
        desc = (entity.get('description') or '').strip()
        qty = (entity.get('quantity') or '').strip()
        unit = (entity.get('unitofMeasures') or '').strip()
        brand = (entity.get('brand') or '').strip()
        remarks = (entity.get('remarks') or '').strip()

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
        elif brand and qty:
            # If no description but have brand and quantity, show brand
            if unit:
                items.append(f"{qty} {unit} {brand} brand")
            else:
                items.append(f"{qty} {brand} brand")

        if remarks:
            remarks_list.append(remarks)

    # Start with acknowledgment
    message = "*Got it!*"

    # Show extracted items if we have any
    if items:
        if len(items) <= MAX_ITEMS_TO_SHOW:
            items_text = ", ".join(items)
            message += f" You need {items_text}."
        else:
            # Show first few items and indicate there are more
            shown_items = ", ".join(items[:MAX_ITEMS_TO_SHOW])
            remaining = len(items) - MAX_ITEMS_TO_SHOW
            message += f" You need {shown_items}, and {remaining} more items."

    # Show collected delivery information if available
    delivery_info = []
    if global_fields.get("deliveryDate"):
        delivery_date = global_fields["deliveryDate"]
        # Format date nicely
        if isinstance(delivery_date, datetime):
            formatted_date = delivery_date.strftime("%d %b %Y")
        else:
            formatted_date = str(delivery_date)
        delivery_info.append(f"*Delivery Date:* {formatted_date}")

    if global_fields.get("city") or global_fields.get("state") or global_fields.get("pincode"):
        location_parts = []
        if global_fields.get("city"):
            location_parts.append(global_fields["city"])
        if global_fields.get("state"):
            location_parts.append(global_fields["state"])
        if global_fields.get("pincode"):
            location_parts.append(global_fields["pincode"])
        location_text = ", ".join(location_parts)
        delivery_info.append(f"*Delivery Location:* {location_text}")

    if delivery_info:
        message += "\n\n" + "\n".join(delivery_info)

    # Show remarks if available (consolidated from all products)
    if remarks_list:
        # Combine all remarks, truncate if too long
        combined_remarks = "; ".join(remarks_list)
        if len(combined_remarks) > MAX_REMARKS_LENGTH:
            combined_remarks = combined_remarks[:MAX_REMARKS_LENGTH] + "..."
        message += f"\n*Remarks:* {combined_remarks}"

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
    questions = missing_fields if missing_fields else []

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
            message += "\n\nI couldn't get everything though — looks like we're still missing:\n\n"
            for issue in all_issues:
                message += f"{issue}\n"
            message += "\nPlease share to proceed."
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


