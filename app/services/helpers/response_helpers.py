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
            elif workflow_state in ["error", "credit_check_error", "rfq_fetch_error", "plan_fetch_error",
                                    "payment_link_error"]:
                return await self._generate_error_response(context)
            else:
                return await self._generate_fallback_seller_response(context)

        except Exception as e:
            logger.error(f"Error generating seller contextual response: {e}")
            return self._get_fallback_message(context.get("workflow_state"))

    async def _generate_rfq_display_response(self, context: Dict[str, Any]) -> str:
        """Generate response for displaying RFQs to seller with credits."""
        try:
            seller_info = context.get("seller_info", {})
            rfqs = context.get("rfqs", [])
            total_count = context.get("total_count", 0)
            credits_available = context.get("credits_available", 0)

            prompt = f"""
            Generate a professional WhatsApp message for a seller showing available RFQs.

            Context:
            - Seller has {credits_available} credits available
            - Total RFQs in category: {total_count}
            - Showing latest 3 RFQs: {rfqs}
            - Seller can request RFQ details via email using credits

            Requirements:
            - Show the RFQ count and credit balance
            - List the 3 RFQs with ID, location, and date
            - Explain they can request RFQ details by typing RFQ IDs
            - Keep tone professional and helpful

            Format: Direct WhatsApp message, no quotes or extra formatting.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating RFQ display response: {e}")
            return self._get_rfq_display_fallback(context)

    async def _generate_no_credits_rfq_response(self, context: Dict[str, Any]) -> str:
        """Generate response for displaying RFQs when seller has no credits."""
        try:
            rfqs = context.get("rfqs", [])
            total_count = context.get("total_count", 0)

            prompt = f"""
            Generate a WhatsApp message for a seller with 0 credits viewing RFQs.

            Context:
            - Seller has 0 credits
            - Total RFQs available: {total_count}
            - Showing latest 3 RFQs: {rfqs}
            - Need to upgrade to access RFQ details

            Requirements:
            - Show the available RFQs (ID, location, date)
            - Explain they have 0 credits
            - Mention they need to choose a subscription plan to access RFQ details
            - Ask if they want to see subscription plans
            - Keep encouraging tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating no credits RFQ response: {e}")
            return self._get_no_credits_rfq_fallback(context)

    async def _generate_subscription_plans_response(self, context: Dict[str, Any]) -> str:
        """Generate response showing subscription plans."""
        try:
            plans = context.get("plans", [])

            prompt = f"""
            Generate a WhatsApp message showing subscription plans to a seller.

            Context:
            - Available plans: {plans}
            - Seller wants to upgrade to access RFQs

            Requirements:
            - List each plan with name, price, and RFQ count
            - Explain benefits of each plan
            - Ask seller to choose a plan by typing plan name
            - Keep persuasive but professional tone
            
            Format: Direct WhatsApp message with numbered list.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating subscription plans response: {e}")
            return self._get_subscription_plans_fallback(context)

    async def _generate_no_credits_response(self, context: Dict[str, Any]) -> str:
        """Generate response showing subscription plans."""
        try:
            plans = context.get("plans", [])
            credits_available = context.get("credits_available", 0)

            prompt = f"""
            Generate a WhatsApp message showing subscription plans to a seller.

            Context:
            - Available Credit: {credits_available}
            - Seller wants to upgrade to access RFQs

            Requirements:
            - Show current credit avaiable also ask to upgrade their credits
            - List each plan with name, price, and RFQ count
            - Explain benefits of each plan
            - Ask seller to choose a plan by typing plan name
            - Keep persuasive but professional tone

            Format: Direct WhatsApp message with numbered list.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating subscription plans response: {e}")
            return self._get_subscription_plans_fallback(context)

    async def _generate_payment_link_response(self, context: Dict[str, Any]) -> str:
        """Generate response with payment link."""
        try:
            selected_plan = context.get("selected_plan", {})
            payment_link = context.get("payment_link", "")

            prompt = f"""
            Generate a WhatsApp message with payment link for subscription.

            Context:
            - Selected plan: {selected_plan.get('name')} - ₹{selected_plan.get('price')}
            - Payment link: {payment_link}
            - Plan includes {selected_plan.get('rfq_count')} RFQ requests

            Requirements:
            - Confirm the selected plan and price
            - Provide the payment link
            - Mention payment is secure via Razorpay
            - Explain what happens after payment
            - Set expectations about timeline
            - Professional and reassuring tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating payment link response: {e}")
            return self._get_payment_link_fallback(context)

    async def _generate_email_processing_response(self, context: Dict[str, Any]) -> str:
        """Generate response acknowledging RFQ email processing."""
        try:
            selected_rfq_ids = context.get("selected_rfq_ids", [])

            prompt = f"""
            Generate an acknowledgment message for RFQ email processing.

            Context:
            - Seller requested RFQ IDs: {selected_rfq_ids}
            - Processing email requests

            Requirements:
            - Acknowledge the RFQ ID requests
            - Mention sending detailed information to email
            - Professional and efficient tone
            - Brief message

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating email processing response: {e}")
            return f"Thank you! Processing your request for RFQ IDs: {', '.join(selected_rfq_ids)}. Sending details to your email..."

    async def _generate_email_status_response(self, context: Dict[str, Any]) -> str:
        """Generate response with email sending status."""
        try:
            successful_emails = context.get("successful_emails", 0)
            total_requested = context.get("total_requested", 0)
            email_results = context.get("email_results", [])

            prompt = f"""
            Generate a status message for RFQ email sending results.

            Context:
            - Total requested: {total_requested}
            - Successfully sent: {successful_emails}
            - Detailed results: {email_results}

            Requirements:
            - Report success/failure status clearly
            - If some failed, list which RFQ IDs failed
            - Provide support contact if issues occurred
            - Professional and helpful tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating email status response: {e}")
            return self._get_email_status_fallback(context)

    async def _generate_invalid_rfq_response(self, context: Dict[str, Any]) -> str:
        """Generate response for invalid RFQ selection."""
        try:
            available_rfqs = context.get("available_rfqs", [])
            user_message = context.get("user_message", "")

            prompt = f"""
            Generate a clarification message for invalid RFQ selection.

            Context:
            - User message: {user_message}
            - Available RFQ IDs: {available_rfqs}
            - User's selection was not recognized

            Requirements:
            - Explain the issue politely
            - Show available RFQ IDs clearly
            - Helpful and patient tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating invalid RFQ response: {e}")
            return f"I couldn't find valid RFQ IDs in your message. Please select from: {', '.join(available_rfqs)}"

    async def _generate_invalid_plan_response(self, context: Dict[str, Any]) -> str:
        """Generate response for invalid plan selection."""
        try:
            available_plans = context.get("available_plans", [])

            prompt = f"""
            Generate a clarification message for invalid plan selection.

            Context:
            - Available plans: {available_plans}
            - User's selection was not recognized

            Requirements:
            - Explain the issue politely
            - Re-list available plans clearly
            - Give examples of correct selection
            - Encouraging tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating invalid plan response: {e}")
            return "Please select a valid plan from the options above. You can type the plan name or number."

    async def _generate_general_seller_response(self, context: Dict[str, Any]) -> str:
        """Generate response for general seller queries."""
        try:
            message = context.get("message", "")
            credits_available = context.get("credits_available", 0)

            prompt = f"""
            Generate a helpful response to seller's general query.

            Context:
            - Seller message: {message}
            - Credits available: {credits_available}
            - General seller assistance needed

            Requirements:
            - Address the query helpfully
            - Mention available options (RFQ access, plans, etc.)
            - Professional and supportive tone
            - Offer specific next steps

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating general seller response: {e}")
            return "I'm here to help! You can request RFQ details, view subscription plans, or ask any questions about our services."

    # Add these methods to the ResponseHelpers class

    async def _generate_general_affirmative_response(self, context: Dict[str, Any]) -> str:
        """Generate response for general affirmative responses (contextual 'yes')."""
        try:
            seller_info = context.get("seller_info", {})
            credits_available = context.get("credits_available", 0)
            ai_analysis = context.get("ai_analysis", {})

            prompt = f"""
            Generate a helpful response to a seller's affirmative response that needs context.

            Context:
            - Seller said something like "yes" but context is unclear
            - Seller info: {seller_info}
            - Credits available: {credits_available}
            - AI analysis: {ai_analysis.get('reasoning', 'Context unclear')}

            Requirements:
            - Acknowledge their positive response
            - Offer clear options: view RFQs, subscription plans, or ask questions
            - Be helpful and guide them to next steps
            - Professional and friendly tone
            - Present options clearly

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating general affirmative response: {e}")
            return "Great! I can help you with:\n\n• View available RFQs in your category\n• Check subscription plans\n• Answer any questions\n\nWhat would you like to do?"

    async def _generate_contextual_plan_request_response(self, context: Dict[str, Any]) -> str:
        """Generate response when seller contextually requests plans (like saying 'yes' to plan offer)."""
        try:
            seller_info = context.get("seller_info", {})
            ai_analysis = context.get("ai_analysis", {})

            prompt = f"""
            Generate a response acknowledging seller's interest in subscription plans.

            Context:
            - Seller responded positively to subscription plan offer
            - Seller info: {seller_info}
            - AI detected plan interest: {ai_analysis.get('reasoning', 'Contextual plan request')}

            Requirements:
            - Acknowledge their interest in plans
            - Mention you're fetching the latest plans
            - Professional and encouraging tone
            - Brief transition message

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating contextual plan request response: {e}")
            return "Perfect! Let me show you our subscription plans. One moment while I fetch the latest options for you..."

    async def _generate_ambiguous_seller_response(self, context: Dict[str, Any]) -> str:
        """Generate response for ambiguous seller messages."""
        try:
            message = context.get("message", "")
            seller_info = context.get("seller_info", {})
            credits_available = context.get("credits_available", 0)
            ai_analysis = context.get("ai_analysis", {})

            prompt = f"""
            Generate a clarification response for an ambiguous seller message.

            Context:
            - Seller message: "{message}"
            - Credits available: {credits_available}
            - AI confidence: {ai_analysis.get('confidence', 'low')}%
            - AI reasoning: {ai_analysis.get('reasoning', 'Message unclear')}

            Requirements:
            - Acknowledge their message politely
            - Ask for clarification in a helpful way
            - Offer specific options they can choose from
            - Mention available services (RFQ access, plans, support)
            - Professional and patient tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating ambiguous seller response: {e}")
            return f"I want to make sure I understand correctly. Could you clarify what you'd like help with?\n\nI can assist with:\n• RFQ details and access\n• Subscription plans\n• General questions\n\nWhat would be most helpful?"

    # Update the main generate_seller_contextual_response method to handle new workflow states


    # Also add a method to handle RFQ selection prompts
    async def _generate_rfq_selection_prompt(self, context: Dict[str, Any]) -> str:
        """Generate prompt asking seller to select specific RFQs."""
        try:
            available_rfqs = context.get("available_rfqs", [])
            credits_available = context.get("credits_available", 0)

            prompt = f"""
            Generate a message asking seller to specify which RFQs they want.

            Context:
            - Seller showed interest in RFQ details
            - Credits available: {credits_available}
            - Available RFQs: {available_rfqs}
            - Need them to specify RFQ IDs

            Requirements:
            - Acknowledge their interest
            - Remind them of available RFQ IDs
            - Ask them to specify which ones they want
            - Give example format (e.g., "23112" or "23112, 23087")
            - Mention credit usage
            - Encouraging tone

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating RFQ selection prompt: {e}")
            rfq_list = ", ".join(str(rfq) for rfq in available_rfqs)
            return f"Great! Please specify which RFQ IDs you'd like details for.\n\nAvailable: {rfq_list}\n\nExample: Type '23112' or '23112, 23087'\n\nEach request uses 1 credit."

    async def _generate_error_response(self, context: Dict[str, Any]) -> str:
        """Generate error response."""
        try:
            workflow_state = context.get("workflow_state", "error")
            error_message = context.get("error_message", "")

            prompt = f"""
            Generate a helpful error message for seller.

            Context:
            - Error type: {workflow_state}
            - Technical error: {error_message}

            Requirements:
            - Apologize for the issue
            - Explain what went wrong in simple terms
            - Provide alternative actions or support contact
            - Professional and reassuring tone
            - Don't show technical details to user

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating error response: {e}")
            return "I apologize for the technical issue. Please try again or contact support@procurev.com for assistance."

    async def _generate_fallback_seller_response(self, context: Dict[str, Any]) -> str:
        """Generate fallback response for unknown states."""
        try:
            workflow_state = context.get("workflow_state", "unknown")

            prompt = f"""
            Generate a helpful fallback message for seller.

            Context:
            - Workflow state: {workflow_state}
            - Need general seller assistance

            Requirements:
            - Acknowledge the interaction
            - Offer available services (RFQ access, plans, support)
            - Professional and helpful tone
            - Provide clear next steps

            Format: Direct WhatsApp message.
            """

            response = self.openai_service.generate_response(prompt, context)
            return response.strip()

        except Exception as e:
            logger.error(f"Error generating fallback seller response: {e}")
            return "I'm here to help with your RFQ needs. You can request RFQ details, view subscription plans, or contact support@procurev.com."

    # Fallback methods for when AI generation fails
    def _get_fallback_message(self, workflow_state: str) -> str:
        """Get appropriate fallback message based on workflow state."""
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
        return fallbacks.get(workflow_state, "How can I help you with your RFQ needs today?")

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
        """Generate clarification response using OpenAI with optional chat summary context."""
        try:
            # Enhance context with chat summaries if available
            enhanced_context = context.copy()
            if chat_summaries:
                enhanced_context["chat_summaries"] = chat_summaries
                enhanced_context["has_historical_context"] = True
                print(f"ResponseHelpers: Enhanced clarification context with {len(chat_summaries)} chat summaries")
            
            return self.openai_service.generate_clarification_response(questions, completeness, enhanced_context)
        except Exception as e:
            logger.error(f"Error generating clarification response: {e}")
            questions_text = "\\n".join(f"• {q}" for q in questions)
            return f"I need a few more details to complete your RFQ:\\n\\n{questions_text}"
    
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