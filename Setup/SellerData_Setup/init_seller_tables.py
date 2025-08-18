"""
Database initialization script for seller recommendation system.

This script creates all the necessary database tables for the seller
recommendation system including sellers, notifications, interactions,
and system configuration tables.

Usage:
    python init_seller_tables.py [--drop-existing]
"""

import sys
import os
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
import logging

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.config import get_settings
from app.models import Base
from app.database import get_db_session

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def init_seller_recommendation_tables(drop_existing: bool = False):
    """
    Initialize all seller recommendation system tables.
    
    Args:
        drop_existing: Whether to drop existing tables first
    """
    try:
        settings = get_settings()
        logger.info("🚀 Starting seller recommendation system table initialization")
        
        # Create engine
        engine = create_engine(settings.get_database_url())
        
        # Drop tables if requested
        if drop_existing:
            logger.info("🗑️ Dropping existing tables...")
            
            # Drop in reverse dependency order
            drop_statements = [
                "DROP TABLE IF EXISTS seller_categorization_jobs",
                "DROP TABLE IF EXISTS seller_learning_mappings", 
                "DROP TABLE IF EXISTS seller_subscriptions",
                "DROP TABLE IF EXISTS seller_rfq_interactions",
                "DROP TABLE IF EXISTS rfq_seller_notifications",
                "DROP TABLE IF EXISTS mock_rfqs",
                "DROP TABLE IF EXISTS system_configurations",
                "DROP TABLE IF EXISTS sellers"
            ]
            
            with engine.connect() as connection:
                for statement in drop_statements:
                    try:
                        connection.execute(text(statement))
                        logger.info(f"   ✓ {statement}")
                    except Exception as e:
                        logger.warning(f"   ⚠️ {statement} - {str(e)}")
                connection.commit()
        
        # Create all tables
        logger.info("📋 Creating seller recommendation system tables...")
        
        # Create tables using SQLAlchemy metadata
        Base.metadata.create_all(engine)
        
        # Verify table creation
        with engine.connect() as connection:
            # Check if key tables exist
            tables_to_check = [
                'sellers',
                'rfq_seller_notifications', 
                'seller_rfq_interactions',
                'seller_subscriptions',
                'system_configurations',
                'mock_rfqs',
                'seller_learning_mappings',
                'seller_categorization_jobs'
            ]
            
            existing_tables = []
            for table_name in tables_to_check:
                try:
                    result = connection.execute(text(f"SHOW TABLES LIKE '{table_name}'"))
                    if result.fetchone():
                        existing_tables.append(table_name)
                        logger.info(f"   ✅ Table '{table_name}' created successfully")
                    else:
                        logger.error(f"   ❌ Table '{table_name}' not found")
                except Exception as e:
                    logger.error(f"   ❌ Error checking table '{table_name}': {str(e)}")
        
        # Create indexes for performance
        logger.info("🔍 Creating performance indexes...")
        
        index_statements = [
            # Seller indexes
            "CREATE INDEX IF NOT EXISTS idx_sellers_ranking ON sellers(ranking)",
            "CREATE INDEX IF NOT EXISTS idx_sellers_credits ON sellers(subscription_credits)",
            "CREATE INDEX IF NOT EXISTS idx_sellers_active ON sellers(last_active_at)",
            "CREATE INDEX IF NOT EXISTS idx_sellers_location ON sellers((JSON_EXTRACT(location, '$.city')))",
            
            # Notification indexes
            "CREATE INDEX IF NOT EXISTS idx_notifications_seller_time ON rfq_seller_notifications(seller_id, sent_at)",
            "CREATE INDEX IF NOT EXISTS idx_notifications_rfq ON rfq_seller_notifications(rfq_id)",
            
            # Interaction indexes
            "CREATE INDEX IF NOT EXISTS idx_interactions_seller ON seller_rfq_interactions(seller_id)",
            "CREATE INDEX IF NOT EXISTS idx_interactions_rfq ON seller_rfq_interactions(rfq_id)",
            "CREATE INDEX IF NOT EXISTS idx_interactions_type ON seller_rfq_interactions(interaction_type)",
            
            # Mock RFQ indexes
            "CREATE INDEX IF NOT EXISTS idx_mock_rfqs_status ON mock_rfqs(status)",
            "CREATE INDEX IF NOT EXISTS idx_mock_rfqs_created ON mock_rfqs(created_at)",
            
            # Learning mapping indexes
            "CREATE INDEX IF NOT EXISTS idx_learning_mappings_seller ON seller_learning_mappings(seller_id)",
            "CREATE INDEX IF NOT EXISTS idx_learning_mappings_category ON seller_learning_mappings(level_1_category, level_2_category, level_3_category)"
        ]
        
        with engine.connect() as connection:
            for statement in index_statements:
                try:
                    connection.execute(text(statement))
                    logger.info(f"   ✓ Index created")
                except Exception as e:
                    logger.warning(f"   ⚠️ Index creation warning: {str(e)}")
            connection.commit()
        
        logger.info("✅ Seller recommendation system tables initialized successfully!")
        
        return {
            "success": True,
            "tables_created": len(existing_tables),
            "message": "Database initialization completed"
        }
        
    except Exception as e:
        logger.error(f"❌ Error initializing tables: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

def verify_table_structure():
    """Verify that all tables have the expected structure."""
    try:
        logger.info("🔍 Verifying table structure...")
        
        db = get_db_session()
        
        # Test basic operations
        
        # 1. Test system configuration
        from app.models import SystemConfiguration
        test_config = SystemConfiguration(
            config_key="TEST_KEY",
            config_value={"test": "value"},
            description="Test configuration"
        )
        db.add(test_config)
        db.commit()
        
        # Verify it was created
        saved_config = db.query(SystemConfiguration)\
            .filter(SystemConfiguration.config_key == "TEST_KEY").first()
        
        if saved_config:
            logger.info("   ✅ System configuration table working")
            db.delete(saved_config)
            db.commit()
        else:
            logger.error("   ❌ System configuration table not working")
        
        # 2. Test sellers table
        from app.models import Seller, SellerRanking
        test_seller = Seller(
            seller_name="Test Seller",
            phone_number="919999999999",
            email="test@example.com",
            categories=["Test Category"],
            location={"city": "Test City", "lat": 12.0, "lng": 77.0},
            ranking=SellerRanking.Gold
        )
        db.add(test_seller)
        db.commit()
        
        # Verify seller was created
        saved_seller = db.query(Seller)\
            .filter(Seller.phone_number == "919999999999").first()
        
        if saved_seller:
            logger.info("   ✅ Sellers table working")
            db.delete(saved_seller)
            db.commit()
        else:
            logger.error("   ❌ Sellers table not working")
        
        logger.info("✅ Table structure verification completed!")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Error verifying table structure: {str(e)}")
        return False
    finally:
        if 'db' in locals():
            db.close()

def main():
    """Main function for command line usage."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Initialize seller recommendation system tables")
    parser.add_argument("--drop-existing", action="store_true", 
                       help="Drop existing tables before creating new ones")
    parser.add_argument("--verify", action="store_true",
                       help="Verify table structure after creation")
    
    args = parser.parse_args()
    
    # Initialize tables
    result = init_seller_recommendation_tables(drop_existing=args.drop_existing)
    
    if not result["success"]:
        print(f"\n❌ Initialization failed: {result['error']}")
        return 1
    
    # Verify structure if requested
    if args.verify:
        if not verify_table_structure():
            print("\n⚠️ Table structure verification failed")
            return 1
    
    print(f"\n🎉 Success! Seller recommendation system is ready.")
    print(f"📊 Tables created: {result['tables_created']}")
    print("\nNext steps:")
    print("1. Run: python generate_mock_data.py --clear --sellers 60 --rfqs 20")
    print("2. Test the API endpoints at /docs")
    print("3. Process an RFQ: POST /api/seller/rfq-approved")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())