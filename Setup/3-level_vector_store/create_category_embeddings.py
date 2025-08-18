#!/usr/bin/env python3
"""
Script 3: Create unified ChromaDB vector store for learning taxonomy.

This script creates vector embeddings for:
1. Learning categories and items (for auto-categorization)
2. Seller mappings to categories (for seller matching)

Both use the same 3-level taxonomy in one unified vector store.

Usage:
    python create_category_embeddings.py [--clear-existing]
"""

import sys
import os
import logging
import argparse
import time

import chromadb
import chromadb.utils.embedding_functions as embedding_functions

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.database import get_db_session
from app.models import LearningCategory, LearningCategoryItem, SellerLearningMapping, Seller

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def create_unified_vector_store(clear_existing: bool = False):
    """Create unified ChromaDB vector store for learning taxonomy."""
    
    # Initialize ChromaDB
    chroma_path = "./unified_chroma_db"
    chroma_client = chromadb.PersistentClient(path=chroma_path)
    embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    
    # Handle existing collection
    if clear_existing:
        try:
            chroma_client.delete_collection("learning_taxonomy")
            logger.info("Cleared existing collection")
        except:
            logger.info("No existing collection to clear")
    
    collection = chroma_client.get_or_create_collection(
        name="learning_taxonomy",
        embedding_function=embedding_function
    )
    
    # Get data from database
    db = get_db_session()
    try:
        # Get learning category items
        learning_items = db.query(LearningCategoryItem).join(LearningCategory).all()
        logger.info(f"Found {len(learning_items)} learning items")
        
        # Get seller mappings
        seller_mappings = db.query(SellerLearningMapping).join(Seller).all()
        logger.info(f"Found {len(seller_mappings)} seller mappings")
        
        if not learning_items and not seller_mappings:
            logger.error("No data found to embed!")
            return False
        
        # Prepare data
        documents = []
        metadatas = []
        ids = []
        
        # Add learning category items (for auto-categorization)
        for item in learning_items:
            category = item.learning_category
            category_path = f"{category.level_1_category} > {category.level_2_category} > {category.level_3_category}"
            
            # Document for embedding
            doc_text = f"{item.item_description} {category_path} {item.client_category_name or ''}"
            documents.append(doc_text)
            
            # Metadata for search results
            metadatas.append({
                "type": "category_item",
                "learning_item_id": item.id,
                "learning_category_id": category.id,
                "level_1_category": category.level_1_category,
                "level_2_category": category.level_2_category,
                "level_3_category": category.level_3_category,
                "category_path": category_path,
                "client_category_name": item.client_category_name or "",
                "confidence_score": float(category.confidence_score),
                "item_description": item.item_description
            })
            
            ids.append(f"item_{item.id}")
        
        # Add seller mappings (for seller matching)
        for mapping in seller_mappings:
            seller = mapping.seller
            category_path = f"{mapping.level_1_category} > {mapping.level_2_category} > {mapping.level_3_category}"
            
            # Document for embedding
            location_text = ""
            if seller.location:
                location_text = f"{seller.location.get('city', '')} {seller.location.get('state', '')}"
            doc_text = f"{seller.seller_name} {mapping.original_category} {category_path} {location_text}"
            documents.append(doc_text)
            
            # Metadata for search results
            metadatas.append({
                "type": "seller_mapping",
                "seller_id": seller.seller_id,
                "seller_name": seller.seller_name,
                "phone_number": seller.phone_number,
                "email": seller.email or "",
                "original_category": mapping.original_category,
                "learning_category_id": mapping.learning_category_id,
                "level_1_category": mapping.level_1_category,
                "level_2_category": mapping.level_2_category,
                "level_3_category": mapping.level_3_category,
                "category_path": category_path,
                "confidence_score": float(mapping.confidence_score),
                "location": str(seller.location) if seller.location else "",
                "ranking": seller.ranking.value if seller.ranking else "Gold"
            })
            
            ids.append(f"seller_{mapping.mapping_id}")
        
        # Add to ChromaDB
        logger.info(f"Creating embeddings for {len(documents)} items (this may take a while)...")
        collection.add(documents=documents, metadatas=metadatas, ids=ids)
        
        logger.info(f"✅ Successfully created unified vector store:")
        logger.info(f"   - {len(learning_items)} category items")
        logger.info(f"   - {len(seller_mappings)} seller mappings")
        logger.info(f"   - Total: {len(documents)} embeddings")
        return True
        
    finally:
        db.close()

def main():
    parser = argparse.ArgumentParser(description='Create unified vector store for learning taxonomy')
    parser.add_argument('--clear-existing', action='store_true', help='Clear existing collection')
    args = parser.parse_args()
    
    logger.info("Creating unified vector store...")
    success = create_unified_vector_store(args.clear_existing)
    
    if success:
        logger.info("✅ Unified vector store created successfully!")
        sys.exit(0)
    else:
        logger.error("❌ Failed to create unified vector store!")
        sys.exit(1)

if __name__ == "__main__":
    main()