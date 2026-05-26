#!/usr/bin/env python3
"""
Script to properly set up ChromaDB on server environment.

This script handles server-specific issues like permissions, database locks,
and ensures clean ChromaDB initialization for auto-categorization.
"""

import os
import sys
import shutil
import tempfile
import time
import chromadb
from chromadb.config import Settings
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.utils.chroma_client import get_chroma_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

def check_chroma_directory(chroma_path: str) -> dict:
    """Check the current state of ChromaDB directory."""
    status = {
        "exists": False,
        "writable": False,
        "files": [],
        "issues": []
    }
    
    if os.path.exists(chroma_path):
        status["exists"] = True
        status["writable"] = os.access(chroma_path, os.W_OK)
        
        try:
            status["files"] = os.listdir(chroma_path)
        except PermissionError:
            status["issues"].append("Permission denied reading directory")
        
        # Check SQLite file specifically
        sqlite_file = os.path.join(chroma_path, "chroma.sqlite3")
        if os.path.exists(sqlite_file):
            if not os.access(sqlite_file, os.W_OK):
                status["issues"].append("SQLite file not writable")
    
    return status

def clean_chroma_directory(chroma_path: str, backup: bool = True) -> bool:
    """Safely clean ChromaDB directory."""
    try:
        if os.path.exists(chroma_path):
            # Try to fix permissions first
            try:
                os.chmod(chroma_path, 0o755)
                sqlite_file = os.path.join(chroma_path, "chroma.sqlite3")
                if os.path.exists(sqlite_file):
                    os.chmod(sqlite_file, 0o644)
                print("✅ Fixed directory permissions")
            except Exception as perm_error:
                print(f"⚠️  Could not fix permissions: {perm_error}")
            
            # Try to remove files individually first
            try:
                for file in os.listdir(chroma_path):
                    file_path = os.path.join(chroma_path, file)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        print(f"✅ Removed file: {file}")
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                        print(f"✅ Removed directory: {file}")
                print("✅ Cleaned existing ChromaDB directory")
            except Exception as clean_error:
                print(f"⚠️  Could not clean files: {clean_error}")
                
                # If individual file removal fails, try moving the whole directory
                if backup:
                    backup_path = f"/tmp/chroma_backup_{int(time.time())}"
                    try:
                        shutil.move(chroma_path, backup_path)
                        print(f"✅ Moved existing ChromaDB to: {backup_path}")
                    except Exception as move_error:
                        print(f"❌ Could not backup directory: {move_error}")
                        return False
        
        # Create new directory with proper permissions
        os.makedirs(chroma_path, mode=0o755, exist_ok=True)
        return True
    except Exception as e:
        print(f"❌ Failed to clean directory: {e}")
        return False

def test_chroma_connection(chroma_path: str) -> bool:
    """Test ChromaDB connection and basic operations."""
    try:
        # Test client creation using utility with Settings (v2 API)
        client = get_chroma_client(path=chroma_path)
        print("✅ ChromaDB client created successfully with Settings configuration")
        
        # Test collection creation
        collection = client.get_or_create_collection(
            name="test_collection",
            embedding_function=chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
        )
        print("✅ Test collection created successfully")
        
        # Test adding data
        collection.add(
            documents=["test document"],
            metadatas=[{"test": "metadata"}],
            ids=["test_id"]
        )
        print("✅ Test document added successfully")
        
        # Test querying
        results = collection.query(
            query_texts=["test"],
            n_results=1
        )
        print("✅ Test query executed successfully")
        
        # Clean up test collection
        client.delete_collection(name="test_collection")
        print("✅ Test collection deleted successfully")
        
        return True
    except Exception as e:
        print(f"❌ ChromaDB connection test failed: {e}")
        return False

def setup_chroma_for_server(force_clean: bool = False) -> bool:
    """Main setup function for ChromaDB on server."""
    import time
    
    # Try different paths if the default one has permission issues
    possible_paths = [
        "/home/srujan/workspace/mohap-ai/procucev_proc_agent/chroma_db",
        "/tmp/procucev_chroma_db",
        os.path.expanduser("~/chroma_db_procucev"),
        "/home/srujan/workspace/mohap-ai/procucev_proc_agent/data/chroma_db"
    ]
    
    chroma_path = None
    for path in possible_paths:
        try:
            # Test if we can create/write to this path
            os.makedirs(path, mode=0o755, exist_ok=True)
            test_file = os.path.join(path, "test_write.tmp")
            with open(test_file, 'w') as f:
                f.write("test")
            os.remove(test_file)
            chroma_path = path
            print(f"✅ Using ChromaDB path: {chroma_path}")
            break
        except Exception as e:
            print(f"⚠️  Cannot use path {path}: {e}")
            continue
    
    if not chroma_path:
        print("❌ No writable path found for ChromaDB")
        return False
    
    print("🚀 Setting up ChromaDB for server environment...")
    
    # Step 1: Check current state
    print("\n📋 Checking current ChromaDB state...")
    status = check_chroma_directory(chroma_path)
    
    print(f"Directory exists: {status['exists']}")
    print(f"Directory writable: {status['writable']}")
    print(f"Files found: {status['files']}")
    if status['issues']:
        print(f"Issues found: {status['issues']}")
    
    # Step 2: Determine if cleanup is needed
    needs_cleanup = force_clean or bool(status['issues']) or not status['writable']
    
    if needs_cleanup:
        print("\n🧹 Cleaning ChromaDB directory...")
        if not clean_chroma_directory(chroma_path, backup=True):
            return False
    
    # Step 3: Test ChromaDB connection
    print("\n🔍 Testing ChromaDB connection...")
    if not test_chroma_connection(chroma_path):
        print("\n⚠️  Connection test failed. Trying with temporary directory...")
        
        # Try with temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            if test_chroma_connection(temp_dir):
                print("✅ ChromaDB works with temporary directory")
                print("❌ Issue is with the target directory permissions")
                return False
            else:
                print("❌ ChromaDB has fundamental issues")
                return False
    
    print("\n✅ ChromaDB setup completed successfully!")
    return True

def create_auto_categorization_service_test():
    """Test creating AutoCategorizationService after setup."""
    try:
        from app.services.auto_categorization_service import AutoCategorizationService
        
        print("\n🧪 Testing AutoCategorizationService creation...")
        service = AutoCategorizationService()
        
        # Test health check
        health = service.health_check()
        print(f"Health check: {health}")
        
        if health.get("status") == "healthy":
            print("✅ AutoCategorizationService created successfully")
            return True
        else:
            print(f"❌ AutoCategorizationService health check failed: {health}")
            return False
    except Exception as e:
        print(f"❌ AutoCategorizationService creation failed: {e}")
        return False

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Setup ChromaDB for server environment")
    parser.add_argument("--force-clean", action="store_true", 
                       help="Force clean existing ChromaDB directory")
    parser.add_argument("--test-service", action="store_true",
                       help="Test AutoCategorizationService after setup")
    
    args = parser.parse_args()
    
    # Setup ChromaDB
    success = setup_chroma_for_server(force_clean=args.force_clean)
    
    if success and args.test_service:
        create_auto_categorization_service_test()
    
    if success:
        print("\n🎉 Setup completed! You can now run create_sample_category_data.py")
    else:
        print("\n❌ Setup failed. Please check the error messages above.")
        sys.exit(1)