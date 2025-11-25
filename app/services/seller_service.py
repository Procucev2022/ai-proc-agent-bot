"""
Enhanced Seller Service with Complete Workflow Management.

This implementation provides the complete seller workflow including:
- Credit-based RFQ access
- Subscription plan management
- Payment link generation
- Proper workflow state management
- AI-generated contextual responses
"""

import logging
import asyncio
from app.utils.datetime_utils import utc_now
from typing import Dict, Any, List
from app.models import WorkflowType, ConversationSession, User
from app.services.whatsapp_service import WhatsAppService
from app.services.workflow_manager import WorkflowManager
from app.procucev_apis.seller_apis import SellerAPIService
from app.config import get_settings
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.session_management_service import SessionManagementService
from app.database import SessionLocal, DatabaseManager
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
from app.services.rfq_status_service import RFQStatusService

logger = logging.getLogger(__name__)


class SellerService:
    """
    Enhanced Seller Service for complete seller workflow management.

    Handles:
    - Seller identification and registration
    - Credit-based RFQ access
    - RFQ listing and email sending
    - Subscription plan management
    - Payment processing
    - Proper workflow state management
    """

    def __init__(self, whatsapp_service: WhatsAppService = None,
                 session_manager: SessionManagementService = None,
                 db_session=None):
        """
        Initialize SellerService with optional dependencies.

        Args:
            whatsapp_service: WhatsApp service instance (optional).
            session_manager: Session management service instance (optional).
            db_session: Database session (optional). If provided, will be passed to DatabaseManager.
        """
        # Use provided whatsapp_service or create new instance as fallback
        self.whatsapp_service = whatsapp_service or WhatsAppService()

        self.db_manager = DatabaseManager(session=db_session)
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        
        # Use provided session_manager or create new instance as fallback
        if session_manager:
            self.session_manager = session_manager
        else:
            self.session_manager = SessionManagementService(
                self.db_manager, self.whatsapp_service,
                self.chat_summary_service, self.daily_summary_service
            )
        
        self.seller_api_service = SellerAPIService()
        self.settings = get_settings()
        self.openai_service = OpenAIService()
        self.rfq_status_service = RFQStatusService(
            whatsapp_service=self.whatsapp_service,
            session_manager=self.session_manager,
            db_session=db_session
        )
        self.response_helpers = ResponseHelpers(self.openai_service)

    async def handle_seller_workflow(self, user: User, session: ConversationSession, message: str ,intent_result: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Main seller workflow handler with complete state management.

        Args:
            user: User object
            session: Current conversation session
            message: Incoming message

        Returns:
            Dict containing workflow response and next steps
        """
        try:
            # Get current workflow state
            workflow_state = session.workflow_state or {}
            current_state = workflow_state.get("seller_workflow_state")

            logger.info(f"Seller workflow - Current state: {current_state}")

            # ------------------------------
            # Route workflow based on existing state
            # ------------------------------

            # Step 1: Seller is selecting an RFQ
            if (
                    current_state == "awaiting_rfq_selection"
                    or (
                        str(session.workflow_type) == "seller_rfq_view"
                        and intent_result
                        and intent_result.get("intent") == "rfq_status_check"
                    )
                ):

                return await self._handle_rfq_selection_response(user, session, message)

            # Step 2: Seller is selecting subscription plan
            elif current_state == "awaiting_plan_selection":
                return await self._handle_plan_selection_response(user, session, message)

            # Step 3: Seller is providing general follow-up responses
            # AI Based Flow decided ( seller intent + ochrestartor )
            elif current_state == "awaiting_general_response":
                return await self._handle_general_seller_response(user, session, message)

            else:
                # Step 4. Initial workflow entry point. If the seller is entering the workflow for the first time,
                # we begin by displaying available RFQs.
                return await self._display_rfqs_to_seller(user, session, message)

        except Exception as e:
                logger.error(f"Error in seller workflow: {e}")
                return await self._handle_workflow_error(user, session, str(e))

    async def _handle_initial_seller_flow(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Handle initial seller flow with credit checking and RFQ listing."""
        try:
            # Step 1: Fetch active RFQs for seller's category
            rfq_result = await self._fetch_seller_rfqs(user.org_id)

            if not rfq_result.get("success"):
                return await self._handle_rfq_fetch_error(user, session)

            # Step 2: Display RFQs with appropriate context based on credits
            return await self._display_rfqs_to_seller(user, session, rfq_result)

        except Exception as e:
            logger.error(f"Error in initial seller flow: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _display_rfqs_to_seller(self, user: User, session: ConversationSession,message:str) -> Dict[str, Any]:
        """Display RFQs to seller with credit-based context."""
        try:
            # Step 1: Fetch active RFQs for seller's category
            rfq_result = await self._fetch_seller_rfqs(user.org_id)

            if not rfq_result.get("success"):
                return await self._handle_rfq_fetch_error(user, session)

            # Step 2: Fetch seller current credits

            credits_result = await self._check_seller_credits(user.org_id)

            rfqs = rfq_result.get("rfqs")
            total_count = rfq_result.get("total_count")
            credits_available = credits_result.get("credits_available")

            # Prepare context for AI response generation
            context = {
                "workflow_state": "display_rfqs_to_seller",
                "rfqs": rfqs,
                "total_count": total_count,
                "credits_available": credits_available,
                "has_credits": credits_available > 0
            }

            # Generate contextual response based on credit status
            if credits_available > 0:
                # Show RFQs with credit info
                response_message = await self.response_helpers.generate_seller_contextual_response(context)
            else:
                # Show RFQs with subscription prompt
                context["workflow_state"] = "display_rfqs_no_credits"
                response_message = await self.response_helpers.generate_seller_contextual_response(context)

            # Update session workflow state
            WorkflowManager.set_workflow_type(session, WorkflowType.seller_rfq_view, caller="seller_service")
            session.workflow_state = {
                "seller_workflow_state": "awaiting_general_response"
            }

            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            # ADD THIS: Schedule end-of-flow reminder after 5 minutes for payment link
            asyncio.create_task(self._schedule_end_of_flow_reminder(user, session))

            return {
                "success": True,
                "workflow_step": "display_rfqs_to_seller",
                "message": response_message,
                "message_already_sent": False
            }

        except Exception as e:
            logger.error(f"Error displaying RFQs to seller: {e}")
            raise

    async def _handle_rfq_selection_response(self, user: User, session: ConversationSession,message: str) -> Dict[str, Any]:
        """Handle seller's RFQ selection when they have credits."""
        try:
            credits = await self._check_seller_credits(user.org_id)
            rfq_result = await self._fetch_seller_rfqs(user.org_id)

            if not rfq_result.get("success"):
                return await self._handle_rfq_fetch_error(user, session)

            rfqs = rfq_result.get("rfqs")
            credits_available= credits.get("credits_available")

            # Check if seller still has credits
            if credits_available <= 0:
                return await self._handle_no_credits_response(user, session)

            # Extract RFQ IDs from message
            selected_rfq_ids = await self._extract_rfq_ids_from_message(message, session)

            if not selected_rfq_ids:
                # Check if user is asking for plan upgrade or other query
                return await self._handle_general_seller_response(user, session, message)

            # Validate selected RFQ IDs
            available_rfqs =[rfq.get("rfq_id") for rfq in rfqs]
            valid_selections = [rfq_id for rfq_id in selected_rfq_ids if rfq_id in available_rfqs]

            if not valid_selections:
                return await self._handle_invalid_rfq_selection(user, session, message)

            # Process RFQ selections
            return await self._process_rfq_email_requests(user, session, valid_selections)

        except Exception as e:
            logger.error(f"Error handling RFQ selection: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _generate_ambiguous_seller_response(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Generate response for ambiguous seller messages."""
        try:
            response_message = await self.response_helpers.generate_seller_contextual_response({
                "workflow_state": "ambiguous_seller_response",
                **context
            })

            return {
                "success": True,
                "workflow_step": "awaiting_general_response",
                "message": response_message,
            }

        except Exception as e:
            logger.error(f"Error generating ambiguous seller response: {e}")
            return {
                "success": True,
                "workflow_step": "awaiting_general_response",
                "message": "I want to make sure I understand correctly. Could you clarify what you'd like help with?\n\nI can assist with:\n• RFQ details and access\n• Subscription plans\n• General questions\n\nWhat would be most helpful?"
            }


    async def _generate_general_seller_response(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Generate response for general seller queries."""
        try:
            response_message = await self.response_helpers.generate_seller_contextual_response({
                "workflow_state": "general_seller_response",
                **context
            })

            return {
                "success": True,
                "workflow_step": "general_seller_response",
                "message": response_message
            }

        except Exception as e:
            logger.error(f"Error generating general seller response: {e}")
            return {
                "success": True,
                "workflow_step": "general_seller_response",
                "message": "I'm here to help! You can request RFQ details, view subscription plans, or ask any questions about our services."
            }


    async def _handle_general_seller_response(self, user: User, session: ConversationSession,
                                              message: str) -> Dict[str, Any]:
        """Handle general seller responses using AI-powered intent classification."""
        try:
            # ADD THIS: Update activity timestamp to prevent unnecessary reminders
            session.workflow_state = session.workflow_state or {}
            session.workflow_state["last_activity_timestamp"] = utc_now().isoformat()
            workflow_state = session.workflow_state or {}
            # Build conversation context for AI analysis
            conversation_context = self._build_seller_conversation_context(session, message)

            print("conversation context", conversation_context)

            # Use AI to classify seller's intent with context awareness
            seller_intent = await self._classify_seller_intent(message, conversation_context, session)

            logger.info(f"Seller Intent is {seller_intent}")


            credits = await self._check_seller_credits(user.org_id)
            credits_available = credits.get("credits_available")

            # Route based on AI-classified intent
            intent_type = seller_intent.get("intent")
            confidence = seller_intent.get("confidence", 0)

            if intent_type == "plan_upgrade_request" and confidence > 0.7:
                return await self._handle_plan_upgrade_request(user, session, message)

            elif intent_type == "rfq_access_request" and confidence > 0.7:
                print("credits avaiable", credits_available)
                # Check if they have credits for RFQ access

                if credits_available <= 0:
                    return await self._handle_no_credits_response(user, session)
                else:
                    # They have credits, treat as RFQ selection attempt
                    return await self._handle_rfq_selection_response(user, session, message)

            elif intent_type == "rfq_status_check" and confidence > 0.7:
                return await self.rfq_status_service.handle_rfq_status_inquiry(user, message, session)

            elif intent_type == "general_question" and confidence > 0.7:
                context = {
                    "message": message,
                    "credits_available": credits_available,
                    "ai_analysis": seller_intent

                }

                return await self._generate_general_seller_response(context)

            elif intent_type == "affirmative_response" and confidence > 0.7:
                # Handle contextual "yes" responses based on previous bot message
                return await self._handle_contextual_affirmative_response(user, session, message, seller_intent)

            else:
                # Low confidence or unknown intent - generate contextual response
                context = {
                    "message":  message,
                    "credits_available": credits_available,
                    "ai_analysis":  seller_intent

                }
                return await self._generate_ambiguous_seller_response(context)

        except Exception as e:
            logger.error(f"Error handling general seller response: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _classify_seller_intent(self, message: str, conversation_context: Dict[str, Any],
                                      session: ConversationSession) -> Dict[str, Any]:
        """Use AI to classify seller's intent with conversation context."""
        try:
            # Get recent conversation history for context
            recent_messages = session.conversation_history["messages"][-5:] if session.conversation_history else []

            # Check if last bot message mentioned subscription plans
            last_bot_message = ""
            for msg in reversed(recent_messages):
                if msg.get("role") == "assistant":
                    last_bot_message = msg.get("content", "")
                    break

            # Prepare context for AI classification
            classification_context = {
                "current_message": message,
                "conversation_context": conversation_context,
                "recent_messages": recent_messages,
                "last_bot_message": last_bot_message,
                "seller_credits": conversation_context.get("workflow_state", {}).get("credits_available", 0),
                "workflow_state": conversation_context.get("workflow_state")
            }
            try:

                return await self.response_helpers.generate_seller_contextual_intent_response(classification_context)

            except Exception as e:
                logger.info("seller intent classification fallback logic triggered")
                logger.error(f"Error in seller intent classification fallback logic triggered: {e}")
                # Fallback to keyword-based classification
                return self._fallback_intent_classification(message, classification_context)

        except Exception as e:
            logger.error(f"Error seller intent classification: {e}")
            # Fallback to simple keyword matching
            return self._fallback_intent_classification(message, conversation_context)

    async def _handle_contextual_affirmative_response(self, user: User, session: ConversationSession,
                                                      message: str, seller_intent: Dict[str, Any]) -> Dict[str, Any]:
        """Handle 'yes' responses based on conversation context."""
        try:
            # Get context clues from AI analysis
            context_clues = seller_intent.get("context_clues", [])

            # Check what the last bot message was about
            recent_messages = session.conversation_history["messages"][-3:] if session.conversation_history else []
            last_bot_message = ""

            for msg in reversed(recent_messages):
                if msg.get("role") == "assistant":
                    last_bot_message = msg.get("content", "").lower()
                    break

            # Determine what they're saying "yes" to
            if any(keyword in last_bot_message for keyword in ["subscription", "plan", "upgrade", "choose"]):
                # They're saying yes to seeing subscription plans
                return await self._handle_plan_upgrade_request(user, session, message)

            elif any(keyword in last_bot_message for keyword in ["rfq", "details", "email"]):
                # They're confirming RFQ access request
                credits = await self._check_seller_credits(user.id)
                credits_available = credits.get("credits_available")
                if credits_available <= 0:
                    return await self._handle_no_credits_response(user, session)
                else:
                    # Treat as general RFQ interest, ask them to specify which RFQs
                    return await self._handle_rfq_selection_prompt(user, session)

            else:
                # Generic affirmative - provide helpful options
                return await self._handle_general_affirmative_response(user, session, seller_intent)

        except Exception as e:
            logger.error(f"Error handling contextual affirmative response: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _handle_general_affirmative_response(self, user: User, session: ConversationSession,
                                                   seller_intent: Dict[str, Any]) -> Dict[str, Any]:
        """Handle generic 'yes' responses by offering options."""
        try:
            context = {
                "workflow_state": "general_affirmative_response",
                "credits_available": session.workflow_state.get("credits_available", 0),
                "ai_analysis": seller_intent
            }

            response_message = await self.response_helpers.generate_seller_contextual_response(context)

            return {
                "success": True,
                "workflow_step": "general_affirmative_response",
                "message": response_message,
                "message_already_sent": False
            }

        except Exception as e:
            logger.error(f"Error handling general affirmative response: {e}")
            return await self._handle_workflow_error(user, session, str(e))



    def _fallback_intent_classification(self, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback intent classification using keyword matching."""
        message_lower = message.lower().strip()

        # Affirmative responses
        if message_lower in ["yes", "yeah", "yup", "sure", "ok", "okay", "please", "yep"]:
            return {
                "intent": "affirmative_response",
                "confidence": 90,
                "reasoning": "Direct affirmative keyword match",
                "context_clues": [message_lower]
            }

        # Plan upgrade keywords
        upgrade_keywords = ["upgrade", "plan", "subscription", "subscribe", "payment", "pay", "buy", "purchase"]
        if any(keyword in message_lower for keyword in upgrade_keywords):
            return {
                "intent": "plan_upgrade_request",
                "confidence": 80,
                "reasoning": "Plan upgrade keyword match",
                "context_clues": [kw for kw in upgrade_keywords if kw in message_lower]
            }

        # RFQ access keywords
        rfq_keywords = ["rfq", "request", "details", "information", "email", "send"]
        if any(keyword in message_lower for keyword in rfq_keywords):
            return {
                "intent": "rfq_access_request",
                "confidence": 75,
                "reasoning": "RFQ access keyword match",
                "context_clues": [kw for kw in rfq_keywords if kw in message_lower]
            }

        # Default to general question
        return {
            "intent": "general_question",
            "confidence": 50,
            "reasoning": "No specific intent detected",
            "context_clues": []
        }



    async def _handle_plan_upgrade_request(self, user: User, session: ConversationSession,
                                           message: str) -> Dict[str, Any]:
        """Handle seller's plan upgrade request."""
        try:
            # Fetch available subscription plans only if not already available
            plans_result = await self.seller_api_service.get_subscription_plans()
            available_plans = plans_result.get("plans", [])

            if not plans_result.get("success"):
                return await self._handle_plan_fetch_error(user, session)

            # Generate contextual response showing plans
            context = {
                "workflow_state": "show_subscription_plans",
                "plans": available_plans,
                "message": message
            }

            response_message = await self.response_helpers.generate_seller_contextual_response(context)

            # Update session state to await plan selection
            session.workflow_state["seller_workflow_state"] = "awaiting_plan_selection"

            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            return {
                "success": True,
                "workflow_step": "show_subscription_plans",
                "message": response_message,
                "plans": available_plans,
                "message_already_sent": False
            }

        except Exception as e:
            logger.error(f"Error handling plan upgrade request: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _handle_plan_selection_response(self, user: User, session: ConversationSession,
                                              message: str) -> Dict[str, Any]:
        """Handle seller's plan selection response."""
        try:
            # Fetch available subscription plans only if not already available
            plans_result = await self.seller_api_service.get_subscription_plans()
            available_plans = plans_result.get("plans", [])
            # Extract plan selection from message using AI
            selected_plan = await self._extract_plan_selection(message, available_plans)

            if not selected_plan:
                # Don't show plans again, just ask for clarification
                context = {
                    "workflow_state": "invalid_plan_selection",
                    "user_message": message
                }
                response_message = await self.response_helpers.generate_seller_contextual_response(context)
                return {
                    "success": False,
                    "workflow_step": "invalid_plan_selection",
                    "message": response_message,
                    "message_already_sent": False
                }

            # Generate payment link
            payment_result = await self.seller_api_service.generate_payment_link(
                selected_plan.get("id"),
                user.id
            )

            if not payment_result.get("success"):
                return await self._handle_payment_link_error(user, session, selected_plan)

            # Generate contextual response with payment link
            context = {
                "workflow_state": "payment_link_generated",
                "selected_plan": selected_plan,
                "payment_link": payment_result.get("payment_link"),
            }

            response_message = await self.response_helpers.generate_seller_contextual_response(context)

            # Update session state - clear available_plans to prevent re-showing
            session.workflow_state["seller_workflow_state"] = "payment_link_sent"
            session.workflow_state["selected_plan"] = selected_plan

            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            # ADD THIS: Schedule end-of-flow reminder after 5 minutes for payment link
            asyncio.create_task(self._schedule_end_of_flow_reminder(user, session))

            return {
                "success": True,
                "workflow_step": "payment_link_generated",
                "message": response_message,
                "payment_link": payment_result.get("payment_link"),
                "message_already_sent": False
            }

        except Exception as e:
            logger.error(f"Error handling plan selection: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _schedule_end_of_flow_reminder(self, user: User, session: ConversationSession):
        """Schedule end-of-flow reminder after 5 minutes."""
        try:
            print("schedult end of flow reminder called")
            # Wait for 5 minutes as specified in the document
            await asyncio.sleep(300)  # 5 minutes = 300 seconds

            # Send end-of-flow reminder
            await self.handle_seller_flow_completion(user, session)

        except Exception as e:
            logger.error(f"Error scheduling end-of-flow reminder: {e}")

    async def _process_rfq_email_requests(self, user: User, session: ConversationSession,
                                          selected_rfq_ids: List[str]) -> Dict[str, Any]:
        """Process RFQ email requests after credit verification using batch API with enhanced error handling."""
        try:
            seller_id = user.id
            

            # Send acknowledgment
            ack_context = {
                "workflow_state": "rfq_email_processing",
                "selected_rfq_ids": selected_rfq_ids,
            }

            ack_message = await self.response_helpers.generate_seller_contextual_response(ack_context)
            await self.whatsapp_service.send_message(user.phone_number, ack_message)

            # Send batch RFQ email request
            try:
                batch_result = await self.seller_api_service.send_rfq_email(
                    rfq_ids=selected_rfq_ids,
                    seller_email=user.email,
                    seller_id=user.org_id
                )

                if batch_result.get("success"):
                    # Process successful results
                    successful_results = batch_result.get("results", {}).get("successful", [])
                    failed_results = batch_result.get("results", {}).get("failed", [])

                    # Update sent flags for successful emails
                    successful_rfq_ids = [result["rfq_id"] for result in successful_results]
                    if successful_rfq_ids:
                        await self.seller_api_service.update_rfq_seller_sent_flag(
                            successful_rfq_ids[0], seller_id
                        )

                    # Format results for consistency with enhanced error handling
                    email_results = []

                    # Add successful results
                    for result in successful_results:
                        email_results.append({
                            "rfq_id": result["rfq_id"],
                            "success": True,
                            "error": None,
                            "error_code": None
                        })

                    # Add failed results with error code analysis
                    for result in failed_results:
                        error_code = result.get("error_code")
                        error_message = result.get("error", "Unknown error")

                        email_results.append({
                            "rfq_id": result["rfq_id"],
                            "success": False,
                            "error": error_message,
                            "error_code": error_code
                        })

                    successful_emails = len(successful_results)

                else:
                    # Handle batch failure - all emails failed
                    logger.error(f"Batch email request failed: {batch_result.get('error')}")
                    batch_error_code = batch_result.get("error_code")
                    batch_error = batch_result.get("error", "Batch request failed")

                    email_results = []
                    for rfq_id in selected_rfq_ids:
                        email_results.append({
                            "rfq_id": rfq_id,
                            "success": False,
                            "error": batch_error,
                            "error_code": batch_error_code
                        })
                    successful_emails = 0

            except Exception as e:
                logger.error(f"Error in batch RFQ email request: {e}")
                # Fallback: mark all as failed
                email_results = []
                for rfq_id in selected_rfq_ids:
                    email_results.append({
                        "rfq_id": rfq_id,
                        "success": False,
                        "error": f"Batch request error: {str(e)}",
                        "error_code": "API_ERROR"
                    })
                successful_emails = 0

            # Analyze error codes and categorize results
            error_analysis = self._analyze_email_errors(email_results)

            # Send appropriate status message based on error analysis
            status_context = {
                "workflow_state": "rfq_email_status_with_errors",
                "email_results": email_results,
                "total_requested": len(selected_rfq_ids),
                "successful_emails": successful_emails,
                "error_analysis": error_analysis
            }

            status_message = await self.response_helpers.generate_seller_contextual_response(status_context)

            # Complete workflow
            session.workflow_state["seller_workflow_state"] = "completed"
            session.workflow_state["email_results"] = email_results
            session.workflow_state["error_analysis"] = error_analysis

            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            # Schedule end-of-flow reminder after 5 minutes
            asyncio.create_task(self._schedule_end_of_flow_reminder(user, session))

            return {
                "success": True,
                "workflow_step": "rfq_emails_processed",
                "message": status_message,
                "emails_sent": successful_emails,
                "error_analysis": error_analysis,
                "message_already_sent": False
            }

        except Exception as e:
            logger.error(f"Error processing RFQ email requests: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    def _analyze_email_errors(self, email_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze email errors and categorize them by error code."""
        error_analysis = {
            "has_errors": False,
            "error_categories": {
                "NO_CREDITS": [],
                "RFQ_NOT_FOUND": [],
                "API_ERROR": [],
                "UNKNOWN": []
            },
            "error_counts": {
                "NO_CREDITS": 0,
                "RFQ_NOT_FOUND": 0,
                "API_ERROR": 0,
                "UNKNOWN": 0
            },
            "total_failed": 0,
            "total_successful": 0
        }

        for result in email_results:
            if result["success"]:
                error_analysis["total_successful"] += 1
            else:
                error_analysis["has_errors"] = True
                error_analysis["total_failed"] += 1

                error_code = result.get("error_code", "UNKNOWN")
                rfq_id = result.get("rfq_id")

                # Categorize by error code
                if error_code == "NO_CREDITS":
                    error_analysis["error_categories"]["NO_CREDITS"].append(rfq_id)
                    error_analysis["error_counts"]["NO_CREDITS"] += 1
                elif error_code == "RFQ_NOT_FOUND":
                    error_analysis["error_categories"]["RFQ_NOT_FOUND"].append(rfq_id)
                    error_analysis["error_counts"]["RFQ_NOT_FOUND"] += 1
                elif error_code == "API_ERROR":
                    error_analysis["error_categories"]["API_ERROR"].append(rfq_id)
                    error_analysis["error_counts"]["API_ERROR"] += 1
                else:
                    error_analysis["error_categories"]["UNKNOWN"].append(rfq_id)
                    error_analysis["error_counts"]["UNKNOWN"] += 1

        return error_analysis

    async def _handle_no_credits_response(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Handle response when seller has no credits."""
        try:
            workflow_state = session.workflow_state or {}
            rfq_details = workflow_state.get("rfq_details", [])

            # Generate contextual response about no credits with plan options
            context = {
                "workflow_state": "no_credits_available",
                "rfq_details": rfq_details,
                "credits_available": 0
            }

            response_message = await self.response_helpers.generate_seller_contextual_response(context)

            # Update state to handle general responses (plan upgrade requests)
            session.workflow_state["seller_workflow_state"] = "awaiting_general_response"

            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            # ADD THIS: Schedule end-of-flow reminder after 5 minutes if no further interaction
            asyncio.create_task(self._schedule_conditional_end_of_flow_reminder(user, session))

            return {
                "success": True,
                "workflow_step": "no_credits_available",
                "message": response_message,
                "message_already_sent": False
            }

        except Exception as e:
            logger.error(f"Error handling no credits response: {e}")
            return await self._handle_workflow_error(user, session, str(e))

    async def _schedule_conditional_end_of_flow_reminder(self, user: User, session: ConversationSession):
        """
        Schedule conditional end-of-flow reminder.
        Only send if user doesn't interact further within 5 minutes.
        """
        try:
            # Store the current session state timestamp
            current_timestamp = utc_now()
            session.workflow_state["last_activity_timestamp"] = current_timestamp.isoformat()
            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            # Wait for 5 minutes
            await asyncio.sleep(300)  # 5 minutes = 300 seconds

            # Check if there was any activity since we scheduled this reminder
            updated_session = await self.session_manager.get_conversation_context(user.phone_number)
            last_activity = updated_session.workflow_state.get("last_activity_timestamp")

            # If no new activity, send end-of-flow reminder
            if last_activity == current_timestamp.isoformat():
                await self.handle_seller_flow_completion(user, updated_session)
            else:
                logger.info(f"Skipping end-of-flow reminder for {user.phone_number} due to recent activity")

        except Exception as e:
            logger.error(f"Error in conditional end-of-flow reminder: {e}")

    # Helper methods

    async def _check_seller_credits(self, seller_id: str) -> Dict[str, Any]:
        """Check seller's credit balance."""
        try:
            return await self.seller_api_service.check_seller_credits(seller_id)
        except Exception as e:
            logger.error(f"Error checking seller credits: {e}")
            return {"success": False, "error": str(e), "credits_available": 0}

    async def _fetch_seller_rfqs(self, org_id: str) -> Dict[str, Any]:
        """Fetch active RFQs for seller's category."""
        try:
            return await self.seller_api_service.fetch_active_rfqs(org_id)
        except Exception as e:
            logger.error(f"Error fetching seller RFQs: {e}")
            return {"success": False, "error": str(e)}

    async def handle_seller_flow_completion(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """
        Handle seller flow completion with end-of-flow reminder (Step 13).

        This method is called when the seller workflow is completing to show
        open RFQs where they haven't submitted bids yet.
        """
        try:
            # Fetch open RFQs where seller has not submitted bids
            reminder_result = await self._fetch_seller_open_rfqs_for_reminder(user.org_id)

            if not reminder_result.get("success"):
                # If API fails, send generic closing message
                return await self._send_generic_closing_message(user, session)

            open_rfqs = reminder_result.get("open_rfqs", [])

            if not open_rfqs:
                # No open RFQs, send standard closing message
                return await self._send_standard_closing_message(user, session)

            # Generate reminder message with open RFQs
            context = {
                "workflow_state": "end_of_flow_reminder",
                "open_rfqs": open_rfqs[:3],  # Show last 3 as per document
                "total_open_rfqs": len(open_rfqs)
            }

            reminder_message = await self.response_helpers.generate_seller_contextual_response(context)

            # Send reminder message
            await self.whatsapp_service.send_message(user.phone_number, reminder_message)
            self.session_manager.add_message_to_history(session, "assistant", reminder_message)

            # Complete the session
            session.outcome = 'completed'
            session.completed_at = utc_now().replace(tzinfo=None)
            await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

            return {
                "success": True,
                "workflow_step": "end_of_flow_reminder",
                "message": reminder_message,
                "message_already_sent": False,
                "session_completed": True
            }

        except Exception as e:
            logger.error(f"Error in seller flow completion: {e}")
            return await self._send_generic_closing_message(user, session)

    async def _fetch_seller_open_rfqs_for_reminder(self, seller_id: str) -> Dict[str, Any]:
        """
        Fetch open RFQs where seller has not submitted bids.

        This calls the GMT API to get RFQs that:
        - Seller previously requested via email
        - Are still open for bidding
        - Haven't received bids from this seller yet
        """
        try:
            return await self.seller_api_service.fetch_seller_open_rfqs_for_reminder(seller_id)
        except Exception as e:
            logger.error(f"Error fetching seller open RFQs for reminder: {e}")
            return {"success": False, "error": str(e)}

    async def _send_generic_closing_message(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Send generic closing message when reminder API fails."""
        context = {
            "workflow_state": "generic_closing_message"
        }

        closing_message = await self.response_helpers.generate_seller_contextual_response(context)

        await self.whatsapp_service.send_message(user.phone_number, closing_message)
        self.session_manager.add_message_to_history(session, "assistant", closing_message)

        # Complete the session
        session.outcome = 'completed'
        session.completed_at = utc_now().replace(tzinfo=None)
        await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

        return {
            "success": True,
            "workflow_step": "generic_closing",
            "message": closing_message,
            "message_already_sent": False,
            "session_completed": True
        }

    async def _send_standard_closing_message(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Send standard closing message when no open RFQs found."""
        context = {
            "workflow_state": "standard_closing_message"
        }

        closing_message = await self.response_helpers.generate_seller_contextual_response(context)

        await self.whatsapp_service.send_message(user.phone_number, closing_message)
        self.session_manager.add_message_to_history(session, "assistant", closing_message)

        # Complete the session
        session.outcome = 'completed'
        session.completed_at = utc_now().replace(tzinfo=None)
        await self.session_manager.save_session(session, WorkflowType.seller_rfq_view)

        return {
            "success": True,
            "workflow_step": "standard_closing",
            "message": closing_message,
            "message_already_sent": False,
            "session_completed": True
        }

    async def _extract_rfq_ids_from_message(self, message: str, session: ConversationSession) -> List[str]:
        """Extract RFQ IDs from seller's message."""
        try:
            # Use AI entity extraction
            extraction = await self.openai_service.extract_entities(
                message=message,
                workflow_type="rfq_status_check"
            )

            extracted_ids = extraction.get("rfq_id") or []

            # Limit to maximum allowed
            max_allowed = self.settings.rfq_max_allowed
            return extracted_ids[:max_allowed]

        except Exception as e:
            logger.error(f"Error extracting RFQ IDs: {e}")
            return []

    async def _extract_plan_selection(self, message: str, available_plans: List[Dict]) -> Dict[str, Any]:
        """Extract plan selection from seller's message using AI."""
        try:
            # Use AI to extract plan selection
            extraction = await self.openai_service.extract_entities(
                message=message,
                workflow_type="plan_selection"
            )

            print("etxraction result of plans", extraction)
            
            # Get the selected plan from AI extraction
            selected_plan_info = extraction.get("selected_plan")
            
            if selected_plan_info:
                # Find matching plan from available plans
                for plan in available_plans:
                    if (plan.get("planName", "").lower() == selected_plan_info.lower() or 
                        plan.get("id", "") == selected_plan_info or
                        str(plan.get("id", "")) == selected_plan_info):
                        return plan
            
            # Fallback to simple matching if AI extraction fails
            message_lower = message.lower()
            for plan in available_plans:
                plan_name = plan.get("planName", "").lower()
                if plan_name in message_lower:
                    return plan
            
            return None

        except Exception as e:
            logger.error(f"Error extracting plan selection: {e}")
            return None


    def _build_seller_conversation_context(self, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Build conversation context for seller responses."""
        return {
            "workflow_type": "seller_rfq_view",
            "current_message": message,
            "session_history": session.conversation_history.get("messages", [])[-5:],
            "workflow_state": session.workflow_state
        }

    # Error handlers
    async def _handle_workflow_error(self, user: User, session: ConversationSession, error: str) -> Dict[str, Any]:
        """Handle general workflow errors."""
        context = {
            "workflow_state": "error",
            "error_message": error
        }

        error_message = await self.response_helpers.generate_seller_contextual_response(context)

        return {
            "success": False,
            "workflow_step": "error",
            "message": error_message,
            "error": error,
            "message_already_sent": False
        }

    async def _handle_invalid_rfq_selection(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """Handle invalid RFQ selection."""
        # Step 1: Fetch active RFQs for seller's category
        rfq_result = await self._fetch_seller_rfqs(user.org_id)

        if not rfq_result.get("success"):
            return await self._handle_rfq_fetch_error(user, session)


        rfqs = rfq_result.get("rfqs")
        available_rfqs = [rfq.get("rfq_id") for rfq in rfqs]

        context = {
            "workflow_state": "invalid_rfq_selection",
            "available_rfqs": available_rfqs,
            "user_message": message
        }

        response_message = await self.response_helpers.generate_seller_contextual_response(context)

        return {
            "success": False,
            "workflow_step": "invalid_rfq_selection",
            "message": response_message,
            "message_already_sent": False
        }







    async def _handle_rfq_fetch_error(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Handle RFQ fetch errors."""
        context = {
            "workflow_state": "rfq_fetch_error"
        }

        response_message = await self.response_helpers.generate_seller_contextual_response(context)

        return {
            "success": False,
            "workflow_step": "rfq_fetch_error",
            "message": response_message,
            "message_already_sent": False
        }

    async def _handle_plan_fetch_error(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Handle plan fetch errors."""
        context = {
            "workflow_state": "plan_fetch_error"
        }

        response_message = await self.response_helpers.generate_seller_contextual_response(context)

        return {
            "success": False,
            "workflow_step": "plan_fetch_error",
            "message": response_message,
            "message_already_sent": False
        }

    async def _handle_payment_link_error(self, user: User, session: ConversationSession,selected_plan: Dict[str, Any]) -> Dict[str, Any]:
        """Handle payment link generation errors."""
        context = {
            "workflow_state": "payment_link_error",
            "selected_plan": selected_plan
        }

        response_message = await self.response_helpers.generate_seller_contextual_response(context)

        return {
            "success": False,
            "workflow_step": "payment_link_error",
            "message": response_message,
            "message_already_sent": False
        }