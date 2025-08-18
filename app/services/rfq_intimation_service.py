"""
RFQ intimation service for WhatsApp-based seller notifications.

This service handles the complete WhatsApp conversation flow with sellers
when they receive RFQ notifications, as defined in the flow document:

1. Initial RFQ notification (different based on credit status)
2. Subscription plan selection and payment processing  
3. RFQ ID validation and email sending
4. 5-minute timeout handling with reminders
5. End-of-flow reminders with pending bids

Key responsibilities:
- Send structured RFQ notifications via WhatsApp
- Handle subscription plan conversations
- Process RFQ ID requests and email triggers
- Manage conversation timeouts and reminders
- Track seller interactions for analytics
- Integration with mock Procurev APIs
"""

import logging
import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.database import get_db_session
from app.models import (
    Seller, SellerRFQInteraction, RFQSellerNotification, 
    InteractionType, ResponseType, RFQ, MockRFQ, SystemConfiguration
)
from app.services.whatsapp_service import WhatsAppService, MessageResponse
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class RFQIntimationService:
    """
    Service for managing WhatsApp-based RFQ intimation conversations with sellers.
    
    Implements the complete conversation flow including credit checking,
    subscription management, RFQ delivery, and timeout handling.
    """
    
    def __init__(self, db_session: Optional[Session] = None):
        self.db_session = db_session or get_db_session()
        self.settings = get_settings()
        self.whatsapp_service = WhatsAppService()
        self.mock_procurev = None  # Lazy load
        
        # Subscription plan details
        self.subscription_plans = {
            "basic": {"price": 499, "credits": 5},
            "pro": {"price": 999, "credits": 15}
        }
        
        # Conversation timeout in minutes
        self.conversation_timeout_minutes = 5
    
    def _get_mock_procurev(self):
        """Lazy load MockProcucevService to avoid circular imports."""
        if self.mock_procurev is None:
            from app.services.procucev_service import MockProcucevService
            self.mock_procurev = MockProcucevService()
        return self.mock_procurev
    
    @log_service_method("rfq_intimation")
    async def send_rfq_notification(self, seller_id: str, rfq_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send initial RFQ notification to seller based on their credit status.
        
        Args:
            seller_id: Seller identifier
            rfq_data: RFQ information including id, title, description, categories
            
        Returns:
            Dictionary with notification status and next steps
        """
        try:
            logger.info(f"Sending RFQ notification to seller {seller_id} for RFQ {rfq_data.get('rfq_id')}")
            
            # Get seller details
            seller = await self._get_seller_details(seller_id)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Check if seller is opted out
            if seller.opted_out_notifications:
                logger.info(f"Seller {seller_id} has opted out of notifications")
                return {"success": False, "error": "Seller opted out of notifications"}
            
            # Generate RFQ description for notification
            rfq_brief = await self._generate_rfq_brief(rfq_data)
            
            # Determine message based on credit status
            if seller.subscription_credits > 0:
                # Seller has credits - direct RFQ request flow
                message = await self._create_credited_seller_message(seller, rfq_brief, rfq_data)
                next_step = "rfq_id_request"
            else:
                # Seller has no credits - subscription flow
                message = await self._create_uncredited_seller_message(seller, rfq_brief)
                next_step = "subscription_selection"
            
            # Send WhatsApp message
            response = await self.whatsapp_service.send_message(
                recipient_id=seller.phone_number,
                message=message
            )
            
            if response.success:
                # Record notification in database
                await self._record_notification(seller_id, rfq_data.get('rfq_id'), response.message_id)
                
                # Record interaction
                await self._record_interaction(
                    seller_id, 
                    rfq_data.get('rfq_id'),
                    InteractionType.notification_sent,
                    {
                        "message_sent": message,
                        "has_credits": seller.subscription_credits > 0,
                        "credits_remaining": seller.subscription_credits,
                        "next_step": next_step,
                        "whatsapp_message_id": response.message_id
                    }
                )
                
                # Start conversation timeout timer
                await self._start_conversation_timeout(seller_id, rfq_data.get('rfq_id'))
                
                return {
                    "success": True,
                    "message_id": response.message_id,
                    "next_step": next_step,
                    "seller_has_credits": seller.subscription_credits > 0,
                    "message_sent": message
                }
            else:
                logger.error(f"Failed to send WhatsApp message to {seller.phone_number}: {response.error}")
                return {"success": False, "error": f"WhatsApp delivery failed: {response.error}"}
                
        except Exception as e:
            logger.error(f"Error sending RFQ notification: {str(e)}")
            return {"success": False, "error": str(e)}
    
    @log_service_method("rfq_intimation")
    async def handle_subscription_selection(self, seller_id: str, rfq_id: str, plan_type: str) -> Dict[str, Any]:
        """
        Handle seller's subscription plan selection and initiate payment flow.
        
        Args:
            seller_id: Seller identifier
            rfq_id: Associated RFQ identifier  
            plan_type: Selected plan ("basic" or "pro")
            
        Returns:
            Dictionary with payment link and next steps
        """
        try:
            logger.info(f"Processing subscription selection for seller {seller_id}: {plan_type}")
            
            seller = await self._get_seller_details(seller_id)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            if plan_type not in self.subscription_plans:
                return {"success": False, "error": "Invalid subscription plan"}
            
            plan_details = self.subscription_plans[plan_type]
            
            # Generate payment link via mock Procurev service
            payment_response = await self._get_mock_procurev().generate_payment_link(
                seller_id=seller_id,
                amount=plan_details["price"],
                plan_type=plan_type,
                credits=plan_details["credits"]
            )
            
            if payment_response["success"]:
                # Send payment message to seller
                payment_message = f"""Thanks for selecting the *{plan_type.title()} Plan*! 

💳 *Amount*: ₹{plan_details['price']} for {plan_details['credits']} RFQs

Click the link below to pay. We've also emailed it to you.
{payment_response['payment_link']}

⏰ This conversation will automatically close in 5 minutes."""
                
                whatsapp_response = await self.whatsapp_service.send_message(
                    recipient_id=seller.phone_number,
                    message=payment_message
                )
                
                if whatsapp_response.success:
                    # Record interaction
                    await self._record_interaction(
                        seller_id, rfq_id,
                        InteractionType.payment_initiated,
                        {
                            "plan_selected": plan_type,
                            "amount": plan_details["price"], 
                            "credits": plan_details["credits"],
                            "payment_link": payment_response["payment_link"],
                            "payment_id": payment_response.get("payment_id")
                        }
                    )
                    
                    return {
                        "success": True,
                        "payment_link": payment_response["payment_link"],
                        "payment_id": payment_response.get("payment_id"),
                        "message_sent": payment_message,
                        "timeout_minutes": self.conversation_timeout_minutes
                    }
                else:
                    return {"success": False, "error": "Failed to send payment message"}
            else:
                return {"success": False, "error": f"Payment link generation failed: {payment_response.get('error')}"}
                
        except Exception as e:
            logger.error(f"Error handling subscription selection: {str(e)}")
            return {"success": False, "error": str(e)}
    
    @log_service_method("rfq_intimation")
    async def handle_rfq_id_request(self, seller_id: str, rfq_id: str, user_message: str) -> Dict[str, Any]:
        """
        Handle seller's RFQ ID entry and trigger email sending.
        
        Args:
            seller_id: Seller identifier
            rfq_id: RFQ identifier from user input
            user_message: Raw user message containing RFQ ID
            
        Returns:
            Dictionary with email sending status and confirmation
        """
        try:
            logger.info(f"Processing RFQ ID request from seller {seller_id}: {user_message}")
            
            seller = await self._get_seller_details(seller_id)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Validate RFQ ID exists
            rfq_exists = await self._validate_rfq_id(rfq_id)
            if not rfq_exists:
                error_message = f"""❌ Sorry, RFQ ID "{rfq_id}" is not valid or not found.

Please check the RFQ ID and try again, or contact support@procurev.com for assistance."""
                
                await self.whatsapp_service.send_message(seller.phone_number, error_message)
                return {"success": False, "error": "Invalid RFQ ID"}
            
            # Check if seller has credits
            if seller.subscription_credits <= 0:
                no_credits_message = """❌ You don't have enough credits to request this RFQ.

Please purchase a subscription plan first:
• *Basic Plan* – ₹499 for 5 RFQs
• *Pro Plan* – ₹999 for 15 RFQs

Reply with "Basic" or "Pro" to subscribe."""
                
                await self.whatsapp_service.send_message(seller.phone_number, no_credits_message)
                return {"success": False, "error": "Insufficient credits"}
            
            # Trigger RFQ email via mock Procurev service
            email_response = await self._get_mock_procurev().send_rfq_email(seller_id, rfq_id)
            
            if email_response["success"]:
                # Deduct credit from seller
                await self._deduct_seller_credit(seller_id)
                
                # Send confirmation message
                confirmation_message = f"""✅ Thank you! Your RFQ ID *{rfq_id}* has been emailed to you.

📧 Check your email: {seller.email}
💳 Credits remaining: {seller.subscription_credits - 1}

⏰ This conversation will automatically close in 5 minutes."""
                
                whatsapp_response = await self.whatsapp_service.send_message(
                    recipient_id=seller.phone_number,
                    message=confirmation_message
                )
                
                if whatsapp_response.success:
                    # Record interaction
                    await self._record_interaction(
                        seller_id, rfq_id,
                        InteractionType.email_sent,
                        {
                            "rfq_id_requested": rfq_id,
                            "email_sent_to": seller.email,
                            "credits_deducted": 1,
                            "remaining_credits": seller.subscription_credits - 1,
                            "email_reference": email_response.get("email_id")
                        }
                    )
                    
                    # Update notification response
                    await self._update_notification_response(seller_id, rfq_id, ResponseType.rfq_request)
                    
                    return {
                        "success": True,
                        "email_sent": True,
                        "email_reference": email_response.get("email_id"),
                        "credits_deducted": 1,
                        "remaining_credits": seller.subscription_credits - 1,
                        "message_sent": confirmation_message
                    }
                else:
                    return {"success": False, "error": "Failed to send confirmation message"}
            else:
                error_message = f"""❌ Sorry, there was an error sending the RFQ email.

Please contact support@procurev.com with RFQ ID: {rfq_id}

We'll resolve this issue and send you the RFQ manually."""
                
                await self.whatsapp_service.send_message(seller.phone_number, error_message)
                return {"success": False, "error": f"Email sending failed: {email_response.get('error')}"}
                
        except Exception as e:
            logger.error(f"Error handling RFQ ID request: {str(e)}")
            return {"success": False, "error": str(e)}
    
    @log_service_method("rfq_intimation")
    async def handle_conversation_timeout(self, seller_id: str, rfq_id: str) -> Dict[str, Any]:
        """
        Handle 5-minute conversation timeout with end-of-flow reminder.
        
        Args:
            seller_id: Seller identifier
            rfq_id: Associated RFQ identifier
            
        Returns:
            Dictionary with timeout handling status
        """
        try:
            logger.info(f"Handling conversation timeout for seller {seller_id}, RFQ {rfq_id}")
            
            seller = await self._get_seller_details(seller_id)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Get seller's pending bids via mock Procurev API
            pending_bids_response = await self._get_mock_procurev().get_seller_pending_bids(seller_id)
            
            if pending_bids_response["success"]:
                pending_bids = pending_bids_response.get("pending_bids", [])
                
                if pending_bids:
                    # Create reminder message with pending bids
                    reminder_message = f"""Thanks for chatting with us! You still have live RFQs for which bids haven't been submitted.

📝 *Your Pending RFQs:*
"""
                    
                    for i, bid in enumerate(pending_bids[:3], 1):  # Show max 3
                        reminder_message += f"{i}. *{bid.get('rfq_title', 'RFQ')}* (ID: {bid.get('rfq_id', 'N/A')})\n"
                        reminder_message += f"   Deadline: {bid.get('deadline', 'Not specified')}\n\n"
                    
                    if len(pending_bids) > 3:
                        reminder_message += f"... and {len(pending_bids) - 3} more RFQs\n\n"
                    
                    reminder_message += """We encourage you to submit bids. For help, contact@procurev.com

Have a great day! 👋"""
                else:
                    # No pending bids
                    reminder_message = f"""Thanks for chatting with us!

You're all caught up with your RFQs. We'll notify you when new opportunities matching your categories become available.

For any assistance, contact@procurev.com

Have a great day! 👋"""
            else:
                # Fallback message if pending bids API fails
                reminder_message = f"""Thanks for chatting with us!

For any assistance with RFQs or your account, contact@procurev.com

Have a great day! 👋"""
            
            # Send reminder message
            whatsapp_response = await self.whatsapp_service.send_message(
                recipient_id=seller.phone_number,
                message=reminder_message
            )
            
            if whatsapp_response.success:
                # Record interaction
                await self._record_interaction(
                    seller_id, rfq_id,
                    InteractionType.notification_sent,
                    {
                        "interaction_type": "timeout_reminder",
                        "pending_bids_count": len(pending_bids) if pending_bids_response["success"] else 0,
                        "message_sent": reminder_message
                    }
                )
                
                # Update notification as ignored if no response
                await self._update_notification_response(seller_id, rfq_id, ResponseType.ignored)
                
                return {
                    "success": True,
                    "reminder_sent": True,
                    "pending_bids_count": len(pending_bids) if pending_bids_response["success"] else 0,
                    "message_sent": reminder_message
                }
            else:
                return {"success": False, "error": "Failed to send timeout reminder"}
                
        except Exception as e:
            logger.error(f"Error handling conversation timeout: {str(e)}")
            return {"success": False, "error": str(e)}
    
    # Private helper methods
    
    async def _get_seller_details(self, seller_id: str) -> Optional[Seller]:
        """Get seller details from database."""
        return self.db_session.query(Seller).filter(Seller.seller_id == seller_id).first()
    
    async def _generate_rfq_brief(self, rfq_data: Dict[str, Any]) -> str:
        """Generate brief RFQ description for notification."""
        title = rfq_data.get('rfq_title', 'RFQ Available')
        categories = rfq_data.get('categories', [])
        quantity = rfq_data.get('quantity_info', '')
        
        brief = title
        if categories:
            brief += f" - {', '.join(categories)}"
        if quantity:
            brief += f" ({quantity})"
        
        return brief
    
    async def _create_credited_seller_message(self, seller: Seller, rfq_brief: str, rfq_data: Dict[str, Any]) -> str:
        """Create notification message for sellers with credits."""
        return f"""🔔 Hi {seller.seller_name}, there is a New RFQ available in your category.

📋 *RFQ Brief*: {rfq_brief}

💳 Credits remaining: {seller.subscription_credits}

Please enter the RFQ ID so that we can email this to you:
*{rfq_data.get('rfq_id')}*"""
    
    async def _create_uncredited_seller_message(self, seller: Seller, rfq_brief: str) -> str:
        """Create notification message for sellers without credits."""
        return f"""🔔 Hi {seller.seller_name}, there is a New RFQ available in your category.

📋 *RFQ Brief*: {rfq_brief}

Looks like your request credits are over. Here are our subscription plans:

1. *Basic Plan* – ₹499 for 5 RFQs
2. *Pro Plan* – ₹999 for 15 RFQs

Please reply with "Basic" or "Pro" to subscribe and download the RFQ."""
    
    async def _record_notification(self, seller_id: str, rfq_id: str, message_id: str) -> None:
        """Record notification in database."""
        notification = RFQSellerNotification(
            rfq_id=rfq_id,
            seller_id=seller_id,
            sent_at=datetime.utcnow()
        )
        self.db_session.add(notification)
        self.db_session.commit()
    
    async def _record_interaction(self, seller_id: str, rfq_id: str, interaction_type: InteractionType, data: Dict[str, Any]) -> None:
        """Record seller interaction in database."""
        interaction = SellerRFQInteraction(
            seller_id=seller_id,
            rfq_id=rfq_id,
            interaction_type=interaction_type,
            interaction_data=data
        )
        self.db_session.add(interaction)
        self.db_session.commit()
    
    async def _start_conversation_timeout(self, seller_id: str, rfq_id: str) -> None:
        """Start 5-minute conversation timeout timer."""
        # In production, this would use Celery or similar task queue
        # For now, we'll implement a simple async timeout
        async def timeout_handler():
            await asyncio.sleep(self.conversation_timeout_minutes * 60)  # 5 minutes
            await self.handle_conversation_timeout(seller_id, rfq_id)
        
        # Start timeout task in background
        asyncio.create_task(timeout_handler())
    
    async def _validate_rfq_id(self, rfq_id: str) -> bool:
        """Validate that RFQ ID exists and is accessible."""
        # Check in main RFQ table first, then mock RFQ table for backward compatibility
        rfq = self.db_session.query(RFQ).filter(RFQ.rfq_id == rfq_id).first()
        if rfq:
            return True
        
        # Fallback to mock RFQ table for testing
        mock_rfq = self.db_session.query(MockRFQ).filter(MockRFQ.rfq_id == rfq_id).first()
        return mock_rfq is not None
    
    async def _deduct_seller_credit(self, seller_id: str) -> None:
        """Deduct one credit from seller's account."""
        seller = self.db_session.query(Seller).filter(Seller.seller_id == seller_id).first()
        if seller and seller.subscription_credits > 0:
            seller.subscription_credits -= 1
            self.db_session.commit()
    
    async def _update_notification_response(self, seller_id: str, rfq_id: str, response_type: ResponseType) -> None:
        """Update notification with seller's response type."""
        notification = self.db_session.query(RFQSellerNotification)\
            .filter(and_(
                RFQSellerNotification.seller_id == seller_id,
                RFQSellerNotification.rfq_id == rfq_id
            ))\
            .order_by(RFQSellerNotification.sent_at.desc())\
            .first()
        
        if notification:
            notification.response_type = response_type
            notification.responded_at = datetime.utcnow()
            self.db_session.commit()