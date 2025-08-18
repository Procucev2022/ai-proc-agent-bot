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

from app.database import get_db_session
from app.models import CategoryMapping
from app.services.learning_categorization_service import LearningCategorizationService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def process_category_mappings(batch_size: int = 10, start_from: int = 0) -> Dict[str, Any]:
    """
    Process CategoryMapping entries to build 3-level taxonomy.
    
    Args:
        batch_size: Number of mappings to process in each batch
        start_from: Index to start processing from (for resuming)
        
    Returns:
        Dict with processing results
    """
    db = get_db_session()
    learning_service = LearningCategorizationService()
    
    try:
        # Get total count and items to process
        total_mappings = db.query(CategoryMapping).count()
        category_mappings = db.query(CategoryMapping).offset(start_from).limit(50).all()
        
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
                logger.info(f"Processing mapping {start_from + i + 1}/{total_mappings}: {mapping.category} - {mapping.item}")
                
                # Create item description combining item and category
                item_description = f"{mapping.item} {mapping.category}"
                
                # Create 3-level category using learning service
                learning_result = learning_service.create_3_level_category(
                    item_description=item_description,
                    client_category=mapping.category,
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
                    error_msg = f"Failed to process mapping {mapping.id}: {learning_result.get('error', 'Unknown error')}"
                    logger.error(f"  → {error_msg}")
                    results["errors"].append(error_msg)
                
                batch_count += 1
                
                # Batch processing pause
                if batch_count >= batch_size:
                    logger.info(f"Completed batch of {batch_size} items. Pausing briefly...")
                    time.sleep(2)  # Brief pause between batches
                    batch_count = 0
                
            except Exception as e:
                error_msg = f"Exception processing mapping {mapping.id}: {str(e)}"
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
    finally:
        db.close()

def main():
    """Main function with command line argument parsing."""
    parser = argparse.ArgumentParser(description='Build 3-level taxonomy from CategoryMapping table')
    parser.add_argument('--batch-size', type=int, default=10, 
                       help='Number of items to process in each batch (default: 10)')
    parser.add_argument('--start-from', type=int, default=0,
                       help='Index to start processing from (for resuming, default: 0)')
    
    args = parser.parse_args()
    
    logger.info("Starting 3-level taxonomy building process")
    logger.info(f"Configuration: batch_size={args.batch_size}, start_from={args.start_from}")
    
    # Process the mappings
    results = process_category_mappings(
        batch_size=args.batch_size,
        start_from=args.start_from
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