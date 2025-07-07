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
from app.services.gmt_api_service import GMTAPIService

logger = logging.getLogger(__name__)


class RFQService:
    """
    RFQ creation and management service.
    
    Focuses on database operations and RFQ lifecycle management.
    Works with EntityService which provides intelligent field collection.
    """
    
    def __init__(self):
        pass
        
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
            confidence = entity_result.get('confidence', 0)
            
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
                
                # Update RFQ with entities from EntityService
                updated_fields = self._update_rfq_from_entities(active_rfq, entities)
                db.commit()
                
                # Determine next steps based on completeness
                if completeness >= 100:
                    # RFQ is complete!
                    active_rfq.status = "pending_approval"
                    db.commit()
                    
                    return {
                        "rfq_complete": True,
                        "rfq_data": self._rfq_to_dict(active_rfq),
                        "updated_fields": updated_fields,
                        "completeness": completeness
                    }
                
                elif completeness >= 50 and next_questions:
                    # Substantial progress, ask specific questions
                    return {
                        "needs_clarification": True,
                        "questions": next_questions[:2],  # Limit to 2 questions
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
    
    def _update_rfq_from_entities(self, rfq: RFQ, entities: Dict[str, Any]) -> List[str]:
        """
        Update RFQ object with entities extracted by EntityService.
        Maps GMT API fields to RFQ model fields.
        """
        updated_fields = []
        
        # Map GMT API fields to RFQ model fields
        field_mapping = {
            # GMT API field -> RFQ model field
            "projectDesc": "product_name",
            "description": "specifications", 
            "quantity": "quantity",
            "unitofMeasures": "unit_of_measure",
            "deliveryDate": "deadline",
            "division": "division",
            "category": "category",
            "brand": "preferred_brand",
            "state": "delivery_state",
            "city": "delivery_city", 
            "pincode": "delivery_pincode",
            "remarks": "remarks"
        }
        
        for gmt_field, rfq_field in field_mapping.items():
            if gmt_field in entities and entities[gmt_field] is not None:
                value = entities[gmt_field]
                
                # Set the value if we don't have it or new value is more detailed
                current_value = getattr(rfq, rfq_field, None)
                if not current_value or (isinstance(value, str) and len(str(value)) > len(str(current_value))):
                    setattr(rfq, rfq_field, value)
                    updated_fields.append(rfq_field)
                    logger.info(f"Updated RFQ field {rfq_field}: {value}")
        
        return updated_fields
    
    def _rfq_to_dict(self, rfq: RFQ) -> Dict[str, Any]:
        """Convert RFQ object to dictionary for easy handling."""
        return {
            "id": rfq.id,
            "product_name": rfq.product_name,
            "specifications": rfq.specifications,
            "quantity": rfq.quantity,
            "unit_of_measure": rfq.unit_of_measure,
            "deadline": rfq.deadline,
            "division": rfq.division,
            "category": rfq.category,
            "preferred_brand": rfq.preferred_brand,
            "delivery_state": rfq.delivery_state,
            "delivery_city": rfq.delivery_city,
            "delivery_pincode": rfq.delivery_pincode,
            "remarks": rfq.remarks,
            "status": rfq.status,
            "created_at": rfq.created_at.isoformat() if rfq.created_at else None
        }
    
    async def get_rfq_by_id(self, rfq_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve RFQ by ID."""
        try:
            with SessionLocal() as db:
                rfq = db.query(RFQ).filter(RFQ.id == rfq_id).first()
                if rfq:
                    return self._rfq_to_dict(rfq)
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
                return [self._rfq_to_dict(rfq) for rfq in rfqs]
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
        """Submit completed RFQ to GMT backend system."""
        try:
            rfq_data = await self.get_rfq_by_id(rfq_id)
            if not rfq_data:
                return {"success": False, "error": "RFQ not found"}
            
            gmt_service = GMTAPIService()
            
            # Submit to actual GMT API
            submission_result = await gmt_service.create_rfq(rfq_data)
            
            if submission_result.get("success"):
                # Update status to submitted
                success = await self.update_rfq_status(rfq_id, "submitted")
                
                if success:
                    logger.info(f"Successfully submitted RFQ {rfq_id} to GMT backend")
                    return {
                        "success": True, 
                        "rfq_id": rfq_id,
                        "backend_reference": submission_result.get("backend_reference", f"GMT_{rfq_id[:8]}"),
                        "gmt_response": submission_result.get("response")
                    }
                else:
                    return {"success": False, "error": "RFQ submitted to GMT but failed to update local status"}
            else:
                logger.error(f"Failed to submit RFQ {rfq_id} to GMT: {submission_result.get('error')}")
                return {"success": False, "error": f"GMT API error: {submission_result.get('error')}"}
                
        except Exception as e:
            logger.error(f"Error submitting RFQ to backend: {e}")
            return {"success": False, "error": str(e)}
    
    # Legacy methods kept for compatibility
    def initialize_rfq_workflow(self, user_id: str, initial_message: str):
        """Legacy method - functionality moved to create_or_update_rfq."""
        pass
        
    def collect_rfq_field(self, user_id: str, field_response: str):
        """Legacy method - functionality handled by EntityService."""
        pass
        
    def validate_and_submit_rfq(self, user_id: str):
        """Legacy method - use submit_rfq_to_backend instead."""
        pass
        
    def get_field_collection_prompt(self, field_name: str, rfq_state: dict):
        """Legacy method - EntityService now provides next_questions."""
        pass
        
    def _generate_rfq_id(self):
        """Generate unique RFQ identifier."""
        return str(uuid.uuid4())
        
    def _get_next_required_field(self, collected_fields: dict):
        """Legacy method - EntityService handles this logic."""
        pass
        
    def _store_rfq_state(self, user_id: str, rfq_state: dict):
        """Legacy method - state stored in database."""
        pass
        
    def _get_rfq_state(self, user_id: str):
        """Legacy method - state retrieved from database."""
        pass
        
    def _extract_field_value(self, field_name: str, response: str):
        """Legacy method - EntityService handles extraction."""
        pass
        
    def _validate_field_value(self, field_name: str, value):
        """Legacy method - EntityService provides validation."""
        pass
        
    def _validate_complete_rfq(self, fields: dict):
        """Legacy method - EntityService tracks completeness."""
        pass
        
    def _submit_to_backend(self, rfq_state: dict):
        """Legacy method - use submit_rfq_to_backend instead."""
        pass