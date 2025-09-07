"""
Excel processing helpers for chat service.

Contains helper methods for processing Excel uploads, validation,
and integration with the chat workflow.
"""

import logging
from typing import Dict, Any, List
from datetime import datetime
from app.schemas.rfq import ExcelValidationSchema

logger = logging.getLogger(__name__)

class ExcelHelpers:
    """Helper methods for Excel processing in chat workflows."""
    
    @staticmethod
    def convert_excel_to_entities(items: List[Dict]) -> List[Dict]:
        """Convert Excel items to entity format for chat workflow."""
        entities = []
        for item in items:
            entity = {
                'description': item.get('ItemDescription', ''),
                'specification': item.get('Specification', ''),
                'quantity': item.get('Quantity', '1'),
                'unit_of_measure': item.get('Uom', 'pcs'),
                'remarks': item.get('Remarks', '')
            }
            entities.append(entity)
        return entities
    
    @staticmethod
    def calculate_excel_completeness(items: List[Dict]) -> int:
        """Calculate completeness percentage of Excel data."""
        if not items:
            return 0
        
        required_fields = ['ItemDescription', 'Quantity']
        total_checks = len(items) * len(required_fields)
        passed_checks = 0
        
        for item in items:
            for field in required_fields:
                if item.get(field) and str(item[field]).strip():
                    passed_checks += 1
        
        return int((passed_checks / total_checks) * 100) if total_checks > 0 else 0
    
    @staticmethod
    def generate_excel_summary(processing_result: Dict) -> str:
        """Generate a human-readable summary of Excel processing results."""
        try:
            filename = processing_result.get('filename', 'Excel file')
            total_items = processing_result.get('total_items', 0)
            items = processing_result.get('items', [])
            
            if total_items == 0:
                return f"I processed your file '{filename}' but didn't find any items."
            
            # Basic summary
            summary = f"I found {total_items} items in '{filename}'."
            
            # Add first few items as examples
            if items:
                summary += "\n\nHere are the first few items:"
                for i, item in enumerate(items[:3]):
                    desc = item.get('ItemDescription', f'Item {i+1}')
                    qty = item.get('Quantity', '1')
                    uom = item.get('Uom', 'pcs')
                    summary += f"\n• {desc} - {qty} {uom}"
            
            return summary
            
        except Exception as e:
            logger.error(f"Error generating Excel summary: {e}")
            return f"I processed your Excel file with {processing_result.get('total_items', 0)} items."
    
    @staticmethod
    def identify_missing_fields(items: List[Dict]) -> List[str]:
        """Identify which fields are commonly missing in Excel data."""
        missing_fields = []
        
        if not items:
            return ["No items found"]
        
        # Check for missing descriptions
        missing_descriptions = sum(1 for item in items if not item.get('ItemDescription', '').strip())
        if missing_descriptions > len(items) * 0.3:  # More than 30% missing
            missing_fields.append("Item descriptions")
        
        # Check for missing quantities
        missing_quantities = sum(1 for item in items if not item.get('Quantity', '').strip())
        if missing_quantities > len(items) * 0.3:
            missing_fields.append("Quantities")
        
        # Check for missing UOM
        missing_uom = sum(1 for item in items if not item.get('Uom', '').strip())
        if missing_uom > len(items) * 0.5:  # More than 50% missing
            missing_fields.append("Units of measure")
        
        # Check for missing specifications
        missing_specs = sum(1 for item in items if not item.get('Specification', '').strip())
        if missing_specs > len(items) * 0.7:  # More than 70% missing
            missing_fields.append("Specifications")
        
        return missing_fields
    
    @staticmethod
    def prepare_excel_context(processing_result: Dict, user_phone: str) -> Dict[str, Any]:
        """Prepare context for Excel-based chat workflow."""
        items = processing_result.get('items', [])
        
        context = {
            'workflow_type': 'excel_rfq_upload',
            'excel_data': processing_result,
            'extracted_entities': ExcelHelpers.convert_excel_to_entities(items),
            'completeness': ExcelHelpers.calculate_excel_completeness(items),
            'missing_fields': ExcelHelpers.identify_missing_fields(items),
            'conversation_stage': 'excel_processing',
            'user_phone': user_phone,
            'upload_timestamp': datetime.now().isoformat(),
            'filename': processing_result.get('filename', ''),
            'total_items': processing_result.get('total_items', 0)
        }
        
        return context
    
    @staticmethod
    def should_complete_immediately(completeness: int, items: List[Dict]) -> bool:
        """Determine if Excel data is complete enough for immediate RFQ creation."""
        if completeness < 80:
            return False
        
        # Check if all items have ALL required fields for GMT API
        required_fields = ['ItemDescription', 'Specification', 'Uom', 'Quantity']
        
        for item in items:
            for field in required_fields:
                if not item.get(field, '').strip():
                    return False
        
        return True
    
    @staticmethod
    def create_excel_validation_schema_from_items(items: List[Dict]) -> ExcelValidationSchema:
        """Create ExcelValidationSchema from Excel items data."""
        if not items:
            return ExcelValidationSchema()
        
        # Analyze all items to determine overall completeness
        # Check if most items have the required fields
        total_items = len(items)
        field_completeness = {
            'item_description': sum(1 for item in items if item.get('ItemDescription', '').strip()),
            'specification': sum(1 for item in items if item.get('Specification', '').strip()),
            'uom': sum(1 for item in items if item.get('Uom', '').strip()),
            'quantity': sum(1 for item in items if str(item.get('Quantity', '')).strip()),
            'serial_no': sum(1 for item in items if item.get('S.No', '').strip()),
            'remarks': sum(1 for item in items if item.get('Remarks', '').strip())
        }
        
        # Consider a field complete if at least 80% of items have it
        threshold = total_items * 0.8
        
        return ExcelValidationSchema(
            item_description="sample" if field_completeness['item_description'] >= threshold else None,
            specification="sample" if field_completeness['specification'] >= threshold else None,
            uom="sample" if field_completeness['uom'] >= threshold else None,
            quantity="sample" if field_completeness['quantity'] >= threshold else None,
            serial_no="sample" if field_completeness['serial_no'] >= threshold else None,
            remarks="sample" if field_completeness['remarks'] >= threshold else None
        )
    
    @staticmethod
    def generate_reupload_instructions(missing_fields: List[str], context: Dict) -> List[str]:
        """Generate instructions for user to fix Excel file and re-upload."""
        excel_data = context.get('excel_data', {})
        items = excel_data.get('items', [])
        filename = excel_data.get('filename', 'your Excel file')
        headers = excel_data.get('headers', [])
        column_mapping = excel_data.get('column_mapping', {})
        validation_result = excel_data.get('validation_result', {})
        
        instructions = []
        
        # Check if no items were found
        if not items:
            instructions.append(f"I couldn't find any data rows in your Excel file '{filename}'.")
            
            # Check if headers were detected
            if not headers:
                instructions.append("The file appears to be empty or corrupted.")
                instructions.append("Please ensure your Excel file contains:")
                instructions.append("• A header row with columns: S.No, ItemDescription, Specification, Uom, Quantity, Remarks")
                instructions.append("• At least one row of data below the headers")
            else:
                instructions.append(f"Found these columns: {', '.join(headers)}")
                
                # Check if target columns are missing
                target_columns = ['S.No', 'ItemDescription', 'Specification', 'Uom', 'Quantity', 'Remarks']
                mapped_columns = list(column_mapping.keys())
                
                missing_columns = []
                for target in target_columns:
                    if target not in column_mapping.values():
                        missing_columns.append(target)
                
                if missing_columns:
                    instructions.append("Missing required columns:")
                    for col in missing_columns:
                        instructions.append(f"• {col}")
                
                instructions.append("Please add data rows below your headers.")
            
            instructions.append("After fixing your Excel file, please upload it again.")
            return instructions
        
        # If items exist, check validation issues
        if validation_result.get('errors') or validation_result.get('missing_required_fields'):
            if validation_result.get('missing_required_fields'):
                missing_items = validation_result['missing_required_fields']
                
                # Count missing fields by type
                missing_by_field = {}
                for missing in missing_items:
                    if 'Missing' in missing:
                        field_name = missing.split("Missing '")[1].split("'")[0]
                        if field_name not in missing_by_field:
                            missing_by_field[field_name] = []
                        item_num = missing.split("Item ")[1].split(":")[0]
                        missing_by_field[field_name].append(item_num)
                
                # Create a simple, direct message
                message_parts = []
                for field, items in missing_by_field.items():
                    message_parts.append(f"Your Excel file '{filename}' is missing the {field} column.")
                    message_parts.append(f"Add a {field} column with details like:\n")
                    
                    # Add examples based on field type
                    if field == 'Specification':
                        message_parts.append('"Intel i7, 16GB RAM"')
                        message_parts.append('"Ergonomic, Adjustable"\n')
                    elif field == 'ItemDescription':
                        message_parts.append('"Laptop"')
                        message_parts.append('"Office Chair"\n')
                    elif field == 'Uom':
                        message_parts.append('"pcs"')
                        message_parts.append('"kg"\n')
                    elif field == 'Quantity':
                        message_parts.append('"10"')
                        message_parts.append('"5"\n')
                    else:
                        message_parts.append(f'"{field} details"\n')
                
                message_parts.append("Then resend the file.")
                
                # Return as a single instruction
                return ["\n".join(message_parts)]
            
            # Fallback for other errors
            return [f"Your Excel file '{filename}' has some issues. Please fix them and upload again."]
        
        # If only warnings (like empty fields), provide guidance
        if validation_result.get('warnings'):
            instructions.append(f"Your Excel file '{filename}' has some warnings:")
            for warning in validation_result['warnings'][:3]:  # Show first 3 warnings
                instructions.append(f"• {warning}")
            
            instructions.append("You can fix these issues and re-upload, or proceed with the current file.")
            return instructions
        
        # File appears complete
        return [f"Your Excel file '{filename}' appears to be complete for processing."]