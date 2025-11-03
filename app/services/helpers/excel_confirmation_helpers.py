"""
Excel Confirmation Helpers.

Helper functions for generating Excel processing confirmation messages
and handling Excel-based RFQ creation flow.
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class ExcelConfirmationHelpers:
    """Helper class for Excel confirmation messages and flow."""
    
    @staticmethod
    def generate_confirmation_message(processing_stats: Dict[str, Any], filename: str) -> str:
        """
        Generate confirmation message based on Excel processing statistics.
        
        Args:
            processing_stats: Dictionary containing processing statistics
            filename: Name of the processed Excel file
            
        Returns:
            Formatted confirmation message string
        """
        total_rows = processing_stats.get('total_rows', 0)
        identified_rows = processing_stats.get('identified_for_rfq', 0)
        extracted_rows = processing_stats.get('extracted', 0)
        skipped_rows = processing_stats.get('skipped', 0)
        has_missing_items = processing_stats.get('has_missing_items', False)
        
        if has_missing_items:
            # When items are missing/skipped
            message = f"📊 *Excel Processing Summary*\n\n"
            message += f"📁 File: {filename}\n"
            message += f"📋 Total rows: {total_rows}\n"
            message += f"🔍 Identified for RFQ: {identified_rows}\n"
            message += f"✅ Extracted: {extracted_rows}\n"
            message += f"⚠️ Skipped: {skipped_rows}\n\n"
            message += f"❌ *Missing quantity information*\n\n"
            message += f"Please check your Excel file and ensure all items have quantity values, then reupload the file."
            
            # Add specific skipped items details if available
            skipped_summary = processing_stats.get('skipped_items_summary', '')
            if skipped_summary:
                message += f"\n\n📝 *Details:*\n{skipped_summary}"
        else:
            # When all items are extracted successfully
            message = f"📊 *Excel Processing Summary*\n\n"
            message += f"📁 File: {filename}\n"
            message += f"📋 Total rows: {total_rows}\n"
            message += f"🔍 Identified for RFQ: {identified_rows}\n"
            message += f"✅ Extracted: {extracted_rows}\n\n"
            message += f"✨ *All items processed successfully!*\n\n"
            message += f"I will now create RFQs for these {extracted_rows} items. Please confirm to proceed."
        
        return message
    
    @staticmethod
    def save_excel_confirmation_data(session, processing_result: Dict[str, Any]) -> None:
        """
        Save Excel processing data in session for confirmation handling.
        
        Args:
            session: Conversation session object
            processing_result: Result from Excel processing
        """
        try:
            # Initialize workflow state if needed
            if not hasattr(session, 'workflow_state') or not session.workflow_state:
                session.workflow_state = {}
            
            # Save Excel processing data for confirmation
            session.workflow_state['pending_excel_confirmation'] = {
                'processing_result': processing_result,
                'processing_stats': processing_result.get('processing_stats', {}),
                'rfqs': processing_result.get('rfqs', []),
                'items': processing_result.get('items', []),
                'filename': processing_result.get('filename', ''),
                'timestamp': processing_result.get('timestamp', ''),
                'ready_for_multiple_rfq_creation': True
            }
            
            logger.info(f"Saved Excel confirmation data in session: {len(processing_result.get('rfqs', []))} RFQs, {len(processing_result.get('items', []))} items")
            
        except Exception as e:
            logger.error(f"Error saving Excel confirmation data: {e}")
            raise
    
    @staticmethod
    def get_excel_confirmation_data(session) -> Dict[str, Any]:
        """
        Retrieve Excel confirmation data from session.
        
        Args:
            session: Conversation session object
            
        Returns:
            Dictionary containing Excel confirmation data
        """
        try:
            if not hasattr(session, 'workflow_state') or not session.workflow_state:
                return {}
            
            return session.workflow_state.get('pending_excel_confirmation', {})
            
        except Exception as e:
            logger.error(f"Error retrieving Excel confirmation data: {e}")
            return {}
    
    @staticmethod
    def clear_excel_confirmation_data(session) -> None:
        """
        Clear Excel confirmation data from session.
        
        Args:
            session: Conversation session object
        """
        try:
            if hasattr(session, 'workflow_state') and session.workflow_state:
                session.workflow_state.pop('pending_excel_confirmation', None)
                logger.info("Cleared Excel confirmation data from session")
                
        except Exception as e:
            logger.error(f"Error clearing Excel confirmation data: {e}")
    
    @staticmethod
    def prepare_multiple_rfq_data(excel_confirmation_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Prepare RFQ data for multiple RFQ creation from Excel confirmation data.
        
        Args:
            excel_confirmation_data: Excel confirmation data from session
            
        Returns:
            List of RFQ data dictionaries ready for creation
        """
        try:
            rfqs = excel_confirmation_data.get('rfqs', [])
            
            if not rfqs:
                # Fallback: convert items to RFQ format
                items = excel_confirmation_data.get('items', [])
                if items:
                    # Create a single RFQ with all items
                    rfq_data = {
                        'products': [],
                        'deliveryDate': None,
                        'state': None,
                        'city': None,
                        'pincode': None
                    }
                    
                    for item in items:
                        product = {
                            'description': item.get('ItemDescription', ''),
                            'quantity': item.get('Quantity'),
                            'unitofMeasures': item.get('Uom', 'pcs'),
                            'brand': item.get('Specification', ''),
                            'remarks': item.get('Remarks', '')
                        }
                        rfq_data['products'].append(product)
                    
                    rfqs = [rfq_data]
            
            logger.info(f"Prepared {len(rfqs)} RFQs for creation from Excel data")
            return rfqs
            
        except Exception as e:
            logger.error(f"Error preparing multiple RFQ data: {e}")
            return []
    
    @staticmethod
    def format_rfq_creation_summary(created_rfqs: List[Dict[str, Any]]) -> str:
        """
        Format summary message for created RFQs.
        
        Args:
            created_rfqs: List of created RFQ data
            
        Returns:
            Formatted summary message
        """
        try:
            total_rfqs = len(created_rfqs)
            total_products = sum(len(rfq.get('products', [])) for rfq in created_rfqs)
            
            message = f"🎉 *RFQ Creation Successful!*\n\n"
            message += f"📋 Created: {total_rfqs} RFQ{'s' if total_rfqs != 1 else ''}\n"
            message += f"📦 Total Products: {total_products}\n\n"
            
            # Add RFQ details
            for i, rfq in enumerate(created_rfqs, 1):
                products = rfq.get('products', [])
                message += f"*RFQ {i}:* {len(products)} product{'s' if len(products) != 1 else ''}\n"
                
                # Show first few products
                for j, product in enumerate(products[:3], 1):
                    desc = product.get('description', 'Unknown')
                    qty = product.get('quantity', 'N/A')
                    unit = product.get('unitofMeasures', 'pcs')
                    message += f"  {j}. {desc} - {qty} {unit}\n"
                
                if len(products) > 3:
                    message += f"  ... and {len(products) - 3} more items\n"
                
                message += "\n"
            
            message += "Your RFQs have been submitted to our vendor network. You'll receive quotes soon!"
            
            return message
            
        except Exception as e:
            logger.error(f"Error formatting RFQ creation summary: {e}")
            return "RFQs created successfully! You'll receive quotes soon."