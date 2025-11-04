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
        Format Excel validation errors using standardized format.
        
        Args:
            error_type: Type of error (row_limit, invalid_quantity, merged_cells, etc.)
            details: Error-specific details
            
        Returns:
            Formatted error message following common pattern
        """
        if details is None:
            details = {}
            
        error_formats = {
            'row_limit': lambda d: f"Row limit exceeded:\n Reason: Your Excel file contains {d.get('actual_rows', 'N/A')} rows, but only {d.get('max_rows', 50)} rows are allowed.",
            
            'invalid_quantity': lambda d: f"Invalid quantity values:\n Reason: The 'Quantity' column contains mixed data types (e.g., text values such as \"{d.get('example_values', '@200')}\"). Please ensure all quantities are numeric.",
            
            'merged_cells': lambda d: "Merged cells:\n Reason: Your Excel file contains merged cells. Please unmerge all cells before re-uploading.",
            
            'empty_file': lambda d: "Empty file:\n Reason: The uploaded file contains no data. Please provide the required product details in Excel or directly in your message.",
            
            'multiple_worksheets': lambda d: f"Multiple worksheets:\n Reason: The Excel file has {d.get('worksheet_count', 'multiple')} worksheets, but only 1 worksheet is allowed.",
            
            'file_too_large': lambda d: f"File size exceeded:\n Reason: Your Excel file is {d.get('file_size_mb', 'N/A'):.1f}MB, but only files up to {d.get('max_size_mb', 3)}MB are allowed.",
            
            'password_protected': lambda d: "Password protected file:\n Reason: Your Excel file is password protected. Please upload an unprotected Excel file.",
            
            'invalid_format': lambda d: "Invalid file format:\n Reason: The uploaded file is not a valid Excel format. Please upload a .xlsx, .xls, or .xlsm file.",
            
            'corrupted_file': lambda d: "Corrupted file:\n Reason: The Excel file appears to be corrupted or damaged. Please try uploading again.",
            
            'no_headers': lambda d: "Missing headers:\n Reason: Your Excel file doesn't have proper column headers. Please add clear column headers and reupload.",
            
            'mixed_data_types': lambda d: f"Mixed data types:\n Reason: Column '{d.get('column_name', 'Unknown')}' contains both text and numeric values. Please ensure consistent data types in each column.",
            
            'unsupported_format': lambda d: f"Unsupported file format:\n Reason: The file format '{d.get('format', 'unknown')}' is not supported. Please upload an Excel file (.xlsx, .xls, .xlsm).",
            
            'download_failed': lambda d: "File download failed:\n Reason: Could not download the uploaded file. Please try uploading again.",
            
            'validation_error': lambda d: f"Validation error:\n Reason: {d.get('message', 'An error occurred while validating your Excel file. Please try again.')}"
        }
        
        formatter = error_formats.get(error_type)
        if formatter:
            try:
                return formatter(details)
            except Exception:
                # Fallback if formatting fails
                return f"Excel validation error:\n Reason: {details.get('message', 'Unknown error occurred.')}"
        else:
            return f"Excel validation error:\n Reason: {details.get('message', 'Unknown error occurred.')}"


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