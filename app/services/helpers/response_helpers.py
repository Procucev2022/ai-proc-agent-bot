"""
Response generation helpers for ChatService.

Contains methods for generating various types of responses using OpenAI,
extracted from the main ChatService class for better organization.
"""

import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class ResponseHelpers:
    """Response generation helper methods extracted from ChatService."""
    
    def __init__(self, openai_service):
        """Initialize with OpenAI service dependency."""
        self.openai_service = openai_service
    
    async def generate_contextual_response(self, context: dict, base_questions: list = None, conversation_stage: str = "collecting") -> str:
        """Generate contextual response using OpenAI."""
        try:
            return self.openai_service.generate_contextual_response(context, base_questions, conversation_stage)
        except Exception as e:
            logger.error(f"Error generating contextual response: {e}")
            if base_questions:
                return f"Thank you for the information! {base_questions[0]}"
            else:
                return "Thank you for the information. Could you provide more details to help me assist you?"
    
    async def generate_completion_response(self, rfq_schema, context: dict) -> str:
        """Generate completion response using OpenAI."""
        try:
            rfq_data = rfq_schema.dict() if hasattr(rfq_schema, 'dict') else {}
            return self.openai_service.generate_completion_response(rfq_data, context)
        except Exception as e:
            logger.error(f"Error generating completion response: {e}")
            return "Excellent! Your RFQ is now complete. I'll process this request and get back to you soon."
    
    async def generate_clarification_response(self, questions: list, completeness: float, context: dict) -> str:
        """Generate clarification response using OpenAI."""
        try:
            return self.openai_service.generate_clarification_response(questions, completeness, context)
        except Exception as e:
            logger.error(f"Error generating clarification response: {e}")
            questions_text = "\\n".join(f"• {q}" for q in questions)
            return f"Your RFQ is {completeness}% complete. I need:\\n\\n{questions_text}"
    
    async def generate_rfq_summary_and_confirmation(self, rfq_schema, context: dict) -> str:
        """Generate RFQ summary and ask for confirmation using OpenAI."""
        try:
            # Use OpenAI to generate the summary and confirmation naturally
            summary_context = {
                **context,
                "conversation_stage": "rfq_summary_confirmation",
                "rfq_data": rfq_schema.dict() if hasattr(rfq_schema, 'dict') else {},
                "action_needed": "Generate RFQ summary and ask for user confirmation"
            }
            
            return self.openai_service.generate_contextual_response(
                summary_context, 
                ["Please review the RFQ details and confirm if you want to proceed"], 
                "rfq_confirmation"
            )
            
        except Exception as e:
            logger.error(f"Error generating RFQ summary: {e}")
            return "Your RFQ is ready! Would you like me to create it? Reply 'Yes' to confirm or 'No' to make changes."
    
    async def generate_multiple_rfq_summary_and_confirmation(self, rfq_schemas: list, context: dict) -> str:
        """Generate comprehensive summary for multiple RFQs and ask for confirmation using OpenAI."""
        try:
            # Prepare data for all RFQs
            all_rfq_data = []
            for schema in rfq_schemas:
                all_rfq_data.append(schema.dict() if hasattr(schema, 'dict') else {})
            
            # Use OpenAI to generate comprehensive summary and confirmation
            summary_context = {
                **context,
                "conversation_stage": "multiple_rfq_summary_confirmation",
                "all_rfq_data": all_rfq_data,
                "action_needed": "Generate comprehensive summary for all RFQs and ask for user confirmation"
            }
            
            return self.openai_service.generate_contextual_response(
                summary_context, 
                ["Please review all RFQ details and confirm if you want to proceed with creating all RFQs"], 
                "multiple_rfq_confirmation"
            )
            
        except Exception as e:
            logger.error(f"Error generating multiple RFQ summary: {e}")
            return f"Your {len(rfq_schemas)} RFQs are ready! Would you like me to create them? Reply 'Yes' to confirm or 'No' to make changes."
    
    async def generate_rfq_result_response(self, gmt_result: dict, context: dict) -> str:
        """Generate response for RFQ creation result using OpenAI."""
        success = gmt_result.get("success", False)
        
        if success:
            # For successful RFQ creation, return simple thank you message without percentage
            backend_ref = gmt_result.get("backend_reference")
            if backend_ref:
                return f"Thank you. Your RFQ has been created successfully. Reference: {backend_ref}"
            else:
                return "Thank you. Your RFQ has been created successfully."
        else:
            # For failures, generate error response
            stage = "rfq_failure"
            try:
                result_context = {
                    **context,
                    "conversation_stage": stage,
                    "error_message": gmt_result.get("error")
                }
                
                questions = ["There was an issue creating your RFQ"]
                return await self.generate_contextual_response(result_context, questions, stage)
                
            except Exception as e:
                logger.error(f"Error generating RFQ failure response: {e}")
                return f"There was an issue creating your RFQ: {gmt_result.get('error', 'Unknown error')}. Please try again."