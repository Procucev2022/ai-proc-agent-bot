#!/usr/bin/env python3
"""
Script 1: Build 3-level taxonomy from CategoryMapping table.

This script processes all CategoryMapping entries through OpenAI to create
a comprehensive 3-level learning categorization system.

Usage:
    python build_3_level_taxonomy.py [--batch-size 10] [--start-from 0]
    python build_3_level_taxonomy.py --clear --random --count 100

Options:
    --batch-size    Number of items to process in each batch (default: 50)
    --start-from    Index to start processing from for resuming (default: 0)
    --clear         Clear existing taxonomy tables before populating
    --random        Select random records instead of sequential
    --count         Number of random records to process (requires --random)
"""

import sys
import os
import logging
import argparse
import time
import random
import asyncio
from typing import Dict, Any, List

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.database import get_db_session, get_remote_item_categories, test_remote_connection
from app.models import (
    CategoryMapping, LearningCategory, LearningCategoryItem,
    ClientCategoryMapping, SellerLearningMapping
)
from app.services.learning_categorization_service import LearningCategorizationService
from app.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def clear_taxonomy_tables() -> Dict[str, Any]:
    """
    Clear all taxonomy-related tables before rebuilding.

    Clears the following tables in order (respecting foreign key constraints):
    1. SellerLearningMapping - seller mappings to learning categories
    2. ClientCategoryMapping - cross-references between learning and client categories
    3. LearningCategoryItem - items linked to learning categories
    4. LearningCategory - main 3-level taxonomy table

    Returns:
        Dict with deletion counts for each table
    """
    db = get_db_session()
    results = {
        "success": True,
        "tables_cleared": {},
        "total_deleted": 0
    }

    try:
        logger.info("=" * 60)
        logger.info("CLEARING TAXONOMY TABLES")
        logger.info("=" * 60)

        # Order matters due to foreign key constraints
        tables_to_clear = [
            ("SellerLearningMapping", SellerLearningMapping),
            ("ClientCategoryMapping", ClientCategoryMapping),
            ("LearningCategoryItem", LearningCategoryItem),
            ("LearningCategory", LearningCategory),
        ]

        for table_name, model in tables_to_clear:
            count = db.query(model).count()
            db.query(model).delete()
            results["tables_cleared"][table_name] = count
            results["total_deleted"] += count
            logger.info(f"  Cleared {table_name}: {count} records deleted")

        db.commit()
        logger.info(f"Total records deleted: {results['total_deleted']}")
        logger.info("=" * 60)

        return results

    except Exception as e:
        db.rollback()
        logger.error(f"Error clearing tables: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "tables_cleared": results["tables_cleared"]
        }
    finally:
        db.close()


async def process_category_mappings(batch_size: int = 50, start_from: int = 0, use_random: bool = False, count: int = None, delay: float = 3.0) -> Dict[str, Any]:
    """
    Process category data from item_category table to build 3-level taxonomy.

    Always uses remote item_category data as the primary source to ensure
    consistency between auto-categorization and learning taxonomy systems.
    This will also populate the client_category_mapping table automatically.

    Args:
        batch_size: Number of mappings to process in each batch
        start_from: Index to start processing from (for resuming)
        use_random: If True, select random records instead of sequential
        count: Number of random records to select (only used with use_random)
        delay: Delay in seconds between LLM calls to avoid rate limiting

    Returns:
        Dict with processing results
    """
    try:
        # Always use remote item_category data as primary source
        settings = get_settings()
        if not settings.enable_remote_categorization:
            logger.error("Remote categorization must be enabled to build taxonomy from item_category")
            return {
                "success": False,
                "error": "Remote categorization not enabled - required for item_category access",
                "processed_count": 0
            }
        
        if not test_remote_connection():
            logger.error("Remote database connection failed")
            return {
                "success": False,
                "error": "Remote database connection failed",
                "processed_count": 0
            }
        
        logger.info("Using remote item_category data as primary source for learning taxonomy")
        all_items = get_remote_item_categories()
        total_mappings = len(all_items)
        logger.info(f"Found {total_mappings} items in item_category table")

        # Select items based on mode (random or sequential)
        if use_random:
            # Random selection mode
            num_to_select = count if count else batch_size
            num_to_select = min(num_to_select, total_mappings)  # Don't exceed available items
            selected_items = random.sample(all_items, num_to_select)
            logger.info(f"Random mode: Selected {num_to_select} random items from {total_mappings} total")
        else:
            # Sequential mode (original behavior)
            selected_items = all_items[start_from:start_from + batch_size]
            logger.info(f"Sequential mode: Processing {len(selected_items)} items from index {start_from}")

        # Convert remote items to standard format
        category_mappings = []
        for item_data in selected_items:
            category_mappings.append({
                'id': item_data['uuid'],
                'category': item_data['category'],
                'item': item_data['item'],
                'division': item_data.get('division', ''),
                'serial_no': item_data.get('serial_no', 0)
            })

        logger.info(f"Total mappings in database: {total_mappings}")
        logger.info(f"Processing {len(category_mappings)} items, batch size: {batch_size}")
        
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

                # Create fresh service instance for each call to avoid connection issues
                learning_service = LearningCategorizationService()

                # Create 3-level category using learning service (async)
                learning_result = await learning_service.create_3_level_category(
                    item_description=item_description,
                    client_category=mapping['category'],
                    similar_items=None,
                    user_id="taxonomy_builder",
                    session_id=f"build_taxonomy_{int(time.time())}"
                )

                # Rate limiting delay after each LLM call
                await asyncio.sleep(delay)

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

                # Longer pause between batches
                if batch_count >= batch_size:
                    logger.info(f"Completed batch of {batch_size} items. Pausing for 5 seconds...")
                    await asyncio.sleep(5)  # Longer pause between batches
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
    parser = argparse.ArgumentParser(description='Build 3-level taxonomy from item_category data')
    parser.add_argument('--batch-size', type=int, default=50,
                       help='Number of items to process in each batch (default: 50)')
    parser.add_argument('--start-from', type=int, default=0,
                       help='Index to start processing from (for resuming, default: 0)')
    parser.add_argument('--clear', action='store_true',
                       help='Clear existing taxonomy tables before populating')
    parser.add_argument('--random', action='store_true',
                       help='Select random records instead of sequential')
    parser.add_argument('--count', type=int, default=None,
                       help='Number of random records to process (requires --random)')
    parser.add_argument('--delay', type=float, default=3.0,
                       help='Delay in seconds between LLM calls to avoid rate limiting (default: 3.0)')

    args = parser.parse_args()

    # Validate arguments
    if args.count and not args.random:
        parser.error("--count requires --random flag")

    logger.info("Starting 3-level taxonomy building process")
    logger.info(f"Configuration: batch_size={args.batch_size}, start_from={args.start_from}")
    logger.info(f"Options: clear={args.clear}, random={args.random}, count={args.count}, delay={args.delay}s")
    logger.info("Data source: remote item_category table (primary source)")
    logger.info("This will automatically populate client_category_mapping table")

    # Clear existing tables if requested
    if args.clear:
        logger.info("Clearing existing taxonomy tables...")
        clear_result = clear_taxonomy_tables()
        if not clear_result["success"]:
            logger.error(f"Failed to clear tables: {clear_result.get('error')}")
            sys.exit(1)
        logger.info("Tables cleared successfully")

    # Process the mappings (async)
    results = asyncio.run(process_category_mappings(
        batch_size=args.batch_size,
        start_from=args.start_from,
        use_random=args.random,
        count=args.count,
        delay=args.delay
    ))
    
    if results["success"]:
        logger.info("Taxonomy building completed successfully!")
        exit_code = 0
    else:
        logger.error("Taxonomy building failed!")
        logger.error(f"Error: {results.get('error', 'Unknown error')}")
        exit_code = 1
    
    # Exit with appropriate code
    sys.exit(exit_code)

if __name__ == "__main__":
    main()