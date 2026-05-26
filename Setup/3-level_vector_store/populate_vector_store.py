"""
Script to populate ChromaDB vector store with category mappings from database.
This script initializes the auto-categorization service and populates its vector store
with all available category mappings from either the remote item_category table or
local CategoryMapping table, depending on configuration.
"""

import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from app.services.auto_categorization_service import AutoCategorizationService
from app.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

def main():
    """Main function to populate vector store."""
    try:
        logger.info("=" * 80)
        logger.info("VECTOR STORE POPULATION SCRIPT")
        logger.info("=" * 80)

        # Get settings
        settings = get_settings()
        logger.info(f"Remote categorization enabled: {settings.enable_remote_categorization}")

        # Initialize service
        logger.info("\nInitializing Auto-Categorization Service...")
        service = AutoCategorizationService()

        # Check current collection stats
        logger.info("\nChecking current vector store stats...")
        current_stats = service.get_collection_stats()
        logger.info(f"Current collection stats: {current_stats}")

        # Populate from database
        logger.info("\n" + "=" * 80)
        logger.info("Starting population from database...")
        logger.info("=" * 80)

        items_count = service.populate_embeddings_from_db()

        logger.info("\n" + "=" * 80)
        logger.info(f"✓ Successfully populated {items_count} items to vector store")
        logger.info("=" * 80)

        # Verify population
        logger.info("\nVerifying population...")
        final_stats = service.get_collection_stats()
        logger.info(f"Final collection stats: {final_stats}")

        # Health check
        logger.info("\nPerforming health check...")
        health = service.health_check()
        logger.info(f"Health check result: {health}")

        logger.info("\n" + "=" * 80)
        logger.info("POPULATION COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)

        return True

    except Exception as e:
        logger.error(f"\n{'=' * 80}")
        logger.error(f"ERROR during population: {str(e)}")
        logger.error(f"{'=' * 80}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
