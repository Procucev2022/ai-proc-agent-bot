#!/usr/bin/env python3
"""
ChromaDB Embedding Update Script

This script updates ChromaDB embeddings with item categorization data.
It can use either remote item_category data or local CategoryMapping data
based on the ENABLE_REMOTE_CATEGORIZATION environment variable.

Also updates the unified learning taxonomy store used by enhanced categorization.

Usage:
    python update_chroma_embeddings.py [--force-remote] [--force-local] [--test-only] [--unified-only]

Options:
    --force-remote    Force use of remote database (ignore config)
    --force-local     Force use of local database (ignore config)
    --test-only       Only test connections, don't update embeddings
    --unified-only    Only update unified store, skip main categorization store
    --stats          Show collection statistics
"""

import sys
import argparse
import logging
import time
from typing import Dict, Any

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_connections():
    """Test database connections."""
    print("Testing database connections...")
    
    from app.config import get_settings
    from app.database import get_db_session, test_remote_connection
    
    settings = get_settings()
    results = {
        "local": False,
        "remote": False,
        "remote_enabled": settings.enable_remote_categorization
    }
    
    # Test local connection
    try:
        db = get_db_session()
        db.execute("SELECT 1").fetchone()
        db.close()
        results["local"] = True
        print("Local database connection: SUCCESS")
    except Exception as e:
        print(f"Local database connection: FAILED - {e}")
    
    # Test remote connection if enabled
    if settings.enable_remote_categorization:
        try:
            results["remote"] = test_remote_connection()
            if results["remote"]:
                print("Remote database connection: SUCCESS")
            else:
                print("Remote database connection: FAILED")
        except Exception as e:
            print(f"Remote database connection: ERROR - {e}")
    else:
        print("Remote database connection: DISABLED")
    
    return results

def get_data_source_info(force_remote=False, force_local=False):
    """Get information about the data source to be used."""
    from app.config import get_settings
    from app.database import get_remote_category_stats
    
    settings = get_settings()
    
    # Determine data source
    if force_remote:
        use_remote = True
        source = "FORCED REMOTE"
    elif force_local:
        use_remote = False
        source = "FORCED LOCAL"
    else:
        use_remote = settings.enable_remote_categorization
        source = "REMOTE" if use_remote else "LOCAL"
    
    print(f"Data source: {source}")
    
    if use_remote:
        try:
            stats = get_remote_category_stats()
            print(f"   Total items: {stats['total_items']}")
            print(f"   Unique categories: {stats['unique_categories']}")
            print(f"   Unique divisions: {stats['unique_divisions']}")
            print("   Top categories:")
            for cat in stats['top_categories'][:5]:
                print(f"      - {cat['category']}: {cat['item_count']} items")
        except Exception as e:
            print(f"   Failed to get remote stats: {e}")
    else:
        from app.database import get_db_session
        from app.models import CategoryMapping
        try:
            db = get_db_session()
            total = db.query(CategoryMapping).count()
            db.close()
            print(f"   Total items: {total}")
        except Exception as e:
            print(f"   Failed to get local stats: {e}")
    
    return use_remote

def update_main_categorization_store(force_remote=False, force_local=False):
    """Update main auto-categorization ChromaDB store."""
    from app.services.auto_categorization_service import AutoCategorizationService
    from app.config import get_settings
    
    print("Starting main categorization store update...")
    start_time = time.time()
    
    try:
        # Initialize service
        service = AutoCategorizationService()
        
        # Override settings if forced
        settings = get_settings()
        original_setting = settings.enable_remote_categorization
        
        if force_remote:
            settings.enable_remote_categorization = True
            print("   Forcing remote data source")
        elif force_local:
            settings.enable_remote_categorization = False
            print("   Forcing local data source")
        
        # Update embeddings
        count = service.populate_embeddings_from_db()
        
        # Restore original setting
        settings.enable_remote_categorization = original_setting
        
        elapsed_time = time.time() - start_time
        
        print(f"Successfully embedded {count} items into main categorization store")
        print(f"Time taken: {elapsed_time:.2f} seconds")
        
        # Show collection stats
        stats = service.get_collection_stats()
        print(f"Collection stats: {stats}")
        
        return True, count
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        print(f"Failed to update main categorization store: {e}")
        print(f"Time taken: {elapsed_time:.2f} seconds")
        return False, 0

def update_unified_learning_store():
    """Update unified learning taxonomy store."""
    print("Starting unified learning taxonomy store update...")
    start_time = time.time()
    
    try:
        # Check if enhanced service exists
        try:
            from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
        except ImportError:
            print("Enhanced auto-categorization service not available, skipping unified store update")
            return True, 0
        
        # Initialize enhanced service
        enhanced_service = EnhancedAutoCategorizationService()
        
        # Get current collection stats
        try:
            total_items = enhanced_service.collection.count()
            print(f"Current unified store items: {total_items}")
        except Exception as e:
            print(f"Could not get current unified store stats: {e}")
            total_items = 0
        
        # The unified store is typically populated by the learning categorization service
        # We'll just report its current status since it's populated differently
        elapsed_time = time.time() - start_time
        
        print(f"Unified learning taxonomy store check completed")
        print(f"Time taken: {elapsed_time:.2f} seconds")
        print("Note: Unified store is populated by learning categorization service during normal operation")
        
        return True, total_items
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        print(f"Failed to check unified learning store: {e}")
        print(f"Time taken: {elapsed_time:.2f} seconds")
        return False, 0

def show_collection_stats():
    """Show current ChromaDB collection statistics."""
    print("Current ChromaDB Collection Statistics:")
    
    # Main categorization store
    print("\nMain Categorization Store:")
    try:
        from app.services.auto_categorization_service import AutoCategorizationService
        service = AutoCategorizationService()
        stats = service.get_collection_stats()
        
        print(f"   Total items: {stats.get('total_items', 'Unknown')}")
        print(f"   Collection name: {stats.get('collection_name', 'Unknown')}")
        print(f"   Persist directory: {stats.get('persist_directory', 'Unknown')}")
        
        # Test a sample search
        try:
            similar_items = service.find_similar_items("cable electrical", top_k=3)
            print(f"   Sample search results (3 items):")
            for item in similar_items:
                print(f"      - {item['category']}: {item['item'][:50]}... (similarity: {item['similarity_score']:.3f})")
        except Exception as e:
            print(f"   Could not perform sample search: {e}")
            
    except Exception as e:
        print(f"   Failed to get main store stats: {e}")
    
    # Unified learning store
    print("\nUnified Learning Taxonomy Store:")
    try:
        from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
        enhanced_service = EnhancedAutoCategorizationService()
        
        total_items = enhanced_service.collection.count()
        print(f"   Total items: {total_items}")
        print(f"   Collection name: learning_taxonomy")
        print(f"   Chroma path: {enhanced_service.chroma_path}")
        
        # Test enhanced service
        try:
            suggestions = enhanced_service.get_category_suggestions("cable electrical", top_k=3)
            print(f"   Sample suggestions (3 items):")
            for suggestion in suggestions:
                print(f"      - {suggestion['client_category']}: {suggestion['similarity_score']:.3f}")
        except Exception as e:
            print(f"   Could not get suggestions: {e}")
            
    except Exception as e:
        print(f"   Failed to get unified store stats: {e}")

def main():
    parser = argparse.ArgumentParser(description="Update ChromaDB embeddings for item categorization")
    parser.add_argument("--force-remote", action="store_true", help="Force use of remote database")
    parser.add_argument("--force-local", action="store_true", help="Force use of local database")
    parser.add_argument("--test-only", action="store_true", help="Only test connections")
    parser.add_argument("--unified-only", action="store_true", help="Only update unified store")
    parser.add_argument("--stats", action="store_true", help="Show collection statistics")
    
    args = parser.parse_args()
    
    if args.force_remote and args.force_local:
        print("ERROR: Cannot specify both --force-remote and --force-local")
        sys.exit(1)
    
    print("=" * 60)
    print("ChromaDB Embedding Update Script")
    print("=" * 60)
    
    # Test connections
    connection_results = test_connections()
    
    if args.test_only:
        print("\nConnection test completed.")
        sys.exit(0)
    
    if args.stats:
        print("\n")
        show_collection_stats()
        sys.exit(0)
    
    print("\n")
    
    # Check if we can proceed (only for main store updates)
    if not args.unified_only:
        use_remote = get_data_source_info(args.force_remote, args.force_local)
        
        if use_remote and not connection_results["remote"]:
            print("ERROR: Cannot use remote database - connection failed!")
            if not args.force_remote:
                print("SUGGESTION: Try --force-local to use local data instead")
            sys.exit(1)
        
        if not use_remote and not connection_results["local"]:
            print("ERROR: Cannot use local database - connection failed!")
            sys.exit(1)
        
        # Confirm before proceeding
        print(f"\nWARNING: About to update ChromaDB embeddings using {'REMOTE' if use_remote else 'LOCAL'} data.")
        print("This will clear existing embeddings and rebuild them.")
        
        if not args.force_remote and not args.force_local:
            response = input("\nProceed? [y/N]: ").lower().strip()
            if response != 'y':
                print("Update cancelled by user.")
                sys.exit(0)
    
    print("\n")
    
    # Update stores
    success = True
    total_items = 0
    
    if args.unified_only:
        # Only update unified store
        store_success, store_items = update_unified_learning_store()
        success = store_success
        total_items = store_items
    else:
        # Update main categorization store
        main_success, main_items = update_main_categorization_store(args.force_remote, args.force_local)
        success = main_success
        total_items = main_items
        
        if main_success:
            # Also check/update unified store
            print("\n")
            unified_success, unified_items = update_unified_learning_store()
            # Don't fail overall if unified update fails
            if not unified_success:
                print("WARNING: Unified store update had issues, but main store updated successfully")
    
    print("\n" + "=" * 60)
    if success:
        print("ChromaDB embedding update completed successfully!")
        print(f"Total items processed: {total_items}")
    else:
        print("ChromaDB embedding update failed!")
        sys.exit(1)

if __name__ == "__main__":
    main()