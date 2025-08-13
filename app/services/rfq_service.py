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
from app.services.gmt_api_service import GMTAPIService
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
        self.gmt_service = GMTAPIService()

        self.openai_service = OpenAIService()

        self.response_helpers = ResponseHelpers(self.openai_service)

        
    async def create_or_update_rfq(self, user_id: int, entity_result: Dict[str, Any], session_id: int) -> Dict[str, Any]:
        """
        Create new RFQ or update existing one using EntityService results.
        
        The EntityService provides:
        - entities: extracted field values
        - completeness: percentage complete (0-100)
        - missing_required_fields: what's still needed
        - next_questions: intelligent questions to ask
        - confidence: extraction confidence
        """
        try:
            # Extract EntityService results
            entities = entity_result.get('entities', {})
            completeness = entity_result.get('completeness', 0)
            missing_fields = entity_result.get('missing_required_fields', [])
            next_questions = entity_result.get('next_questions', [])
            
            with SessionLocal() as db:
                # Get or create active RFQ for user
                active_rfq = db.query(RFQ).filter(
                    RFQ.user_id == user_id,
                    RFQ.status == "draft"
                ).first()
                
                if not active_rfq:
                    # Create new RFQ
                    active_rfq = RFQ(
                        id=str(uuid.uuid4()),
                        user_id=user_id,
                        status="draft",
                        created_at=datetime.utcnow()
                    )
                    db.add(active_rfq)
                    logger.info(f"Created new RFQ: {active_rfq.id}")
                
                # Store entities in RFQ - let EntityService handle validation
                # Minimal update - just track that we have new data
                updated_fields = ["entities_updated"]
                db.commit()
                
                # Determine next steps based on completeness
                if completeness >= 100:
                    # RFQ is complete!
                    active_rfq.status = "pending_approval"
                    db.commit()
                    
                    return {
                        "rfq_complete": True,
                        "rfq_id": active_rfq.id,
                        "updated_fields": updated_fields,
                        "completeness": completeness
                    }
                
                elif completeness >= 50 and next_questions:
                    # Substantial progress, ask specific questions
                    return {
                        "needs_clarification": True,
                        "questions": next_questions,  # Ask all relevant questions
                        "missing_fields": missing_fields,
                        "updated_fields": updated_fields,
                        "completeness": completeness
                    }
                
                else:
                    # Early stage or low confidence, continue collection
                    next_question = next_questions[0] if next_questions else "Can you provide more details about your requirement?"
                    
                    return {
                        "continue_collection": True,
                        "next_question": next_question,
                        "missing_fields": missing_fields,
                        "updated_fields": updated_fields,
                        "completeness": completeness
                    }
                    
        except Exception as e:
            logger.error(f"Error creating/updating RFQ: {e}")
            raise
    
    
    
    async def get_rfq_by_id(self, rfq_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve RFQ by ID."""
        try:
            with SessionLocal() as db:
                rfq = db.query(RFQ).filter(RFQ.id == rfq_id).first()
                if rfq:
                    # Return essential fields only
                    return {
                        "id": rfq.id,
                        "status": rfq.status,
                        "user_id": rfq.user_id,
                        "created_at": rfq.created_at.isoformat() if rfq.created_at else None
                    }
                return None
        except Exception as e:
            logger.error(f"Error retrieving RFQ {rfq_id}: {e}")
            return None
    
    async def get_user_rfqs(self, user_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all RFQs for a user, optionally filtered by status."""
        try:
            with SessionLocal() as db:
                query = db.query(RFQ).filter(RFQ.user_id == user_id)
                if status:
                    query = query.filter(RFQ.status == status)
                
                rfqs = query.order_by(RFQ.created_at.desc()).all()
                return [{"id": rfq.id, "status": rfq.status, "created_at": rfq.created_at.isoformat() if rfq.created_at else None} for rfq in rfqs]
        except Exception as e:
            logger.error(f"Error retrieving RFQs for user {user_id}: {e}")
            return []
    
    async def update_rfq_status(self, rfq_id: str, status: str) -> bool:
        """Update RFQ status."""
        try:
            with SessionLocal() as db:
                rfq = db.query(RFQ).filter(RFQ.id == rfq_id).first()
                if rfq:
                    rfq.status = status
                    if status == "submitted":
                        rfq.submitted_at = datetime.utcnow()
                    db.commit()
                    return True
                return False
        except Exception as e:
            logger.error(f"Error updating RFQ status: {e}")
            return False
    
    async def submit_rfq_to_backend(self, rfq_id: str) -> Dict[str, Any]:
        """Submit completed RFQ to GMT backend system with Pydantic validation."""
        try:
            rfq_data = await self.get_rfq_by_id(rfq_id)
            if not rfq_data:
                return {"success": False, "error": "RFQ not found"}
            
            # Validate RFQ data before submission using Pydantic schema
            try:
                # Transform RFQ data to GMT API format
                gmt_payload = self._transform_rfq_to_gmt_format(rfq_data)
                
                # Validate using Pydantic schema
                validated_rfq = RFQCreateRequestSchema(**gmt_payload)
                
                gmt_service = GMTAPIService()
                
                # Submit validated data to GMT API
                submission_result = await gmt_service.create_rfq(validated_rfq.model_dump(by_alias=True))
                
                if submission_result.get("success"):
                    # Update status to submitted
                    success = await self.update_rfq_status(rfq_id, "submitted")
                    
                    if success:
                        logger.info(f"Successfully submitted RFQ {rfq_id} to GMT backend")
                        return {
                            "success": True, 
                            "rfq_id": rfq_id,
                            "backend_reference": submission_result.get("rfq_id", f"GMT_{rfq_id[:8]}"),
                            "gmt_response": submission_result.get("response")
                        }
                    else:
                        return {"success": False, "error": "RFQ submitted to GMT but failed to update local status"}
                else:
                    logger.error(f"Failed to submit RFQ {rfq_id} to GMT: {submission_result.get('error')}")
                    return {"success": False, "error": f"GMT API error: {submission_result.get('error')}"}
                    
            except Exception as validation_error:
                logger.error(f"RFQ validation failed: {validation_error}")
                return {"success": False, "error": f"RFQ validation failed: {str(validation_error)}"}
                
        except Exception as e:
            logger.error(f"Error submitting RFQ to backend: {e}")
            return {"success": False, "error": str(e)}
    
    def _transform_rfq_to_gmt_format(self, rfq_data: Dict[str, Any]) -> Dict[str, Any]:
        """Transform internal RFQ format to GMT API format."""
        return {
            "createdBy": "AI_Procurement_Agent",
            "projectDesc": rfq_data.get("product_name") or rfq_data.get("project_desc", ""),
            "deliveryDate": rfq_data.get("deadline") or rfq_data.get("delivery_date"),
            "division": rfq_data.get("division", ""),
            "user": rfq_data.get("user_id", ""),
            "org": {"id": rfq_data.get("organization_id", "")},
            "rfqItem": [{
                "description": rfq_data.get("specifications") or rfq_data.get("product_name", ""),
                "quantity": rfq_data.get("quantity", 1),
                "unitofMeasures": rfq_data.get("unit_of_measure", "pcs"),
                "brand": rfq_data.get("preferred_brand", ""),
                "remarks": rfq_data.get("remarks", "")
            }],
            "clientdeliverylocationrfq": [{
                "state": rfq_data.get("delivery_state", ""),
                "city": rfq_data.get("delivery_city", ""),
                "pincode": rfq_data.get("delivery_pincode", "")
            }],
            "noPrFlag": True,
            "vendors": [],
            "remarks": rfq_data.get("remarks", ""),
            "rfqDocument": [],
            "category": rfq_data.get("category", "")
        }

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
        rfq_ids = entity_result.get("rfq_id")

        # Limit to only 5 RFQ IDs if there are more
        if rfq_ids:
            rfq_ids = rfq_ids[:self.settings.rfq_max_allowed]

        # Step 2: Fetch from GMT API
        gmt_service = GMTAPIService()
        result = await gmt_service.get_rfq_status(client_id="4004", rfq_ids=rfq_ids)

        rfq_data = result.get("data", {}).get("data", [])

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
    