"""
Seller categorization service for offline 3-level mapping using OpenAI.

This service maps sellers from simple category strings to the sophisticated
3-level learning categorization system through background processing.

The service processes sellers in batches during off-peak hours, using OpenAI
to intelligently map their basic categories (e.g., "Electronics") to the 
structured 3-level system (Level 1 > Level 2 > Level 3).

Key responsibilities:
- Process seller categories in offline batches
- Use OpenAI to generate 3-level mappings
- Validate and store mapping results
- Update learning category associations
- Provide mapping statistics and monitoring
- Handle errors and retry logic
"""

import logging
import asyncio
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, or_

from app.database import get_db_session
from app.models import (
    Seller, SellerLearningMapping, SellerCategorizationJob, JobStatus,
    LearningCategory, LearningCategoryItem
)
from app.services.openai_service import OpenAIService
from app.config import get_settings
from app.utils.logging_utils import log_service_method

logger = logging.getLogger(__name__)

class SellerCategorizationService:
    """
    Service for offline mapping of sellers to 3-level learning categories.
    
    Processes seller category mappings in background using OpenAI to create
    structured 3-level categorizations that enhance the recommendation system.
    """
    
    def __init__(self, db_session: Optional[Session] = None):
        self.db_session = db_session or get_db_session()
        self.settings = get_settings()
        self.openai_service = OpenAIService()
        
        # Processing configuration
        self.batch_size = 10  # Process 10 sellers at a time
        self.max_concurrent_jobs = 3  # Run 3 categorization jobs concurrently
        self.retry_attempts = 2
        self.retry_delay_minutes = 5
        
        # Statistics tracking
        self._processing_stats = {
            "sellers_processed": 0,
            "categories_mapped": 0,
            "openai_calls_made": 0,
            "errors_encountered": 0,
            "processing_time_total": 0.0
        }
    
    @log_service_method("seller_categorization")
    async def process_all_sellers(self, force_reprocess: bool = False) -> Dict[str, Any]:
        """
        Process all sellers for 3-level categorization mapping.
        
        Args:
            force_reprocess: If True, reprocess sellers that already have mappings
            
        Returns:
            Dictionary with processing results and statistics
        """
        try:
            
            processing_start = datetime.utcnow()
            
            # Get sellers that need categorization
            sellers_to_process = await self._get_sellers_needing_categorization(force_reprocess)
            
            if not sellers_to_process:
                logger.info("No sellers found requiring categorization")
                return {
                    "success": True,
                    "sellers_processed": 0,
                    "message": "No sellers require categorization"
                }
            
            logger.info(f"Found {len(sellers_to_process)} sellers requiring categorization")
            
            # Process sellers in batches
            batch_results = []
            total_processed = 0
            total_errors = 0
            
            for i in range(0, len(sellers_to_process), self.batch_size):
                batch = sellers_to_process[i:i + self.batch_size]
                
                logger.info(f"Processing batch {i // self.batch_size + 1} - {len(batch)} sellers")
                
                batch_result = await self._process_seller_batch(batch)
                batch_results.append(batch_result)
                
                total_processed += batch_result.get("processed", 0)
                total_errors += batch_result.get("errors", 0)
                
                # Small delay between batches to avoid overwhelming OpenAI API
                if i + self.batch_size < len(sellers_to_process):
                    await asyncio.sleep(2)
            
            processing_duration = (datetime.utcnow() - processing_start).total_seconds()
            
            result = {
                "success": True,
                "processing_time_seconds": processing_duration,
                "total_sellers_found": len(sellers_to_process),
                "sellers_processed": total_processed,
                "sellers_failed": total_errors,
                "batches_processed": len(batch_results),
                "batch_details": batch_results,
                "statistics": self._processing_stats.copy()
            }
            
            logger.info(f"Completed bulk categorization - {total_processed} sellers processed in {processing_duration:.2f}s")
            return result
            
        except Exception as e:
            logger.error(f"Error in bulk seller categorization: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "statistics": self._processing_stats.copy()
            }
    
    @log_service_method("seller_categorization")
    async def categorize_seller_categories(self, seller_id: str) -> Dict[str, Any]:
        """
        Process a single seller's categories for 3-level mapping.
        
        Args:
            seller_id: Seller identifier to process
            
        Returns:
            Dictionary with categorization results
        """
        try:
            logger.info(f"Starting categorization for seller {seller_id}")
            
            # Get seller details
            seller = await self._get_seller_details(seller_id)
            if not seller:
                return {"success": False, "error": "Seller not found"}
            
            # Create categorization job record
            job = await self._create_categorization_job(seller_id, seller.categories)
            
            processing_start = datetime.utcnow()
            
            try:
                # Process each category
                mappings = []
                for category in seller.categories:
                    mapping_result = await self._generate_3_level_mapping_for_category(
                        category, seller
                    )
                    if mapping_result["success"]:
                        mappings.append(mapping_result["mapping"])
                    else:
                        logger.warning(f"Failed to map category '{category}' for seller {seller_id}: {mapping_result.get('error')}")
                
                if not mappings:
                    raise ValueError("No categories could be successfully mapped")
                
                # Store mappings in database
                await self._store_seller_mappings(seller_id, mappings)
                
                # Update job as completed
                processing_time = (datetime.utcnow() - processing_start).total_seconds()
                await self._update_categorization_job(
                    job.job_id, JobStatus.completed, mappings, int(processing_time * 1000)
                )
                
                # Update statistics
                self._processing_stats["sellers_processed"] += 1
                self._processing_stats["categories_mapped"] += len(mappings)
                self._processing_stats["processing_time_total"] += processing_time
                
                result = {
                    "success": True,
                    "seller_id": seller_id,
                    "job_id": job.job_id,
                    "processing_time_seconds": processing_time,
                    "categories_processed": len(seller.categories),
                    "mappings_created": len(mappings),
                    "mappings": mappings
                }
                
                logger.info(f"Completed categorization for seller {seller_id} - {len(mappings)} mappings created")
                return result
                
            except Exception as processing_error:
                # Update job as failed
                await self._update_categorization_job(
                    job.job_id, JobStatus.failed, None, None, str(processing_error)
                )
                raise processing_error
                
        except Exception as e:
            logger.error(f"Error categorizing seller {seller_id}: {str(e)}")
            self._processing_stats["errors_encountered"] += 1
            return {
                "success": False,
                "seller_id": seller_id,
                "error": str(e)
            }
    
    async def _generate_3_level_mapping_for_category(self, category: str, seller: Seller) -> Dict[str, Any]:
        """
        Use OpenAI to generate 3-level categorization for a seller's category.
        
        Args:
            category: Original category string from seller
            seller: Seller model instance for context
            
        Returns:
            Dictionary with mapping result
        """
        try:
            logger.debug(f"Generating 3-level mapping for category: {category}")
            
            # Get similar items from existing learning system for context
            similar_items = await self._get_similar_category_items(category)
            
            # Build seller context for better categorization
            seller_context = {
                "seller_name": seller.seller_name,
                "location": seller.location,
                "ranking": seller.ranking.value if seller.ranking else "Gold",
                "other_categories": [c for c in seller.categories if c != category]
            }
            
            # Call OpenAI service for 3-level categorization
            self._processing_stats["openai_calls_made"] += 1
            
            categorization_result = await asyncio.to_thread(
                self.openai_service.generate_3_level_categorization,
                category,
                similar_items,
                category  # Use original category as client_category reference
            )
            
            if not categorization_result.get("success"):
                return {
                    "success": False,
                    "error": categorization_result.get("error", "OpenAI categorization failed")
                }
            
            # Structure the mapping result
            mapping = {
                "original_category": category,
                "level_1_category": categorization_result["categorization"]["level_1"],
                "level_2_category": categorization_result["categorization"]["level_2"], 
                "level_3_category": categorization_result["categorization"]["level_3"],
                "confidence_score": categorization_result.get("confidence_score", 0.8),
                "ai_reasoning": categorization_result.get("reasoning", ""),
                "processing_time_ms": categorization_result.get("processing_time_ms", 0),
                "seller_context": seller_context
            }
            
            logger.debug(f"Generated mapping: {category} -> {mapping['level_1_category']} > {mapping['level_2_category']} > {mapping['level_3_category']}")
            
            return {
                "success": True,
                "mapping": mapping
            }
            
        except Exception as e:
            logger.error(f"Error generating 3-level mapping for category '{category}': {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
    
    async def _get_similar_category_items(self, category: str) -> List[Dict[str, Any]]:
        """
        Get similar items from the learning category system for context.
        
        Args:
            category: Category to find similar items for
            
        Returns:
            List of similar item dictionaries
        """
        try:
            # Query learning category items for similar categories
            similar_items = self.db_session.query(LearningCategoryItem)\
                .join(LearningCategory)\
                .filter(
                    or_(
                        LearningCategory.level_1_category.ilike(f"%{category}%"),
                        LearningCategory.level_2_category.ilike(f"%{category}%"),
                        LearningCategory.level_3_category.ilike(f"%{category}%"),
                        LearningCategoryItem.item_description.ilike(f"%{category}%")
                    )
                )\
                .limit(5)\
                .all()
            
            result = []
            for item in similar_items:
                result.append({
                    "item": item.item_description,
                    "category": f"{item.learning_category.level_1_category} > {item.learning_category.level_2_category} > {item.learning_category.level_3_category}",
                    "similarity_score": 0.8  # Placeholder similarity score
                })
            
            return result
            
        except Exception as e:
            logger.warning(f"Error getting similar category items: {str(e)}")
            return []
    
    async def _process_seller_batch(self, sellers: List[Seller]) -> Dict[str, Any]:
        """
        Process a batch of sellers concurrently.
        
        Args:
            sellers: List of seller objects to process
            
        Returns:
            Dictionary with batch processing results
        """
        try:
            logger.debug(f"Processing batch of {len(sellers)} sellers")
            
            batch_start = datetime.utcnow()
            
            # Process sellers concurrently with semaphore
            semaphore = asyncio.Semaphore(self.max_concurrent_jobs)
            
            async def process_single_seller(seller):
                async with semaphore:
                    return await self.categorize_seller_categories(seller.seller_id)
            
            # Execute all seller processing tasks
            results = await asyncio.gather(
                *[process_single_seller(seller) for seller in sellers],
                return_exceptions=True
            )
            
            # Compile batch results
            processed = 0
            errors = 0
            error_messages = []
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    errors += 1
                    error_messages.append(f"Seller {sellers[i].seller_id}: {str(result)}")
                elif result.get("success"):
                    processed += 1
                else:
                    errors += 1
                    error_messages.append(f"Seller {sellers[i].seller_id}: {result.get('error', 'Unknown error')}")
            
            batch_duration = (datetime.utcnow() - batch_start).total_seconds()
            
            return {
                "processed": processed,
                "errors": errors,
                "total": len(sellers),
                "processing_time_seconds": batch_duration,
                "error_messages": error_messages[:5]  # Limit error messages
            }
            
        except Exception as e:
            logger.error(f"Error processing seller batch: {str(e)}")
            return {
                "processed": 0,
                "errors": len(sellers),
                "total": len(sellers),
                "error": str(e)
            }
    
    async def _get_sellers_needing_categorization(self, force_reprocess: bool = False) -> List[Seller]:
        """Get sellers that need 3-level categorization processing."""
        try:
            if force_reprocess:
                # Get all sellers
                sellers = self.db_session.query(Seller).all()
            else:
                # Get sellers without existing mappings
                sellers_with_mappings = self.db_session.query(SellerLearningMapping.seller_id).distinct()
                
                sellers = self.db_session.query(Seller)\
                    .filter(~Seller.seller_id.in_(sellers_with_mappings))\
                    .all()
            
            return sellers
            
        except Exception as e:
            logger.error(f"Error getting sellers needing categorization: {str(e)}")
            return []
    
    async def _get_seller_details(self, seller_id: str) -> Optional[Seller]:
        """Get seller details from database."""
        return self.db_session.query(Seller).filter(Seller.seller_id == seller_id).first()
    
    async def _create_categorization_job(self, seller_id: str, input_categories: List[str]) -> SellerCategorizationJob:
        """Create a categorization job record."""
        job = SellerCategorizationJob(
            seller_id=seller_id,
            input_categories=input_categories,
            job_status=JobStatus.processing
        )
        self.db_session.add(job)
        self.db_session.commit()
        return job
    
    async def _update_categorization_job(
        self, 
        job_id: str, 
        status: JobStatus, 
        output_mappings: Optional[List[Dict]] = None,
        processing_time_ms: Optional[int] = None,
        error_message: Optional[str] = None
    ) -> None:
        """Update categorization job with results."""
        job = self.db_session.query(SellerCategorizationJob)\
            .filter(SellerCategorizationJob.job_id == job_id).first()
        
        if job:
            job.job_status = status
            job.completed_at = datetime.utcnow()
            
            if output_mappings:
                job.output_mappings = output_mappings
            if processing_time_ms:
                job.processing_time_ms = processing_time_ms
            if error_message:
                job.error_message = error_message
            
            self.db_session.commit()
    
    async def _store_seller_mappings(self, seller_id: str, mappings: List[Dict[str, Any]]) -> None:
        """Store seller categorization mappings in database."""
        try:
            # Remove existing mappings for this seller if any
            self.db_session.query(SellerLearningMapping)\
                .filter(SellerLearningMapping.seller_id == seller_id).delete()
            
            # Create new mappings
            for mapping in mappings:
                # Try to find existing learning category
                learning_category = self.db_session.query(LearningCategory)\
                    .filter(
                        and_(
                            LearningCategory.level_1_category == mapping["level_1_category"],
                            LearningCategory.level_2_category == mapping["level_2_category"],
                            LearningCategory.level_3_category == mapping["level_3_category"]
                        )
                    ).first()
                
                # Create mapping record
                seller_mapping = SellerLearningMapping(
                    seller_id=seller_id,
                    original_category=mapping["original_category"],
                    learning_category_id=learning_category.id if learning_category else None,
                    level_1_category=mapping["level_1_category"],
                    level_2_category=mapping["level_2_category"],
                    level_3_category=mapping["level_3_category"],
                    confidence_score=mapping["confidence_score"],
                    ai_reasoning=mapping["ai_reasoning"],
                    mapping_method="openai_analysis"
                )
                
                self.db_session.add(seller_mapping)
            
            self.db_session.commit()
            logger.debug(f"Stored {len(mappings)} mappings for seller {seller_id}")
            
        except Exception as e:
            logger.error(f"Error storing seller mappings: {str(e)}")
            self.db_session.rollback()
            raise
    
    @log_service_method("seller_categorization")
    async def get_categorization_statistics(self) -> Dict[str, Any]:
        """Get comprehensive categorization statistics."""
        try:
            # Database statistics
            total_sellers = self.db_session.query(func.count(Seller.seller_id)).scalar()
            sellers_with_mappings = self.db_session.query(func.count(
                SellerLearningMapping.seller_id.distinct()
            )).scalar()
            total_mappings = self.db_session.query(func.count(SellerLearningMapping.mapping_id)).scalar()
            
            # Job statistics
            total_jobs = self.db_session.query(func.count(SellerCategorizationJob.job_id)).scalar()
            completed_jobs = self.db_session.query(func.count(SellerCategorizationJob.job_id))\
                .filter(SellerCategorizationJob.job_status == JobStatus.completed).scalar()
            failed_jobs = self.db_session.query(func.count(SellerCategorizationJob.job_id))\
                .filter(SellerCategorizationJob.job_status == JobStatus.failed).scalar()
            
            # Recent activity
            recent_jobs = self.db_session.query(SellerCategorizationJob)\
                .filter(SellerCategorizationJob.created_at > datetime.utcnow() - timedelta(days=7))\
                .count()
            
            return {
                "database_statistics": {
                    "total_sellers": total_sellers,
                    "sellers_with_mappings": sellers_with_mappings,
                    "sellers_without_mappings": total_sellers - sellers_with_mappings,
                    "total_mappings": total_mappings,
                    "coverage_percentage": round((sellers_with_mappings / total_sellers * 100) if total_sellers > 0 else 0, 2)
                },
                "job_statistics": {
                    "total_jobs": total_jobs,
                    "completed_jobs": completed_jobs,
                    "failed_jobs": failed_jobs,
                    "pending_jobs": total_jobs - completed_jobs - failed_jobs,
                    "success_rate": round((completed_jobs / total_jobs * 100) if total_jobs > 0 else 0, 2),
                    "recent_jobs_7d": recent_jobs
                },
                "processing_statistics": self._processing_stats.copy(),
                "configuration": {
                    "batch_size": self.batch_size,
                    "max_concurrent_jobs": self.max_concurrent_jobs,
                    "retry_attempts": self.retry_attempts
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting categorization statistics: {str(e)}")
            return {
                "error": str(e),
                "processing_statistics": self._processing_stats.copy()
            }