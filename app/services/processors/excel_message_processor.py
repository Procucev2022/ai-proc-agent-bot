"""
Excel Message Processor.

Handles Excel file upload processing for RFQ creation.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any
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
            if not isinstance(content, dict):
                raise ValueError("Invalid Excel upload content format")
            
            document_info = content.get("document", {})
            file_url = document_info.get("link")
            filename = document_info.get("filename", "")
            
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
            validation_service = ExcelValidationService()
            validation_result = await validation_service.validate_excel_file_from_url(file_url, filename)
            
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
            processing_service = ExcelProcessingService(self.openai_service)
            processing_result = await processing_service.process_excel_file(
                content=validation_result['content'],
                filename=filename
            )
            
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
            
            # Prepare context using helpers
            excel_context = ExcelHelpers.prepare_excel_context(processing_result, user.phone_number)
            
            # Update session  
            WorkflowManager.set_workflow_type(session, WorkflowType.rfq_creation, caller="handler")
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update(excel_context)
            
            # Determine flow based on completeness
            completeness = excel_context['completeness']
            items = processing_result.get('items', [])
            
            if ExcelHelpers.should_complete_immediately(completeness, items):
                return await self._handle_complete_excel(user, session, processing_result)
            else:
                return await self._handle_incomplete_excel(user, session, excel_context)
            
        except Exception as e:
            logger.error(f"Error processing Excel upload: {e}")
            await self.whatsapp_service.send_message(
                user.phone_number,
                "Sorry, I encountered an error processing your Excel file. Please try again."
            )
            return {"status": "error", "response": str(e)}
    
    async def _handle_complete_excel(self, user: User, session: ConversationSession, processing_result: Dict) -> Dict[str, Any]:
        """Handle complete Excel files that can create RFQ immediately."""
        try:
            # Generate processing response using OpenAI
            context = {
                'excel_data': processing_result,
                'workflow_type': 'excel_rfq_upload',
                'conversation_stage': 'excel_processing',
                'total_items': processing_result.get('total_items', 0),
                'filename': processing_result.get('filename', '')
            }
            
            processing_response = await self.response_helpers.generate_contextual_response(
                context,
                [f"Processing {processing_result.get('total_items', 0)} items from Excel file"],
                "excel_processing"
            )
            
            await self.whatsapp_service.send_message(user.phone_number, processing_response)
            
            # Validate items before creating template
            validation_result = processing_result.get('validation_result', {})
            if not validation_result.get('valid', False):
                # Items don't meet GMT API requirements
                error_msg = "Excel file doesn't meet GMT API requirements:\\n"
                for error in validation_result.get('errors', []):
                    error_msg += f"• {error}\\n"
                for warning in validation_result.get('warnings', []):
                    error_msg += f"• {warning}\\n"
                
                error_response = await self.response_helpers.generate_contextual_response(
                    {**context, 'error': error_msg},
                    ["Please check your Excel file format and try again."],
                    "error"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return {"status": "failed", "error": error_msg}
            
            # Create GMT template and submit
            processing_service = ExcelProcessingService(self.openai_service)
            template_bytes = processing_service.create_standard_template(processing_result['items'])
            api_data = processing_service.encode_for_api(template_bytes, processing_result['filename'])
            
            # Submit to GMT API
            rfq_service = RFQAPIService()
            gmt_result = await rfq_service.bulk_upload_rfq(api_data)
            
            if gmt_result.get('success'):
                # Generate completion response using OpenAI
                rfq_data = {
                    'items': processing_result['items'],
                    'filename': processing_result['filename'],
                    'total_items': processing_result['total_items']
                }
                
                completion_response = self.openai_service.generate_completion_response(rfq_data, context)
                await self.whatsapp_service.send_message(user.phone_number, completion_response)
                
                session.outcome = 'completed'
                session.completed_at = utc_now().replace(tzinfo=None)
                
                return {"status": "completed", "response": "rfq_created"}
            else:
                # GMT API failed, fall back to conversation completion
                error_context = {**context, 'error': gmt_result.get('error', 'Unknown error')}
                error_response = await self.response_helpers.generate_contextual_response(
                    error_context,
                    ["There was an issue creating the RFQ. Let me help you complete it through conversation."],
                    "error_recovery"
                )
                await self.whatsapp_service.send_message(user.phone_number, error_response)
                return await self._handle_incomplete_excel(user, session, {"excel_data": processing_result})
            
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
        """Handle incomplete Excel files that need conversation completion."""
        try:
            processing_result = excel_context['excel_data']
            missing_fields = excel_context['missing_fields']
            
            # Generate reupload instructions using helper
            instructions = ExcelHelpers.generate_reupload_instructions(missing_fields, excel_context)
            
            # Prepare context for OpenAI response generation
            context = {
                'excel_data': processing_result,
                'workflow_type': 'excel_rfq_upload',
                'conversation_stage': 'excel_completion',
                'missing_fields': missing_fields,
                'total_items': processing_result.get('total_items', 0),
                'filename': processing_result.get('filename', ''),
                'completeness': excel_context.get('completeness', 0)
            }
            
            # Generate clarification response using OpenAI with reupload instructions
            clarification_response = self.openai_service.generate_clarification_response(
                instructions, 
                excel_context.get('completeness', 0), 
                context
            )
            
            await self.whatsapp_service.send_message(user.phone_number, clarification_response)
            
            # Update session state - waiting for excel reupload
            session.workflow_state = session.workflow_state or {}
            session.workflow_state['stage'] = 'excel_reupload_required'
            session.workflow_state['pending_excel_reupload'] = True
            session.workflow_state['last_excel_issues'] = missing_fields
            
            return {"status": "excel_reupload_required", "response": "excel_reupload_instructions_sent"}
            
        except Exception as e:
            logger.error(f"Error handling incomplete Excel: {e}")
            raise