"""
Simple Excel processing service that uses OpenAI for intelligent analysis.
"""

import logging
import io
import pandas as pd
import base64
import re
from typing import Dict, Any, List, Optional
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
        logger.info(f"[EXCEL-PROCESS] Starting processing for {filename}, size: {len(content)} bytes")
        try:
            # Validate file size (10MB limit)
            max_size = 10 * 1024 * 1024  # 10MB in bytes
            if len(content) > max_size:
                return {
                    'success': False,
                    'error': f'File size ({len(content)} bytes) exceeds maximum allowed size of {max_size} bytes (10MB)'
                }
            
            # Validate file format
            if not self._is_valid_excel_file(content, filename):
                return {
                    'success': False,
                    'error': 'Invalid Excel file format. Please upload a valid .xlsx or .xls file'
                }
            
            # Validate Excel structure (row count and merged cells) before processing
            structure_validation = await self._validate_excel_structure(content)
            if not structure_validation['valid']:
                return {
                    'success': False,
                    'error': structure_validation['error']
                }
            
            # Read Excel file
            file_obj = io.BytesIO(content)
            
            # Check if file has multiple sheets and validate
            try:
                excel_file = pd.ExcelFile(file_obj)
                sheet_names = excel_file.sheet_names
                
                if len(sheet_names) > 1:
                    logger.info(f"DEBUG: Multiple sheets found: {sheet_names}. Using first sheet: {sheet_names[0]}")
                
                # Use the first sheet
                df = pd.read_excel(file_obj, sheet_name=0, header=None)
                excel_file.close()
                
            except Exception as e:
                return {
                    'success': False,
                    'error': f'Failed to read Excel file: {str(e)}'
                }
            
            # Remove completely empty rows
            df = df.dropna(how='all')
            
            if df.empty:
                return {
                    'success': False,
                    'error': 'No data found in Excel file'
                }
            
            logger.info(f"DEBUG: Excel file shape: {df.shape}")
            logger.info(f"DEBUG: First 3 rows: {df.head(3).values.tolist()}")
            
            # Get first 10 rows for enhanced header detection
            sample_rows = df.head(10).values.tolist()
            
            # Use OpenAI to detect header row with extended range
            header_result = await self.openai_service.detect_excel_header_row(sample_rows)
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
            mapping_result = await self.openai_service.map_excel_columns(headers)
            column_mapping = mapping_result.get('column_mapping', {})
            
            logger.info(f"[EXCEL-PROCESS] Column mapping result: {mapping_result}")
            logger.info(f"[EXCEL-PROCESS] Column mapping: {column_mapping}")
            
            # Fallback: If OpenAI returns empty mapping, create direct mapping for exact matches
            if not column_mapping:
                logger.warning(f"[EXCEL-PROCESS] OpenAI returned empty column mapping, using fallback direct mapping")
                column_mapping = self._create_fallback_mapping(headers)
                logger.info(f"[EXCEL-PROCESS] Fallback column mapping: {column_mapping}")
            
            # Extract items using the mapping
            logger.info(f"[EXCEL-PROCESS] Extracting items using column mapping")
            extraction_result = self._extract_items_with_mapping(data_df, headers, column_mapping)
            
            # Check if extraction failed due to special characters
            if not extraction_result.get('success', True):
                logger.error(f"[EXCEL-PROCESS] Extraction failed: {extraction_result.get('error')}")
                return {
                    'success': False,
                    'error': extraction_result.get('error'),
                    'special_char_errors': extraction_result.get('special_char_errors', [])
                }
            
            items = extraction_result.get('items', [])
            logger.info(f"[EXCEL-PROCESS] Extracted {len(items)} items")
            if items:
                logger.info(f"[EXCEL-PROCESS] First item: {items[0]}")
            else:
                logger.warning(f"[EXCEL-PROCESS] No items extracted from {filename}")
            
            # Enhanced validation for GMT API requirements and business rules
            logger.info(f"[EXCEL-PROCESS] Validating {len(items)} items for GMT API requirements")
            validation_result = self._validate_items_comprehensive(items)
            logger.info(f"[EXCEL-PROCESS] Validation result: valid={validation_result.get('valid', False)}, errors={len(validation_result.get('errors', []))}, warnings={len(validation_result.get('warnings', []))}")
            
            # Additional business rule validation
            business_validation = self._validate_business_rules(items)
            if not business_validation['valid']:
                validation_result['valid'] = False
                validation_result['errors'].extend(business_validation['errors'])
                validation_result['warnings'].extend(business_validation.get('warnings', []))
            
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
            logger.error(f"[EXCEL-PROCESS] Error processing Excel file {filename}: {e}")
            import traceback
            logger.error(f"[EXCEL-PROCESS] Stack trace: {traceback.format_exc()}")
            return {
                'success': False,
                'error': f'Failed to process Excel: {str(e)}'
            }
    
    def _create_fallback_mapping(self, headers: List[str]) -> Dict[str, str]:
        """Create fallback column mapping for exact matches."""
        mapping = {}
        for header in headers:
            if header in self.target_columns:
                mapping[header] = header
        logger.info(f"[EXCEL-PROCESS] Created fallback mapping: {mapping}")
        return mapping
    
    def _is_valid_excel_file(self, content: bytes, filename: str) -> bool:
        """Validate if the file is a valid Excel file."""
        try:
            # Check file extension
            valid_extensions = ['.xlsx', '.xls']
            if not any(filename.lower().endswith(ext) for ext in valid_extensions):
                logger.warning(f"Invalid file extension for: {filename}")
                return False
            
            # Check file signature/magic bytes
            if len(content) < 8:
                logger.warning(f"File too small to be valid Excel: {len(content)} bytes")
                return False
            
            # Excel file signatures
            xlsx_signature = b'PK\x03\x04'  # ZIP signature (XLSX files are ZIP archives)
            xls_signature = b'\xd0\xcf\x11\xe0'  # OLE2 signature (XLS files)
            
            if content.startswith(xlsx_signature) or content.startswith(xls_signature):
                # Additional validation: try to read with pandas
                try:
                    file_obj = io.BytesIO(content)
                    pd.ExcelFile(file_obj).close()
                    return True
                except Exception as e:
                    logger.warning(f"File failed pandas validation: {e}")
                    return False
            else:
                logger.warning(f"Invalid file signature for: {filename}")
                return False
                
        except Exception as e:
            logger.error(f"Error validating Excel file: {e}")
            return False
    
    def _extract_items_with_mapping(self, df: pd.DataFrame, headers: List[str], column_mapping: Dict[str, str]) -> Dict[str, Any]:
        """Extract items using the column mapping."""
        logger.info(f"[EXCEL-EXTRACT] Starting item extraction with {len(headers)} headers and {len(column_mapping)} mappings")
        logger.info(f"[EXCEL-EXTRACT] Headers: {headers}")
        logger.info(f"[EXCEL-EXTRACT] Mappings: {column_mapping}")
        items = []
        special_char_errors = []
        
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
                            logger.debug(f"[EXCEL-EXTRACT] Extracting header '{header}' -> '{target_col}', value: '{value}'")
                            if pd.notna(value) and str(value).strip():
                                # Check for special characters during extraction
                                value_str = str(value).strip()
                                special_chars = ['@', '#', '$', '%', '^', '&', '*', '~', '`', '|', '\\', '<', '>', '?', '/', ':', ';', '"', "'"]
                                found_chars = [char for char in special_chars if char in value_str]
                                
                                if found_chars:
                                    error_msg = f"Row {index + 2}, Column '{header}': '{value_str}' contains invalid characters: {', '.join(found_chars)}"
                                    logger.error(f"[EXCEL-EXTRACT] SPECIAL CHARACTER DETECTED in {error_msg}")
                                    special_char_errors.append(error_msg)
                                    # Don't add the item with special characters
                                    continue
                                else:
                                    item[target_col] = value_str
                                has_data = True
                            else:
                                logger.debug(f"[EXCEL-EXTRACT] Empty/NaN value for '{header}' -> '{target_col}': '{value}'")
                    else:
                        logger.debug(f"[EXCEL-EXTRACT] Header '{header}' not in column mapping")
                
                # Add serial number if missing
                if has_data:
                    if 'S.No' not in item:
                        item['S.No'] = str(len(items) + 1)
                    
                    # Set default UOM if missing but quantity exists
                    if 'Uom' not in item and 'Quantity' in item:
                        item['Uom'] = 'pcs'
                    
                    items.append(item)
                    logger.info(f"[EXCEL-EXTRACT] Added item {len(items)}: {item}")
                else:
                    logger.debug(f"[EXCEL-EXTRACT] Skipped row {index} - no data found")
            
            # Return error if special characters found
            if special_char_errors:
                logger.error(f"[EXCEL-EXTRACT] Extraction failed due to {len(special_char_errors)} special character errors")
                return {
                    'success': False,
                    'items': [],
                    'special_char_errors': special_char_errors,
                    'error': f"Excel contains invalid special characters in {len(special_char_errors)} location(s). Please remove special characters and reupload."
                }
            
            logger.info(f"[EXCEL-EXTRACT] Successfully extracted {len(items)} items")
            return {
                'success': True,
                'items': items,
                'special_char_errors': []
            }
            
        except Exception as e:
            logger.error(f"[EXCEL-EXTRACT] Error extracting items: {e}")
            import traceback
            logger.error(f"[EXCEL-EXTRACT] Stack trace: {traceback.format_exc()}")
            return {
                'success': False,
                'items': [],
                'special_char_errors': [],
                'error': f"Failed to extract items: {str(e)}"
            }
    
    def _validate_items_comprehensive(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate items meet GMT API requirements."""
        validation_result = {
            'valid': True,
            'warnings': [],
            'errors': [],
            'missing_required_fields': [],
            'empty_required_fields': []
        }
        
        required_fields = ['ItemDescription', 'Specification', 'Uom', 'Quantity']
        
        # Track which fields are missing across all items
        missing_fields_summary = set()
        
        for i, item in enumerate(items, 1):
            # Check for missing required fields
            for field in required_fields:
                if field not in item:
                    validation_result['missing_required_fields'].append(f"Item {i}: Missing '{field}'")
                    missing_fields_summary.add(field)
                    validation_result['valid'] = False
                elif not str(item[field]).strip():
                    validation_result['empty_required_fields'].append(f"Item {i}: Empty '{field}'")
                    validation_result['warnings'].append(f"Item {i}: Empty '{field}'")
        
        # Add field explanations summary if there are missing fields
        if missing_fields_summary:
            field_explanations = {
                'ItemDescription': 'product name (e.g., Laptop, Office Chair)',
                'Specification': 'technical details (e.g., Intel i7 16GB RAM, Ergonomic Adjustable)',
                'Uom': 'unit (pcs, nos, kg, meters)',
                'Quantity': 'number needed'
            }
            
            explanations = []
            for field in missing_fields_summary:
                explanation = field_explanations.get(field, field)
                explanations.append(f"• {field}: {explanation}")
            
            validation_result['field_explanations'] = explanations
        
        # Enhanced data type validation
        data_type_validation = self._validate_data_types(items)
        if not data_type_validation['valid']:
            validation_result['errors'].extend(data_type_validation['issues'])
            validation_result['valid'] = False
        
        # Regional format detection
        regional_formats = self._detect_regional_formats(items)
        if regional_formats['issues']:
            validation_result['warnings'].extend(regional_formats['issues'])
        
        # Check for common issues
        if validation_result['missing_required_fields']:
            validation_result['errors'].extend(validation_result['missing_required_fields'])
        
        if validation_result['empty_required_fields']:
            validation_result['warnings'].extend(validation_result['empty_required_fields'])
        
        logger.info(f"DEBUG: Validation result: {validation_result}")
        return validation_result
    
    def _validate_business_rules(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate business rules and edge cases."""
        validation_result = {
            'valid': True,
            'errors': [],
            'warnings': []
        }
        
        # Check for duplicate item descriptions
        descriptions = [item.get('ItemDescription', '').strip().lower() for item in items if item.get('ItemDescription')]
        duplicates = [desc for desc in set(descriptions) if descriptions.count(desc) > 1]
        if duplicates:
            validation_result['warnings'].append(f"Duplicate items found: {', '.join(duplicates[:3])}")
        
        # Validate quantities
        for i, item in enumerate(items, 1):
            qty = item.get('Quantity')
            if qty is not None:
                try:
                    qty_float = float(str(qty).replace(',', ''))
                    if qty_float <= 0:
                        validation_result['errors'].append(f"Item {i}: Quantity must be positive (found: {qty})")
                        validation_result['valid'] = False
                    elif qty_float > 10000:
                        validation_result['warnings'].append(f"Item {i}: Very large quantity ({qty_float})")
                except (ValueError, TypeError):
                    # Handle text quantities like "Five", "5 pieces"
                    qty_str = str(qty).lower().strip()
                    if any(word in qty_str for word in ['five', 'ten', 'twenty', 'hundred']):
                        validation_result['errors'].append(f"Item {i}: Please use numeric quantities instead of text ({qty})")
                        validation_result['valid'] = False
            
            # Validate UOM values
            uom = item.get('Uom', '').lower().strip()
            if uom in ['each', 'per item', 'item']:
                validation_result['warnings'].append(f"Item {i}: Consider using standard UOM like 'pcs' instead of '{uom}'")
            
            # Check for special characters in product names
            desc = item.get('ItemDescription', '')
            if desc and any(char in desc for char in ['@', '#', '$', '%', '^', '&', '*']):
                validation_result['warnings'].append(f"Item {i}: Product name contains special characters")
        
        return validation_result
    
    def _normalize_quantity(self, qty_value: Any) -> Optional[float]:
        """Normalize quantity values handling various formats."""
        if qty_value is None:
            return None
        
        try:
            # Handle string quantities with commas
            if isinstance(qty_value, str):
                qty_str = qty_value.strip().replace(',', '')
                # Extract numeric part from strings like "5 pieces", "10 kg"
                import re
                numeric_match = re.search(r'\d+(?:\.\d+)?', qty_str)
                if numeric_match:
                    return float(numeric_match.group())
            
            return float(qty_value)
        except (ValueError, TypeError):
            return None
    
    def _detect_regional_formats(self, items: List[Dict]) -> Dict[str, Any]:
        """Detect and handle regional number formats."""
        format_info = {
            'decimal_separator': '.',
            'thousands_separator': ',',
            'issues': []
        }
        
        # Check for European format (comma as decimal separator)
        for item in items[:5]:  # Check first 5 items
            qty = str(item.get('Quantity', ''))
            if ',' in qty and '.' not in qty:
                # Likely European format
                format_info['decimal_separator'] = ','
                format_info['thousands_separator'] = '.'
                format_info['issues'].append("European number format detected (comma as decimal)")
                break
        
        return format_info
    
    def _validate_data_types(self, items: List[Dict]) -> Dict:
        """Enhanced data type validation."""
        issues = []
        for i, item in enumerate(items, 1):
            # Quantity validation
            if 'Quantity' in item:
                qty = item['Quantity']
                if qty is not None:
                    normalized_qty = self._normalize_quantity(qty)
                    if normalized_qty is None:
                        issues.append(f"Item {i}: Invalid quantity format '{qty}'")
                    elif normalized_qty <= 0:
                        issues.append(f"Item {i}: Quantity must be positive")
            
            # ItemDescription validation
            desc = item.get('ItemDescription', '').strip()
            if not desc:
                issues.append(f"Item {i}: Missing item description")
            elif len(desc) > 200:
                issues.append(f"Item {i}: Item description too long (max 200 characters)")
            
            # UOM validation
            uom = item.get('Uom', '').strip()
            if uom and len(uom) > 20:
                issues.append(f"Item {i}: UOM too long (max 20 characters)")
        
        return {"valid": len(issues) == 0, "issues": issues}
    
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
    
    async def _validate_excel_structure(self, content: bytes) -> Dict[str, Any]:
        """Validate Excel structure: row count and merged cells."""
        try:
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException
            
            file_obj = io.BytesIO(content)
            MAX_ROWS = 50  # Maximum allowed rows
            
            # Use openpyxl for structure validation
            try:
                workbook = load_workbook(file_obj, read_only=False)
                worksheet = workbook.active
                
                # Check 1: Count only filled rows (rows with actual data)
                filled_rows = 0
                for row in worksheet.iter_rows():
                    # Check if row has any non-empty cells
                    if any(cell.value is not None and str(cell.value).strip() != '' for cell in row):
                        filled_rows += 1
                
                logger.info(f"[EXCEL-STRUCTURE] Excel has {filled_rows} filled rows (out of {worksheet.max_row} total), max allowed: {MAX_ROWS}")
                if filled_rows > MAX_ROWS:
                    workbook.close()
                    logger.error(f"[EXCEL-STRUCTURE] Too many filled rows: {filled_rows} > {MAX_ROWS}")
                    return {
                        'valid': False,
                        'error': f"Your Excel file contains {filled_rows} rows with data, but only 50 rows are allowed per upload. Please reduce to 50 rows and reupload."
                    }
                
                # Check 2: Merged cells validation
                merged_ranges = list(worksheet.merged_cells.ranges)
                logger.info(f"[EXCEL-STRUCTURE] Found {len(merged_ranges)} merged cell ranges")
                if merged_ranges:
                    workbook.close()
                    logger.error(f"[EXCEL-STRUCTURE] Merged cells found: {merged_ranges}")
                    return {
                        'valid': False,
                        'error': "Your Excel file contains merged cells. Please unmerge all cells and reupload."
                    }
                
                workbook.close()
                return {'valid': True}
                
            except Exception as openpyxl_error:
                # Fallback: Try with pandas for basic row count check
                file_obj.seek(0)
                try:
                    df = pd.read_excel(file_obj, sheet_name=0)
                    # Remove completely empty rows
                    df_cleaned = df.dropna(how='all')
                    filled_rows = len(df_cleaned)
                    logger.info(f"[EXCEL-STRUCTURE-FALLBACK] Pandas found {filled_rows} filled rows, max allowed: {MAX_ROWS}")
                    if filled_rows > MAX_ROWS:
                        logger.error(f"[EXCEL-STRUCTURE-FALLBACK] Too many filled rows: {filled_rows} > {MAX_ROWS}")
                        return {
                            'valid': False,
                            'error': f"Your Excel file contains {filled_rows} rows with data, but only 50 rows are allowed per upload. Please reduce to 50 rows and reupload."
                        }
                    # Can't check merged cells with pandas, so assume valid
                    return {'valid': True}
                except Exception:
                    # If both methods fail, return the original error
                    raise openpyxl_error
                
        except Exception as e:
            logger.error(f"Error validating Excel structure: {e}")
            return {
                'valid': False,
                'error': "Failed to validate Excel file structure."
            }
    
    def encode_for_api(self, excel_bytes: bytes, filename: str = "rfq_items.xlsx") -> Dict[str, str]:
        """Encode Excel file for GMT API submission."""
        return {
            'boqFileName': filename,
            'boqfile': base64.b64encode(excel_bytes).decode('utf-8')
        }