"""
RFQ (Request for Quotation) creation and management service.

This service handles the complete RFQ workflow focusing on database storage,
status management, and backend submission. The EntityService handles the 
intelligent field collection, completeness tracking, and question generation.

Key responsibilities:
- Store and update RFQ data in database
- Manage RFQ status lifecycle (draft → pending → submitted)
- Submit completed RFQs to backend procurement APIs
- Provide RFQ retrieval and summary functions
- Handle RFQ validation using OpenAI-powered field validation
"""

import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
import uuid

from app.database import SessionLocal
from app.models import RFQ, User, ConversationSession
from app.schemas.rfq import RFQCreateRequestSchema, RFQValidationSchema

from app.services.openai_service import OpenAIService
from app.services.helpers.response_helpers import ResponseHelpers
from app.procucev_apis.rfq_apis import RFQAPIService
from app.procucev_apis.seller_apis import SellerAPIService
from app.config import get_settings

logger = logging.getLogger(__name__)


class RFQService:
    """
    RFQ creation and management service.
    
    Focuses on database operations and RFQ lifecycle management.
    Works with EntityService which provides intelligent field collection.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.rfq_service = RFQAPIService()
        self.seller_service=SellerAPIService()

        self.openai_service = OpenAIService()

        self.response_helpers = ResponseHelpers(self.openai_service)
    async def process_rfq_status_request(self, user: User, message: str) -> Dict[str, Any]:
        """
        Extract RFQ IDs, fetch status from API, and generate response.
        """

        # Step 1: Extract entities (RFQ IDs) via LLM
        entity_result = await self.openai_service.extract_entities(
            message=message,
            workflow_type="rfq_status_check"
        )

        print("entity result", entity_result)
        rfq_ids = entity_result.get("rfq_id", [])

        # Limit to only 5 RFQ IDs if there are more
        if rfq_ids:
            rfq_ids = rfq_ids[:self.settings.rfq_max_allowed]

        # Step 2: Fetch from GMT API
        rfq_service = RFQAPIService()
        seller_service = SellerAPIService()

        role = getattr(user.role, "value", user.role)
        if role == "buyer":
            result = await rfq_service.get_rfq_status(client_id=user.id, rfq_ids=rfq_ids)
        elif role == "seller":
            result = await seller_service.check_seller_rfq_status(seller_id=user.org_id, rfq_ids=rfq_ids)
        else:
            result = {"success": False, "error": f"No RFQ status lookup for role {role!r}"}

        # Handle case where data might be a list or dict
        data = result.get("data", {})
        if isinstance(data, dict):
            rfq_data = data.get("data", [])
        else:
            rfq_data = data if isinstance(data, list) else []

        status = "rfq_status_found" if rfq_ids else "recent_rfqs_found"

        # Nothing to report: answer directly. Asking the language model here
        # only added a way to fail, and its failure text ("technical issue")
        # reached users who simply had no RFQs yet.
        if result.get("success") is False or all(self._is_status_placeholder(row) for row in rfq_data):
            if result.get("success") is False:
                logger.warning(f"RFQ status lookup failed for {role}: {result.get('error')}")
                response_message = self._rfq_status_unavailable_message()
            elif rfq_ids:
                response_message = self._rfq_status_not_found_message(rfq_ids)
            else:
                response_message = self._no_rfqs_message(role, user.email)
            return {
                "status": status,
                "rfq_ids": rfq_ids or [],
                "rfq_statuses": rfq_data,
                "response_message": response_message,
            }

        # Step 3: Prepare context for AI message generation
        context = {
            "rfq_statuses": rfq_data,
            "rfq_ids": rfq_ids or [],
            "has_results": bool(rfq_data),
            "max_allowed": self.settings.rfq_max_allowed,
            "followup_note": self.settings.rfq_followup_note,
            "user_role":user.role,
            "user_email":user.email,
            "support_email":self.settings.support_email,
            "support_info_email":self.settings.support_contact_info
        }

        # Step 4: Generate AI Response for the fetched results
        response_message = await self.response_helpers.generate_rfq_status_contextual_response(
            context=context
        )
        logger.info(f"user role while checking rfq status:{user.role}")
        if not response_message or "technical issue" in response_message.lower():
            response_message = self._format_rfq_statuses(rfq_data, rfq_ids, role, user.email)

        return {
            "status": status,
            "rfq_ids": rfq_ids or [],
            "rfq_statuses": rfq_data,
            "response_message": response_message
        }

    # ── RFQ status replies that need no language model ──────────────────────

    @staticmethod
    def _is_status_placeholder(row: Any) -> bool:
        """The backend's "nothing here" row: no RFQ ID and status Not Found."""
        return (
            isinstance(row, dict)
            and not row.get("rfqid")
            and str(row.get("status") or "").strip().lower() == "not found"
        )

    @staticmethod
    def _no_rfqs_message(role: str, email: Optional[str]) -> str:
        profile = f" (*{email}*)" if email else ""
        if role == "seller":
            return (
                f"Looks like you haven't requested any RFQs yet with this profile{profile}.\n\n"
                "To see the latest RFQs in your categories, tap *Other Active RFQs*.\n"
                "If you are looking for the status of a specific RFQ, send the RFQ ID and I shall fetch it."
            )
        return (
            f"It looks like you haven't created any RFQs yet with this profile{profile}.\n\n"
            "If you are looking for the status of a specific RFQ, send the RFQ ID and I shall fetch it."
        )

    @staticmethod
    def _rfq_status_not_found_message(rfq_ids: List[str]) -> str:
        ids = ", ".join(f"*{rfq_id}*" for rfq_id in rfq_ids)
        return (
            f"We have checked {ids}, but it is not associated with your current profile (email ID). "
            "Please verify the RFQ ID, or exit and log in with the correct profile before trying again."
        )

    @staticmethod
    def _rfq_status_unavailable_message() -> str:
        return (
            "I couldn't fetch your RFQ status right now. Please try again in a few minutes, "
            "or send the RFQ ID to check a specific RFQ."
        )

    def _format_rfq_statuses(self, rfq_data: List[Any], rfq_ids: List[str], role: str, email: Optional[str]) -> str:
        """Plain listing used when the language model cannot write the reply."""
        lines = [
            f"*{row['rfqid']}* : {row.get('status') or 'Unknown'}."
            for row in rfq_data
            if isinstance(row, dict) and row.get("rfqid")
        ]
        if not lines:
            return self._no_rfqs_message(role, email)
        heading = (
            "✅ Here is the status of the RFQs you requested:" if rfq_ids
            else "ℹ️ Here is the status of your recent RFQs:"
        )
        return (
            heading + "\n\n" + "\n".join(lines) + "\n\n"
            "If you are looking for the status of any specific RFQ please provide the RFQID "
            "and I shall fetch the status for the same."
        )
    