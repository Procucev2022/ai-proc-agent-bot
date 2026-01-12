"""
Auto-categorization service for RFQ items using vector search and AI.

This service implements a hybrid approach:
1. Uses ChromaDB with Sentence Transformer embeddings for vector similarity search
2. Re-ranks top results and passes to OpenAI for final categorization decision
3. Maintains audit trail and confidence scores for monitoring

The service operates as an offline process after RFQ submission to avoid
impacting conversation flow performance.
"""

import chromadb
import chromadb.utils.embedding_functions as embedding_functions
import os
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from pathlib import Path

from ..database import get_db_session, get_remote_item_categories, test_remote_connection
from ..models import CategoryMapping, AutoCategorizationLog
from ..config import get_settings
from .openai_service import OpenAIService
from .learning_categorization_service import LearningCategorizationService

logger = logging.getLogger(__name__)

# Global singleton instance
_auto_categorization_service_instance = None

def get_auto_categorization_service() -> 'AutoCategorizationService':
    """
    Get singleton instance of AutoCategorizationService.
    This ensures the model is loaded only once and reused across requests.
    """
    global _auto_categorization_service_instance
    if _auto_categorization_service_instance is None:
        logger.info("Initializing AutoCategorizationService singleton")
        _auto_categorization_service_instance = AutoCategorizationService()
        logger.info("AutoCategorizationService singleton initialized")
    return _auto_categorization_service_instance

def get_project_root() -> Path:
    """
    Find the project root directory by looking for the 'app' folder.
    This ensures ChromaDB is always stored in the project root regardless of where the script is run from.
    """
    current_dir = Path(__file__).resolve().parent
    
    # Walk up the directory tree to find the project root (directory containing 'app')
    for parent in [current_dir] + list(current_dir.parents):
        if (parent / 'app').exists():
            return parent
    
    # Fallback: use the directory containing this file
    return current_dir.parent.parent

class AutoCategorizationService:
    """
    Service for automatically categorizing RFQ items using vector search and AI.

    Uses ChromaDB with Sentence Transformer embeddings for fast similarity search,
    then leverages OpenAI for intelligent final categorization decisions with
    confidence scoring and reasoning.

    Uses ChromaDB server mode (HttpClient) for multi-worker deployments.
    """

    def __init__(self):
        """Initialize the auto-categorization service with ChromaDB server."""
        settings = get_settings()

        # Use Sentence Transformer embedding function
        self.embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )

        # Connect to ChromaDB server (required for multi-worker support)
        self.chroma_client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port
        )

        # Test connection - fail fast if server is not running
        try:
            self.chroma_client.heartbeat()
            logger.info(f"Connected to ChromaDB server at {settings.chroma_host}:{settings.chroma_port}")
        except Exception as e:
            raise RuntimeError(
                f"ChromaDB server not available at {settings.chroma_host}:{settings.chroma_port}. "
                f"Start the server with: chroma run --host 0.0.0.0 --port {settings.chroma_port} --path ./chroma_db"
            ) from e

        # Get or create collection with embedding function
        self.collection = self.chroma_client.get_or_create_collection(
            name="category_items",
            embedding_function=self.embedding_function
        )

        # Initialize OpenAI service
        self.openai_service = OpenAIService()

        # Initialize Learning Categorization service
        self.learning_service = LearningCategorizationService()
    
    def populate_embeddings_from_db(self) -> int:
        """
        Populate ChromaDB collection with category mappings from database.
        Uses remote item_category data if enabled, otherwise local CategoryMapping.
        
        Returns:
            Number of items added to the collection
        """
        settings = get_settings()
        
        try:
            # Clear existing collection
            try:
                self.chroma_client.delete_collection(name="category_items")
                self.collection = self.chroma_client.get_or_create_collection(
                    name="category_items",
                    embedding_function=self.embedding_function
                )
            except Exception as e:
                logger.info(f"Collection didn't exist or couldn't be deleted: {e}")
            
            # Get data from appropriate source
            if settings.enable_remote_categorization:
                logger.info("Using remote item_category data for categorization")
                items_data = self._get_remote_category_data()
            else:
                logger.info("Using local CategoryMapping data for categorization")
                items_data = self._get_local_category_data()
            
            if not items_data:
                logger.warning("No category data found")
                return 0
            
            # Prepare data for ChromaDB
            documents = []
            metadatas = []
            ids = []
            
            for item in items_data:
                # Simple document text: item + category for better matching
                doc_text = f"{item['item']} {item['category']}"
                documents.append(doc_text)

                # Metadata includes division for remote data but isn't used in search
                metadata = {
                    "category": item['category'],
                    "item": item['item'],
                    "mapping_id": item['id']
                }

                # Add division for remote data (stored but not used in search)
                if 'division' in item:
                    metadata["division"] = item['division']
                if 'serial_no' in item:
                    metadata["serial_no"] = item['serial_no']

                metadatas.append(metadata)
                ids.append(item['id'])

            # Add to ChromaDB collection in batches to avoid max batch size error
            # ChromaDB has a max batch size limit, so we process in chunks
            batch_size = 5000  # Safe batch size for ChromaDB
            total_items = len(items_data)

            for i in range(0, total_items, batch_size):
                end_idx = min(i + batch_size, total_items)
                batch_documents = documents[i:end_idx]
                batch_metadatas = metadatas[i:end_idx]
                batch_ids = ids[i:end_idx]

                logger.info(f"Adding batch {i//batch_size + 1}: items {i+1} to {end_idx} of {total_items}")

                self.collection.add(
                    documents=batch_documents,
                    metadatas=batch_metadatas,
                    ids=batch_ids
                )
            
            source = "remote item_category" if settings.enable_remote_categorization else "local CategoryMapping"
            logger.info(f"Successfully populated ChromaDB with {len(items_data)} items from {source}")
            return len(items_data)
            
        except Exception as e:
            logger.error(f"Error populating embeddings from database: {str(e)}")
            # If remote fails, try fallback to local data
            if settings.enable_remote_categorization:
                logger.warning("Remote categorization failed, attempting fallback to local data")
                try:
                    settings.enable_remote_categorization = False  # Temporarily disable
                    return self.populate_embeddings_from_db()
                except Exception as fallback_error:
                    logger.error(f"Fallback to local data also failed: {str(fallback_error)}")
            raise
    
    def _get_remote_category_data(self) -> List[Dict[str, Any]]:
        """Get category data from remote database."""
        try:
            # Test connection first
            if not test_remote_connection():
                raise Exception("Remote database connection test failed")
            
            remote_items = get_remote_item_categories()
            
            # Convert to standard format
            formatted_items = []
            for item in remote_items:
                formatted_items.append({
                    'id': item['uuid'],
                    'category': item['category'],
                    'item': item['item'],
                    'division': item.get('division', ''),
                    'serial_no': item.get('serial_no', 0)
                })
            
            return formatted_items
            
        except Exception as e:
            logger.error(f"Failed to get remote category data: {str(e)}")
            raise
    
    def _get_local_category_data(self) -> List[Dict[str, Any]]:
        """Get category data from local database."""
        db = get_db_session()
        try:
            category_mappings = db.query(CategoryMapping).all()
            
            # Convert to standard format
            formatted_items = []
            for mapping in category_mappings:
                formatted_items.append({
                    'id': mapping.id,
                    'category': mapping.category,
                    'item': mapping.item
                })
            
            return formatted_items
            
        except Exception as e:
            logger.error(f"Failed to get local category data: {str(e)}")
            raise
        finally:
            db.close()
    
    def find_similar_items(self, item_description: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Find similar items using vector search in ChromaDB.
        
        Args:
            item_description: Description of the item to categorize
            top_k: Number of similar items to return
            
        Returns:
            List of similar items with metadata and similarity scores
        """
        try:
            # Query ChromaDB using text (embeddings generated automatically)
            results = self.collection.query(
                query_texts=[item_description],
                n_results=top_k
            )
            
            if not results['documents'][0]:
                logger.warning(f"No similar items found for: {item_description}")
                return []
            
            similar_items = []
            for i, doc in enumerate(results['documents'][0]):
                metadata = results['metadatas'][0][i]
                distance = results['distances'][0][i]
                
                # Convert distance to similarity score (1 - distance for cosine similarity)
                similarity_score = 1.0 - distance
                
                similar_items.append({
                    "item_description": doc,
                    "category": metadata['category'],
                    "item": metadata['item'],
                    "similarity_score": similarity_score
                })
            
            logger.info(f"Found {len(similar_items)} similar items for categorization")
            return similar_items
            
        except Exception as e:
            logger.error(f"Error finding similar items: {str(e)}")
            raise
    
    def _get_similar_items(self, item_description: str) -> List[Dict]:
        """Get top 3 similar items from ChromaDB and re-rank by similarity."""
        try:
            # ChromaDB automatically generates embeddings for the query text
            results = self.collection.query(
                query_texts=[item_description],  # Use query_texts instead of query_embeddings
                n_results=3,
                include=['documents', 'metadatas', 'distances']
            )
            
            if not results['distances'][0]:
                return []
            
            # Convert distances to similarity scores and format
            similar_items = []
            for i in range(len(results['distances'][0])):
                distance = results['distances'][0][i]
                similarity_score = 1 - distance  # Convert distance to similarity
                metadata = results['metadatas'][0][i]
                
                similar_items.append({
                    "item": metadata["item"],
                    "category": metadata["category"],
                    "similarity_score": round(similarity_score, 4)
                })
            
            # Re-rank by similarity score (should already be sorted, but ensure)
            similar_items.sort(key=lambda x: x["similarity_score"], reverse=True)
            
            return similar_items
            
        except Exception as e:
            raise Exception(f"Failed to get similar items: {str(e)}")
    
    def _handle_no_similar_items(self, item_description: str, user_id: str,
                                session_id: Optional[str], rfq_id: Optional[str],
                                processing_time: int) -> Dict:
        """Handle case when no similar items found - create fallback learning entry."""
        
        try:
            # Create fallback learning category and client mapping
            fallback_category = self._create_fallback_learning_entry(item_description, user_id)
            
            if fallback_category:
                self._log_categorization(
                    item_description, user_id, session_id, rfq_id,
                    fallback_category, 0.5, 0.5, "fallback_created", processing_time
                )
                
                return {
                    "success": True,
                    "category": fallback_category,
                    "method": "fallback_learning_entry",
                    "reason": "no_similar_items_fallback",
                    "message": f"Created fallback learning entry, categorized as '{fallback_category}'",
                    "processing_time_ms": processing_time,
                    "confidence_score": 0.5,
                    "requires_review": True
                }
        except Exception as e:
            logger.warning(f"Failed to create fallback learning entry: {e}")
        
        # Original fallback if learning entry creation fails
        self._log_categorization(
            item_description, user_id, session_id, rfq_id,
            None, 0.0, 0.0, "no_similar_items", processing_time
        )
        
        return {
            "success": False,
            "method": "hybrid_vector_ai",
            "reason": "no_similar_items_found",
            "message": "No similar items found in reference database",
            "processing_time_ms": processing_time,
            "suggestion": "Manual categorization required or expand reference database"
        }
    
    async def categorize_item(self,
                       item_description: str,
                       user_id: str,
                       session_id: Optional[str] = None,
                       rfq_id: Optional[str] = None) -> Dict:
        """
        Categorize an item using hybrid approach: vector search + OpenAI final selection.
        """
        import time
        start_time = time.time()

        try:
            # Step 1: Vector search for top 3 similar items
            similar_items = self._get_similar_items(item_description)

            if not similar_items:
                return self._handle_no_similar_items(
                    item_description, user_id, session_id, rfq_id,
                    int((time.time() - start_time) * 1000)
                )

            # HIGH CONFIDENCE SHORTCUT: If top match similarity >= 0.9 and category is not "Other", skip LLM call
            HIGH_SIMILARITY_THRESHOLD = 0.9
            top_match = similar_items[0]
            if top_match["similarity_score"] >= HIGH_SIMILARITY_THRESHOLD and top_match["category"] != "Other":
                processing_time = int((time.time() - start_time) * 1000)
                selected_category = top_match["category"]
                logger.info(f"High similarity ({top_match['similarity_score']:.3f} >= {HIGH_SIMILARITY_THRESHOLD}), skipping LLM call. Using category: '{selected_category}'")

                result = {
                    "success": True,
                    "method": "hybrid_vector_high_similarity",
                    "category": selected_category,
                    "confidence_score": 0.95,
                    "reasoning": f"High similarity match ({top_match['similarity_score']:.3f}), LLM call skipped",
                    "similar_items_used": similar_items,
                    "processing_time_ms": processing_time
                }

                # Log high-confidence categorization
                self._log_categorization(
                    item_description, user_id, session_id, rfq_id,
                    selected_category,
                    0.95, top_match["similarity_score"],
                    "hybrid_vector_high_similarity", processing_time
                )

                return result

            # Step 2: OpenAI final selection with context
            openai_result = await self.openai_service.categorize_with_similar_items(
                item_description, similar_items
            )

            processing_time = int((time.time() - start_time) * 1000)

            if openai_result["success"]:
                result = {
                    "success": True,
                    "method": "hybrid_vector_ai",
                    "category": openai_result["category"],
                    "confidence_score": openai_result.get("confidence", 0.8),
                    "reasoning": openai_result.get("reasoning", ""),
                    "similar_items_used": similar_items,
                    "processing_time_ms": processing_time
                }
                
                # Log successful categorization
                self._log_categorization(
                    item_description, user_id, session_id, rfq_id,
                    result["category"],
                    result["confidence_score"], similar_items[0]["similarity_score"],
                    "hybrid_vector_ai", processing_time
                )
                
                return result
            else:
                # OpenAI failed to categorize
                return {
                    "success": False,
                    "method": "hybrid_vector_ai",
                    "reason": "openai_categorization_failed",
                    "error": openai_result.get("reasoning", "Unknown error"),
                    "similar_items_found": similar_items,
                    "processing_time_ms": processing_time
                }
                
        except Exception as e:
            processing_time = int((time.time() - start_time) * 1000)
            return {
                "success": False,
                "method": "hybrid_vector_ai",
                "error": str(e),
                "processing_time_ms": processing_time
            }
    
    def _log_categorization(self, input_description: str, user_id: str,
                           session_id: Optional[str], rfq_id: Optional[str],
                           predicted_category: Optional[str], confidence_score: float,
                           similarity_score: Optional[float], method: str,
                           processing_time: int):
        """Log categorization attempt to database."""
        db = get_db_session()
        try:
            log_entry = AutoCategorizationLog(
                rfq_id=rfq_id,
                session_id=session_id,
                user_id=user_id,
                input_description=input_description,
                predicted_category=predicted_category,
                confidence_score=confidence_score,
                similarity_score=similarity_score,
                method_used=method,
                processing_time_ms=processing_time
            )
            db.add(log_entry)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to log categorization: {e}")
        finally:
            db.close()
    
    def add_category_mapping(self, category: str, item: str) -> str:
        """Add a new category mapping to both MySQL and ChromaDB."""
        import uuid
        
        db = get_db_session()
        try:
            # Generate UUID manually
            mapping_id = str(uuid.uuid4())
            
            # Create MySQL record
            mapping = CategoryMapping(
                id=mapping_id,
                category=category,
                item=item
            )
            db.add(mapping)
            db.commit()
            
            # Add to ChromaDB - embeddings generated automatically by ChromaDB
            doc_id = mapping_id
            doc_text = f"{item} {category}"
            
            self.collection.upsert(
                documents=[doc_text],
                metadatas=[{
                    "category": category,
                    "item": item,
                    "mapping_id": mapping_id
                }],
                ids=[doc_id]
                # No need to pass embeddings - ChromaDB generates them automatically
            )
            
            return mapping_id
            
        except Exception as e:
            db.rollback()
            raise Exception(f"Failed to add category mapping: {str(e)}")
        finally:
            db.close()
    
    def clear_collection(self):
        """Clear the ChromaDB collection."""
        try:
            self.chroma_client.delete_collection("category_items")
            self.collection = self.chroma_client.get_or_create_collection(
                name="category_items",
                embedding_function=self.embedding_function
            )
        except Exception as e:
            logger.error(f"Failed to clear collection: {e}")
    
    def add_item_embedding(self, item_id: str, category: str, item: str):
        """Add a single item embedding to ChromaDB."""
        doc_text = f"{item} {category}"
        
        self.collection.upsert(
            documents=[doc_text],
            metadatas=[{
                "category": category,
                "item": item,
                "mapping_id": item_id
            }],
            ids=[item_id]
        )
    
    def get_collection_stats(self) -> Dict:
        """Get statistics about the ChromaDB collection."""
        try:
            count = self.collection.count()
            return {
                "count": count,
                "collection_name": "category_items",
                "embedding_model": "all-MiniLM-L6-v2"
            }
        except Exception as e:
            return {"error": str(e)}
    
    def get_collection_stats(self) -> Dict[str, Any]:
        """Get statistics about the ChromaDB collection."""
        try:
            settings = get_settings()
            count = self.collection.count()
            return {
                "total_items": count,
                "collection_name": self.collection.name,
                "chroma_server": f"{settings.chroma_host}:{settings.chroma_port}"
            }
        except Exception as e:
            logger.error(f"Error getting collection stats: {str(e)}")
            return {"error": str(e)}
    
    async def categorize_with_learning(
        self,
        item_description: str,
        user_id: str,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Enhanced categorization that includes learning category creation.

        This method first performs normal auto-categorization, then creates
        a 3-level learning category using the result as context.

        Args:
            item_description: Description of the item to categorize
            user_id: User ID for audit trail
            session_id: Optional session ID for tracking

        Returns:
            Dict with both auto-categorization and learning categorization results
        """
        try:
            # Step 1: Perform regular auto-categorization
            auto_result = await self.categorize_item(
                item_description=item_description,
                user_id=user_id,
                session_id=session_id
            )

            # Step 2: Create learning category if auto-categorization succeeded
            learning_result = None
            if auto_result.get("success"):
                client_category = auto_result.get("category")
                similar_items = auto_result.get("similar_items_used", [])

                learning_result = await self.learning_service.create_3_level_category(
                    item_description=item_description,
                    client_category=client_category,
                    similar_items=similar_items,
                    user_id=user_id,
                    session_id=session_id
                )

            # Combine results
            return {
                "success": auto_result.get("success", False),
                "auto_categorization": auto_result,
                "learning_categorization": learning_result,
                "method": "auto_categorization_with_learning"
            }

        except Exception as e:
            logger.error(f"Error in categorize_with_learning: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "method": "auto_categorization_with_learning"
            }
    
    def health_check(self) -> Dict[str, Any]:
        """Perform health check on the auto-categorization service."""
        try:
            # Check ChromaDB connection
            collection_stats = self.get_collection_stats()
            
            # Check database connection
            db = get_db_session()
            try:
                category_count = db.query(CategoryMapping).count()
                db.close()
            except Exception as e:
                return {
                    "status": "unhealthy",
                    "error": f"Database connection failed: {str(e)}"
                }
            
            # Check OpenAI service
            if not hasattr(self.openai_service, 'client'):
                return {
                    "status": "unhealthy",
                    "error": "OpenAI service not properly initialized"
                }
            
            settings = get_settings()
            return {
                "status": "healthy",
                "chromadb_items": collection_stats.get("total_items", 0),
                "database_categories": category_count,
                "chroma_server": f"{settings.chroma_host}:{settings.chroma_port}"
            }
            
        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            return {
                "status": "unhealthy",
                "error": str(e)
            }