"""
Celery task for building 3-level taxonomy from item_category data.

This task processes category mappings and creates a comprehensive 
3-level learning categorization system using the build_3_level_taxonomy script.
"""

import logging
import asyncio
from typing import Dict, Any
from datetime import datetime
from celery import shared_task
import json

logger = logging.getLogger(__name__)

from app.config import get_settings
from app.database import get_remote_item_categories
from app.redis_db import AsyncRedisConnectionManager


@shared_task(
    bind=True,
    queue='taxonomy_build',
    priority=1,  # LOWEST priority - bulk processing
    soft_time_limit=14400,  # 4 hours (soft limit - runaway protection)
    time_limit=18000,        # 5 hours (hard limit - NEVER kill early, only for stuck tasks)
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 2, 'countdown': 600}  # Retry up to 2 times with 10 min delay
)
def build_taxonomy(self, batch_size: int = 50, process_all: bool = True, parallel: bool = True, resume: bool = True):
    """
    Build 3-level taxonomy by running the taxonomy building process.
    
    Args:
        batch_size: Number of items to process in each batch
        process_all: If True, processes all items in chunks of batch_size
                    If False, processes only one batch of batch_size items
        parallel: If True and process_all=True, processes batches in parallel using all workers
        resume: If True, resumes from last checkpoint. If False, starts from beginning
        
    Returns:
        dict: Result of the taxonomy build process
    """
    return asyncio.run(build_taxonomy_async(self, batch_size, process_all, parallel, resume))


async def build_taxonomy_async(self, batch_size: int = 50, process_all: bool = True, parallel: bool = True, resume: bool = True):
    """
    Async implementation of the taxonomy build task.
    
    Args:
        batch_size: Number of items to process in each batch
        process_all: If True, processes all items in chunks of batch_size
        parallel: If True and process_all=True, processes batches in parallel using all workers
        resume: If True, resumes from last checkpoint. If False, starts from beginning
    """
    try:
        logger.info("=" * 60)
        logger.info("STARTING 3-LEVEL TAXONOMY BUILD TASK")
        logger.info("=" * 60)
        logger.info(f"Batch size: {batch_size}")
        logger.info(f"Process all items: {process_all}")
        logger.info(f"Parallel processing: {parallel}")
        logger.info(f"Resume from checkpoint: {resume}")
        
        start_time = datetime.utcnow()
        
        # Redis checkpoint key
        CHECKPOINT_KEY = "taxonomy_build_checkpoint"
        
        # Get Redis client
        redis_client = await AsyncRedisConnectionManager.get_client()
        
        # Load checkpoint if resuming
        checkpoint_data = None
        if resume:
            try:
                checkpoint_json = await redis_client.get(CHECKPOINT_KEY)
                if checkpoint_json:
                    checkpoint_data = json.loads(checkpoint_json)
                    logger.info(f"📍 Found checkpoint: {checkpoint_data}")
            except Exception as e:
                logger.warning(f"Could not load checkpoint: {e}")
                checkpoint_data = None
        
        # Import the taxonomy building function
        import sys
        import os
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))
        
        # Import from the script file directly
        import importlib.util
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'Setup', '3-level_vector_store', 'build_3_level_taxonomy.py'
        )
        
        spec = importlib.util.spec_from_file_location("build_3_level_taxonomy", script_path)
        build_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_module)
        
        # Get total count first
        settings = get_settings()
        all_items_count = len(get_remote_item_categories()) if settings.enable_remote_categorization else 0
        
        if process_all and all_items_count > 0:
            if parallel:
                # PARALLEL MODE: Dynamic batch assignment across workers
                # Get bulk pool workers count from config to match actual worker concurrency
                settings = get_settings()
                NUM_WORKERS = getattr(settings, 'bulk_pool_workers', 6)  # Default to 6 if not configured
                logger.info(f"\n🚀 PARALLEL MODE: Processing ALL {all_items_count} items with dynamic batching")
                logger.info(f"Batch size: {batch_size}, Worker concurrency: {NUM_WORKERS}")
                
                # Calculate total number of batches
                num_batches = (all_items_count + batch_size - 1) // batch_size
                logger.info(f"Total batches to process: {num_batches}")
                
                # Initialize batch counter from checkpoint or start from beginning
                # asyncio is imported at module scope and is shared by both
                # sequential and parallel execution paths.
                batch_counter = {'current': 0}
                
                # Resume from checkpoint if available
                if checkpoint_data and 'next_batch' in checkpoint_data:
                    batch_counter['current'] = checkpoint_data['next_batch']
                    logger.info(f"📍 Resuming from batch {batch_counter['current']}/{num_batches}")
                
                counter_lock = asyncio.Lock()
                
                # Results aggregation
                results_lock = asyncio.Lock()
                total_results = {
                    "success": True,
                    "total_processed": 0,
                    "total_created": 0,
                    "total_existing": 0,
                    "total_errors": [],
                    "batches_processed": 0
                }
                
                # Track failed batches for retry
                failed_batches = []
                failed_batches_lock = asyncio.Lock()
                max_retries = 2
                
                async def process_next_available_batch():
                    """Worker function that grabs the next available batch with retry logic"""
                    while True:
                        # Get next batch number atomically
                        async with counter_lock:
                            if batch_counter['current'] >= num_batches:
                                # No more batches
                                return
                            batch_num = batch_counter['current']
                            batch_counter['current'] += 1
                        
                        # Calculate item range for this batch
                        start_idx = batch_num * batch_size
                        end_idx = min(start_idx + batch_size, all_items_count)
                        current_batch_size = end_idx - start_idx
                        
                        logger.info(f"Worker processing Batch #{batch_num + 1}/{num_batches}: items {start_idx} to {end_idx-1}")
                        
                        # Try to process batch with retries
                        success = False
                        for attempt in range(max_retries + 1):  # 1 initial + max_retries
                            try:
                                result = await build_module.process_category_mappings(
                                    batch_size=current_batch_size,
                                    start_from=start_idx,
                                    use_random=False,
                                    count=None,
                                    delay=0.5,  # Reduced delay for parallel processing
                                    file_items=None
                                )
                                
                                # Aggregate results
                                async with results_lock:
                                    if result.get("success"):
                                        total_results["total_processed"] += result.get("processed_count", 0)
                                        total_results["total_created"] += result.get("created_categories", 0)
                                        total_results["total_existing"] += result.get("existing_categories", 0)
                                        total_results["batches_processed"] += 1
                                        success = True
                                        
                                        # Save checkpoint after successful batch
                                        try:
                                            await redis_client.set(
                                                CHECKPOINT_KEY,
                                                json.dumps({
                                                    "next_batch": batch_counter['current'],
                                                    "batches_processed": total_results["batches_processed"],
                                                    "last_updated": datetime.utcnow().isoformat()
                                                })
                                            )
                                        except Exception as checkpoint_error:
                                            logger.warning(f"Could not save checkpoint: {checkpoint_error}")
                                    else:
                                        logger.error(f"Batch #{batch_num + 1} failed: {result.get('error')}")
                                        total_results["total_errors"].append(f"Batch {batch_num + 1}: {result.get('error')}")
                                        total_results["success"] = False
                                        success = False
                                        
                                if success:
                                    break  # Success, exit retry loop
                                    
                            except Exception as e:
                                error_msg = f"Batch #{batch_num + 1} exception (attempt {attempt + 1}/{max_retries + 1}): {str(e)}"
                                logger.error(error_msg)
                                async with results_lock:
                                    if attempt == max_retries:  # Last attempt
                                        total_results["total_errors"].append(f"Batch {batch_num + 1}: {str(e)}")
                                        total_results["success"] = False
                                    success = False
                            
                            if not success and attempt < max_retries:
                                logger.warning(f"Retrying Batch #{batch_num + 1} in 10 seconds... (attempt {attempt + 1}/{max_retries + 1})")
                                await asyncio.sleep(10)  # Wait before retry
                        
                        # If still failed after all retries, don't stop - continue with next batch
                        if not success:
                            logger.error(f"Batch #{batch_num + 1} FAILED after {max_retries + 1} attempts. Continuing with next batch...")
                
                # Start 8 concurrent workers that dynamically grab batches
                logger.info(f"Starting {NUM_WORKERS} concurrent workers with dynamic batch assignment...")
                workers = [process_next_available_batch() for _ in range(NUM_WORKERS)]
                await asyncio.gather(*workers)
                
                end_time = datetime.utcnow()
                duration_seconds = (end_time - start_time).total_seconds()
                
                logger.info("\n" + "=" * 60)
                logger.info("TAXONOMY BUILD COMPLETED (PARALLEL MODE)")
                logger.info(f"Total items processed: {total_results['total_processed']}/{all_items_count}")
                logger.info(f"Total categories created: {total_results['total_created']}")
                logger.info(f"Total existing categories: {total_results['total_existing']}")
                logger.info(f"Total errors: {len(total_results['total_errors'])}")
                logger.info(f"Batches processed: {total_results['batches_processed']}/{num_batches}")
                
                # Show failed batches summary
                if len(total_results['total_errors']) > 0:
                    logger.info("\n⚠️ FAILED BATCHES SUMMARY:")
                    logger.info("-" * 60)
                    for error in total_results['total_errors']:
                        logger.error(f"  • {error}")
                    logger.info("-" * 60)
                    logger.info(f"Note: {len(total_results['total_errors'])} batch(es) failed after {max_retries + 1} attempts each")
                    logger.info("These items will need manual review or reprocessing.")
                
                logger.info(f"Duration: {duration_seconds:.2f} seconds ({duration_seconds/3600:.2f} hours)")
                logger.info("=" * 60)
                
                # Clear checkpoint on successful completion
                if total_results["success"] and total_results["batches_processed"] == num_batches:
                    try:
                        await redis_client.delete(CHECKPOINT_KEY)
                        logger.info("✅ Taxonomy build completed successfully - checkpoint cleared")
                    except Exception as e:
                        logger.warning(f"Could not clear checkpoint: {e}")
                
                return {
                    "success": total_results["success"],
                    "message": "Taxonomy build completed (parallel mode)",
                    "total_processed": total_results["total_processed"],
                    "total_created": total_results["total_created"],
                    "total_existing": total_results["total_existing"],
                    "total_errors": len(total_results["total_errors"]),
                    "batches_processed": total_results["batches_processed"],
                    "duration_seconds": duration_seconds,
                    "timestamp": end_time.isoformat(),
                    "checkpoint_cleared": total_results["success"] and total_results["batches_processed"] == num_batches
                }
            else:
                # SEQUENTIAL MODE: Original behavior - one batch at a time
                logger.info(f"Processing ALL {all_items_count} items in batches of {batch_size} (SEQUENTIAL MODE)")
            
            total_results = {
                "success": True,
                "total_processed": 0,
                "total_created": 0,
                "total_existing": 0,
                "total_errors": [],
                "batches_processed": 0
            }
            
            # Loop through all items in batches with retry logic
            max_retries = 2
            for start_index in range(0, all_items_count, batch_size):
                current_batch_size = min(batch_size, all_items_count - start_index)
                logger.info(f"\n{'='*60}")
                logger.info(f"BATCH {total_results['batches_processed'] + 1}: Processing items {start_index} to {start_index + current_batch_size - 1}")
                logger.info(f"{'='*60}")
                
                # Retry failed batches up to max_retries times
                success = False
                for attempt in range(max_retries + 1):
                    try:
                        result = await build_module.process_category_mappings(
                            batch_size=current_batch_size,
                            start_from=start_index,
                            use_random=False,
                            count=None,
                            delay=3.0,
                            file_items=None
                        )
                        
                        if result.get("success"):
                            total_results["total_processed"] += result.get("processed_count", 0)
                            total_results["total_created"] += result.get("created_categories", 0)
                            total_results["total_existing"] += result.get("existing_categories", 0)
                            total_results["total_errors"].extend(result.get("errors", []))
                            total_results["batches_processed"] += 1
                            success = True
                        else:
                            logger.error(f"Batch failed at index {start_index}: {result.get('error')}")
                            total_results["success"] = False
                            success = False
                        
                        if success:
                            break  # Success, exit retry loop
                            
                    except Exception as e:
                        error_msg = f"Batch exception (attempt {attempt + 1}/{max_retries + 1}): {str(e)}"
                        logger.error(error_msg)
                        total_results["success"] = False
                        success = False
                    
                    if not success and attempt < max_retries:
                        logger.warning(f"Retrying Batch at index {start_index} in 10 seconds... (attempt {attempt + 1}/{max_retries + 1})")
                        await asyncio.sleep(10)  # Wait before retry
                
                # If still failed after all retries, log it but continue with next batch
                if not success:
                    logger.error(f"Batch at index {start_index} FAILED after {max_retries + 1} attempts. Continuing with next batch...")
                    total_results["total_errors"].append(f"Batch at index {start_index}: Failed after {max_retries + 1} attempts")
            
            end_time = datetime.utcnow()
            duration_seconds = (end_time - start_time).total_seconds()
            
            logger.info("\n" + "=" * 60)
            logger.info("TAXONOMY BUILD COMPLETED SUCCESSFULLY")
            logger.info(f"Total items processed: {total_results['total_processed']}/{all_items_count}")
            logger.info(f"Total categories created: {total_results['total_created']}")
            logger.info(f"Total existing categories: {total_results['total_existing']}")
            logger.info(f"Total errors: {len(total_results['total_errors'])}")
            logger.info(f"Batches processed: {total_results['batches_processed']}")
            logger.info(f"Duration: {duration_seconds:.2f} seconds ({duration_seconds/60:.2f} minutes)")
            logger.info("=" * 60)
            
            return {
                "success": total_results["success"],
                "message": "Taxonomy build completed successfully",
                "total_processed": total_results["total_processed"],
                "total_created": total_results["total_created"],
                "total_existing": total_results["total_existing"],
                "total_errors": len(total_results["total_errors"]),
                "batches_processed": total_results["batches_processed"],
                "duration_seconds": duration_seconds,
                "timestamp": end_time.isoformat()
            }
        else:
            # Process only one batch (original behavior)
            logger.info(f"Processing single batch of {batch_size} items from index 0")
            result = await build_module.process_category_mappings(
                batch_size=batch_size,
                start_from=0,
                use_random=False,
                count=None,
                delay=3.0,
                file_items=None
            )
        
            
            end_time = datetime.utcnow()
            duration_seconds = (end_time - start_time).total_seconds()
            
            if result.get("success"):
                logger.info("=" * 60)
                logger.info("TAXONOMY BUILD COMPLETED SUCCESSFULLY")
                logger.info(f"Processed: {result.get('processed_count', 0)} items")
                logger.info(f"Created categories: {result.get('created_categories', 0)}")
                logger.info(f"Existing categories: {result.get('existing_categories', 0)}")
                logger.info(f"Duration: {duration_seconds:.2f} seconds")
                logger.info("=" * 60)
                
                return {
                    "success": True,
                    "message": "Taxonomy build completed successfully",
                    "processed_count": result.get('processed_count', 0),
                    "created_categories": result.get('created_categories', 0),
                    "existing_categories": result.get('existing_categories', 0),
                    "duration_seconds": duration_seconds,
                    "timestamp": end_time.isoformat()
                }
            else:
                logger.error(f"Taxonomy build failed: {result.get('error', 'Unknown error')}")
                return {
                    "success": False,
                    "error": result.get('error', 'Unknown error'),
                    "timestamp": end_time.isoformat()
                }
            
    except Exception as e:
        logger.error(f"Error in taxonomy build task: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }
