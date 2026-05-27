#!/usr/bin/env python3
"""
Script to sync category_mappings table with remote database categories.

This populates the local category_mappings table with the actual categories
from the remote database so that cross-references can be created.
"""

import sys
import os
sys.path.insert(0, '.')

from app.database import get_db_session, get_remote_item_categories, test_remote_connection
from app.models import CategoryMapping
import uuid
from datetime import datetime
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def sync_categories_from_remote(clear_existing: bool = False):
    """
    Sync category_mappings table with remote database.

    Args:
        clear_existing: Whether to clear existing category_mappings first
    """

    # Test remote connection
    if not test_remote_connection():
        logger.error("Cannot connect to remote database!")
        return False

    # Get remote data
    logger.info("Fetching remote item categories...")
    remote_items = get_remote_item_categories()
    logger.info(f"Found {len(remote_items)} remote items")

    # Get database session
    db = get_db_session()

    try:
        # Clear existing if requested
        if clear_existing:
            deleted_count = db.query(CategoryMapping).delete()
            db.commit()
            logger.info(f"Cleared {deleted_count} existing category mappings")

        # Group items by category
        category_items = {}
        for item in remote_items:
            category = item['category']
            item_name = item['item']

            if category not in category_items:
                category_items[category] = []
            category_items[category].append(item_name)

        logger.info(f"Found {len(category_items)} unique categories")

        # Insert into category_mappings
        total_inserted = 0

        for category, items in category_items.items():
            logger.info(f"Processing category '{category}' with {len(items)} items...")

            for item in items:
                # Check if already exists
                existing = db.query(CategoryMapping).filter(
                    CategoryMapping.category == category,
                    CategoryMapping.item == item
                ).first()

                if not existing:
                    mapping = CategoryMapping(
                        id=str(uuid.uuid4()),
                        category=category,
                        item=item
                    )
                    db.add(mapping)
                    total_inserted += 1

            # Commit in batches per category
            db.commit()
            logger.info(f"  → Committed {len(items)} items for '{category}'")

        logger.info("\n" + "="*80)
        logger.info("SYNC COMPLETE")
        logger.info("="*80)
        logger.info(f"Total categories: {len(category_items)}")
        logger.info(f"Total items inserted: {total_inserted}")
        logger.info(f"Total items in category_mappings: {db.query(CategoryMapping).count()}")

        return True

    except Exception as e:
        db.rollback()
        logger.error(f"Error syncing categories: {str(e)}")
        return False
    finally:
        db.close()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Sync category_mappings with remote database')
    parser.add_argument('--clear-existing', action='store_true',
                       help='Clear existing category_mappings before sync')
    args = parser.parse_args()

    logger.info("Starting category sync from remote database...")

    if args.clear_existing:
        logger.warning("⚠️  WARNING: --clear-existing flag set. All existing mappings will be deleted!")
        logger.warning("⚠️  Press Ctrl+C within 5 seconds to cancel...")
        import time
        time.sleep(5)

    success = sync_categories_from_remote(args.clear_existing)

    if success:
        logger.info("\n✅ Category sync completed successfully!")
        logger.info("\nNext steps:")
        logger.info("1. Re-run: python Setup/3-level_vector_store/build_3_level_taxonomy.py --clear-existing")
        logger.info("2. Then run: python Setup/3-level_vector_store/create_category_embeddings.py --clear-existing")
        sys.exit(0)
    else:
        logger.error("\n❌ Category sync failed!")
        sys.exit(1)
