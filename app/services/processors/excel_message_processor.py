"""
Excel Message Processor.

Handles Excel file upload processing for RFQ creation.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any, List
from app.models import WorkflowType, User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.excel_validation_service import ExcelValidationService
from app.services.excel_processing_service import ExcelProcessingService
from app.procucev_apis.rfq_apis import RFQAPIService
from app.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)


class ExcelMessageProcessor:
    """Processes Excel file uploads."""
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers, 
                 openai_service, **kwargs):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.openai_service = openai_service
    
    async def process_excel_upload(self, user: User, session: ConversationSession, content: Any) -> Dict[str, Any]:
        """Process Excel file upload for RFQ creation."""
        logger.info(f"[EXCEL-UPLOAD] Starting Excel upload processing for user {user.phone_number}")
        try:
            # Check if user needs registration
            if not user.is_registered:
                registration_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'registration_required'}
                registration_response = await self.response_helpers.generate_contextual_response(
                    registration_context,
                    ["Please complete your registration first before uploading files."],
                    "registration_required"
                )
                await self.whatsapp_service.send_message(user.phone_number, registration_response)
                return {"status": "handled", "response": "registration_required"}
            
            # Extract document information
            logger.info(f"[EXCEL-UPLOAD] Extracting document info from content: {type(content)}")
            if not isinstance(content, dict):
                logger.error(f"[EXCEL-UPLOAD] Invalid content format: {type(content)}")
                raise ValueError("Invalid Excel upload content format")
            
            document_info = content.get("document", {})
            file_url = document_info.get("link")
            filename = document_info.get("filename", "")
            logger.info(f"[EXCEL-UPLOAD] File details - Name: {filename}, URL: {file_url[:50] if file_url else 'None'}...")
            
            if not file_url:
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'file_access_error'}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    ["I couldn't access your Excel file. Please try uploading again."],
                    "file_access_error"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "handled", "response": "file_access_error"}
            
            # Validate Excel file
            logger.info(f"[EXCEL-UPLOAD] Starting validation for {filename}")
            validation_service = ExcelValidationService()
            validation_result = await validation_service.validate_excel_file_from_url(file_url, filename)
            logger.info(f"[EXCEL-UPLOAD] Validation result: {validation_result.get('valid', False)}")
            
            if not validation_result.get('valid'):
                validation_error = validation_result.get('error', 'Invalid Excel file')
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'validation_failed', 'error': validation_error}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    [validation_error],
                    "validation_failed"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "handled", "response": "validation_failed"}
            
            # Process Excel file
            logger.info(f"[EXCEL-UPLOAD] Starting Excel processing for {filename}, size: {len(validation_result.get('content', b''))} bytes")
            processing_service = ExcelProcessingService(self.openai_service)
            processing_result = await processing_service.process_excel_file(
                content=validation_result['content'],
                filename=filename
            )
            logger.info(f"[EXCEL-UPLOAD] Processing completed - Success: {processing_result.get('success', False)}, Items: {processing_result.get('total_items', 0)}")
            
            if not processing_result.get('success'):
                processing_error = processing_result.get('error', 'Failed to process Excel file')
                error_context = {'workflow_type': 'excel_upload', 'conversation_stage': 'processing_failed', 'error': processing_error}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    [f"Error processing Excel: {processing_error}"],
                    "processing_failed"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "handled", "response": "processing_failed"}
            
            # Check for missing quantities BEFORE saving any data to session
            items = processing_result.get('items', [])
            missing_quantities = [item for item in items if not item.get('Quantity') or str(item.get('Quantity', '')).strip() == '']
            
            if missing_quantities and len(items) > 3:  # Excel upload with missing quantities
                logger.info(f"[EXCEL-UPLOAD] Found {len(missing_quantities)} items with missing quantities out of {len(items)} total items")
                
                # Convert to entities format for message formatting
                excel_entities = self._convert_excel_to_entities(items)
                
                # Use the message formatter to generate appropriate error message
                from app.utils.rfq_message_formatter import format_rfq_response_message
                error_message = format_rfq_response_message(
                    extracted_entities=excel_entities,
                    global_fields={},
                    missing_fields=[]
                )
                
                await self.whatsapp_service.send_message(user.phone_number, error_message)
                
                # DO NOT save any data to session - return without updating session
                return {"status": "handled", "response": "missing_quantities_reupload_required"}
            
            # Only proceed with session updates if no missing quantities
            # Prepare context using helpers
            excel_context = ExcelHelpers.prepare_excel_context(processing_result, user.phone_number)
            
            # Update session  
            logger.info(f"[EXCEL-UPLOAD] Setting workflow type to rfq_creation for session {session.session_id}")
            WorkflowManager.set_workflow_type(session, WorkflowType.rfq_creation, caller="excel_upload_handler")
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update(excel_context)
            logger.info(f"[EXCEL-UPLOAD] Session state updated with Excel context, completeness: {excel_context.get('completeness', 0)}%")
            
            # Determine flow based on completeness
            completeness = excel_context['completeness']
            
            if ExcelHelpers.should_complete_immediately(completeness, items):
                return await self._handle_complete_excel(user, session, processing_result)
            else:
                return await self._handle_incomplete_excel(user, session, excel_context)
            
        except Exception as e:
            logger.error(f"[EXCEL-UPLOAD] Critical error processing Excel upload for {user.phone_number}: {e}")
            import traceback
            logger.error(f"[EXCEL-UPLOAD] Stack trace: {traceback.format_exc()}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Sorry, I encountered an error processing your Excel file. Please try again."
            )
            return {"status": "error", "response": str(e)}
    
    async def _handle_complete_excel(self, user: User, session: ConversationSession, processing_result: Dict) -> Dict[str, Any]:
        """Handle complete Excel files using new streamlined RFQ format."""
        try:
            # Use new RFQ format from streamlined processing
            rfqs = processing_result.get('rfqs', [])
            
            if not rfqs:
                # Fallback to legacy conversion if no RFQs in new format
                excel_entities = self._convert_excel_to_entities(processing_result['items'])
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['extracted_entities'] = excel_entities
            else:
                # Use new structured RFQ format
                # Convert RFQs to entities format for compatibility
                excel_entities = []
                for rfq in rfqs:
                    for product in rfq.get('products', []):
                        entity = {
                            'description': product.get('description', ''),
                            'quantity': product.get('quantity'),
                            'unitofMeasures': product.get('unitofMeasures', 'pcs'),
                            'brand': product.get('brand', ''),
                            'remarks': product.get('remarks', '')
                        }
                        excel_entities.append(entity)
                
                # Store both formats in session
                session.workflow_state = session.workflow_state or {}
                session.workflow_state['extracted_entities'] = excel_entities
                session.workflow_state['structured_rfqs'] = rfqs  # New structured format
            
            session.workflow_state['excel_source'] = True
            session.workflow_state['excel_filename'] = processing_result.get('filename', '')
            
            # Generate processing response
            total_products = processing_result.get('processing_summary', {}).get('total_products_extracted', len(excel_entities))
            context = {
                'excel_data': processing_result,
                'workflow_type': 'excel_rfq_upload',
                'conversation_stage': 'excel_processing',
                'total_items': total_products,
                'filename': processing_result.get('filename', '')
            }
            
            processing_response = await self.response_helpers.generate_contextual_response(
                context,
                [f"I found {total_products} items from your Excel file. Now I need some additional details to create your RFQ."],
                "excel_processing"
            )
            
            await self.whatsapp_service.send_message(user.phone_number, processing_response)
            
            # Redirect to multiple product flow by returning continue status
            return {"status": "redirect_to_multiple_flow", "entities": excel_entities, "continue_with_purchase_intent": True}
            
        except Exception as e:
            logger.error(f"Error handling complete Excel: {e}")
            error_context = {'error': str(e), 'workflow_type': 'excel_rfq_upload'}
            error_response = await self.response_helpers.generate_contextual_response(
                error_context,
                ["There was an issue processing your Excel file. Let me help you through conversation."],
                "error_recovery"
            )
            await self.whatsapp_service.send_message(user.phone_number, error_response)
            return await self._handle_incomplete_excel(user, session, {"excel_data": processing_result})
    
    async def _handle_incomplete_excel(self, user: User, session: ConversationSession, excel_context: Dict) -> Dict[str, Any]:
        """Handle incomplete Excel files using new streamlined format."""
        try:
            processing_result = excel_context['excel_data']
            
            # Use new structured format if available
            rfqs = processing_result.get('rfqs', [])
            if rfqs:
                # Convert structured RFQs to entities for compatibility
                excel_entities = []
                for rfq in rfqs:
                    for product in rfq.get('products', []):
                        entity = {
                            'description': product.get('description', ''),
                            'quantity': product.get('quantity'),
                            'unitofMeasures': product.get('unitofMeasures', 'pcs'),
                            'brand': product.get('brand', ''),
                            'remarks': product.get('remarks', '')
                        }
                        excel_entities.append(entity)
                
                # Check if global fields are missing from RFQs
                missing_common_fields = []
                for rfq in rfqs:
                    if not rfq.get('deliveryDate'):
                        missing_common_fields.append('delivery_date')
                    if not rfq.get('pincode'):
                        missing_common_fields.append('location')
                    break  # Check only first RFQ since global fields apply to all
            else:
                # Fallback to legacy processing
                items = processing_result.get('items', [])
                excel_entities = self._convert_excel_to_entities(items)
                missing_common_fields = self._identify_missing_common_fields(items)
            
            # Store entities in session
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['extracted_entities'] = excel_entities
            session.workflow_state['excel_source'] = True
            session.workflow_state['excel_filename'] = processing_result.get('filename', '')
            if rfqs:
                session.workflow_state['structured_rfqs'] = rfqs
            
            if missing_common_fields:
                # Ask for missing common data
                questions = []
                if 'delivery_date' in missing_common_fields:
                    questions.append("What is your required delivery date?")
                if 'location' in missing_common_fields:
                    questions.append("What is your delivery location (pincode)?")
                
                total_items = processing_result.get('processing_summary', {}).get('total_products_extracted', len(excel_entities))
                context = {
                    'excel_data': processing_result,
                    'workflow_type': 'excel_rfq_upload',
                    'conversation_stage': 'excel_completion',
                    'missing_fields': missing_common_fields,
                    'total_items': total_items,
                    'filename': processing_result.get('filename', '')
                }
                
                clarification_response = await self.response_helpers.generate_clarification_response(
                    questions, 50, context
                )
                
                await self.whatsapp_service.send_message(user.phone_number, clarification_response)
                
                return {"status": "excel_missing_common_data", "continue_with_purchase_intent": True}
            else:
                # All required data is present, proceed with RFQ creation
                return {"status": "redirect_to_multiple_flow", "entities": excel_entities, "continue_with_purchase_intent": True}
            
        except Exception as e:
            logger.error(f"Error handling incomplete Excel: {e}")
            raise
    
    def _convert_excel_to_entities(self, items: List[Dict]) -> List[Dict]:
        """Convert Excel items to entities format for multiple product flow."""
        entities = []
        for item in items:
            # Handle quantity properly - only set if actually present and not empty
            quantity = item.get('Quantity')
            if quantity is not None and str(quantity).strip() != '':
                try:
                    quantity = int(float(str(quantity).strip()))
                except (ValueError, TypeError):
                    quantity = None
            else:
                quantity = None
            
            entity = {
                'description': item.get('ItemDescription', ''),
                'projectDesc': item.get('ItemDescription', ''),
                'quantity': quantity,
                'unitofMeasures': item.get('Uom', 'pcs'),
                'brand': item.get('Specification', ''),  # Use specification as brand for now
                'remarks': item.get('Remarks', '')
            }
            entities.append(entity)
        return entities
    
    def _identify_missing_common_fields(self, items: List[Dict]) -> List[str]:
        """Identify missing common fields that apply to all items."""
        missing = []
        
        # Check if any item has delivery date info (not typically in Excel)
        has_delivery_date = any(item.get('DeliveryDate') for item in items)
        if not has_delivery_date:
            missing.append('delivery_date')
        
        # Check if any item has location info (not typically in Excel)
        has_location = any(item.get('Location') or item.get('City') or item.get('State') for item in items)
        if not has_location:
            missing.append('location')
        
        return missing