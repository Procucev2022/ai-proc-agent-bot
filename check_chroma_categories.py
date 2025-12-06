#!/usr/bin/env python3
"""
Script to check categories in both Chroma stores
"""
import chromadb
import os
from collections import Counter

def check_chroma_store(db_path, store_name):
    print(f"\n=== Checking {store_name} ===")
    print(f"Path: {db_path}")
    
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return
    
    try:
        # Connect to Chroma
        client = chromadb.PersistentClient(path=os.path.dirname(db_path))
        
        # List all collections
        collections = client.list_collections()
        print(f"Collections found: {len(collections)}")
        
        for collection in collections:
            print(f"\nCollection: {collection.name}")
            print(f"   Count: {collection.count()}")
            
            # Get all documents with metadata
            try:
                results = collection.get(include=['metadatas'])
                
                if results and results['metadatas']:
                    # Extract categories from metadata
                    categories = []
                    for metadata in results['metadatas']:
                        if metadata and 'category' in metadata:
                            categories.append(metadata['category'])
                    
                    if categories:
                        category_counts = Counter(categories)
                        print(f"   Categories:")
                        for category, count in sorted(category_counts.items()):
                            print(f"     - {category}: {count} items")
                    else:
                        print(f"   No category metadata found")
                else:
                    print(f"   No documents or metadata found")
                    
            except Exception as e:
                print(f"   Error reading collection: {str(e)}")
                
    except Exception as e:
        print(f"Error connecting to {store_name}: {str(e)}")

def main():
    print("Checking Chroma Stores Categories")
    
    # Check both stores
    check_chroma_store(
        "~\\projects\\procucev_proc_agent\\chroma_db\\chroma.sqlite3",
        "Original Chroma DB"
    )
    
    check_chroma_store(
        "~\\projects\\procucev_proc_agent\\unified_chroma_db\\chroma.sqlite3",
        "Unified Chroma DB"
    )

if __name__ == "__main__":
    main()