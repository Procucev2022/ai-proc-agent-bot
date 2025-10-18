"""
Excel file validation service for WhatsApp document uploads.

This service provides comprehensive validation of Excel files received through WhatsApp,
including format validation, content type checking, readability verification, and
security checks for password protection and file corruption.
"""

import logging
import io
from typing import Dict, Any, Optional
import aiohttp
import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
import xlrd

logger = logging.getLogger(__name__)

class ExcelValidationService:
    """
    Service for validating Excel files from WhatsApp uploads.
    
    Provides multi-layer validation including file extension, content type,
    readability, size limits, and security checks.
    """
    
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
    MAX_ROWS = 50  # Maximum allowed rows
    SUPPORTED_EXTENSIONS = {'.xlsx', '.xls', '.xlsm'}
    EXCEL_MAGIC_NUMBERS = {
        b'PK\x03\x04': 'xlsx',  # ZIP format (XLSX)
        b'\xd0\xcf\x11\xe0': 'xls',  # OLE format (XLS)
    }
    
    async def validate_excel_file_from_url(self, file_url: str, filename: str) -> Dict[str, Any]:
        """
        Download and validate Excel file from WhatsApp URL.
        
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
            
            # Step 2: Download file
            file_content = await self._download_file(file_url)
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
            
            # Step 4: Validate content type
            content_validation = self._validate_excel_content(file_content)
            if not content_validation['valid']:
                return content_validation
            
            # Step 5: Test readability
            readability_validation = await self._validate_excel_readability(file_content, filename)
            if not readability_validation['valid']:
                return readability_validation
            
            # Step 6: Validate row count and merged cells
            structure_validation = await self._validate_excel_structure(file_content)
            if not structure_validation['valid']:
                return structure_validation
            
            # Success - return validated content
            return {
                'valid': True,
                'content': file_content,
                'filename': filename,
                'size': len(file_content),
                'format': content_validation['format']
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
        
        # Extract extension (case insensitive)
        ext = '.' + filename.lower().split('.')[-1] if '.' in filename else ''
        return ext in self.SUPPORTED_EXTENSIONS
    
    async def _download_file(self, file_url: str) -> Optional[bytes]:
        """Download file from WhatsApp URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as response:
                    if response.status == 200:
                        return await response.read()
                    else:
                        logger.error(f"Failed to download file. Status: {response.status}")
                        return None
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return None
    
    def _validate_excel_content(self, content: bytes) -> Dict[str, Any]:
        """Validate file content using magic numbers."""
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
        
        return {
            'valid': False,
            'error': "File does not appear to be a valid Excel file.",
            'error_type': 'invalid_format'
        }
    
    async def _validate_excel_readability(self, content: bytes, filename: str) -> Dict[str, Any]:
        """Test if Excel file can be read by pandas/openpyxl."""
        try:
            file_obj = io.BytesIO(content)
            
            # Try reading with pandas first
            try:
                df = pd.read_excel(file_obj, sheet_name=0, nrows=1)
                return {'valid': True}
            except Exception as pandas_error:
                logger.debug(f"Pandas read failed: {pandas_error}")
                
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
                    if "password" in str(openpyxl_error).lower() or "encrypted" in str(openpyxl_error).lower():
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
    
    async def _validate_excel_structure(self, content: bytes) -> Dict[str, Any]:
        """Validate Excel structure: row count and merged cells."""
        try:
            file_obj = io.BytesIO(content)
            
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
                
                logger.info(f"[EXCEL-STRUCTURE] Excel has {filled_rows} filled rows (out of {worksheet.max_row} total), max allowed: {self.MAX_ROWS}")
                if filled_rows > self.MAX_ROWS:
                    workbook.close()
                    logger.error(f"[EXCEL-STRUCTURE] Too many filled rows: {filled_rows} > {self.MAX_ROWS}")
                    return {
                        'valid': False,
                        'error': f"Your Excel file contains {filled_rows} rows with data, but only 50 rows are allowed per upload. Please reduce to 50 rows and reupload.",
                        'error_type': 'too_many_rows'
                    }
                
                # Check 2: Merged cells validation
                merged_ranges = list(worksheet.merged_cells.ranges)
                logger.info(f"[EXCEL-STRUCTURE] Found {len(merged_ranges)} merged cell ranges")
                if merged_ranges:
                    workbook.close()
                    logger.error(f"[EXCEL-STRUCTURE] Merged cells found: {merged_ranges}")
                    return {
                        'valid': False,
                        'error': "Your Excel file contains merged cells. Please unmerge all cells and reupload.",
                        'error_type': 'merged_cells_found'
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
                    logger.info(f"[EXCEL-STRUCTURE-FALLBACK] Pandas found {filled_rows} filled rows, max allowed: {self.MAX_ROWS}")
                    if filled_rows > self.MAX_ROWS:
                        logger.error(f"[EXCEL-STRUCTURE-FALLBACK] Too many filled rows: {filled_rows} > {self.MAX_ROWS}")
                        return {
                            'valid': False,
                            'error': f"Your Excel file contains {filled_rows} rows with data, but only 50 rows are allowed per upload. Please reduce to 50 rows and reupload.",
                            'error_type': 'too_many_rows'
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
                'error': "Failed to validate Excel file structure.",
                'error_type': 'structure_validation_error'
            }