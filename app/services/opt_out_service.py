"""
Simple opt-out service for managing seller notification preferences.

This service handles WhatsApp-based opt-out and opt-in requests from sellers,
using OpenAI service for intelligent intent detection and message generation.
"""

import logging
from typing import Dict, Optional, Any
from datetime import datetime
from sqlalchemy.orm import Session

from app.database import get_db_session
from app.models import Seller
from app.services.whatsapp_service import WhatsAppService
from app.services.openai_service import OpenAIService
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class OptOutService:
    """Simple service for managing seller opt-out and opt-in preferences."""
    
    def __init__(self, db_session: Optional[Session] = None):
        self.db_session = db_session or get_db_session()
        self.whatsapp_service = WhatsAppService()
        self.openai_service = OpenAIService()
    
    @log_service_method("opt_out")
    async def handle_opt_out_request(self, phone_number: str) -> Dict[str, bool]:
        """Handle opt-out request from a seller."""
        try:
            seller = await self._get_seller_by_phone(phone_number)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Update seller to opt-out
            await self._update_seller_opt_out_status(seller.seller_id, True)
            
            # Generate confirmation message using OpenAI service
            message = self.openai_service.generate_opt_out_confirmation(seller.seller_name)
            
            # Send confirmation
            response = await self.whatsapp_service.send_message(phone_number, message)
            
            return {
                "success": True,
                "seller_id": seller.seller_id,
                "opted_out": True,
                "message_sent": response.success
            }
                
        except Exception as e:
            logger.error(f"Error processing opt-out: {str(e)}")
            return {"success": False, "error": str(e)}
    
    @log_service_method("opt_out")
    async def handle_opt_in_request(self, phone_number: str) -> Dict[str, bool]:
        """Handle opt-in request from a seller."""
        try:
            seller = await self._get_seller_by_phone(phone_number)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Update seller to opt-in
            await self._update_seller_opt_out_status(seller.seller_id, False)
            
            # Generate confirmation message using OpenAI service
            message = self.openai_service.generate_opt_in_confirmation(seller.seller_name, seller.categories)
            
            # Send confirmation
            response = await self.whatsapp_service.send_message(phone_number, message)
            
            return {
                "success": True,
                "seller_id": seller.seller_id,
                "opted_out": False,
                "message_sent": response.success
            }
                
        except Exception as e:
            logger.error(f"Error processing opt-in: {str(e)}")
            return {"success": False, "error": str(e)}
    
    @log_service_method("opt_out")
    async def send_permission_request(self, seller_id: str) -> Dict[str, bool]:
        """Send permission request to a seller with NULL consent status."""
        try:
            seller = await self._get_seller_by_id(seller_id)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Only send permission request if status is NULL (no permission asked yet)
            if seller.opted_out_notifications is not None:
                return {"success": False, "error": "Seller already has consent status"}
            
            # Generate permission request using OpenAI service
            message = self.openai_service.generate_permission_request(seller.seller_name, seller.categories)
            
            # Send permission request
            response = await self.whatsapp_service.send_message(seller.phone_number, message)
            
            return {
                "success": True,
                "seller_id": seller_id,
                "message_sent": response.success
            }
                
        except Exception as e:
            logger.error(f"Error sending permission request: {str(e)}")
            return {"success": False, "error": str(e)}
    
    @log_service_method("opt_out")
    async def check_seller_notification_eligibility(self, seller_id: str) -> Dict[str, Any]:
        """Check if seller is eligible for RFQ notifications based on consent status."""
        try:
            seller = await self._get_seller_by_id(seller_id)
            if not seller:
                return {"eligible": False, "reason": "Seller not found"}
            
            if seller.opted_out_notifications is True:
                return {"eligible": False, "reason": "Seller opted out"}
            elif seller.opted_out_notifications is False:
                return {"eligible": True, "reason": "Seller opted in"}
            else:  # seller.opted_out_notifications is None
                return {"eligible": False, "reason": "Permission needed", "action": "send_permission_request"}
                
        except Exception as e:
            logger.error(f"Error checking seller eligibility: {str(e)}")
            return {"eligible": False, "reason": f"Error: {str(e)}"}

    @log_service_method("opt_out")
    def detect_opt_out_intent(self, message: str) -> Dict[str, str]:
        """Detect if message contains opt-out or opt-in intent using OpenAI."""
        try:
            result = self.openai_service.detect_opt_out_intent(message)
            return {
                "intent": result.get("intent", "none"),
                "confidence": result.get("confidence", 0),
                "reasoning": result.get("reasoning", ""),
                "success": result.get("success", False)
            }
        except Exception as e:
            logger.error(f"Error detecting opt-out intent: {str(e)}")
            return {
                "intent": "none",
                "confidence": 0,
                "reasoning": f"Error: {str(e)}",
                "success": False
            }
    
    # Private helper methods
    
    async def _get_seller_by_phone(self, phone_number: str) -> Optional[Seller]:
        """Get seller by phone number."""
        return self.db_session.query(Seller).filter(Seller.phone_number == phone_number).first()
    
    async def _get_seller_by_id(self, seller_id: str) -> Optional[Seller]:
        """Get seller by ID."""
        return self.db_session.query(Seller).filter(Seller.seller_id == seller_id).first()
    
    async def _update_seller_opt_out_status(self, seller_id: str, opted_out: bool) -> None:
        """Update seller's opt-out status."""
        seller = self.db_session.query(Seller).filter(Seller.seller_id == seller_id).first()
        if seller:
            seller.opted_out_notifications = opted_out
            seller.updated_at = datetime.utcnow()
            self.db_session.commit()