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

        self.openai_service = OpenAIService()

        self.response_helpers = ResponseHelpers(self.openai_service)
    async def process_rfq_status_request(self, user: User, message: str) -> Dict[str, Any]:
        """
        Extract RFQ IDs, fetch status from API, and generate response.
        """

        # Step 1: Extract entities (RFQ IDs) via LLM
        entity_result = self.openai_service.extract_entities(
            message=message,
            workflow_type="rfq_status_check"
        )

        print("enitiy result", entity_result)
        rfq_ids = entity_result.get("rfq_id", [])

        # Limit to only 5 RFQ IDs if there are more
        if rfq_ids:
            rfq_ids = rfq_ids[:self.settings.rfq_max_allowed]

        # Step 2: Fetch from GMT API
        rfq_service = RFQAPIService()
        result = await rfq_service.get_rfq_status(client_id=user.id, rfq_ids=rfq_ids)

        # Handle case where data might be a list or dict
        data = result.get("data", {})
        if isinstance(data, dict):
            rfq_data = data.get("data", [])
        else:
            rfq_data = data if isinstance(data, list) else []

        # Step 3: Prepare context for AI message generation
        context = {
            "rfq_statuses": rfq_data,
            "rfq_ids": rfq_ids or [],
            "has_results": bool(rfq_data),
            "max_allowed": self.settings.rfq_max_allowed,
            "followup_note": self.settings.rfq_followup_note
        }

        # Step 4: Generate AI Response for the fetched results
        response_message = await self.response_helpers.generate_rfq_status_contextual_response(
            context=context
        )

        return {
            "status": "rfq_status_found" if rfq_ids else "recent_rfqs_found",
            "rfq_ids": rfq_ids or [],
            "rfq_statuses": rfq_data,
            "response_message": response_message
        }
    