#!/usr/bin/env python3
"""
Script 2: Map sellers to 3-level learning categories.

This script processes all seller categories through OpenAI to create
mappings to the established 3-level learning taxonomy system.

Usage:
    python map_sellers_to_categories.py [--batch-size 5] [--seller-id SELLER_ID]
"""

import sys
import os
import logging
import argparse
import time
import uuid
from typing import Dict, Any, Optional
from sqlalchemy import and_

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.database import get_db_session
from app.models import Seller, LearningCategory, SellerLearningMapping
from app.services.openai_service import OpenAIService
from app.services.seller_data_adapter import SellerDataAdapter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def map_sellers_to_learning_categories(batch_size: int = 5, target_seller_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Map all sellers to 3-level learning categories.
    
    Args:
        batch_size: Number of sellers to process in each batch
        target_seller_id: Process only specific seller (for testing)
        
    Returns:
        Dict with mapping results
    """
    db = get_db_session()
    openai_service = OpenAIService()
    
    try:
        # Get sellers from real data (remote database) instead of local mock data
        logger.info("Fetching sellers from remote database using SellerDataAdapter...")
        adapter = SellerDataAdapter()

        if not adapter.test_connection():
            logger.error("Cannot connect to remote database")
            return {
                "success": False,
                "error": "Remote database connection failed",
                "processed_count": 0
            }

        # Get real sellers from remote database
        if target_seller_id:
            seller = adapter.get_seller_by_id(target_seller_id)
            sellers = [seller] if seller else []
            logger.info(f"Processing specific seller: {target_seller_id}")
        else:
            sellers = adapter.get_sellers_from_remote()
            logger.info(f"Processing all {len(sellers)} real sellers from remote database")
        
        if not sellers:
            logger.warning("No sellers found to process")
            return {
                "success": False,
                "error": "No sellers found",
                "processed_count": 0
            }
        
        results = {
            "success": True,
            "total_sellers": len(sellers),
            "processed_count": 0,
            "created_mappings": 0,
            "existing_mappings": 0,
            "created_learning_categories": 0,
            "errors": [],
            "processing_time_ms": 0
        }
        
        start_time = time.time()
        batch_count = 0
        
        for i, seller in enumerate(sellers):
            try:
                logger.info(f"Processing seller {i+1}/{len(sellers)}: {seller.seller_name} (ID: {seller.seller_id})")
                
                # Get seller's categories
                seller_categories = seller.categories if isinstance(seller.categories, list) else []
                if not seller_categories:
                    logger.warning(f"  → No categories found for seller {seller.seller_name}")
                    continue
                
                logger.info(f"  → Processing {len(seller_categories)} categories: {seller_categories}")
                
                for category_idx, category in enumerate(seller_categories):
                    try:
                        # Check if mapping already exists
                        existing_mapping = db.query(SellerLearningMapping).filter(
                            and_(
                                SellerLearningMapping.seller_id == seller.seller_id,
                                SellerLearningMapping.original_category == category
                            )
                        ).first()
                        
                        if existing_mapping:
                            logger.info(f"    → Category '{category}' already mapped")
                            results["existing_mappings"] += 1
                            continue
                        
                        # Get existing learning categories for mapping
                        logger.info(f"    → Getting existing learning categories...")
                        existing_categories = db.query(LearningCategory).all()
                        
                        if not existing_categories:
                            logger.warning(f"    → No existing learning categories found! Run build_3_level_taxonomy.py first")
                            continue
                        
                        # Convert to list of dicts for OpenAI
                        categories_for_ai = [
                            {
                                "id": cat.id,
                                "level_1_category": cat.level_1_category,
                                "level_2_category": cat.level_2_category,
                                "level_3_category": cat.level_3_category
                            }
                            for cat in existing_categories
                        ]
                        
                        logger.info(f"    → Mapping category '{category}' to existing learning categories ({len(categories_for_ai)} available)...")
                        
                        mapping_result = openai_service.map_seller_category_to_existing_learning(
                            seller_category=category,
                            existing_categories=categories_for_ai,
                            seller_name=seller.seller_name,
                            location_info=seller.location
                        )
                        
                        if mapping_result.get("success"):
                            selected_category = mapping_result["selected_category"]
                            
                            # Use the existing learning category (no creation needed)
                            learning_category = db.query(LearningCategory).filter(
                                LearningCategory.id == selected_category["id"]
                            ).first()
                            
                            if learning_category:
                                category_path = f"{learning_category.level_1_category} > {learning_category.level_2_category} > {learning_category.level_3_category}"
                                logger.info(f"    → Using existing learning category: {category_path}")
                                
                                # Update usage frequency
                                learning_category.usage_frequency += 1
                            
                                # Create seller learning mapping
                                seller_mapping = SellerLearningMapping(
                                    mapping_id=str(uuid.uuid4()),
                                    seller_id=seller.seller_id,
                                    original_category=category,
                                    learning_category_id=learning_category.id,
                                    level_1_category=learning_category.level_1_category,
                                    level_2_category=learning_category.level_2_category,
                                    level_3_category=learning_category.level_3_category,
                                    confidence_score=mapping_result.get("similarity_score", 0.8),
                                    ai_reasoning=mapping_result.get("reasoning", ""),
                                    mapping_method="openai_existing_category_selection"
                                )
                                db.add(seller_mapping)
                                results["created_mappings"] += 1
                                
                                logger.info(f"    → ✅ Mapped '{category}' to {category_path}")
                            else:
                                error_msg = f"Selected learning category {selected_category['id']} not found in database"
                                logger.error(f"    → ❌ {error_msg}")
                                results["errors"].append(error_msg)
                        
                        else:
                            error_msg = f"Failed to map seller {seller.seller_id} category '{category}': {mapping_result.get('error', 'Unknown error')}"
                            logger.error(f"    → ❌ {error_msg}")
                            results["errors"].append(error_msg)
                    
                    except Exception as e:
                        error_msg = f"Exception mapping category '{category}' for seller {seller.seller_id}: {str(e)}"
                        logger.error(f"    → ❌ {error_msg}")
                        results["errors"].append(error_msg)
                
                results["processed_count"] += 1
                
                # Commit after each seller
                db.commit()
                
                batch_count += 1
                
                # Batch processing pause
                if batch_count >= batch_size:
                    logger.info(f"Completed batch of {batch_size} sellers. Pausing briefly...")
                    time.sleep(3)  # Pause between batches for API rate limiting
                    batch_count = 0
                
            except Exception as e:
                error_msg = f"Exception processing seller {seller.seller_id}: {str(e)}"
                logger.error(f"  → ❌ {error_msg}")
                results["errors"].append(error_msg)
                db.rollback()
        
        results["processing_time_ms"] = int((time.time() - start_time) * 1000)
        
        # Final summary
        logger.info("=" * 60)
        logger.info("SELLER MAPPING COMPLETE")
        logger.info(f"Sellers processed: {results['processed_count']}/{results['total_sellers']}")
        logger.info(f"New mappings created: {results['created_mappings']}")
        logger.info(f"Existing mappings found: {results['existing_mappings']}")
        logger.info(f"New learning categories created: {results['created_learning_categories']}")
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
        db.rollback()
        logger.error(f"Critical error mapping sellers: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "processed_count": 0
        }
    finally:
        db.close()

def main():
    """Main function with command line argument parsing."""
    parser = argparse.ArgumentParser(description='Map sellers to 3-level learning categories')
    parser.add_argument('--batch-size', type=int, default=5,
                       help='Number of sellers to process in each batch (default: 5)')
    parser.add_argument('--seller-id', type=str, default=None,
                       help='Process only specific seller ID (for testing)')
    
    args = parser.parse_args()
    
    logger.info("Starting seller to category mapping process")
    logger.info(f"Configuration: batch_size={args.batch_size}")
    if args.seller_id:
        logger.info(f"Target seller: {args.seller_id}")
    
    # Process the sellers
    results = map_sellers_to_learning_categories(
        batch_size=args.batch_size,
        target_seller_id=args.seller_id
    )
    
    if results["success"]:
        logger.info("✅ Seller mapping completed successfully!")
        exit_code = 0
    else:
        logger.error("❌ Seller mapping failed!")
        logger.error(f"Error: {results.get('error', 'Unknown error')}")
        exit_code = 1
    
    # Exit with appropriate code
    sys.exit(exit_code)

if __name__ == "__main__":
    main()