"""

Copy of seller wokring code
Enhanced Seller Service with RFQ Selection Handling.

This implementation adds the workflow for handling seller RFQ selections
and email requests as per the specified workflow states.
"""

import logging
import re
from typing import Dict, Any, List
from app.models import ConversationSession, User
from app.services.whatsapp_service import WhatsAppService
from app.services.gmt_api_service import GMTAPIService
from app.config import get_settings
from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.session_management_service import SessionManagementService
from app.database import SessionLocal, DatabaseManager
from app.services.chat_summary_service import ChatSummaryService
from app.services.daily_summary_service import DailySummaryService
logger = logging.getLogger(__name__)


class SellerService:
    """
    Enhanced Seller Service for managing seller operations and RFQ selection workflow.

    Handles the complete seller workflow including:
    - Seller identification and registration
    - RFQ listing and display
    - RFQ selection by seller
    - Email sending for selected RFQs
    """

    def __init__(self):
        self.db_manager = DatabaseManager()
        self.chat_summary_service = ChatSummaryService()
        self.daily_summary_service = DailySummaryService()
        self.whatsapp_service = WhatsAppService()
        self.gmt_api_service = GMTAPIService()
        self.settings = get_settings()
        self.openai_service = OpenAIService()
        self.response_helpers = ResponseHelpers(self.openai_service)
        # Initialize extracted services
        self.session_manager = SessionManagementService(
            self.db_manager, self.whatsapp_service,
            self.chat_summary_service, self.daily_summary_service
        )

    async def handle_seller_workflow(self, user: User, session: ConversationSession, message: str) -> Dict[str, Any]:
        """
        Main seller workflow handler with enhanced RFQ selection support.

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

            # Check if we're waiting for RFQ selections
            if current_state == "seller_respond_to_rfq_list":
                return await self._handle_rfq_selection_response(user, session, message)

            # Initial seller identification and RFQ listing
            return await self._handle_initial_seller_flow(user, session, message)

        except Exception as e:
            logger.error(f"Error in enhanced seller workflow: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": "We encountered an issue. Please contact support@procucev.com"
            }

    async def _handle_initial_seller_flow(self, user: User, session: ConversationSession, message: str) -> Dict[
        str, Any]:
        """Handle initial seller flow and RFQ listing."""

        # Step 1: Identify seller
        seller_info = await self.identify_seller(user.phone_number)

        if not seller_info.get("is_auth"):
            return await self._handle_unregistered_seller_flow(seller_info, message)

        # Step 2: Fetch and display RFQs
        category = seller_info.get("seller_category", "General")  # Default category
        rfq_info = await self.fetch_active_rfqs_by_category(category, limit=self.settings.rfq_fetch_limit)

        if not rfq_info.get("success"):
            return {
                "success": False,
                "error": rfq_info.get("error"),
                "message": "Unable to fetch RFQs. Please contact support@procucev.com"
            }

        # Step 3: Send RFQ list to seller
        return await self._send_rfq_list_to_seller(user, session, rfq_info, seller_info)

    async def _send_rfq_list_to_seller(self, user: User, session: ConversationSession,
                                       rfq_info: Dict[str, Any], seller_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send RFQ list to seller and set up workflow state for selection.

        Workflow State: list_rfq_to_seller
        """
        try:
            latest_rfqs = rfq_info.get("latest_rfqs", [])
            print("atest_rfs", latest_rfqs)
            total_count = rfq_info.get("total_count", 0)

            # Generate contextual message using AI
            context = {
                "workflow_state": "list_rfq_to_seller",
                "total_count": total_count,
                "latest_rfqs": latest_rfqs,
                "seller_category": seller_info.get("seller_category", "General"),
                "seller_credits": seller_info.get("credits_available", 0)
            }

            try:
                response_message = await self.response_helpers.generate_seller_contextual_response(context)
            except Exception as e:
                logger.error(f"AI response generation failed: {str(e)}")
                response_message = await self._generate_fallback_rfq_list_message(context)
            print("latest rfq sss", latest_rfqs)

             # Update session workflow state
            session.workflow_type = "seller_rfq_view"
            session.workflow_state = session.workflow_state or {}
            session.workflow_state.update({
                "seller_workflow_state": "list_rfq_to_seller",
                "available_rfqs": latest_rfqs,
                "seller_info": seller_info,
                "next_expected_state": "seller_respond_to_rfq_list"
            })
            await self.session_manager.save_session(session, "seller_rfq_view")

            await self.whatsapp_service.send_message(user.phone_number, response_message)

            # Transition to next state - waiting for seller response
            session.workflow_state["seller_workflow_state"] = "seller_respond_to_rfq_list"

            return {
                "workflow_step": "list_rfq_to_seller",
                "success": True,
                "message": response_message,
                "rfq_ids": [rfq.get("rfq_id") for rfq in latest_rfqs if rfq.get("rfq_id")],
                "total_count": total_count,
                "next_state": "seller_respond_to_rfq_list"
            }

        except Exception as e:
            logger.error(f"Error sending RFQ list to seller: {e}")
            raise

    async def _handle_rfq_selection_response(self, user: User, session: ConversationSession,
                                             message: str) -> Dict[str, Any]:
        """
        Handle seller's response with RFQ selections.

        Workflow State: seller_respond_to_rfq_list
        """
        try:
            # Extract RFQ IDs from seller's message
            selected_rfq_ids = await self._extract_rfq_ids_from_message(message, session)

            print("selected rfq ids", selected_rfq_ids)

            if not selected_rfq_ids:
                return await self._handle_invalid_rfq_selection(user, session, message)

            # Validate selected RFQ IDs against available ones
            available_rfqs = session.workflow_state.get("available_rfqs", [])
            # available_ids = {str(rfq.get("rfq_id")) for rfq in available_rfqs if rfq.get("rfq_id")}
            #
            # valid_selections = [rfq_id for rfq_id in selected_rfq_ids if rfq_id in available_ids]
            # Build a set of valid IDs
            available_ids = {str(rfq_id) for rfq_id in available_rfqs}
            print("avaiable _ids", available_ids)

            # Validate selected RFQ IDs against available ones
            valid_selections = [rfq_id for rfq_id in selected_rfq_ids if rfq_id in available_ids]
            print("valid sections", valid_selections)

            if not valid_selections:
                return await self._handle_invalid_rfq_selection(user, session, message)

            # Process valid selections
            return await self._process_rfq_selections(user, session, valid_selections)

        except Exception as e:
            logger.error(f"Error handling RFQ selection response: {e}")
            return await self._handle_selection_error(user, session, str(e))

    async def _extract_rfq_ids_from_message(self, message: str, session: ConversationSession) -> List[str]:
        """
        Extract RFQ IDs from seller's message using multiple methods.

        Uses both AI extraction and regex fallback for reliability.
        """
        rfq_ids = []
        print("message in fucntin", message)

        # Method 1: Use AI entity extraction for RFQ IDs
        try:
            extraction = self.openai_service.extract_entities(
                message=message,
                workflow_type="rfq_status_check"
            )
            print("extraction rrsult", extraction)
            extracted_ids = extraction.get("rfq_id") or []
            rfq_ids=extracted_ids
            print("extracted", extracted_ids)



        except Exception as e:
            print("error ", e)
            logger.warning(f"AI extraction failed: {e}")

        # Remove duplicates while preserving order
        seen = set()
        unique_rfq_ids = []
        for rfq_id in rfq_ids:
            if rfq_id not in seen:
                seen.add(rfq_id)
                unique_rfq_ids.append(rfq_id)
        #
        # # Limit to maximum allowed selections
        max_allowed = self.settings.rfq_max_allowed
        return unique_rfq_ids[:max_allowed]

    async def _process_rfq_selections(self, user: User, session: ConversationSession,
                                      selected_rfq_ids: List[str]) -> Dict[str, Any]:
        """
        Process validated RFQ selections and initiate email sending.

        Workflow State: sending_email_to_seller
        """
        try:
            # Update workflow state
            session.workflow_state["seller_workflow_state"] = "sending_email_to_seller"
            session.workflow_state["selected_rfq_ids"] = selected_rfq_ids

            # Get seller info
            seller_info = session.workflow_state.get("seller_info", {})
            seller_email = seller_info.get("email","priyasoniy17@gmail.com")
            seller_id = seller_info.get("seller_id","123")

            if not seller_email or not seller_id:
                return await self._handle_missing_seller_info(user, session)

            # Send acknowledgment message
            await self._send_selection_acknowledgment(user, selected_rfq_ids)

            # Process email sending for each selected RFQ
            email_results = []
            successful_emails = 0

            for rfq_id in selected_rfq_ids:
                try:
                    email_result = await self.send_rfq_email(rfq_id, seller_email, seller_id)
                    email_results.append({
                        "rfq_id": rfq_id,
                        "success": email_result.get("success", False),
                        "error": email_result.get("error")
                    })

                    if email_result.get("success"):
                        successful_emails += 1

                except Exception as e:
                    logger.error(f"Error sending email for RFQ {rfq_id}: {e}")
                    email_results.append({
                        "rfq_id": rfq_id,
                        "success": False,
                        "error": str(e)
                    })

            # Send final status message
            await self._send_email_status_message(user, email_results, successful_emails)

            # Complete the workflow
            session.workflow_state["seller_workflow_state"] = "completed"
            session.workflow_state["email_results"] = email_results

            return {
                "workflow_step": "sending_email_to_seller",
                "success": True,
                "selected_rfq_ids": selected_rfq_ids,
                "emails_sent": successful_emails,
                "email_results": email_results,
                "message": f"Processed {len(selected_rfq_ids)} RFQ selections, sent {successful_emails} emails"
            }

        except Exception as e:
            logger.error(f"Error processing RFQ selections: {e}")
            raise

    async def _send_selection_acknowledgment(self, user: User, selected_rfq_ids: List[str]):
        """Send acknowledgment message for RFQ selections."""
        rfq_list = ", ".join(selected_rfq_ids)

        # Generate contextual message using AI
        context = {
            "workflow_state": "rfq_selection_acknowledgment",
            "selected_rfq_id": selected_rfq_ids
        }

        try:
            response_message = await self.response_helpers.generate_seller_contextual_response(context)
        except Exception as e:
            response_message = f"Thank you! I've received your request for RFQ IDs: {rfq_list}\n\nSending detailed information to your registered email address..."
        await self.whatsapp_service.send_message(user.phone_number, response_message)

    async def _send_email_status_message(self, user: User, email_results: List[Dict], successful_count: int):
        """Send final status message about email sending."""
        total_requested = len(email_results)

        if successful_count == total_requested:
            message = f"✅ Successfully sent all {successful_count} RFQ details to your email!"
        elif successful_count > 0:
            failed_count = total_requested - successful_count
            failed_rfqs = [result["rfq_id"] for result in email_results if not result["success"]]
            message = f"✅ Sent {successful_count} RFQ details successfully.\n❌ Failed to send {failed_count} RFQs: {', '.join(failed_rfqs)}\n\nPlease contact support if you didn't receive some emails."
        else:
            message = "❌ Unable to send any RFQ details via email. Please contact support@procucev.com for assistance."

        await self.whatsapp_service.send_message(user.phone_number, message)

    async def _handle_invalid_rfq_selection(self, user: User, session: ConversationSession, message: str) -> Dict[
        str, Any]:
        """Handle invalid or unrecognized RFQ selections."""
        available_rfqs = session.workflow_state.get("available_rfqs", [])
        # available_ids = [str(rfq.get("rfq_id")) for rfq in available_rfqs if rfq.get("rfq_id")]
        # Directly use available_rfqs since it's already a list of IDs (strings)
        available_ids = [str(rfq_id) for rfq_id in available_rfqs]

        if available_ids:
            ids_text = ", ".join(available_ids)
            response = f"I couldn't find valid RFQ IDs in your message. Please select from the available RFQ IDs: {ids_text}\n\nYou can type the IDs like: {available_ids[0]} or {available_ids[0]}, {available_ids[1] if len(available_ids) > 1 else available_ids[0]}"
        else:
            response = "I couldn't find valid RFQ IDs. Please check the list above and try again."

        await self.whatsapp_service.send_message(user.phone_number, response)

        return {
            "workflow_step": "invalid_rfq_selection",
            "success": False,
            "message": "Invalid RFQ selection - requested clarification"
        }

    async def _handle_missing_seller_info(self, user: User, session: ConversationSession) -> Dict[str, Any]:
        """Handle missing seller email or ID information."""
        message = "We couldn't find your registered email address. Please contact support@procurev.com to complete your registration."
        await self.whatsapp_service.send_message(user.phone_number, message)

        return {
            "workflow_step": "missing_seller_info",
            "success": False,
            "error": "Missing seller email or ID",
            "message": "Missing seller information - directed to support"
        }

    async def _handle_selection_error(self, user: User, session: ConversationSession, error: str) -> Dict[str, Any]:
        """Handle errors during RFQ selection processing."""
        message = "There was an error processing your RFQ selection. Please try again or contact support@procurev.com"
        await self.whatsapp_service.send_message(user.phone_number, message)

        return {
            "workflow_step": "selection_error",
            "success": False,
            "error": error,
            "message": "Error processing selection - sent error message"
        }

    async def _generate_fallback_rfq_list_message(self, context: Dict[str, Any]) -> str:
        """Generate fallback message for RFQ listing when AI fails."""
        latest_rfqs = context.get("latest_rfqs", [])
        total_count = context.get("total_count", 0)

        message_parts = [
            f"📋 You have {total_count} active RFQs in your category.",
            "\nHere are the latest available RFQs:\n"
        ]

        for i, rfq in enumerate(latest_rfqs[:3], 1):
            rfq_id = rfq.get("rfq_id", "N/A")
            location = rfq.get("location", "N/A")
            date = rfq.get("submission_date", "N/A")
            product = rfq.get("product_name", "N/A")[:50]  # Truncate long names

            message_parts.append(f"{i}. RFQ {rfq_id}\n   📍 {location}\n   📅 {date}\n   📦 {product}\n")

        message_parts.extend([
            "\n💡 To request detailed information via email, please reply with the RFQ IDs you're interested in.",
            "\nExample: '3343' or '3343, 3351, 3352'",
            "\nType the RFQ numbers you want to receive via email:"
        ])

        return "".join(message_parts)

    async def _handle_unregistered_seller_flow(self, seller_info: Dict[str, Any], message: str) -> Dict[str, Any]:
        """Handle flow for unregistered sellers."""
        return {
            "workflow_step": "unregistered_seller",
            "success": True,
            "message": "Please complete registration first. Contact support@procurev.com to get started."
        }

    # Include existing methods from the original SellerService
    async def identify_seller(self, phone_number: str) -> Dict[str, Any]:
        """Identify seller by phone number."""
        try:
            return {
                "is_auth": True,
                "multiple_emails": False,
            }
        except Exception as e:
            logger.error(f"Error identifying seller: {e}")
            return {"found": False, "error": str(e)}

    async def fetch_active_rfqs_by_category(self, category: str, limit: int = None) -> Dict[str, Any]:
        """Fetch active RFQs by category."""
        if limit is None:
            limit = get_settings().rfq_fetch_limit

        try:
            rfq_response = await self.gmt_api_service.fetch_active_rfqs(
                category=category,
                limit=limit
            )

            if rfq_response.get("success"):
                rfqs = rfq_response.get("rfqs", [])
                total_count = rfq_response.get("total_count", 0)

                return {
                    "success": True,
                    "rfqs": rfqs,
                    "total_count": total_count,
                    "latest_rfqs": rfqs[:limit]
                }
            else:
                return {
                    "success": False,
                    "error": rfq_response.get("error", "Failed to fetch RFQs")
                }

        except Exception as e:
            logger.error(f"Error fetching active RFQs: {e}")
            return {"success": False, "error": str(e)}

    async def send_rfq_email(self, rfq_id: str, seller_email: str, seller_id: str) -> Dict[str, Any]:
        """Send RFQ details via email."""
        try:
            email_response = await self.gmt_api_service.send_rfq_email(
                rfq_id=rfq_id,
                seller_email=seller_email,
                seller_id=seller_id
            )

            if email_response.get("success"):
                await self.gmt_api_service.update_rfq_seller_sent_flag(
                    rfq_id=rfq_id,
                    seller_id=seller_id
                )

                return {
                    "success": True,
                    "email_sent": True,
                    "message": f"RFQ {rfq_id} has been emailed to you"
                }
            else:
                return {
                    "success": False,
                    "error": email_response.get("error", "Failed to send RFQ email")
                }

        except Exception as e:
            logger.error(f"Error sending RFQ email: {e}")
            return {"success": False, "error": str(e)}
