#!/usr/bin/env python3
"""
Script to populate client_category_mapping table for existing learning items.

This script creates ClientCategoryMapping entries for all existing learning items
that have client_category_name values, bridging the gap between client categories
and the learning taxonomy.
"""

import sys
import os
import uuid
import logging
from typing import Dict, Any

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.database import get_db_session
from app.models import LearningCategoryItem, LearningCategory, CategoryMapping, ClientCategoryMapping

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def calculate_category_similarity(learning_path: str, client_category: str) -> float:
    """
    Calculate similarity score between learning category path and client category.
    Simple implementation for now.
    """
    # Convert to lowercase for comparison
    learning_lower = learning_path.lower()
    client_lower = client_category.lower()
    
    # Basic similarity scoring
    if client_lower in learning_lower or learning_lower in client_lower:
        return 0.8
    
    # Check for partial matches
    client_words = set(client_lower.split())
    learning_words = set(learning_lower.split())
    
    if client_words & learning_words:  # Any common words
        return 0.6
    
    return 0.3  # Default similarity

def determine_mapping_confidence(similarity_score: float) -> str:
    """Determine mapping confidence based on similarity score."""
    if similarity_score >= 0.7:
        return "high"
    elif similarity_score >= 0.5:
        return "medium"
    else:
        return "low"

def populate_client_category_mapping() -> Dict[str, Any]:
    """
    Populate client_category_mapping table for existing learning items.
    """
    db = get_db_session()
    results = {
        "success": False,
        "processed_items": 0,
        "created_mappings": 0,
        "skipped_items": 0,
        "errors": []
    }
    
    try:
        # Get all learning items with client categories
        learning_items = db.query(LearningCategoryItem).join(LearningCategory).filter(
            LearningCategoryItem.client_category_name.isnot(None)
        ).all()
        
        logger.info(f"Found {len(learning_items)} learning items with client categories")
        
        # Check existing mappings to avoid duplicates
        existing_mappings = db.query(ClientCategoryMapping).all()
        existing_item_ids = {mapping.learning_category_item_id for mapping in existing_mappings}
        logger.info(f"Found {len(existing_mappings)} existing client category mappings")
        
        for item in learning_items:
            results["processed_items"] += 1
            
            # Skip if mapping already exists
            if item.id in existing_item_ids:
                results["skipped_items"] += 1
                continue
            
            try:
                category = item.learning_category
                category_path = f"{category.level_1_category} > {category.level_2_category} > {category.level_3_category}"
                client_category = item.client_category_name
                
                # Find existing CategoryMapping entry, or create placeholder reference
                client_mapping = db.query(CategoryMapping).filter(
                    CategoryMapping.category == client_category
                ).first()
                
                if client_mapping:
                    mapping_id = client_mapping.id
                    logger.debug(f"Found existing CategoryMapping for {client_category}")
                else:
                    # Use placeholder UUID for client categories not in CategoryMapping
                    mapping_id = str(uuid.uuid4())
                    logger.debug(f"Using placeholder mapping_id for {client_category}")
                
                # Calculate similarity and confidence
                similarity_score = calculate_category_similarity(category_path, client_category)
                confidence = determine_mapping_confidence(similarity_score)
                
                # Create ClientCategoryMapping entry
                cross_ref = ClientCategoryMapping(
                    id=str(uuid.uuid4()),
                    learning_category_item_id=item.id,
                    category_mapping_id=mapping_id,
                    learning_category_path=category_path,
                    client_category_name=client_category,
                    similarity_score=similarity_score,
                    mapping_confidence=confidence
                )
                
                db.add(cross_ref)
                results["created_mappings"] += 1
                
                logger.debug(f"Created mapping: {client_category} -> {category_path} (score: {similarity_score:.3f}, confidence: {confidence})")
                
            except Exception as e:
                error_msg = f"Error processing item {item.id}: {str(e)}"
                logger.error(error_msg)
                results["errors"].append(error_msg)
        
        # Commit all changes
        db.commit()
        results["success"] = True
        
        logger.info("=" * 60)
        logger.info("CLIENT CATEGORY MAPPING POPULATION COMPLETE")
        logger.info(f"Processed items: {results['processed_items']}")
        logger.info(f"Created mappings: {results['created_mappings']}")
        logger.info(f"Skipped items: {results['skipped_items']}")
        logger.info(f"Errors: {len(results['errors'])}")
        logger.info("=" * 60)
        
        return results
        
    except Exception as e:
        db.rollback()
        error_msg = f"Critical error: {str(e)}"
        logger.error(error_msg)
        results["errors"].append(error_msg)
        return results
        
    finally:
        db.close()

def main():
    """Main function."""
    logger.info("Starting client_category_mapping population...")
    
    results = populate_client_category_mapping()
    
    if results["success"]:
        logger.info("✅ Client category mapping population completed successfully!")
        return 0
    else:
        logger.error("❌ Client category mapping population failed!")
        if results["errors"]:
            logger.error("Errors:")
            for error in results["errors"]:
                logger.error(f"  - {error}")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)