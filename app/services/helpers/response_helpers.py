"""
Response generation helpers for ChatService.

Contains methods for generating various types of responses using OpenAI,
extracted from the main ChatService class for better organization.
"""

import logging
from typing import Dict, Any, List, Union
from app.utils.rfq_message_formatter import format_rfq_entities_message, format_simple_missing_fields_message

logger = logging.getLogger(__name__)


class ResponseHelpers:
    """Response generation helper methods extracted from ChatService."""
    
    def __init__(self, openai_service):
        """Initialize with OpenAI service dependency."""
        self.openai_service = openai_service
    
    async def _generate_common_seller_response(self, workflow_state: str, message_type: str, context: Dict[str, Any], fallback: str) -> str:
        """Common method for generating seller responses using optimized prompt."""
        try:
            # Ensure context is a dictionary
            print("context", context)
            if isinstance(context, str):
                context = {"workflow_state": context}
            
            prompt = f"Context: {context}"
            
            instructions = self.openai_service._load_prompt(
                "response_generation", 
                "_get_seller_common_response_prompt",
                workflow_state=workflow_state,
                message_type=message_type,
                credits_available=context.get("credits_available"),
                context_data=context
            )

            response = self.openai_service.client.responses.create(
                model=self.openai_service.default_model,
                input=[{"role": "user", "content": prompt}],
                instructions=instructions
            )
            print("lln repsonse", response.output_text)

            return response.output_text.strip() if response.output_text else fallback
        except Exception as e:
            logger.error(f"Error generating {message_type} response: {e}")
            return fallback
    
    async def generate_contextual_response(self, context: dict, base_questions: list = None, conversation_stage: str = "collecting", chat_summaries: list = None) -> str:
        """Generate contextual response using OpenAI with optional chat summary context."""
        try:
            # Enhance context with chat summaries if available
            enhanced_context = context.copy()
            if chat_summaries:
                enhanced_context["chat_summaries"] = chat_summaries
                enhanced_context["has_historical_context"] = True
                print(f"ResponseHelpers: Enhanced context with {len(chat_summaries)} chat summaries")
            
            return self.openai_service.generate_contextual_response(enhanced_context, base_questions, conversation_stage)
        except Exception as e:
            logger.error(f"Error generating contextual response: {e}")
            if base_questions:
                return f"Thank you for the information! {base_questions[0]}"
            else:
                return "Thank you for the information. Could you provide more details to help me assist you?"

    async def generate_rfq_status_contextual_response(self, context: dict) -> str:
        """Generate contextual response using OpenAI."""
        try:
            return self.openai_service.generate_rfq_status_response(context)
        except Exception as e:
            logger.error(f"Error generating contextual response: {e}")

    async def generate_seller_contextual_intent_response(self, context: dict) -> str:
        """Generate contextual response using OpenAI."""
        try:
            return self.openai_service.generate_seller_intent(context)
        except Exception as e:
            logger.error(f"Error generating contextual response: {e}")


    async def generate_seller_contextual_response(self, context: Dict[str, Any]) -> str:
        """Generate contextual response for seller based on workflow state."""
        try:
            workflow_state = context.get("workflow_state")

            print("workflow state", workflow_state, type(workflow_state))

            if workflow_state == "display_rfqs_to_seller":
                return await self._generate_rfq_display_response(context)
            elif workflow_state == "display_rfqs_no_credits":
                return await self._generate_no_credits_rfq_response(context)
            elif workflow_state == "no_credits_available":
                return await self._generate_no_credits_response(context)
            elif workflow_state == "show_subscription_plans":
                return await self._generate_subscription_plans_response(context)
            elif workflow_state == "payment_link_generated":
                return await self._generate_payment_link_response(context)
            elif workflow_state == "rfq_email_processing":
                return await self._generate_email_processing_response(context)
            elif workflow_state == "rfq_email_status":
                return await self._generate_email_status_response(context)
            elif workflow_state == "rfq_email_status_with_errors":
                return await self._generate_rfq_email_status_with_errors_response(context)
            elif workflow_state == "invalid_rfq_selection":
                return await self._generate_invalid_rfq_response(context)
            elif workflow_state == "invalid_plan_selection":
                return await self._generate_invalid_plan_response(context)
            elif workflow_state == "general_seller_response":
                return await self._generate_general_seller_response(context)
            # NEW WORKFLOW STATES FOR AI INTENT HANDLING
            elif workflow_state == "general_affirmative_response":
                return await self._generate_general_affirmative_response(context)
            elif workflow_state == "contextual_plan_request":
                return await self._generate_contextual_plan_request_response(context)
            elif workflow_state == "ambiguous_seller_response":
                return await self._generate_ambiguous_seller_response(context)
            # ADD THESE NEW WORKFLOW STATES:
            elif workflow_state == "end_of_flow_reminder":
                return await self._generate_end_of_flow_reminder_response(context)
            elif workflow_state == "generic_closing_message":
                return await self._generate_generic_closing_response(context)
            elif workflow_state == "standard_closing_message":
                return await self._generate_standard_closing_response(context)


            elif workflow_state in ["error", "credit_check_error", "rfq_fetch_error", "plan_fetch_error",
                                    "payment_link_error"]:
                return await self._generate_error_response(context)
            else:
                return await self._generate_fallback_seller_response(context)

        except Exception as e:
            logger.error(f"Error generating seller contextual response: {e}")
            fallback_result = self._get_fallback_message(context.get("workflow_state"), context.get("user_role"))
            return fallback_result["message"] if isinstance(fallback_result, dict) else fallback_result

    # Add these new workflow states to your response_helpers.py

    async def _generate_rfq_email_status_with_errors_response(self, context: Dict[str, Any]) -> str:
        """Generate response for RFQ email status with error code handling."""
        return await self._generate_common_seller_response(
            "rfq_email_status_with_errors", "general_assistance", context,
            self._get_email_status_with_errors_openai_fallback(context)
        )

    def _get_email_status_with_errors_openai_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for email status response with error code handling."""
        successful = context.get("successful_emails", 0)
        total = context.get("total_requested", 0)
        error_analysis = context.get("error_analysis", {})

        if successful == total:
            return f"Successfully sent all {successful} RFQ details to your email!"

        message = f"Email Status Summary:\n"
        message += f"Successful: {successful}\n"
        message += f"Failed: {error_analysis.get('total_failed', 0)}\n\n"

        # Handle specific error codes
        error_counts = error_analysis.get("error_counts", {})
        error_categories = error_analysis.get("error_categories", {})

        if error_counts.get("NO_CREDITS", 0) > 0:
            no_credit_rfqs = error_categories.get("NO_CREDITS", [])
            message += f"Insufficient Credits ({len(no_credit_rfqs)} RFQs):\n"
            message += f"RFQ IDs: {', '.join(no_credit_rfqs)}\n"
            message += "Please purchase more credits to access these RFQs.\n\n"

        if error_counts.get("RFQ_NOT_FOUND", 0) > 0:
            not_found_rfqs = error_categories.get("RFQ_NOT_FOUND", [])
            message += f"RFQs Not Found ({len(not_found_rfqs)} RFQs):\n"
            message += f"RFQ IDs: {', '.join(not_found_rfqs)}\n"
            message += "These RFQs may have expired or been withdrawn.\n\n"

        if error_counts.get("API_ERROR", 0) > 0:
            api_error_rfqs = error_categories.get("API_ERROR", [])
            message += f"Technical Issues ({len(api_error_rfqs)} RFQs):\n"
            message += f"RFQ IDs: {', '.join(api_error_rfqs)}\n"
            message += "Please try again later or contact support.\n\n"

        if error_counts.get("UNKNOWN", 0) > 0:
            unknown_error_rfqs = error_categories.get("UNKNOWN", [])
            message += f"Unknown Errors ({len(unknown_error_rfqs)} RFQs):\n"
            message += f"RFQ IDs: {', '.join(unknown_error_rfqs)}\n"
            message += "Please contact support@procurev.com for assistance.\n\n"

        if successful > 0:
            message += f"{successful} RFQ details were sent successfully to your email."

        return message.strip()
    async def _generate_end_of_flow_reminder_response(self, context: Dict[str, Any]) -> str:
        """Generate end-of-flow reminder response showing open RFQs."""
        return await self._generate_common_seller_response(
            "end_of_flow_reminder", "general_assistance", context,
            self._get_end_of_flow_reminder_fallback(context)
        )

    async def _generate_generic_closing_response(self, context: Dict[str, Any]) -> str:
        """Generate generic closing message when reminder API fails."""
        return await self._generate_common_seller_response(
            "generic_closing_message", "general_assistance", context,
            "Thanks for chatting with us! For any assistance, contact support@procurev.com"
        )

    async def _generate_standard_closing_response(self, context: Dict[str, Any]) -> str:
        """Generate standard closing message when no open RFQs found."""
        return await self._generate_common_seller_response(
            "standard_closing_message", "general_assistance", context,
            "Thanks for chatting with us! For help with any queries, contact support@procurev.com"
        )

    def _get_end_of_flow_reminder_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for end-of-flow reminder response."""
        open_rfqs = context.get("open_rfqs", [])
        total_open = context.get("total_open_rfqs", 0)

        if not open_rfqs:
            return "Thanks for chatting with us! For help with any queries, contact support@procurev.com"

        message = f"Thanks for chatting with us! You still have {total_open} live RFQ(s) for which bids haven't been submitted:\n\n"

        for i, rfq in enumerate(open_rfqs, 1):
            rfq_id = rfq.get("rfq_id", "N/A")
            location = rfq.get("location", "N/A")
            date = rfq.get("submission_date", "N/A")
            message += f"{i}. RFQ {rfq_id}\n   📍 {location}\n   📅 {date}\n\n"

        message += "We encourage you to submit bids. For help, contact support@procurev.com"
        return message

    async def _generate_rfq_display_response(self, context: Dict[str, Any]) -> str:
        """Generate response for displaying RFQs to seller with credits."""
        return await self._generate_common_seller_response(
            "display_rfqs_to_seller", "rfq_display", context, self._get_rfq_display_fallback(context)
        )

    async def _generate_no_credits_rfq_response(self, context: Dict[str, Any]) -> str:
        """Generate response for displaying RFQs when seller has no credits."""
        return await self._generate_common_seller_response(
            "display_rfqs_no_credits", "rfq_display", context, self._get_no_credits_rfq_fallback(context)
        )

    async def _generate_subscription_plans_response(self, context: Dict[str, Any]) -> str:
        """Generate response showing subscription plans."""
        return await self._generate_common_seller_response(
            "show_subscription_plans", "subscription_plans", context, self._get_subscription_plans_fallback(context)
        )

    async def _generate_no_credits_response(self, context: Dict[str, Any]) -> str:
        """Generate response showing subscription plans when seller has no/low credits."""
        return await self._generate_common_seller_response(
            "no_credits_available", "subscription_plans", context, self._get_subscription_plans_fallback(context)
        )

    async def _generate_payment_link_response(self, context: Dict[str, Any]) -> str:
        """Generate response with payment link."""
        return await self._generate_common_seller_response(
            "payment_link_generated", "payment_link", context, self._get_payment_link_fallback(context)
        )

    async def _generate_email_processing_response(self, context: Dict[str, Any]) -> str:
        """Generate response acknowledging RFQ email processing."""
        selected_rfq_ids = context.get("selected_rfq_ids", [])
        return await self._generate_common_seller_response(
            "rfq_email_processing", "general_assistance", context,
            f"Thank you! Processing your request for RFQ IDs: {', '.join(selected_rfq_ids)}. Sending details to your email..."
        )

    async def _generate_email_status_response(self, context: Dict[str, Any]) -> str:
        """Generate response with email sending status."""
        return await self._generate_common_seller_response(
            "rfq_email_status", "general_assistance", context, self._get_email_status_fallback(context)
        )

    async def _generate_invalid_rfq_response(self, context: Dict[str, Any]) -> str:
        """Generate response for invalid RFQ selection."""
        available_rfqs = context.get("available_rfqs", [])
        return await self._generate_common_seller_response(
            "invalid_rfq_selection", "clarification", context,
            f"I couldn't find valid RFQ IDs in your message. Please select from: {', '.join(available_rfqs)}"
        )

    async def _generate_invalid_plan_response(self, context: Dict[str, Any]) -> str:
        """Generate response for invalid plan selection."""
        return await self._generate_common_seller_response(
            "invalid_plan_selection", "clarification", context,
            "Please select a valid plan from the options above. You can type the plan name or number."
        )

    async def _generate_general_seller_response(self, context: Dict[str, Any]) -> str:
        """Generate response for general seller queries."""
        return await self._generate_common_seller_response(
            "general_seller_response", "general_assistance", context,
            "I'm here to help! You can request RFQ details, view subscription plans, or ask any questions about our services."
        )

    # Add these methods to the ResponseHelpers class

    async def _generate_general_affirmative_response(self, context: Dict[str, Any]) -> str:
        """Generate response for general affirmative responses (contextual 'yes')."""
        return await self._generate_common_seller_response(
            "general_affirmative_response", "clarification", context,
            "Great! I can help you with:\n\n• View available RFQs in your category\n• Check subscription plans\n• Answer any questions\n\nWhat would you like to do?"
        )

    async def _generate_contextual_plan_request_response(self, context: Dict[str, Any]) -> str:
        """Generate response when seller contextually requests plans (like saying 'yes' to plan offer)."""
        return await self._generate_common_seller_response(
            "contextual_plan_request", "general_assistance", context,
            "Perfect! Let me show you our subscription plans. One moment while I fetch the latest options for you..."
        )

    async def _generate_ambiguous_seller_response(self, context: Dict[str, Any]) -> str:
        """Generate response for ambiguous seller messages."""
        return await self._generate_common_seller_response(
            "ambiguous_seller_response", "clarification", context,
            "I want to make sure I understand correctly. Could you clarify what you'd like help with?\n\nI can assist with:\n• RFQ details and access\n• Subscription plans\n• General questions\n\nWhat would be most helpful?"
        )

    # Update the main generate_seller_contextual_response method to handle new workflow states


    # Also add a method to handle RFQ selection prompts
    async def _generate_rfq_selection_prompt(self, context: Dict[str, Any]) -> str:
        """Generate prompt asking seller to select specific RFQs."""
        available_rfqs = context.get("available_rfqs", [])
        return await self._generate_common_seller_response(
            "rfq_selection_needed", "clarification", context,
            f"Great! Please specify which RFQ IDs you'd like details for.\n\nAvailable: {', '.join(str(rfq) for rfq in available_rfqs)}\n\nExample: Type '23112' or '23112, 23087'\n\nEach request uses 1 credit."
        )

    async def _generate_error_response(self, context: Dict[str, Any]) -> str:
        """Generate error response."""
        return await self._generate_common_seller_response(
            context.get("workflow_state", "error"), "error_handling", context,
            "I apologize for the technical issue. Please try again or contact support@procurev.com for assistance."
        )

    async def _generate_fallback_seller_response(self, context: Dict[str, Any]) -> str:
        """Generate fallback response for unknown states."""
        return await self._generate_common_seller_response(
            context.get("workflow_state", "unknown"), "general_assistance", context,
            self._get_fallback_message(context.get("workflow_state"), context.get("user_role"))
        )

    # Fallback methods for when AI generation fails
    def _get_fallback_message(self, workflow_state: str, user_role: str = None) -> Dict[str, Any]:
        """Get appropriate fallback message based on workflow state and user role."""
        fallbacks = {
            "display_rfqs_to_seller": "Here are the available RFQs in your category. Please let me know which ones interest you.",
            "display_rfqs_no_credits": "You have 0 credits available. Please choose a subscription plan to access RFQ details.",
            "no_credits_available": "You need credits to access RFQ details. Would you like to see our subscription plans?",
            "show_subscription_plans": "Here are our subscription plans. Please choose one to continue.",
            "payment_link_generated": "Your payment link has been generated. Please complete the payment to activate your subscription.",
            "rfq_email_processing": "Processing your RFQ request. Details will be sent to your email shortly.",
            "rfq_email_status": "Your RFQ details have been processed. Please check your email.",
            "error": "I apologize for the technical issue. Please contact support@procurev.com."
        }
        
        default_message = fallbacks.get(workflow_state)
        if default_message:
            return {"message": default_message, "buttons": None}
            
        # For default fallback, show appropriate menu based on user role
        if user_role and user_role.lower() == "buyer":

            buttons_config = [
                {"id": "new_rfq", "title": "Raise a new RFQ"},
                {"id": "rfq_status", "title": "Check your previous RFQs"},
                {"id": "contact_support", "title": "Any other support you need"}
            ]
            message = (
                "What can I assist you with today?"
            )
            return {"message": message, "buttons": buttons_config}
        elif user_role and user_role.lower() == "seller":
            buttons_config = [
                {"id": "check_rfq_status", "title": "Check RFQ status"},
                {"id": "get_support", "title": "Get other support"}
            ]
            message = (
                "What would you like to do today?"
            )
            return {"message": message, "buttons": buttons_config}
        else:
            return {"message": "How can I help you with your procurement needs today?", "buttons": None}

    def _get_rfq_display_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for RFQ display."""
        rfqs = context.get("rfqs", [])
        credits = context.get("credits_available", 0)
        total = context.get("total_count", 0)

        message = f"📋 You have {total} active RFQs in your category.\n"
        message += f"💳 Credits available: {credits}\n\n"
        message += "Latest RFQs:\n"

        for i, rfq in enumerate(rfqs[:3], 1):
            rfq_id = rfq.get("rfq_id", "N/A")
            location = rfq.get("location", "N/A")
            date = rfq.get("submission_date", "N/A")
            message += f"{i}. RFQ {rfq_id}\n   📍 {location}\n   📅 {date}\n\n"

        message += "Type the RFQ IDs you want to receive via email.\nExample: '23112' or '23112, 23087'"
        return message

    def _get_no_credits_rfq_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for no credits RFQ display."""
        rfqs = context.get("rfqs", [])
        total = context.get("total_count", 0)

        message = f"📋 You have {total} active RFQs in your category.\n"
        message += "💳 Credits available: 0\n\n"
        message += "Latest RFQs:\n"

        for i, rfq in enumerate(rfqs[:3], 1):
            rfq_id = rfq.get("rfq_id", "N/A")
            location = rfq.get("location", "N/A")
            date = rfq.get("submission_date", "N/A")
            message += f"{i}. RFQ {rfq_id}\n   📍 {location}\n   📅 {date}\n\n"

        message += "⚠️ You don't have credits to access RFQ details.\n"
        message += "Would you like to see our subscription plans?"
        return message

    def _get_subscription_plans_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for subscription plans display."""
        plans = context.get("plans", [])

        message = "💼 Available Subscription Plans:\n\n"
        for i, plan in enumerate(plans, 1):
            name = plan.get("name", "Plan")
            price = plan.get("price", 0)
            rfq_count = plan.get("rfq_count", 0)
            message += f"{i}. {name} Plan - ₹{price}\n   📊 {rfq_count} RFQ requests\n\n"

        message += "Please choose a plan by typing the plan name or number."
        return message

    def _get_payment_link_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for payment link response."""
        selected_plan = context.get("selected_plan", {})
        payment_link = context.get("payment_link", "")

        plan_name = selected_plan.get("name", "Selected")
        price = selected_plan.get("price", 0)

        message = f"✅ {plan_name} Plan Selected - ₹{price}\n\n"
        message += "💳 Payment Link:\n"
        message += payment_link + "\n\n"
        message += "🔒 Secure payment via Razorpay\n"
        message += "After payment completion, your credits will be activated immediately."
        return message

    def _get_email_status_fallback(self, context: Dict[str, Any]) -> str:
        """Fallback for email status response."""
        successful = context.get("successful_emails", 0)
        total = context.get("total_requested", 0)

        if successful == total:
            return f"✅ Successfully sent all {successful} RFQ details to your email!"
        elif successful > 0:
            failed = total - successful
            return f"✅ Sent {successful} RFQ details successfully.\n❌ {failed} failed to send.\nPlease contact support if needed."
        else:
            return "❌ Unable to send RFQ details via email. Please contact support@procurev.com for assistance."

    async def generate_completion_response(self, rfq_schema, context: dict) -> str:
        """Generate completion response using OpenAI."""
        try:
            rfq_data = rfq_schema.dict() if hasattr(rfq_schema, 'dict') else {}
            return self.openai_service.generate_completion_response(rfq_data, context)
        except Exception as e:
            logger.error(f"Error generating completion response: {e}")
            return "Excellent! Your RFQ is now complete. I'll process this request and get back to you soon."
    
    async def generate_clarification_response(self, questions: list, completeness: float, context: dict, chat_summaries: list = None) -> str:
        """Generate clarification response using enhanced entity display format."""
        try:
            # Get date validation errors (already formatted)
            date_validation_errors = self._extract_date_validation_errors(context)
            
            # Get pincode validation errors (already formatted)
            pincode_validation_errors = self._extract_pincode_validation_errors(context)

            # Combine only the actual questions
            all_questions = date_validation_errors + pincode_validation_errors + questions

            if not all_questions:
                return "Thank you for the information! Let me process your RFQ."

            # Check if we have extracted entities to show
            extracted_entities = context.get("extracted_entities", [])
            if not isinstance(extracted_entities, list):
                extracted_entities = [extracted_entities] if extracted_entities else []

            # Debug logging
            logger.info(f"Clarification response - extracted_entities: {extracted_entities}")
            logger.info(f"Clarification response - all_questions: {all_questions}")
            
            # Use enhanced formatting if we have entities
            has_entities_with_descriptions = extracted_entities and any(entity.get('description') for entity in extracted_entities if isinstance(entity, dict))
            logger.info(f"Clarification response - has_entities_with_descriptions: {has_entities_with_descriptions}")
            
            if has_entities_with_descriptions:
                logger.info("Using enhanced RFQ entities formatting")
                return self.format_rfq_entities_message(extracted_entities, all_questions)
            
            # Fallback to simple format
            logger.info("Using simple format for clarification response")
            questions_text = "\n".join(f"• {q}" for q in all_questions)
            
            if completeness > 0:
                return f"Thank you for that information!\n\nPlease provide the following:\n\n{questions_text}"
            else:
                return f"Please provide the following:\n\n{questions_text}"

        except Exception as e:
            logger.error(f"Error generating clarification response: {e}")
            questions_text = "\n".join(f"• {q}" for q in questions)
            return f"I need a few more details to complete your RFQ:\n\n{questions_text}"
    
    async def generate_rfq_summary_and_confirmation(self, rfq_schema, context: dict, chat_summaries: list = None) -> str:
        """Generate RFQ summary and ask for confirmation using OpenAI with optional chat summary context."""
        try:
            # Use OpenAI to generate the summary and confirmation naturally
            summary_context = {
                **context,
                "conversation_stage": "rfq_summary_confirmation",
                "rfq_data": rfq_schema.dict() if hasattr(rfq_schema, 'dict') else {},
                "action_needed": "Generate RFQ summary and ask for user confirmation"
            }
            
            # Add chat summaries if available
            if chat_summaries:
                summary_context["chat_summaries"] = chat_summaries
                summary_context["has_historical_context"] = True
                print(f"ResponseHelpers: Enhanced RFQ summary context with {len(chat_summaries)} chat summaries")
            
            # Use specialized RFQ confirmation generation
            return self.openai_service.generate_rfq_confirmation(
                rfq_schema.dict() if hasattr(rfq_schema, 'dict') else {},
                summary_context
            )
            
        except Exception as e:
            logger.error(f"Error generating RFQ summary: {e}")
            return "Your RFQ is ready! Would you like me to create it? Reply 'Yes' to confirm or 'No' to make changes."
    
    
    async def generate_rfq_result_response(self, gmt_result: dict, context: dict) -> str:
        """Generate response for RFQ creation result using OpenAI."""
        success = gmt_result.get("success", False)
        
        if success:
            # For successful RFQ creation, return simple thank you message without percentage
            backend_ref = gmt_result.get("rfq_id")
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
    
    def generate_registration_confirmation(self, entities: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Generate registration confirmation message using OpenAI."""
        try:
            return self.openai_service.generate_contextual_response(
                context, 
                ["Please confirm your registration details"], 
                "registration_confirmation"
            )
        except Exception as e:
            logger.error(f"Error generating registration confirmation: {e}")
            # Fallback to simple confirmation
            field_labels = {
                "name": "Name", "companyName": "Company", "email": "Email", 
                "pincode": "Pincode", "gstin": "GSTIN", "products_services": "Products/Services"
            }
            
            confirmation_lines = []
            for field, value in entities.items():
                if value and field in field_labels:
                    confirmation_lines.append(f"• {field_labels[field]}: {value}")
            
            return (
                "Please confirm your registration details:\n\n" +
                "\n".join(confirmation_lines) +
                "\n\nIs this information correct? Reply 'Yes' to confirm or provide corrections."
            )
    
    async def generate_registration_clarification(self, missing_fields: List[str], completeness: float, context: Dict[str, Any]) -> str:
        """Generate registration clarification message using OpenAI."""
        try:
            return self.openai_service.generate_clarification_response(
                missing_fields, completeness, context
            )
        except Exception as e:
            logger.error(f"Error generating registration clarification: {e}")
            # Fallback to simple clarification
            field_mapping = {
                "name": "What's your full name?",
                "companyName": "What's your company name?",
                "email": "What's your organization email address?",
                "pincode": "What's your pincode?",
                "gstin": "What's your GSTIN number?",
                "products_services": "What products or services do you offer?"
            }
            
            questions = []
            for field in missing_fields:
                if field in field_mapping:
                    questions.append(field_mapping[field])
            
            if questions:
                return "I still need a few more details:\n\n" + "\n".join(f"• {q}" for q in questions)
            else:
                return "Please provide the remaining registration details."
    
    def format_rfq_entities_message(self, extracted_entities: List[Dict[str, Any]], missing_fields: List[str]) -> str:
        """Format RFQ message showing extracted entities and asking for missing details.
        
        Args:
            extracted_entities: List of extracted product entities
            missing_fields: List of missing field descriptions
            
        Returns:
            Formatted message string
        """
        try:
            logger.info(f"Formatting RFQ entities message with {len(extracted_entities)} entities and {len(missing_fields)} missing fields")
            
            # Extract global fields from entities if they exist
            global_fields = {}
            if extracted_entities:
                first_entity = extracted_entities[0] if isinstance(extracted_entities[0], dict) else {}
                delivery_date = first_entity.get('deliveryDate')
                # Format date for display if it exists
                if delivery_date:
                    try:
                        from datetime import datetime
                        if isinstance(delivery_date, str) and len(delivery_date) == 10:  # YYYY-MM-DD format
                            date_obj = datetime.strptime(delivery_date, '%Y-%m-%d')
                            delivery_date = date_obj.strftime('%d %b %Y')  # Format as "28 Oct 2025"
                    except:
                        pass  # Keep original format if parsing fails
                
                global_fields = {
                    'deliveryDate': delivery_date,
                    'state': first_entity.get('state'),
                    'city': first_entity.get('city'),
                    'pincode': first_entity.get('pincode')
                }
            
            # Use the enhanced formatter with global fields if any exist
            if any(global_fields.values()):
                from app.utils.rfq_message_formatter import format_rfq_entities_with_global_fields
                result = format_rfq_entities_with_global_fields(extracted_entities, global_fields, missing_fields)
            else:
                result = format_rfq_entities_message(extracted_entities, missing_fields)
            
            logger.info(f"Formatted message result: {result[:100]}...")
            return result
        except Exception as e:
            logger.error(f"Error formatting RFQ entities message: {e}")
            return format_simple_missing_fields_message(missing_fields)

    def _extract_date_validation_errors(self, context: dict) -> list:
        """Extract date validation error messages from context."""
        date_errors = []
        
        # Check extracted entities for date validation errors
        extracted_entities = context.get("extracted_entities", [])
        if isinstance(extracted_entities, list):
            for entity in extracted_entities:
                if isinstance(entity, dict) and entity.get("date_validation_error"):
                    date_errors.append(entity["date_validation_error"])
        elif isinstance(extracted_entities, dict) and extracted_entities.get("date_validation_error"):
            date_errors.append(extracted_entities["date_validation_error"])
        
        # Check products in context
        if context.get("products"):
            products = context["products"]
            if isinstance(products, list):
                for product in products:
                    if isinstance(product, dict) and product.get("date_validation_error"):
                        date_errors.append(product["date_validation_error"])
        
        # Remove duplicate error messages while preserving order
        return list(dict.fromkeys(date_errors))

    def _extract_pincode_validation_errors(self, context: dict) -> list:
        """Extract pincode validation error messages from context."""
        pincode_errors = []
        
        # Check extracted entities for pincode validation errors
        extracted_entities = context.get("extracted_entities", [])
        if isinstance(extracted_entities, list):
            for entity in extracted_entities:
                if isinstance(entity, dict) and entity.get("pincode_validation_error"):
                    pincode_errors.append(entity["pincode_validation_error"])
        elif isinstance(extracted_entities, dict) and extracted_entities.get("pincode_validation_error"):
            pincode_errors.append(extracted_entities["pincode_validation_error"])
        
        # Check products in context
        if context.get("products"):
            products = context["products"]
            if isinstance(products, list):
                for product in products:
                    if isinstance(product, dict) and product.get("pincode_validation_error"):
                        pincode_errors.append(product["pincode_validation_error"])
        
        # Remove duplicate error messages while preserving order
        return list(dict.fromkeys(pincode_errors))