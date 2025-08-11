#!/usr/bin/env python3
"""
Database migration script to convert from 3-level to 2-level category mapping.

This script:
1. Backs up existing category_mappings data
2. Creates new simplified table structure 
3. Migrates data from old format to new format
4. Clears and rebuilds ChromaDB embeddings
"""

import os
import sys
import json
from datetime import datetime
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import text, MetaData, Table, Column, String, TIMESTAMP, CHAR
from sqlalchemy.sql import func
from app.database import get_db_session, init_database
from app.models import CategoryMapping, AutoCategorizationLog
import uuid

def backup_existing_data():
    """Backup existing category mappings to JSON file."""
    print("🔄 Backing up existing category mappings...")
    
    db = get_db_session()
    try:
        # Get existing data
        existing_mappings = db.execute(text("""
            SELECT id, main_category, sub_category, specific_category, 
                   example_items, keywords, created_at 
            FROM category_mappings
        """)).fetchall()
        
        # Convert to list of dicts for JSON serialization
        backup_data = []
        for row in existing_mappings:
            backup_data.append({
                'id': row[0],
                'main_category': row[1],
                'sub_category': row[2], 
                'specific_category': row[3],
                'example_items': json.loads(row[4]) if row[4] else [],
                'keywords': json.loads(row[5]) if row[5] else [],
                'created_at': row[6].isoformat() if row[6] else None
            })
        
        # Save backup
        backup_filename = f"category_mappings_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(backup_filename, 'w') as f:
            json.dump(backup_data, f, indent=2)
        
        print(f"✅ Backed up {len(backup_data)} records to {backup_filename}")
        return backup_data
        
    except Exception as e:
        print(f"⚠️  No existing data found or backup failed: {e}")
        return []
        
    finally:
        db.close()

def drop_and_recreate_tables():
    """Drop existing tables and recreate with new structure."""
    print("🔄 Recreating tables with new structure...")
    
    # Get engine from database module after initialization
    import app.database as db_module
    engine = db_module.engine
    
    if engine is None:
        raise Exception("Database engine not initialized")
    
    with engine.connect() as conn:
        # Drop existing tables
        conn.execute(text("DROP TABLE IF EXISTS auto_categorization_log"))
        conn.execute(text("DROP TABLE IF EXISTS category_mappings"))
        conn.commit()
        
        # Create new tables using SQLAlchemy models
        CategoryMapping.__table__.create(engine)
        AutoCategorizationLog.__table__.create(engine)
        
        print("✅ Tables recreated successfully")

def migrate_data_to_simplified_format(backup_data):
    """Convert 3-level hierarchy to 2-level category-item mapping."""
    print("🔄 Migrating data to simplified format...")
    
    db = get_db_session()
    try:
        migrated_count = 0
        
        for old_record in backup_data:
            # Create simplified category name
            # Format: "Main Category" or "Main Category - Sub Category" if needed
            if old_record['main_category'] == old_record['sub_category']:
                category = old_record['main_category']
            else:
                category = f"{old_record['main_category']} - {old_record['sub_category']}"
            
            # Insert each example item as a separate record
            for item in old_record['example_items']:
                new_mapping = CategoryMapping(
                    id=str(uuid.uuid4()),
                    category=category,
                    item=item,
                    created_at=func.current_timestamp()
                )
                db.add(new_mapping)
                migrated_count += 1
        
        db.commit()
        print(f"✅ Migrated {migrated_count} category-item mappings")
        
    finally:
        db.close()

def clear_and_rebuild_chromadb():
    """Clear ChromaDB collection and rebuild with new data."""
    print("🔄 Rebuilding ChromaDB embeddings...")
    
    try:
        from app.services.auto_categorization_service import AutoCategorizationService
        
        auto_cat_service = AutoCategorizationService()
        
        # Clear existing collection
        auto_cat_service.clear_collection()
        print("✅ Cleared existing ChromaDB collection")
        
        # Get all category mappings
        db = get_db_session()
        try:
            mappings = db.query(CategoryMapping).all()
            
            # Generate embeddings for each item
            for mapping in mappings:
                auto_cat_service.add_item_embedding(
                    item_id=mapping.id,
                    category=mapping.category,
                    item=mapping.item
                )
            
            stats = auto_cat_service.get_collection_stats()
            print(f"✅ Rebuilt ChromaDB with {stats.get('count', 0)} embeddings")
            
        finally:
            db.close()
            
    except Exception as e:
        print(f"❌ ChromaDB rebuild failed: {e}")
        print("You may need to rebuild ChromaDB manually")

def main():
    """Run the complete migration process."""
    print("🚀 Starting migration to simplified category mapping...\n")
    
    # Initialize database first
    init_database()
    
    try:
        # Step 1: Backup existing data
        backup_data = backup_existing_data()
        
        # Step 2: Recreate tables
        drop_and_recreate_tables()
        
        if backup_data:
            # Step 3: Migrate data
            migrate_data_to_simplified_format(backup_data)
        else:
            print("ℹ️  No existing data to migrate - fresh tables created")
        
        # Step 4: Rebuild ChromaDB
        clear_and_rebuild_chromadb()
        
        print("\n✅ Migration completed successfully!")
        print("\nNext steps:")
        print("1. Run python create_sample_category_data.py to populate with sample data")
        print("2. Test the auto-categorization service")
        print("3. Test RFQ creation flow with new categorization")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        print("Check the backup file and database state before retrying")
        raise

if __name__ == "__main__":
    main()