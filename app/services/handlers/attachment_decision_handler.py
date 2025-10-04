"""
Attachment Decision Handler.

Handles user decisions about pending attachments including approval,
rejection, and error handling with contextual responses.
Extracted from ChatService to reduce complexity.
"""

import logging
from typing import Dict, Any
from app.models import WorkflowType, User, ConversationSession
from app.services.whatsapp_service import WhatsAppService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.attachment_helpers import AttachmentHelpers

logger = logging.getLogger(__name__)


class AttachmentDecisionHandler:
    """Handles user decisions about pending attachments."""
    
    def __init__(self, whatsapp_service: WhatsAppService, response_helpers: ResponseHelpers,
                 purchase_intent_handler, session_manager):
        self.whatsapp_service = whatsapp_service
        self.response_helpers = response_helpers
        self.purchase_intent_handler = purchase_intent_handler
        self.session_manager = session_manager
    
    async def handle_attachment_decision(self, user: User, session: ConversationSession, 
                                       message: str, should_use_summary_aware_extraction_func=None) -> Dict[str, Any]:
        """Handle user's decision about pending attachments using contextual responses."""
        try:
            attachment_summary = AttachmentHelpers.get_attachment_summary(session)
            
            # Check if user wants to approve or reject the attachment
            if any(keyword in message.lower() for keyword in ["yes", "attach", "include", "add"]):
                return await self._handle_attachment_approval(user, session, message, attachment_summary, should_use_summary_aware_extraction_func)
            else:
                return await self._handle_attachment_rejection(user, session, message, should_use_summary_aware_extraction_func)
            
        except Exception as e:
            return await self._handle_attachment_error(user, session, message, e, should_use_summary_aware_extraction_func)
    
    async def _handle_attachment_approval(self, user: User, session: ConversationSession, 
                                        message: str, attachment_summary: Dict, 
                                        should_use_summary_aware_extraction_func=None) -> Dict[str, Any]:
        """Handle attachment approval."""
        approval_result = AttachmentHelpers.approve_pending_attachment(session)
        
        if approval_result:
            await self._save_session(session, WorkflowType.rfq_creation)
            
            # Use contextual response for approval confirmation
            context = {
                "conversation_stage": "attachment_approved",
                "attachment_count": attachment_summary["approved_count"] + 1,
                "user_message": message,
                "next_step": "continue_rfq"
            }
            response = await self.response_helpers.generate_contextual_response(
                context,
                ["Great! I've attached your image to the RFQ."],
                "attachment_approved"
            )
            await self.whatsapp_service.send_message(user.phone_number, response)
        else:
            # Use contextual response for approval failure
            context = {
                "conversation_stage": "attachment_approval_failed", 
                "user_message": message
            }
            response = await self.response_helpers.generate_contextual_response(
                context,
                ["I couldn't attach your image. Let's continue with your RFQ."],
                "attachment_approval_failed"
            )
            await self.whatsapp_service.send_message(user.phone_number, response)
        
        # Continue with the RFQ flow by routing back to purchase intent handler
        return await self.purchase_intent_handler.handle_purchase_intent(
            user, session, message, None, should_use_summary_aware_extraction_func
        )
    
    async def _handle_attachment_rejection(self, user: User, session: ConversationSession, 
                                         message: str, should_use_summary_aware_extraction_func=None) -> Dict[str, Any]:
        """Handle attachment rejection."""
        # User wants to reject the attachment
        AttachmentHelpers.reject_pending_attachments(session)
        await self._save_session(session, WorkflowType.rfq_creation)
        
        # Use contextual response for rejection confirmation
        context = {
            "conversation_stage": "attachment_rejected",
            "user_message": message,
            "next_step": "continue_rfq"
        }
        response = await self.response_helpers.generate_contextual_response(
            context,
            ["No problem, I'll continue without the attachment."],
            "attachment_rejected"
        )
        await self.whatsapp_service.send_message(user.phone_number, response)
        
        # Continue with the RFQ flow by routing back to purchase intent handler
        return await self.purchase_intent_handler.handle_purchase_intent(
            user, session, message, None, should_use_summary_aware_extraction_func
        )
    
    async def _handle_attachment_error(self, user: User, session: ConversationSession, 
                                     message: str, error: Exception, 
                                     should_use_summary_aware_extraction_func=None) -> Dict[str, Any]:
        """Handle attachment decision errors."""
        logger.error(f"Error handling attachment decision: {error}")
        
        # Remove the pending state and continue
        session.workflow_state["awaiting_attachment_decision"] = False
        await self._save_session(session, WorkflowType.rfq_creation)
        
        # Use contextual response for error handling
        context = {
            "conversation_stage": "attachment_decision_error",
            "error_message": str(error),
            "user_message": message
        }
        response = await self.response_helpers.generate_contextual_response(
            context,
            ["Let's continue with your RFQ."],
            "attachment_decision_error"
        )
        await self.whatsapp_service.send_message(user.phone_number, response)
        
        return await self.purchase_intent_handler.handle_purchase_intent(
            user, session, message, None, should_use_summary_aware_extraction_func
        )
    
    async def _save_session(self, session: ConversationSession, workflow_type: str) -> ConversationSession:
        """Save session using session manager."""
        return await self.session_manager.save_session(session, workflow_type)