"""
Simple Excel processing service that uses OpenAI for intelligent analysis.
"""

import logging
import io
import pandas as pd
import base64
from typing import Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

class ExcelProcessingService:
    """Simple service for processing Excel files using OpenAI service for intelligent analysis."""
    
    def __init__(self, openai_service=None):
        from app.services.openai_service import OpenAIService
        self.openai_service = openai_service or OpenAIService()
        
        # Target columns based on the screenshot
        self.target_columns = ['S.No', 'ItemDescription', 'Specification', 'Uom', 'Quantity', 'Remarks']
    
    async def process_excel_file(self, content: bytes, filename: str) -> Dict[str, Any]:
        """Process Excel file and extract items data using OpenAI for intelligent analysis."""
        try:
            # Read Excel file
            file_obj = io.BytesIO(content)
            df = pd.read_excel(file_obj, sheet_name=0, header=None)
            
            # Remove completely empty rows
            df = df.dropna(how='all')
            
            if df.empty:
                return {
                    'success': False,
                    'error': 'No data found in Excel file'
                }
            
            logger.info(f"DEBUG: Excel file shape: {df.shape}")
            logger.info(f"DEBUG: First 3 rows: {df.head(3).values.tolist()}")
            
            # Get first 5 rows for header detection
            sample_rows = df.head(5).values.tolist()
            
            # Use OpenAI to detect header row
            header_result = self.openai_service.detect_excel_header_row(sample_rows)
            header_row_index = header_result.get('header_row_index')
            
            logger.info(f"DEBUG: Header detection result: {header_result}")
            
            if header_row_index is None or header_row_index < 0:
                # No clear header found, use first row as data
                headers = [f"Column_{i+1}" for i in range(df.shape[1])]
                data_df = df
            else:
                # Extract headers and data
                headers = df.iloc[header_row_index].astype(str).tolist()
                data_df = df.iloc[header_row_index + 1:].reset_index(drop=True)
            
            # Clean up headers by trimming whitespace
            headers = [header.strip() for header in headers]
            
            logger.info(f"DEBUG: Extracted headers: {headers}")
            logger.info(f"DEBUG: Data shape after header extraction: {data_df.shape}")
            
            # Use OpenAI to map columns to target format
            mapping_result = self.openai_service.map_excel_columns(headers)
            column_mapping = mapping_result.get('column_mapping', {})
            
            logger.info(f"DEBUG: Column mapping result: {mapping_result}")
            logger.info(f"DEBUG: Column mapping: {column_mapping}")
            
            # Extract items using the mapping
            items = self._extract_items_with_mapping(data_df, headers, column_mapping)
            
            logger.info(f"DEBUG: Extracted {len(items)} items")
            if items:
                logger.info(f"DEBUG: First item: {items[0]}")
            
            # Validate items for GMT API requirements
            validation_result = self._validate_items_for_gmt_api(items)
            
            return {
                'success': True,
                'filename': filename,
                'items': items,
                'total_items': len(items),
                'headers': headers,
                'column_mapping': column_mapping,
                'header_row_index': header_row_index,
                'header_detection': header_result,
                'mapping_details': mapping_result,
                'validation_result': validation_result,
                'summary': f"Found {len(items)} items in {filename}"
            }
            
        except Exception as e:
            logger.error(f"Error processing Excel file: {e}")
            return {
                'success': False,
                'error': f'Failed to process Excel: {str(e)}'
            }
    
    def _extract_items_with_mapping(self, df: pd.DataFrame, headers: List[str], column_mapping: Dict[str, str]) -> List[Dict[str, Any]]:
        """Extract items using the column mapping."""
        items = []
        
        try:
            for index, row in df.iterrows():
                # Skip empty rows
                if row.isna().all():
                    continue
                
                item = {}
                has_data = False
                
                # Map columns to target format
                for header in headers:
                    if header in column_mapping:
                        target_col = column_mapping[header]
                        col_index = headers.index(header)
                        
                        if col_index < len(row):
                            value = row.iloc[col_index]
                            logger.info(f"DEBUG: Extracting header '{header}' -> '{target_col}', value: '{value}'")
                            if pd.notna(value) and str(value).strip():
                                item[target_col] = str(value).strip()
                                has_data = True
                            else:
                                logger.warning(f"DEBUG: Empty/NaN value for '{header}' -> '{target_col}': '{value}'")
                
                # Add serial number if missing
                if has_data:
                    if 'S.No' not in item:
                        item['S.No'] = str(len(items) + 1)
                    
                    # Set default UOM if missing but quantity exists
                    if 'Uom' not in item and 'Quantity' in item:
                        item['Uom'] = 'pcs'
                    
                    items.append(item)
            
            return items
            
        except Exception as e:
            logger.error(f"Error extracting items: {e}")
            return []
    
    def _validate_items_for_gmt_api(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate items meet GMT API requirements."""
        validation_result = {
            'valid': True,
            'warnings': [],
            'errors': [],
            'missing_required_fields': [],
            'empty_required_fields': []
        }
        
        required_fields = ['ItemDescription', 'Specification', 'Uom', 'Quantity']
        
        for i, item in enumerate(items, 1):
            # Check for missing required fields
            for field in required_fields:
                if field not in item:
                    validation_result['missing_required_fields'].append(f"Item {i}: Missing '{field}'")
                    validation_result['valid'] = False
                elif not str(item[field]).strip():
                    validation_result['empty_required_fields'].append(f"Item {i}: Empty '{field}'")
                    validation_result['warnings'].append(f"Item {i}: '{field}' is empty")
        
        # Check for common issues
        if validation_result['missing_required_fields']:
            validation_result['errors'].extend(validation_result['missing_required_fields'])
        
        if validation_result['empty_required_fields']:
            validation_result['warnings'].extend(validation_result['empty_required_fields'])
        
        logger.info(f"DEBUG: Validation result: {validation_result}")
        return validation_result
    
    def create_standard_template(self, items: List[Dict[str, Any]]) -> bytes:
        """Create standardized Excel template for GMT API."""
        try:
            logger.info(f"DEBUG: Creating template for {len(items)} items")
            template_data = []
            
            for i, item in enumerate(items, 1):
                logger.info(f"DEBUG: Processing item {i}: {item}")
                
                # Convert numeric fields to proper types
                try:
                    sno = int(item.get('S.No', i))
                except (ValueError, TypeError):
                    sno = i
                
                try:
                    quantity = int(item.get('Quantity', '1'))
                except (ValueError, TypeError):
                    quantity = 1
                
                row = {
                    'S.No': sno,  # Keep as integer
                    'ItemDescription': str(item.get('ItemDescription', '')),
                    'Specification': str(item.get('Specification', '')),
                    'Uom': str(item.get('Uom', 'pcs')),
                    'Quantity': quantity,  # Keep as integer
                    'Remarks': str(item.get('Remarks', ''))
                }
                logger.info(f"DEBUG: Template row {i}: {row}")
                template_data.append(row)
            
            # Create Excel file with exact format expected by GMT API
            # GMT API expects headers in row 1 (not as column names), with empty row 0
            df = pd.DataFrame(template_data)
            
            # Ensure columns are in the exact order required by GMT API
            required_columns = ['S.No', 'ItemDescription', 'Specification', 'Uom', 'Quantity', 'Remarks']
            df = df[required_columns]
            
            logger.info(f"DEBUG: Template DataFrame shape: {df.shape}")
            logger.info(f"DEBUG: Template DataFrame columns: {df.columns.tolist()}")
            logger.info(f"DEBUG: Template DataFrame first row: {df.iloc[0].to_dict() if not df.empty else 'Empty'}")
            
            # Check for empty required fields
            for col in required_columns:
                empty_count = df[col].astype(str).str.strip().eq('').sum()
                if empty_count > 0:
                    logger.warning(f"DEBUG: Column '{col}' has {empty_count} empty values")
            
            # Create the GMT API expected format:
            # Row 0: Empty (NaN values)
            # Row 1: Headers as data
            # Row 2+: Data rows
            
            # Create empty row
            empty_row = pd.DataFrame([[None] * len(required_columns)], columns=required_columns)
            
            # Create header row as data - match the exact format from original file
            # The original file has a space before 'Specification'
            header_row_data = ['S.No', 'ItemDescription', ' Specification', 'Uom', 'Quantity', 'Remarks']
            header_row = pd.DataFrame([header_row_data], columns=required_columns)
            
            # Combine: empty row + header row + data rows
            final_df = pd.concat([empty_row, header_row, df], ignore_index=True)
            
            logger.info(f"DEBUG: Final DataFrame shape: {final_df.shape}")
            logger.info(f"DEBUG: Final DataFrame structure:")
            logger.info(f"  Row 0: {final_df.iloc[0].tolist()}")
            logger.info(f"  Row 1: {final_df.iloc[1].tolist()}")
            if len(final_df) > 2:
                logger.info(f"  Row 2: {final_df.iloc[2].tolist()}")
            
            output = io.BytesIO()
            final_df.to_excel(output, index=False, header=False, engine='openpyxl')
            output.seek(0)
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Error creating template: {e}")
            raise
    
    def encode_for_api(self, excel_bytes: bytes, filename: str = "rfq_items.xlsx") -> Dict[str, str]:
        """Encode Excel file for GMT API submission."""
        return {
            'boqFileName': filename,
            'boqfile': base64.b64encode(excel_bytes).decode('utf-8')
        }