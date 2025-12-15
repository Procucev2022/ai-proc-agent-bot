"""
Excel error formatting utility for consistent error messages across the application.

This module provides standardized error message formatting for Excel upload issues
following a common pattern: Error Type + Reason with specific details.
"""

from typing import Dict, Any


class ExcelErrorFormatter:
    """Utility class for formatting Excel validation errors consistently."""
    
    @staticmethod
    def format_error(error_type: str, details: Dict[str, Any] = None) -> str:
        """
        Format Excel validation errors using standardized format: File Processing Failed: <error_message>
        
        Args:
            error_type: Type of error (row_limit, invalid_quantity, merged_cells, etc.)
            details: Error-specific details
            
        Returns:
            Formatted error message following standard format
        """
        if details is None:
            details = {}
            
        error_formats = {
            'row_limit': lambda d: f"File Processing Failed: Your Excel file contains {d.get('actual_rows', 'N/A')} rows, but only {d.get('max_rows', 50)} rows are allowed per upload. Please reduce the file to {d.get('max_rows', 50)} rows and reupload.",
            
            'invalid_quantity': lambda d: f"File Processing Failed: The 'Quantity' column contains non-numeric values such as {d.get('example_values', '@200')}. Please update the file with numeric quantities only.",
            
            'merged_cells': lambda d: f"File Processing Failed: {d.get('message', 'Merged cells were detected. Please unmerge all cells and upload the corrected file.')}",
            
            'empty_file': lambda d: f"File Processing Failed: {d.get('message', 'The file contains no data. Please provide the required product details in Excel or directly in your message.')}",
            
            'multiple_worksheets': lambda d: f"File Processing Failed: The Excel file contains {d.get('worksheet_count', 'multiple')} worksheets, but only 1 worksheet is allowed. Please use a single sheet and reupload.",
            
            'file_too_large': lambda d: f"File Processing Failed: Your Excel file is {d.get('file_size_mb', 'N/A'):.1f}MB, but only files up to {d.get('max_size_mb', 3)}MB are allowed.",
            
            'password_protected': lambda d: f"File Processing Failed: {d.get('message', 'Your Excel file is password protected. Please upload an unprotected Excel file.')}",
            
            'invalid_format': lambda d: f"File Processing Failed: {d.get('message', 'The uploaded file is not a valid Excel format. Please upload a .xlsx, .xls, or .xlsm file.')}",
            
            'corrupted_file': lambda d: f"File Processing Failed: {d.get('message', 'The Excel file appears to be corrupted or damaged. Please try uploading again.')}",
            
            'no_headers': lambda d: f"File Processing Failed: {d.get('message', 'Your Excel file does not have proper column headers. Please add clear column headers in the first row and reupload.')}",
            
            'mixed_data_types': lambda d: f"File Processing Failed: The '{d.get('column_name', 'Procurement Requirement')}' field contains mixed data types. Please provide a clear and consistent description with uniform formatting.",
            
            'unsupported_format': lambda d: f"File Processing Failed: The file format '{d.get('format', 'unknown')}' is not supported. Please upload an Excel file (.xlsx, .xls, .xlsm).",
            
            'download_failed': lambda d: f"File Processing Failed: {d.get('message', 'Could not download the uploaded file. Please try uploading again.')}",
            
            'validation_error': lambda d: f"File Processing Failed: {d.get('message', 'An error occurred while validating your Excel file. Please try again.')}",
            
            'date_validation': lambda d: f"File Processing Failed: {d.get('message', 'Invalid delivery date.')}",
            
            'pincode_validation': lambda d: f"File Processing Failed: {d.get('message', 'Invalid pincode.')}",
            
            'processing_incomplete': lambda d: f"File Processing Failed: File processing incomplete. {d.get('skipped_rows', 0)} row(s) skipped out of {d.get('total_rows', 0)} total product rows. Only {d.get('extracted_rows', 0)} products extracted successfully.\n\n{d.get('skipped_summary', '')}\n\nPlease fix your Excel file and upload again.",
            
            'date_location_inconsistency': lambda d: f"File Processing Failed: {d.get('message', 'Inconsistent date and location data detected.')}",
            
            'special_characters_in_headers': lambda d: f"File Processing Failed: {d.get('message', f'The column headers in your data contain special characters, such as \'{d.get(\'example_header\', \'Display Name/Code\')}\'. Please modify these headers to include only letters, numbers, spaces, and basic punctuation. Could you please update the headers accordingly and resend the information?')}"
        }
        
        formatter = error_formats.get(error_type)
        if formatter:
            try:
                return formatter(details)
            except Exception:
                # Fallback if formatting fails
                return f"File Processing Failed: {details.get('message', 'Unknown error occurred.')}"
        else:
            return f"File Processing Failed: {details.get('message', 'Unknown error occurred.')}"


# Convenience function for direct usage
def format_excel_error(error_type: str, details: Dict[str, Any] = None) -> str:
    """
    Convenience function to format Excel errors.
    
    Args:
        error_type: Type of error
        details: Error-specific details
        
    Returns:
        Formatted error message
    """
    return ExcelErrorFormatter.format_error(error_type, details)