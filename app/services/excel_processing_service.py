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
        """Process Excel file directly to multiple RFQ format using streamlined OpenAI processing."""
        logger.info(f"[EXCEL-PROCESS] Starting streamlined processing for {filename}, size: {len(content)} bytes")
        try:
            # Validate file size (3MB limit)
            max_size = 3 * 1024 * 1024  # 3MB in bytes
            if len(content) > max_size:
                return {
                    'success': False,
                    'error': f'File size ({len(content)} bytes) exceeds maximum allowed size of {max_size} bytes (3MB)'
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
                    return {
                        'success': False,
                        'error': f'Excel file contains {len(sheet_names)} worksheets. Only 1 worksheet is allowed. Please use a single sheet and reupload.'
                    }
                
                # Use the first sheet
                df = pd.read_excel(file_obj, sheet_name=0, header=None)
                excel_file.close()
                
            except Exception as e:
                return {
                    'success': False,
                    'error': f'Failed to read Excel file: {str(e)}'
                }
            
            # Remove completely empty rows (only rows where ALL columns are NaN)
            initial_count = len(df)
            df = df.dropna(how='all')
            removed_count = initial_count - len(df)
            logger.info(f"[DEBUG-MAIN] Removed {removed_count} completely empty rows from {initial_count} total rows")
            
            if df.empty:
                return {
                    'success': False,
                    'error': '❌ File rejected: The uploaded Excel file contains no data. Please provide a valid Excel file containing the required data for the RFQ.'
                }
            
            logger.info(f"DEBUG: Excel file shape: {df.shape}")
            
            # Use new streamlined OpenAI processing
            processing_result = await self._process_excel_with_openai(df, filename)
            
            if not processing_result.get('success'):
                return {
                    'success': False,
                    'error': processing_result.get('error', 'Failed to process Excel data')
                }
            
            # Extract results from OpenAI processing
            # OpenAI returns 'rfqs' array, extract products from first RFQ
            rfqs = processing_result.get('rfqs', [])
            products = []
            if rfqs and len(rfqs) > 0:
                products = rfqs[0].get('products', [])
            processing_summary = processing_result.get('processing_summary', {})
            
            # Validate date and location consistency
            date_location_validation = self._validate_date_location_consistency(products)
            if not date_location_validation['valid']:
                return {
                    'success': False,
                    'error': date_location_validation['error']
                }

            # Use RFQs directly from OpenAI processing result
            rfqs = processing_result.get('rfqs', [])
            if not rfqs and products:
                # Fallback: create RFQ format if OpenAI didn't return rfqs structure
                rfqs = [{
                    'products': products,
                    'deliveryDate': processing_result.get('deliveryDate', ''),
                    'state': processing_result.get('state', ''),
                    'city': processing_result.get('city', ''),
                    'pincode': processing_result.get('pincode', '')
                }]
            
            # Calculate processing statistics
            total_rows = processing_summary.get('total_rows_processed', 0)
            identified_rows = processing_summary.get('total_products', 0)  # Use total_products instead of identified_for_rfq
            extracted_rows = processing_summary.get('total_products_extracted', 0)
            skipped_rows = processing_summary.get('skipped_rows', 0)
            
            # Create detailed statistics for confirmation message
            processing_stats = {
                'total_rows': total_rows,
                'identified_for_rfq': identified_rows,
                'extracted': extracted_rows,
                'skipped': skipped_rows,
                'has_missing_items': skipped_rows > 0,
                'skipped_items_summary': processing_summary.get('skipped_items_summary', '')
            }
            
            # Reject file if ANY rows are skipped
            if identified_rows > 0 and skipped_rows > 0:
                skipped_summary = processing_summary.get('skipped_items_summary', 'One product row was skipped due to missing quantity.')
                combined_error = f"❌ File rejected: File processing incomplete: {skipped_rows} rows skipped out of {identified_rows} total product rows. Only {extracted_rows} products extracted successfully.\n\n{skipped_summary}\n\nPlease fix your Excel file and upload again."
                return {
                    'success': False,
                    'error': combined_error,
                    'combined_error': combined_error,
                    'processing_summary': processing_summary,
                    'processing_stats': processing_stats,
                    'should_skip_rfq_creation': True
                }
                       
            # Convert to legacy format for compatibility
            items = []
            for i, product in enumerate(products):
                logger.info(f"[DEBUG-CONVERSION] Converting product {i+1}: {product}")
                item = {
                    'S.No': len(items) + 1,
                    'ItemDescription': product.get('description', ''),
                    'Specification': product.get('brand', ''),
                    'Uom': product.get('unitofMeasures', 'pcs'),
                    'Quantity': product.get('quantity'),
                    'Remarks': product.get('remarks', '')
                }
                items.append(item)
                      
            # Include skipped items summary in success response for user feedback
            skipped_summary = processing_summary.get('skipped_items_summary', '')
            success_summary = f"Processed {processing_summary.get('total_products_extracted', len(items))} products from {filename}"
            if skipped_rows > 0:
                success_summary += f" ({skipped_rows} rows skipped: {skipped_summary})"
            
            final_result = {
                'success': True,
                'filename': filename,
                'items': items,
                'total_items': len(items),
                'rfqs': rfqs,  # New structured format
                'processing_summary': processing_summary,
                'processing_stats': processing_stats,  # Add detailed statistics
                'confidence': processing_result.get('confidence', 85),
                'summary': success_summary,
                'skipped_items_summary': skipped_summary,
                'should_skip_rfq_creation': False
            }
            
            
            return final_result
            
        except Exception as e:
            logger.error(f"[EXCEL-PROCESS] Error processing Excel file {filename}: {e}")
            import traceback
            logger.error(f"[EXCEL-PROCESS] Stack trace: {traceback.format_exc()}")
            return {
                'success': False,
                'error': f'Failed to process Excel: {str(e)}'
            }
    
    async def _process_excel_with_openai(self, df: pd.DataFrame, filename: str) -> Dict[str, Any]:
        """Process Excel DataFrame directly using OpenAI for streamlined RFQ creation."""
        logger.info(f"[EXCEL-PROCESS] Starting OpenAI streamlined processing for {filename}")
        try:
            # Preprocess DataFrame to handle duplicates
            df_clean = self._preprocess_excel_data(df)
            logger.info(f"[DEBUG-FLOW] After preprocessing shape: {df_clean.shape}")
            
            # Log first few rows to see actual data
            for i, row in df_clean.head(5).iterrows():
                logger.info(f"[DEBUG-FLOW] Row {i}: {row.to_dict()}")
            
            # Convert DataFrame to dict format for OpenAI processing
            excel_data = df_clean.fillna('').astype(str).to_dict(orient='records')
            logger.info(f"[DEBUG-FLOW] Converted to {len(excel_data)} records for OpenAI")
            
            # Log the data being sent to OpenAI
            for i, record in enumerate(excel_data[:3]):
                logger.info(f"[DEBUG-FLOW] OpenAI Record {i+1}: {record}")
            
            # Create readable string for OpenAI input
            excel_text = "\n".join([f"Row {i+1}: {row}" for i, row in enumerate(excel_data[:50])])  
            
            # Use OpenAI to process Excel data directly
            result = await self.openai_service.process_excel_to_rfqs(excel_text, filename)
            
            if result.get('success'):
                if result.get('rfqs'):
                    logger.info(f"[DEBUG-FLOW] First RFQ has {len(result['rfqs'][0].get('products', []))} products")
                return result
            else:
                logger.error(f"[EXCEL-PROCESS] OpenAI processing failed: {result.get('error')}")
                return {
                    'success': False,
                    'error': result.get('error', 'OpenAI processing failed')
                }
                
        except Exception as e:
            logger.error(f"[EXCEL-PROCESS] Error in OpenAI processing: {e}")
            return {
                'success': False,
                'error': f'OpenAI processing error: {str(e)}'
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
        removed_rows = 0
        
        # Define mandatory fields
        mandatory_fields = ['Specification', 'Quantity']
        
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
                                value_str = str(value).strip()
                                item[target_col] = value_str
                                has_data = True
                            else:
                                logger.debug(f"[EXCEL-EXTRACT] Empty/NaN value for '{header}' -> '{target_col}': '{value}'")
                    else:
                        logger.debug(f"[EXCEL-EXTRACT] Header '{header}' not in column mapping")
                
                # Check if row has mandatory fields before adding
                if has_data:
                    # Check for mandatory fields
                    missing_mandatory = []
                    for field in mandatory_fields:
                        if field not in item or not str(item[field]).strip():
                            missing_mandatory.append(field)
                    
                    # Skip row if missing mandatory fields
                    if missing_mandatory:
                        removed_rows += 1
                        logger.info(f"[EXCEL-EXTRACT] Removed row {index + 1} - missing mandatory fields: {missing_mandatory}")
                        continue
                    
                    # Add serial number if missing
                    if 'S.No' not in item:
                        item['S.No'] = str(len(items) + 1)
                    
                    # Set default UOM if missing but quantity exists
                    if 'Uom' not in item and 'Quantity' in item:
                        item['Uom'] = 'pcs'
                    
                    items.append(item)
                else:
                    logger.debug(f"[EXCEL-EXTRACT] Skipped row {index} - no data found")
            
            return {
                'success': True,
                'items': items,
                'removed_rows': removed_rows
            }
            
        except Exception as e:
            logger.error(f"[EXCEL-EXTRACT] Error extracting items: {e}")
            import traceback
            logger.error(f"[EXCEL-EXTRACT] Stack trace: {traceback.format_exc()}")
            return {
                'success': False,
                'items': [],
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
    
    def _preprocess_excel_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """Preprocess Excel data to handle duplicate columns and clean data."""
        try:
            
            # Log first few rows before processing
            for i, row in df.head(10).iterrows():
                logger.info(f"[DEBUG-PREPROCESS] Original Row {i}: {row.tolist()}")
            
            # Handle duplicate column names by keeping only the first occurrence
            # Pandas automatically renames duplicates with .1, .2, etc.
            seen_base_columns = set()
            columns_to_keep = []
            
            for col in df.columns:
                col_str = str(col).strip()
                
                # Skip completely unnamed columns
                if 'unnamed:' in col_str.lower():
                    continue
                
                # Extract base column name (remove .1, .2, etc. suffixes)
                base_col = col_str
                if '.' in col_str and col_str.split('.')[-1].isdigit():
                    base_col = '.'.join(col_str.split('.')[:-1])
                
                # Normalize for comparison
                normalized_base = base_col.strip().lower()
                
                # Keep first occurrence of each unique column type
                if normalized_base not in seen_base_columns:
                    seen_base_columns.add(normalized_base)
                    columns_to_keep.append(col)
                    logger.info(f"[EXCEL-PREPROCESS] Keeping column: {col} (base: {base_col})")
                else:
                    logger.info(f"[EXCEL-PREPROCESS] Skipping duplicate column: {col} (base: {base_col})")
            
            # Select only unique columns
            df_clean = df[columns_to_keep].copy()
            logger.info(f"[DEBUG-PREPROCESS] After column dedup shape: {df_clean.shape}")
            
            # Log ALL rows before any removal to debug the blank row issue
            logger.info(f"[DEBUG-PREPROCESS] ALL rows before empty removal:")
            for i, row in df_clean.iterrows():
                is_empty = row.isna().all()
                has_data = not row.isna().all() and any(str(val).strip() for val in row if pd.notna(val))
                logger.info(f"[DEBUG-PREPROCESS] Row {i} (empty: {is_empty}, has_data: {has_data}): {row.tolist()}")
            
            # CRITICAL FIX: Only remove rows that are completely empty (all NaN)
            # Do NOT remove rows with blank cells that might have data in other columns
            initial_row_count = len(df_clean)
            df_clean = df_clean.dropna(how='all')  # Only remove rows where ALL columns are NaN
            removed_empty_rows = initial_row_count - len(df_clean)
            
            logger.info(f"[DEBUG-PREPROCESS] Removed {removed_empty_rows} completely empty rows")
            logger.info(f"[DEBUG-PREPROCESS] After empty row removal shape: {df_clean.shape}")
            
            # Log rows after empty removal to verify blank rows with data are preserved
            logger.info(f"[DEBUG-PREPROCESS] Rows after empty removal:")
            for i, row in df_clean.iterrows():
                logger.info(f"[DEBUG-PREPROCESS] Preserved Row {i}: {row.tolist()}")
            
            # CRITICAL FIX: Be more careful with duplicate removal
            # Only remove exact duplicates, not rows that might have slight differences
            initial_rows = len(df_clean)
            
            # Create a more conservative duplicate check
            # Only consider rows duplicates if they have identical non-null values
            df_for_dup_check = df_clean.copy()
            
            # Fill NaN with a unique placeholder for duplicate detection
            df_for_dup_check = df_for_dup_check.fillna('__BLANK__')
            
            # Remove duplicates based on filled data
            df_clean = df_clean[~df_for_dup_check.duplicated(keep='first')]
            removed_duplicates = initial_rows - len(df_clean)
            
            if removed_duplicates > 0:
                logger.info(f"[EXCEL-PREPROCESS] Removed {removed_duplicates} duplicate rows")
            
            logger.info(f"[EXCEL-PREPROCESS] Final cleaned DataFrame shape: {df_clean.shape}")
            logger.info(f"[EXCEL-PREPROCESS] Final cleaned columns: {df_clean.columns.tolist()}")
            
            # Log final rows to verify all product rows are preserved
            logger.info(f"[DEBUG-PREPROCESS] Final rows after all processing:")
            for i, row in df_clean.iterrows():
                logger.info(f"[DEBUG-PREPROCESS] Final Row {i}: {row.to_dict()}")
            
            return df_clean
            
        except Exception as e:
            logger.error(f"[EXCEL-PREPROCESS] Error preprocessing data: {e}")
            return df  # Return original if preprocessing fails
    
    def create_standard_template(self, items: List[Dict[str, Any]]) -> bytes:
        """Create standardized Excel template for GMT API."""
        try:
            logger.info(f"DEBUG: Creating template for {len(items)} items")
            template_data = []
            
            for i, item in enumerate(items, 1):
                
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
                        'error': f"❌ File rejected: Your Excel file contains {filled_rows} rows, but only 50 rows are allowed per upload. Could you please reduce the file to 50 rows and reupload it for processing?"
                    }
                
                # Check 2: Merged cells validation
                merged_ranges = list(worksheet.merged_cells.ranges)
                logger.info(f"[EXCEL-STRUCTURE] Found {len(merged_ranges)} merged cell ranges")
                if merged_ranges:
                    workbook.close()
                    logger.error(f"[EXCEL-STRUCTURE] Merged cells found: {merged_ranges}")
                    return {
                        'valid': False,
                        'error': "Your Excel file contains merged cells. Please unmerge all cells and reupload the file to proceed with your RFQ submission."
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
                            'error': f"❌ File rejected: Your Excel file contains {filled_rows} rows, but only 50 rows are allowed per upload. Could you please reduce the file to 50 rows and reupload it for processing?"
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
    
    def _validate_date_location_consistency(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate that all products have the same date and location."""
        if not products:
            return {'valid': True}
        
        # Extract dates and locations from products
        dates = set()
        locations = set()
        
        for product in products:
            date = product.get('date', '').strip() if product.get('date') else ''
            location = product.get('location', '').strip() if product.get('location') else ''
            
            if date:
                dates.add(date)
            if location:
                locations.add(location)
        
        # Check if there are multiple dates or locations
        has_multiple_dates = len(dates) > 1
        has_multiple_locations = len(locations) > 1
        
        if has_multiple_dates or has_multiple_locations:
            error_parts = []
            if has_multiple_dates:
                error_parts.append(f"different dates ({', '.join(sorted(dates))})")
            if has_multiple_locations:
                error_parts.append(f"different locations ({', '.join(sorted(locations))})")
            
            error_message = f"❌ File rejected due to {' and '.join(error_parts)}. Please correct the file to have consistent date and location for all items, then reupload."
            
            return {
                'valid': False,
                'error': error_message
            }
        
        return {'valid': True}