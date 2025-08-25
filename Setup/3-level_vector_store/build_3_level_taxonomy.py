#!/usr/bin/env python3
"""
Script 1: Build 3-level taxonomy from CategoryMapping table.

This script processes all CategoryMapping entries through OpenAI to create
a comprehensive 3-level learning categorization system.

Usage:
    python build_3_level_taxonomy.py [--batch-size 10] [--start-from 0]
"""

import sys
import os
import logging
import argparse
import time
from typing import Dict, Any

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.database import get_db_session, get_remote_item_categories, test_remote_connection
from app.models import CategoryMapping
from app.services.learning_categorization_service import LearningCategorizationService
from app.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def process_category_mappings(batch_size: int = 10, start_from: int = 0, use_remote: bool = True) -> Dict[str, Any]:
    """
    Process category data (remote or local) to build 3-level taxonomy.
    
    Args:
        batch_size: Number of mappings to process in each batch
        start_from: Index to start processing from (for resuming)
        use_remote: Whether to use remote item_category data
        
    Returns:
        Dict with processing results
    """
    learning_service = LearningCategorizationService()
    
    try:
        # Get items from appropriate source
        if use_remote:
            settings = get_settings()
            if not settings.enable_remote_categorization:
                logger.error("Remote categorization is not enabled in configuration")
                return {
                    "success": False,
                    "error": "Remote categorization not enabled",
                    "processed_count": 0
                }
            
            if not test_remote_connection():
                logger.error("Remote database connection failed")
                return {
                    "success": False,
                    "error": "Remote database connection failed",
                    "processed_count": 0
                }
            
            logger.info("Using remote item_category data")
            all_items = get_remote_item_categories()
            total_mappings = len(all_items)
            
            # Convert remote items to standard format and slice
            category_mappings = []
            for item_data in all_items[start_from:start_from + 50]:
                category_mappings.append({
                    'id': item_data['uuid'],
                    'category': item_data['category'],
                    'item': item_data['item'],
                    'division': item_data.get('division', ''),
                    'serial_no': item_data.get('serial_no', 0)
                })
                
        else:
            logger.info("Using local CategoryMapping data")
            db = get_db_session()
            try:
                total_mappings = db.query(CategoryMapping).count()
                local_mappings = db.query(CategoryMapping).offset(start_from).limit(50).all()
                
                # Convert local mappings to standard format
                category_mappings = []
                for mapping in local_mappings:
                    category_mappings.append({
                        'id': mapping.id,
                        'category': mapping.category,
                        'item': mapping.item,
                        'division': '',
                        'serial_no': 0
                    })
            finally:
                db.close()
        
        logger.info(f"Total mappings in database: {total_mappings}")
        logger.info(f"Processing 50 items from index {start_from}, batch size: {batch_size}")
        
        if not category_mappings:
            logger.warning("No category mappings found to process")
            return {
                "success": False,
                "error": "No category mappings found",
                "processed_count": 0
            }
        
        results = {
            "success": True,
            "total_mappings": total_mappings,
            "start_from": start_from,
            "processed_count": 0,
            "created_categories": 0,
            "existing_categories": 0,
            "errors": [],
            "processing_time_ms": 0
        }
        
        start_time = time.time()
        batch_count = 0
        
        for i, mapping in enumerate(category_mappings):
            try:
                logger.info(f"Processing mapping {start_from + i + 1}/{total_mappings}: {mapping['category']} - {mapping['item']}")
                
                # Create item description combining item and category
                item_description = f"{mapping['item']} {mapping['category']}"
                
                # Create 3-level category using learning service
                learning_result = learning_service.create_3_level_category(
                    item_description=item_description,
                    client_category=mapping['category'],
                    similar_items=None,
                    user_id="taxonomy_builder",
                    session_id=f"build_taxonomy_{int(time.time())}"
                )
                
                if learning_result.get("success"):
                    results["processed_count"] += 1
                    if learning_result.get("existing"):
                        results["existing_categories"] += 1
                        logger.info(f"  → Used existing category")
                    else:
                        results["created_categories"] += 1
                        learning_cat = learning_result.get("learning_category", {})
                        logger.info(f"  → Created: {learning_cat.get('level_1_category')} > {learning_cat.get('level_2_category')} > {learning_cat.get('level_3_category')}")
                else:
                    error_msg = f"Failed to process mapping {mapping['id']}: {learning_result.get('error', 'Unknown error')}"
                    logger.error(f"  → {error_msg}")
                    results["errors"].append(error_msg)
                
                batch_count += 1
                
                # Batch processing pause
                if batch_count >= batch_size:
                    logger.info(f"Completed batch of {batch_size} items. Pausing briefly...")
                    time.sleep(2)  # Brief pause between batches
                    batch_count = 0
                
            except Exception as e:
                error_msg = f"Exception processing mapping {mapping['id']}: {str(e)}"
                logger.error(f"  → {error_msg}")
                results["errors"].append(error_msg)
        
        results["processing_time_ms"] = int((time.time() - start_time) * 1000)
        
        # Final summary
        logger.info("=" * 60)
        logger.info("PROCESSING COMPLETE")
        logger.info(f"Total processed: {results['processed_count']}/{total_mappings}")
        logger.info(f"New categories created: {results['created_categories']}")
        logger.info(f"Existing categories used: {results['existing_categories']}")
        logger.info(f"Errors encountered: {len(results['errors'])}")
        logger.info(f"Total processing time: {results['processing_time_ms'] / 1000:.2f} seconds")
        logger.info("=" * 60)
        
        if results["errors"]:
            logger.warning("Errors encountered:")
            for error in results["errors"][:10]:  # Show first 10 errors
                logger.warning(f"  - {error}")
            if len(results["errors"]) > 10:
                logger.warning(f"  ... and {len(results['errors']) - 10} more errors")
        
        return results
        
    except Exception as e:
        logger.error(f"Critical error building taxonomy: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "processed_count": 0
        }

def main():
    """Main function with command line argument parsing."""
    parser = argparse.ArgumentParser(description='Build 3-level taxonomy from category data')
    parser.add_argument('--batch-size', type=int, default=10, 
                       help='Number of items to process in each batch (default: 10)')
    parser.add_argument('--start-from', type=int, default=0,
                       help='Index to start processing from (for resuming, default: 0)')
    parser.add_argument('--use-local', action='store_true',
                       help='Use local CategoryMapping table instead of remote item_category')
    
    args = parser.parse_args()
    
    use_remote = not args.use_local
    source = "remote item_category" if use_remote else "local CategoryMapping"
    
    logger.info("Starting 3-level taxonomy building process")
    logger.info(f"Configuration: batch_size={args.batch_size}, start_from={args.start_from}")
    logger.info(f"Data source: {source}")
    
    # Process the mappings
    results = process_category_mappings(
        batch_size=args.batch_size,
        start_from=args.start_from,
        use_remote=use_remote
    )
    
    if results["success"]:
        logger.info("✅ Taxonomy building completed successfully!")
        exit_code = 0
    else:
        logger.error("❌ Taxonomy building failed!")
        logger.error(f"Error: {results.get('error', 'Unknown error')}")
        exit_code = 1
    
    # Exit with appropriate code
    sys.exit(exit_code)

if __name__ == "__main__":
    main()