"""
Mock Procucev service for external integrations.

This service mocks all external Procucev API calls including:
- Payment link generation for seller subscriptions
- RFQ email sending to sellers
- Pending bid retrieval
- Credit management and billing
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import random
import uuid

from app.database import get_db_session
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class MockProcucevService:
    """
    Mock implementation of external Procucev API services.
    
    This service simulates all external API calls that would normally
    go to Procucev's payment, email, and RFQ management systems.
    """
    
    def __init__(self, db_session=None):
        self.db_session = db_session or get_db_session()
        self.settings = get_settings()
        
        # Mock statistics
        self.stats = {
            "payment_links_generated": 0,
            "emails_sent": 0,
            "pending_bids_requests": 0,
            "api_calls_total": 0
        }
        
        # Mock pending RFQs data
        self.mock_pending_rfqs = [
            {
                "rfq_id": "RFQ2024001",
                "rfq_title": "Medical Equipment Procurement",
                "deadline": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"),
                "estimated_value": "₹2,50,000"
            },
            {
                "rfq_id": "RFQ2024002", 
                "rfq_title": "Office Supplies Bulk Order",
                "deadline": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
                "estimated_value": "₹75,000"
            },
            {
                "rfq_id": "RFQ2024003",
                "rfq_title": "IT Hardware Procurement",
                "deadline": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
                "estimated_value": "₹5,00,000"
            }
        ]
    
    @log_service_method("mock_procucev")
    async def generate_payment_link(self, seller_id: str, amount: int, plan_type: str, credits: int) -> Dict[str, Any]:
        """
        Generate mock payment link for seller subscription.
        
        Args:
            seller_id: Seller identifier
            amount: Payment amount in INR
            plan_type: Subscription plan type (basic/pro)
            credits: Number of credits in the plan
            
        Returns:
            Dictionary with payment link details
        """
        try:
            logger.info(f"Generating mock payment link for seller {seller_id}: ₹{amount} ({plan_type})")
            
            # Simulate API processing time
            await asyncio.sleep(random.uniform(0.1, 0.3))
            
            # Generate mock payment link
            payment_id = f"pay_{uuid.uuid4().hex[:12]}"
            payment_link = f"https://payments.procucev.com/pay/{payment_id}"
            
            # Update statistics
            self.stats["payment_links_generated"] += 1
            self.stats["api_calls_total"] += 1
            
            return {
                "success": True,
                "payment_link": payment_link,
                "payment_id": payment_id,
                "amount": amount,
                "currency": "INR",
                "plan_type": plan_type,
                "credits": credits,
                "expires_at": (datetime.now() + timedelta(hours=24)).isoformat(),
                "payment_methods": ["UPI", "Net Banking", "Credit Card", "Debit Card"]
            }
            
        except Exception as e:
            logger.error(f"Error generating payment link: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
    
    @log_service_method("mock_procucev")
    async def send_rfq_email(self, seller_id: str, rfq_id: str) -> Dict[str, Any]:
        """
        Send RFQ details via email to seller (mock implementation).
        
        Args:
            seller_id: Seller identifier
            rfq_id: RFQ identifier
            
        Returns:
            Dictionary with email sending status
        """
        try:
            logger.info(f"Sending mock RFQ email for {rfq_id} to seller {seller_id}")
            
            # Simulate email processing time
            await asyncio.sleep(random.uniform(0.2, 0.5))
            
            # Generate mock email ID
            email_id = f"email_{uuid.uuid4().hex[:16]}"
            
            # Update statistics
            self.stats["emails_sent"] += 1
            self.stats["api_calls_total"] += 1
            
            return {
                "success": True,
                "email_id": email_id,
                "rfq_id": rfq_id,
                "seller_id": seller_id,
                "sent_at": datetime.now().isoformat(),
                "email_subject": f"New RFQ Available: {rfq_id}",
                "delivery_status": "queued"
            }
            
        except Exception as e:
            logger.error(f"Error sending RFQ email: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
    
    @log_service_method("mock_procucev")
    async def get_seller_pending_bids(self, seller_id: str) -> Dict[str, Any]:
        """
        Get seller's pending RFQ bids (mock implementation).
        
        Args:
            seller_id: Seller identifier
            
        Returns:
            Dictionary with pending bids list
        """
        try:
            logger.info(f"Fetching mock pending bids for seller {seller_id}")
            
            # Simulate API processing time
            await asyncio.sleep(random.uniform(0.1, 0.2))
            
            # Return random subset of mock pending RFQs
            num_pending = random.randint(0, len(self.mock_pending_rfqs))
            pending_bids = random.sample(self.mock_pending_rfqs, num_pending)
            
            # Update statistics
            self.stats["pending_bids_requests"] += 1
            self.stats["api_calls_total"] += 1
            
            return {
                "success": True,
                "seller_id": seller_id,
                "pending_bids": pending_bids,
                "total_pending": len(pending_bids),
                "fetched_at": datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error fetching pending bids: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get mock service statistics."""
        return {
            "service_name": "MockProcucevService",
            "statistics": self.stats.copy(),
            "uptime_seconds": 3600,  # Mock uptime
            "last_reset": "2024-01-01T00:00:00Z",
            "endpoints": [
                "generate_payment_link",
                "send_rfq_email", 
                "get_seller_pending_bids"
            ]
        }