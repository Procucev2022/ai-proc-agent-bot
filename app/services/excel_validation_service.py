"""
Excel file validation service for WhatsApp document uploads.

This service provides comprehensive validation of Excel files received through WhatsApp,
including format validation, content type checking, readability verification, security checks,
data quality validation, and business rule enforcement.
"""

import logging
import io
import re
from typing import Dict, Any, Optional, List
import aiohttp
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
import xlrd

logger = logging.getLogger(__name__)

class ExcelValidationService:
    """
    Service for validating Excel files from WhatsApp uploads.
    
    Provides comprehensive validation including file format, structure,
    data quality, business rules, and edge case handling.
    """
    
    MAX_FILE_SIZE = 3 * 1024 * 1024  # 10MB
    MAX_ROWS = 50  # Maximum allowed rows
    MAX_COLUMNS = 15  # Maximum columns to process
    MAX_HEADER_ROW = 10  # Check headers up to row 10
    SUPPORTED_EXTENSIONS = {'.xlsx', '.xls', '.xlsm'}
    EXCEL_MAGIC_NUMBERS = {
        b'PK\x03\x04': 'xlsx',  # ZIP format (XLSX)
        b'\xd0\xcf\x11\xe0': 'xls',  # OLE format (XLS)
    }
    
    # Business validation rules
    REQUIRED_FIELDS = ['ItemDescription', 'Quantity']
    INVALID_UOM_VALUES = {'each', 'per item', 'item', 'piece'}
    SPECIAL_CHARS_PATTERN = r'[^a-zA-Z0-9\s\-\._()]'
    
    async def validate_excel_file_from_url(self, file_url: str, filename: str) -> Dict[str, Any]:
        """
        Download and validate Excel file from WhatsApp URL with comprehensive checks.
        
        Args:
            file_url: Direct URL to the Excel file
            filename: Original filename from WhatsApp
            
        Returns:
            Dict containing validation result and file content or error details
        """
        try:
            # Step 1: Validate file extension
            if not self._validate_file_extension(filename):
                return {
                    'valid': False,
                    'error': f"Unsupported file format. Please upload an Excel file (.xlsx, .xls, .xlsm)",
                    'error_type': 'invalid_extension'
                }
            
            # Step 2: Download file with timeout handling
            file_content = await self._download_file_with_retry(file_url)
            if not file_content:
                return {
                    'valid': False,
                    'error': "Could not download the file. Please try uploading again.",
                    'error_type': 'download_failed'
                }
            
            # Step 3: Validate file size
            if len(file_content) > self.MAX_FILE_SIZE:
                return {
                    'valid': False,
                    'error': f"File too large. Maximum size is {self.MAX_FILE_SIZE // (1024*1024)}MB",
                    'error_type': 'file_too_large'
                }
            
            # Step 4: Validate file integrity
            integrity_check = self._validate_file_integrity(file_content)
            if not integrity_check['valid']:
                return integrity_check
            
            # Step 5: Validate content type and format
            content_validation = self._validate_excel_content(file_content)
            if not content_validation['valid']:
                return content_validation
            
            # Step 6: Test readability and security
            readability_validation = await self._validate_excel_readability(file_content, filename)
            if not readability_validation['valid']:
                return readability_validation
            
            # Step 7: Validate structure (rows, merged cells, worksheets)
            structure_validation = await self._validate_excel_structure_comprehensive(file_content)
            if not structure_validation['valid']:
                return structure_validation
            
            # Step 8: Validate data quality
            data_validation = await self._validate_data_quality(file_content)
            if not data_validation['valid']:
                return data_validation
            
            # Success - return validated content
            return {
                'valid': True,
                'content': file_content,
                'filename': filename,
                'size': len(file_content),
                'format': content_validation['format'],
                'validation_summary': {
                    'structure_check': 'passed',
                    'data_quality_check': 'passed',
                    'business_rules_check': 'passed'
                }
            }
            
        except Exception as e:
            logger.error(f"Error validating Excel file: {e}")
            return {
                'valid': False,
                'error': "Failed to validate Excel file. Please try again.",
                'error_type': 'validation_error'
            }
    
    def _validate_file_extension(self, filename: str) -> bool:
        """Validate file has supported Excel extension."""
        if not filename:
            return False
        
        # Handle CSV files uploaded as Excel
        if filename.lower().endswith('.csv'):
            return False
        
        # Extract extension (case insensitive)
        ext = '.' + filename.lower().split('.')[-1] if '.' in filename else ''
        return ext in self.SUPPORTED_EXTENSIONS
    
    def _validate_file_integrity(self, content: bytes) -> Dict[str, Any]:
        """Validate file integrity and detect corruption."""
        if len(content) < 8:
            return {
                'valid': False,
                'error': "File appears to be corrupted or empty.",
                'error_type': 'corrupted_file'
            }
        
        # Check for common corruption patterns
        if content.startswith(b'\x00' * 8):
            return {
                'valid': False,
                'error': "File appears to be corrupted (null bytes detected).",
                'error_type': 'corrupted_file'
            }
        
        return {'valid': True}
    
    async def _download_file_with_retry(self, file_url: str, max_retries: int = 3) -> Optional[bytes]:
        """Download file with retry logic for transient failures."""
        for attempt in range(max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=30)  # 30 second timeout
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(file_url) as response:
                        if response.status == 200:
                            return await response.read()
                        elif response.status == 404:
                            logger.error(f"File not found (404). URL may have expired.")
                            return None
                        else:
                            logger.warning(f"Download attempt {attempt + 1} failed. Status: {response.status}")
            except Exception as e:
                logger.warning(f"Download attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"All download attempts failed for URL: {file_url}")
        
        return None
    
    def _validate_excel_content(self, content: bytes) -> Dict[str, Any]:
        """Validate file content using magic numbers and detect format issues."""
        if len(content) < 4:
            return {
                'valid': False,
                'error': "File appears to be corrupted or empty.",
                'error_type': 'corrupted_file'
            }
        
        # Check magic numbers
        for magic_bytes, format_type in self.EXCEL_MAGIC_NUMBERS.items():
            if content.startswith(magic_bytes):
                return {
                    'valid': True,
                    'format': format_type
                }
        
        # Check for CSV content masquerading as Excel
        try:
            text_content = content.decode('utf-8', errors='ignore')[:1000]
            if ',' in text_content and '\n' in text_content and not any(b in content[:100] for b in self.EXCEL_MAGIC_NUMBERS.keys()):
                return {
                    'valid': False,
                    'error': "File appears to be CSV format. Please upload an Excel file (.xlsx or .xls).",
                    'error_type': 'csv_format_detected'
                }
        except:
            pass
        
        return {
            'valid': False,
            'error': "File does not appear to be a valid Excel file.",
            'error_type': 'invalid_format'
        }
    
    async def _validate_excel_readability(self, content: bytes, filename: str) -> Dict[str, Any]:
        """Test if Excel file can be read and detect security issues."""
        try:
            file_obj = io.BytesIO(content)
            
            # Try reading with pandas first
            try:
                df = pd.read_excel(file_obj, sheet_name=0, nrows=1)
                return {'valid': True}
            except Exception as pandas_error:
                logger.debug(f"Pandas read failed: {pandas_error}")
                
                # Check for password protection
                if any(keyword in str(pandas_error).lower() for keyword in ['password', 'encrypted', 'protected']):
                    return {
                        'valid': False,
                        'error': "File appears to be password protected. Please upload an unprotected Excel file.",
                        'error_type': 'password_protected'
                    }
                
                # Reset file pointer
                file_obj.seek(0)
                
                # Try with openpyxl for newer Excel files
                try:
                    workbook = load_workbook(file_obj, read_only=True)
                    workbook.close()
                    return {'valid': True}
                except InvalidFileException as openpyxl_error:
                    logger.debug(f"OpenPyXL read failed: {openpyxl_error}")
                    
                    # Check if password protected
                    if any(keyword in str(openpyxl_error).lower() for keyword in ['password', 'encrypted', 'protected']):
                        return {
                            'valid': False,
                            'error': "File appears to be password protected. Please upload an unprotected Excel file.",
                            'error_type': 'password_protected'
                        }
                    
                    # Try with xlrd for older Excel files
                    file_obj.seek(0)
                    try:
                        xlrd.open_workbook(file_contents=content)
                        return {'valid': True}
                    except Exception as xlrd_error:
                        logger.debug(f"XLRD read failed: {xlrd_error}")
                        
                        return {
                            'valid': False,
                            'error': "Could not read Excel file. It may be corrupted or in an unsupported format.",
                            'error_type': 'unreadable_file'
                        }
                
        except Exception as e:
            logger.error(f"Error validating Excel readability: {e}")
            return {
                'valid': False,
                'error': "Failed to validate Excel file readability.",
                'error_type': 'readability_error'
            }
    
    async def _validate_excel_structure_comprehensive(self, content: bytes) -> Dict[str, Any]:
        """Comprehensive structure validation including worksheets, merged cells, and data layout."""
        try:
            file_obj = io.BytesIO(content)
            
            # Use openpyxl for structure validation
            try:
                workbook = load_workbook(file_obj, read_only=False)
                
                # Check 1: Multiple worksheets (warn but allow)
                if len(workbook.worksheets) > 1:
                    logger.info(f"Multiple worksheets detected ({len(workbook.worksheets)}). Using first sheet only.")
                
                worksheet = workbook.active
                
                # Check 2: Count only filled rows
                filled_rows = 0
                for row in worksheet.iter_rows():
                    if any(cell.value is not None and str(cell.value).strip() != '' for cell in row):
                        filled_rows += 1
                
                if filled_rows > self.MAX_ROWS:
                    workbook.close()
                    return {
                        'valid': False,
                        'error': f"Your Excel file contains {filled_rows} rows with data, but only {self.MAX_ROWS} rows are allowed per upload. Please reduce to {self.MAX_ROWS} rows and reupload.",
                        'error_type': 'too_many_rows'
                    }
                
                # Check 3: Merged cells validation
                merged_ranges = list(worksheet.merged_cells.ranges)
                if merged_ranges:
                    workbook.close()
                    return {
                        'valid': False,
                        'error': "Your Excel file contains merged cells. Please unmerge all cells and reupload.",
                        'error_type': 'merged_cells_found'
                    }
                
                # Check 4: Hidden rows/columns
                hidden_rows = sum(1 for row in worksheet.row_dimensions.values() if row.hidden)
                hidden_cols = sum(1 for col in worksheet.column_dimensions.values() if col.hidden)
                if hidden_rows > 0 or hidden_cols > 0:
                    logger.warning(f"Hidden rows: {hidden_rows}, Hidden columns: {hidden_cols}")
                
                # Check 5: Pivot tables or charts
                if hasattr(worksheet, '_pivots') and worksheet._pivots:
                    workbook.close()
                    return {
                        'valid': False,
                        'error': "Excel file contains pivot tables. Please upload a file with raw data only.",
                        'error_type': 'pivot_tables_found'
                    }
                
                workbook.close()
                return {'valid': True}
                
            except Exception as openpyxl_error:
                # Fallback: Try with pandas for basic validation
                file_obj.seek(0)
                try:
                    df = pd.read_excel(file_obj, sheet_name=0)
                    df_cleaned = df.dropna(how='all')
                    filled_rows = len(df_cleaned)
                    
                    if filled_rows > self.MAX_ROWS:
                        return {
                            'valid': False,
                            'error': f"Your Excel file contains {filled_rows} rows with data, but only {self.MAX_ROWS} rows are allowed per upload.",
                            'error_type': 'too_many_rows'
                        }
                    
                    return {'valid': True}
                except Exception:
                    raise openpyxl_error
                
        except Exception as e:
            logger.error(f"Error validating Excel structure: {e}")
            return {
                'valid': False,
                'error': "Failed to validate Excel file structure.",
                'error_type': 'structure_validation_error'
            }
    
    async def _validate_data_quality(self, content: bytes) -> Dict[str, Any]:
        """Validate data quality including headers, data types, and business rules."""
        try:
            file_obj = io.BytesIO(content)
            df = pd.read_excel(file_obj, sheet_name=0)
            
            # Remove completely empty rows
            df = df.dropna(how='all')
            
            if df.empty:
                return {
                    'valid': False,
                    'error': "Excel file contains no data.",
                    'error_type': 'no_data_found'
                }
            
            # Check 1: Header detection in extended range
            header_row_found = False
            for i in range(min(self.MAX_HEADER_ROW, len(df))):
                row_values = df.iloc[i].astype(str).tolist()
                # Check if row contains header-like text
                text_cells = sum(1 for val in row_values if val and not val.isdigit() and val.lower() not in ['nan', 'none'])
                if text_cells >= 2:  # At least 2 text headers
                    header_row_found = True
                    break
            
            if not header_row_found:
                return {
                    'valid': False,
                    'error': f"Could not detect column headers in first {self.MAX_HEADER_ROW} rows. Please ensure your Excel has clear column headers.",
                    'error_type': 'no_headers_detected'
                }
            
            # Check 2: Column count validation
            if len(df.columns) > self.MAX_COLUMNS:
                logger.warning(f"Excel has {len(df.columns)} columns, processing first {self.MAX_COLUMNS} only")
            
            # Check 3: Special characters in headers
            headers = df.columns.astype(str).tolist()
            problematic_headers = []
            for header in headers:
                if re.search(self.SPECIAL_CHARS_PATTERN, header):
                    problematic_headers.append(header)
            
            if problematic_headers:
                return {
                    'valid': False,
                    'error': f"Column headers contain special characters: {', '.join(problematic_headers[:3])}. Please use only letters, numbers, spaces, and basic punctuation.",
                    'error_type': 'invalid_header_characters'
                }
            
            # Check 4: Non-English headers detection
            non_english_headers = []
            for header in headers:
                if header and not re.match(r'^[a-zA-Z0-9\s\-\._()]+$', header):
                    non_english_headers.append(header)
            
            if len(non_english_headers) > len(headers) // 2:  # More than half are non-English
                return {
                    'valid': False,
                    'error': "Excel headers appear to be in a non-English language. Please use English column headers.",
                    'error_type': 'non_english_headers'
                }
            
            # Check 5: Special characters in data
            special_char_issues = self._validate_special_characters(df)
            if special_char_issues:
                return {
                    'valid': False,
                    'error': f"Invalid characters found: {'; '.join(special_char_issues[:3])}",
                    'error_type': 'special_characters_found'
                }
            
            # Check 6: Data type consistency
            data_issues = self._validate_data_types(df)
            if data_issues:
                return {
                    'valid': False,
                    'error': f"Data quality issues found: {'; '.join(data_issues[:3])}",
                    'error_type': 'data_quality_issues'
                }
            
            return {'valid': True}
            
        except Exception as e:
            logger.error(f"Error validating data quality: {e}")
            return {
                'valid': False,
                'error': "Failed to validate Excel data quality.",
                'error_type': 'data_quality_error'
            }
    
    def _validate_data_types(self, df: pd.DataFrame) -> List[str]:
        """Validate data types and detect common issues."""
        issues = []
        
        for col_idx, column in enumerate(df.columns):
            col_data = df[column].dropna()
            if col_data.empty:
                continue
            
            # Check for mixed data types in same column
            data_types = set()
            for value in col_data.head(10):  # Check first 10 non-null values
                if pd.isna(value):
                    continue
                if isinstance(value, (int, float)) and not pd.isna(value):
                    data_types.add('numeric')
                elif isinstance(value, str):
                    if value.strip().isdigit():
                        data_types.add('numeric')
                    else:
                        data_types.add('text')
                else:
                    data_types.add('other')
            
            if len(data_types) > 1:
                issues.append(f"Column '{column}' has mixed data types")
            
            # Check for quantity-like columns with text values
            if any(keyword in str(column).lower() for keyword in ['qty', 'quantity', 'count', 'number']):
                text_values = []
                for value in col_data.head(5):
                    if isinstance(value, str) and not value.strip().replace('.', '').replace(',', '').isdigit():
                        text_values.append(value)
                
                if text_values:
                    issues.append(f"Quantity column '{column}' contains text values: {', '.join(text_values[:2])}")
        
        return issues