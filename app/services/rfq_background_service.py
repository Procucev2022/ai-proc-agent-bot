"""
Background job processing service for RFQ seller notifications.

This service handles the offline processing of approved RFQs including:
- Seller selection and recommendation processing
- Batch WhatsApp notification sending  
- Conversation timeout handling
- System cleanup and maintenance jobs
- Error handling and retry logic

The service uses async processing to handle multiple RFQs concurrently
while respecting rate limits and system constraints.

Key responsibilities:
- Process approved RFQ notifications in batches
- Coordinate seller selection and notification sending
- Handle background job scheduling and execution
- Manage conversation timeouts and reminders
- Provide monitoring and status tracking
- Implement retry logic for failed operations
"""

import logging
import asyncio
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from app.database import get_db_session
from app.models import (
    RFQ, MockRFQ, Seller, RFQSellerNotification, SellerRFQInteraction,
    RFQStatus, InteractionType, NotificationType
)
from app.services.seller_recommendation_service import SellerRecommendationService
from app.services.rfq_intimation_service import RFQIntimationService
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class RFQBackgroundService:
    """
    Background service for processing RFQ notifications and seller interactions.
    
    Manages the complete background workflow from RFQ approval through
    seller selection, notification sending, and conversation management.
    """
    
    def __init__(self, db_session: Optional[Session] = None):
        self.db_session = db_session or get_db_session()
        self.settings = get_settings()
        
        # Initialize dependent services
        self.recommendation_service = SellerRecommendationService(self.db_session)
        self.intimation_service = RFQIntimationService(self.db_session)
        
        # Configuration
        self.max_concurrent_rfqs = 5  # Process 5 RFQs concurrently
        self.max_concurrent_notifications = 10  # Send 10 notifications concurrently
        self.notification_batch_size = 50  # Process notifications in batches of 50
        self.retry_attempts = 3
        self.retry_delay_seconds = 30
        
        # Job tracking
        self._active_jobs = {}
        self._job_stats = {
            "rfqs_processed": 0,
            "sellers_notified": 0,
            "notifications_sent": 0,
            "errors_occurred": 0
        }
    
    @log_service_method("rfq_background")
    async def process_approved_rfq(self, rfq_id: str) -> Dict[str, Any]:
        """
        Main method to process an approved RFQ through the complete workflow.
        
        Workflow:
        1. Validate and fetch RFQ data
        2. Select qualifying sellers using recommendation service
        3. Send batch notifications via intimation service
        4. Track results and handle errors
        
        Args:
            rfq_id: RFQ identifier to process
            
        Returns:
            Dictionary with processing results and statistics
        """
        job_id = f"rfq_job_{rfq_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
        
        try:
            logger.info(f"Starting RFQ processing job {job_id} for RFQ {rfq_id}")
            
            # Track active job
            job_start = datetime.utcnow()
            self._active_jobs[job_id] = {
                "rfq_id": rfq_id,
                "status": "processing",
                "started_at": job_start,
                "stage": "initialization"
            }
            
            # Step 1: Fetch and validate RFQ data
            self._active_jobs[job_id]["stage"] = "rfq_validation"
            rfq_data = await self._fetch_rfq_data(rfq_id)
            if not rfq_data:
                raise ValueError(f"RFQ {rfq_id} not found or invalid")
            
            logger.info(f"Processing RFQ: {rfq_data.get('rfq_title', 'Unknown')}")
            
            # Step 2: Select sellers using recommendation service
            self._active_jobs[job_id]["stage"] = "seller_selection"
            selection_result = await self.recommendation_service.select_sellers_for_rfq(rfq_data)
            
            if selection_result["total_selected"] == 0:
                logger.warning(f"No sellers selected for RFQ {rfq_id}")
                return {
                    "success": True,
                    "job_id": job_id,
                    "rfq_id": rfq_id,
                    "sellers_selected": 0,
                    "notifications_sent": 0,
                    "reason": "No qualifying sellers found"
                }
            
            logger.info(f"Selected {selection_result['total_selected']} sellers for RFQ {rfq_id}")
            
            # Step 3: Send notifications in batches
            self._active_jobs[job_id]["stage"] = "notification_sending"
            all_selected_sellers = (
                selection_result["subscribed_sellers"] + 
                selection_result["unsubscribed_sellers"]
            )
            
            notification_results = await self._send_batch_notifications(
                rfq_data, all_selected_sellers, job_id
            )
            
            # Step 4: Compile results
            job_duration = (datetime.utcnow() - job_start).total_seconds()
            
            result = {
                "success": True,
                "job_id": job_id,
                "rfq_id": rfq_id,
                "processing_time_seconds": job_duration,
                "sellers_selected": selection_result["total_selected"],
                "subscribed_sellers": len(selection_result["subscribed_sellers"]),
                "unsubscribed_sellers": len(selection_result["unsubscribed_sellers"]),
                "notifications_sent": notification_results["successful"],
                "notifications_failed": notification_results["failed"],
                "selection_metadata": selection_result["selection_metadata"],
                "notification_details": notification_results["details"]
            }
            
            # Update job tracking
            self._active_jobs[job_id]["status"] = "completed"
            self._active_jobs[job_id]["completed_at"] = datetime.utcnow()
            self._active_jobs[job_id]["result"] = result
            
            # Update statistics
            self._job_stats["rfqs_processed"] += 1
            self._job_stats["sellers_notified"] += selection_result["total_selected"]
            self._job_stats["notifications_sent"] += notification_results["successful"]
            
            # Update RFQ status to submitted after successful processing
            await self._update_rfq_status(rfq_id, RFQStatus.submitted)
            
            logger.info(f"Completed RFQ processing job {job_id} - {notification_results['successful']} notifications sent")
            return result
            
        except Exception as e:
            logger.error(f"Error in RFQ processing job {job_id}: {str(e)}")
            
            # Update job tracking
            self._active_jobs[job_id]["status"] = "failed"
            self._active_jobs[job_id]["error"] = str(e)
            self._active_jobs[job_id]["completed_at"] = datetime.utcnow()
            
            # Update statistics
            self._job_stats["errors_occurred"] += 1
            
            return {
                "success": False,
                "job_id": job_id,
                "rfq_id": rfq_id,
                "error": str(e),
                "processing_time_seconds": (datetime.utcnow() - job_start).total_seconds()
            }
    
    @log_service_method("rfq_background")
    async def process_multiple_rfqs(self, rfq_ids: List[str]) -> Dict[str, Any]:
        """
        Process multiple approved RFQs concurrently.
        
        Args:
            rfq_ids: List of RFQ identifiers to process
            
        Returns:
            Dictionary with batch processing results
        """
        try:
            logger.info(f"Starting batch processing for {len(rfq_ids)} RFQs")
            
            batch_start = datetime.utcnow()
            
            # Process RFQs in concurrent batches
            semaphore = asyncio.Semaphore(self.max_concurrent_rfqs)
            
            async def process_single_rfq(rfq_id: str):
                async with semaphore:
                    return await self.process_approved_rfq(rfq_id)
            
            # Execute all RFQ processing tasks concurrently
            results = await asyncio.gather(
                *[process_single_rfq(rfq_id) for rfq_id in rfq_ids],
                return_exceptions=True
            )
            
            # Compile batch results
            successful = 0
            failed = 0
            total_notifications = 0
            errors = []
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    failed += 1
                    errors.append(f"RFQ {rfq_ids[i]}: {str(result)}")
                elif result.get("success"):
                    successful += 1
                    total_notifications += result.get("notifications_sent", 0)
                else:
                    failed += 1
                    errors.append(f"RFQ {rfq_ids[i]}: {result.get('error', 'Unknown error')}")
            
            batch_duration = (datetime.utcnow() - batch_start).total_seconds()
            
            return {
                "success": True,
                "batch_processing_time_seconds": batch_duration,
                "rfqs_processed": len(rfq_ids),
                "successful_rfqs": successful,
                "failed_rfqs": failed,
                "total_notifications_sent": total_notifications,
                "errors": errors,
                "individual_results": [r for r in results if not isinstance(r, Exception)]
            }
            
        except Exception as e:
            logger.error(f"Error in batch RFQ processing: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "rfqs_attempted": len(rfq_ids)
            }
    
    async def _send_batch_notifications(self, rfq_data: Dict[str, Any], sellers: List[Dict[str, Any]], job_id: str) -> Dict[str, Any]:
        """
        Send RFQ notifications to a batch of sellers concurrently.
        
        Args:
            rfq_data: RFQ information
            sellers: List of selected seller dictionaries
            job_id: Job identifier for tracking
            
        Returns:
            Dictionary with notification sending results
        """
        try:
            logger.info(f"Sending notifications to {len(sellers)} sellers for job {job_id}")
            
            # Prepare notification tasks
            semaphore = asyncio.Semaphore(self.max_concurrent_notifications)
            
            async def send_single_notification(seller: Dict[str, Any]):
                async with semaphore:
                    try:
                        result = await self.intimation_service.send_rfq_notification(
                            seller_id=seller["seller_id"],
                            rfq_data=rfq_data
                        )
                        return {
                            "seller_id": seller["seller_id"],
                            "seller_name": seller["seller_name"],
                            "success": result["success"],
                            "message_id": result.get("message_id"),
                            "error": result.get("error")
                        }
                    except Exception as e:
                        logger.error(f"Error sending notification to {seller['seller_id']}: {str(e)}")
                        return {
                            "seller_id": seller["seller_id"],
                            "seller_name": seller["seller_name"],
                            "success": False,
                            "error": str(e)
                        }
            
            # Execute all notification tasks concurrently
            notification_results = await asyncio.gather(
                *[send_single_notification(seller) for seller in sellers],
                return_exceptions=True
            )
            
            # Compile results
            successful = 0
            failed = 0
            details = []
            
            for result in notification_results:
                if isinstance(result, Exception):
                    failed += 1
                    details.append({
                        "seller_id": "unknown",
                        "success": False,
                        "error": str(result)
                    })
                elif result.get("success"):
                    successful += 1
                    details.append(result)
                else:
                    failed += 1
                    details.append(result)
            
            logger.info(f"Notification batch complete - {successful} successful, {failed} failed")
            
            return {
                "successful": successful,
                "failed": failed,
                "total": len(sellers),
                "details": details
            }
            
        except Exception as e:
            logger.error(f"Error in batch notification sending: {str(e)}")
            return {
                "successful": 0,
                "failed": len(sellers),
                "total": len(sellers),
                "error": str(e),
                "details": []
            }
    
    async def _fetch_rfq_data(self, rfq_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch RFQ data from main RFQ table.
        
        Args:
            rfq_id: RFQ identifier
            
        Returns:
            Dictionary with RFQ data or None if not found
        """
        try:
            rfq = self.db_session.query(RFQ).filter(RFQ.rfq_id == rfq_id).first()
            
            if not rfq:
                return None
            
            # Extract RFQ data from api_payload JSON
            payload = rfq.api_payload
            
            return {
                "rfq_id": rfq.rfq_id,
                "rfq_title": payload.get("rfq_title", "RFQ"),
                "rfq_description": payload.get("rfq_description", ""),
                "categories": payload.get("categories", []),
                "delivery_location": payload.get("delivery_location", {}),
                "quantity_info": payload.get("quantity_info", ""),
                "deadline": payload.get("deadline"),
                "division": payload.get("division", ""),
                "status": rfq.status.value,
                "created_at": rfq.created_at.isoformat(),
                "external_user_id": rfq.external_user_id
            }
            
        except Exception as e:
            logger.error(f"Error fetching RFQ data for {rfq_id}: {str(e)}")
            return None
    
    @log_service_method("rfq_background")
    async def cleanup_old_notifications(self, days_old: int = 30) -> Dict[str, Any]:
        """
        Clean up old notification records and interactions.
        
        Args:
            days_old: Remove records older than this many days
            
        Returns:
            Dictionary with cleanup results
        """
        try:
            logger.info(f"Starting cleanup of notifications older than {days_old} days")
            
            cutoff_date = datetime.utcnow() - timedelta(days=days_old)
            
            # Count records to be deleted
            old_notifications = self.db_session.query(RFQSellerNotification)\
                .filter(RFQSellerNotification.sent_at < cutoff_date).count()
            
            old_interactions = self.db_session.query(SellerRFQInteraction)\
                .filter(SellerRFQInteraction.created_at < cutoff_date).count()
            
            if old_notifications == 0 and old_interactions == 0:
                logger.info("No old records found for cleanup")
                return {
                    "success": True,
                    "notifications_deleted": 0,
                    "interactions_deleted": 0,
                    "message": "No old records found"
                }
            
            # Delete old records
            deleted_notifications = self.db_session.query(RFQSellerNotification)\
                .filter(RFQSellerNotification.sent_at < cutoff_date).delete()
            
            deleted_interactions = self.db_session.query(SellerRFQInteraction)\
                .filter(SellerRFQInteraction.created_at < cutoff_date).delete()
            
            self.db_session.commit()
            
            logger.info(f"Cleanup completed - {deleted_notifications} notifications, {deleted_interactions} interactions deleted")
            
            return {
                "success": True,
                "notifications_deleted": deleted_notifications,
                "interactions_deleted": deleted_interactions,
                "cutoff_date": cutoff_date.isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")
            self.db_session.rollback()
            return {
                "success": False,
                "error": str(e)
            }
    
    @log_service_method("rfq_background")
    async def get_pending_rfqs(self) -> List[Dict[str, Any]]:
        """
        Get list of approved RFQs that haven't been processed yet.
        
        Returns:
            List of pending RFQ dictionaries
        """
        try:
            pending_rfqs = self.db_session.query(RFQ)\
                .filter(RFQ.status == RFQStatus.ready)\
                .order_by(RFQ.created_at.asc())\
                .all()
            
            rfq_list = []
            for rfq in pending_rfqs:
                # Check if this RFQ has already been processed (has notifications sent)
                notification_count = self.db_session.query(func.count(RFQSellerNotification.notification_id))\
                    .filter(RFQSellerNotification.rfq_id == rfq.rfq_id).scalar()
                
                if notification_count == 0:  # Not yet processed
                    payload = rfq.api_payload
                    rfq_list.append({
                        "rfq_id": rfq.rfq_id,
                        "rfq_title": payload.get("rfq_title", "RFQ"),
                        "categories": payload.get("categories", []),
                        "created_at": rfq.created_at.isoformat(),
                        "deadline": payload.get("deadline"),
                        "external_user_id": rfq.external_user_id,
                        "priority": self._calculate_rfq_priority(rfq)
                    })
            
            # Sort by priority (higher priority first)
            rfq_list.sort(key=lambda x: x["priority"], reverse=True)
            
            logger.info(f"Found {len(rfq_list)} pending RFQs for processing")
            return rfq_list
            
        except Exception as e:
            logger.error(f"Error getting pending RFQs: {str(e)}")
            return []
    
    def _calculate_rfq_priority(self, rfq: RFQ) -> int:
        """Calculate priority score for RFQ processing order."""
        priority = 100  # Base priority
        
        payload = rfq.api_payload
        
        # Higher priority for urgent deadlines
        deadline_str = payload.get("deadline")
        if deadline_str:
            try:
                from datetime import datetime as dt
                if isinstance(deadline_str, str):
                    deadline = dt.fromisoformat(deadline_str.replace('Z', '+00:00')).date()
                else:
                    deadline = deadline_str
                days_until_deadline = (deadline - datetime.utcnow().date()).days
                if days_until_deadline < 7:
                    priority += 50
                elif days_until_deadline < 14:
                    priority += 25
            except:
                pass
        
        # Higher priority for certain categories (can be configured)
        high_priority_categories = ["Medical Equipment", "Emergency Supplies"]
        categories = payload.get("categories", [])
        for category in categories:
            if category in high_priority_categories:
                priority += 30
                break
        
        # Higher priority for older RFQs
        age_hours = (datetime.utcnow() - rfq.created_at).total_seconds() / 3600
        priority += min(20, int(age_hours / 24))  # Max 20 points for age
        
        return priority
    
    async def _update_rfq_status(self, rfq_id: str, new_status: RFQStatus) -> None:
        """Update RFQ status after processing."""
        try:
            rfq = self.db_session.query(RFQ).filter(RFQ.rfq_id == rfq_id).first()
            if rfq:
                rfq.status = new_status
                if new_status == RFQStatus.submitted:
                    rfq.submitted_at = datetime.utcnow()
                self.db_session.commit()
                logger.debug(f"Updated RFQ {rfq_id} status to {new_status.value}")
        except Exception as e:
            logger.error(f"Error updating RFQ status: {str(e)}")
            self.db_session.rollback()
    
    def get_job_statistics(self) -> Dict[str, Any]:
        """Get current job processing statistics."""
        active_jobs = len([j for j in self._active_jobs.values() if j["status"] == "processing"])
        
        return {
            "active_jobs": active_jobs,
            "total_jobs_tracked": len(self._active_jobs),
            "statistics": self._job_stats.copy(),
            "configuration": {
                "max_concurrent_rfqs": self.max_concurrent_rfqs,
                "max_concurrent_notifications": self.max_concurrent_notifications,
                "notification_batch_size": self.notification_batch_size,
                "retry_attempts": self.retry_attempts
            }
        }
    
    def get_active_jobs(self) -> List[Dict[str, Any]]:
        """Get list of currently active background jobs."""
        return [
            {
                "job_id": job_id,
                "rfq_id": job_info["rfq_id"],
                "status": job_info["status"],
                "stage": job_info.get("stage"),
                "started_at": job_info["started_at"].isoformat(),
                "duration_seconds": (datetime.utcnow() - job_info["started_at"]).total_seconds()
            }
            for job_id, job_info in self._active_jobs.items()
            if job_info["status"] == "processing"
        ]