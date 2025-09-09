"""
Enhanced Auto-Categorization Service using unified vector store.

This service provides auto-categorization using the unified 3-level learning taxonomy
vector store. It searches for category items first, with fallback to the existing
auto-categorization service when no good matches are found.

Key features:
- Primary search in unified vector store for category items
- Returns ClientCategoryMapping/client category for RFQ processing
- Fallback to existing auto-categorization service
- Comprehensive logging and confidence scoring
"""

import logging
import time
from typing import Dict, Any, List, Optional

import chromadb
import chromadb.utils.embedding_functions as embedding_functions

from ..database import get_db_session
from ..models import AutoCategorizationLog
from .auto_categorization_service import AutoCategorizationService
from .openai_service import OpenAIService

logger = logging.getLogger(__name__)

class EnhancedAutoCategorizationService:
    """
    Enhanced auto-categorization using unified 3-level taxonomy vector store.
    
    Provides fast categorization with fallback to existing service when needed.
    """
    
    def __init__(self, chroma_path: str = "./unified_chroma_db"):
        """Initialize the enhanced auto-categorization service."""
        self.chroma_path = chroma_path
        
        # Initialize ChromaDB client
        self.chroma_client = chromadb.PersistentClient(path=chroma_path)
        self.embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        
        # # Get collection
        # self.collection = self.chroma_client.get_collection(
        #     name="learning_taxonomy",
        #     embedding_function=self.embedding_function
        # )
        # Get collection
        try:
            self.collection = self.chroma_client.get_collection(
                name="learning_taxonomy",
                embedding_function=self.embedding_function
            )
        except Exception:  # Handles case when collection doesn't exist
            self.collection = self.chroma_client.create_collection(
                name="learning_taxonomy",
                embedding_function=self.embedding_function
            )
        # Initialize fallback service
        self.fallback_service = AutoCategorizationService()
        
        # Initialize OpenAI service
        self.openai_service = OpenAIService()
    
    def categorize_item(
        self, 
        item_description: str, 
        user_id: str, 
        session_id: Optional[str] = None,
        rfq_id: Optional[str] = None,
        similarity_threshold: float = 0.1,
        top_k: int = 5
    ) -> Dict[str, Any]:
        """
        Categorize item using enhanced vector search with fallback.
        
        Args:
            item_description: Description of item to categorize
            user_id: User ID for audit trail
            session_id: Session ID for tracking
            rfq_id: RFQ ID if applicable
            similarity_threshold: Minimum similarity score to accept match
            top_k: Number of similar items to consider
            
        Returns:
            Dict with categorization results including client category
        """
        start_time = time.time()
        
        try:
            # Step 1: Search unified vector store for category items
            # First check collection status
            total_items = self.collection.count()
            logger.info(f"Enhanced categorization: Collection has {total_items} total items")
            
            results = self.collection.query(
                query_texts=[item_description],
                n_results=top_k,
                include=['documents', 'metadatas', 'distances'],
                where={"type": "category_item"}  # Only search category items
            )
            
            # Debug log the results
            logger.info(f"Enhanced categorization: Found {len(results['documents'][0]) if results['documents'][0] else 0} category items for '{item_description}'")
            
            # Debug log raw distances for troubleshooting
            if results['distances'][0]:
                logger.info(f"Enhanced categorization: Raw distances: {results['distances'][0][:3]}")  # Show first 3 distances
                # Also show what documents were found
                logger.info(f"Enhanced categorization: Sample documents: {results['documents'][0][:2]}")  # Show first 2 docs
            
            # Check if we found any results at all
            if results['documents'][0] and len(results['documents'][0]) > 0:
                # Found matches in learning taxonomy
                best_match = results['metadatas'][0][0]
                # Calculate similarity score for cosine distance (distance range 0-2)
                raw_distance = results['distances'][0][0]
                similarity_score = max(0.0, min(1.0, 1.0 - (raw_distance / 2.0)))
                
                logger.info(f"Best match similarity: {similarity_score:.3f} for '{item_description}'")
                
                # Check if similarity meets threshold
                if similarity_score >= similarity_threshold:
                    # Prepare top matches for OpenAI final selection
                    top_matches = []
                    for meta, dist in zip(results['metadatas'][0], results['distances'][0]):
                        # Calculate similarity score for cosine distance (distance range 0-2)
                        match_similarity = max(0.0, min(1.0, 1.0 - (dist / 2.0)))
                        if match_similarity >= 0.3:  # Lower threshold for cosine similarity
                            top_matches.append({
                                "item": meta["item_description"],
                                "category": meta["client_category_name"],
                                "similarity_score": round(match_similarity, 4)
                            })
                    
                    # Limit to top 3 matches for OpenAI
                    top_matches = top_matches[:3]
                    
                    # Extract available categories from the matches
                    available_categories = list(set([match['category'] for match in top_matches]))
                    
                    # Debug log the categories being passed to OpenAI
                    logger.info(f"Available categories being passed to OpenAI: {available_categories}")
                    logger.info(f"Top matches count: {len(top_matches)}")
                    
                    # Use OpenAI for final categorization decision with category constraints
                    openai_result = self.openai_service.categorize_with_similar_items(
                        item_description, top_matches, available_categories
                    )
                    
                    processing_time = int((time.time() - start_time) * 1000)
                    
                    if openai_result.get("success"):
                        # Log successful categorization
                        self._log_categorization(
                            item_description, user_id, session_id, rfq_id,
                            openai_result["category"],
                            openai_result.get("confidence", 0.8), similarity_score,
                            "enhanced_vector_openai", processing_time
                        )
                        
                        return {
                            "success": True,
                            "method": "enhanced_vector_openai",
                            "client_category": openai_result["category"],
                            "learning_category": {
                                "level_1": best_match["level_1_category"],
                                "level_2": best_match["level_2_category"], 
                                "level_3": best_match["level_3_category"],
                                "category_path": best_match["category_path"]
                            },
                            "confidence_score": openai_result.get("confidence", 0.8),
                            "similarity_score": similarity_score,
                            "processing_time_ms": processing_time,
                            "learning_item_id": best_match["learning_item_id"],
                            "reasoning": openai_result.get("reasoning", f"OpenAI selection from {len(top_matches)} similar items"),
                            "openai_reasoning": openai_result.get("reasoning", ""),
                            "all_matches": [
                                {
                                    "client_category": meta["client_category_name"],
                                    "category_path": meta["category_path"],
                                    "similarity_score": max(0.0, min(1.0, 1.0 - dist)),
                                    "confidence_score": meta["confidence_score"]
                                }
                                for meta, dist in zip(results['metadatas'][0], results['distances'][0])
                                if max(0.0, min(1.0, 1.0 - dist)) >= 0.5  # Only show reasonable matches
                            ][:3]  # Top 3 matches
                        }
                    else:
                        # OpenAI failed, fallback to best vector match
                        logger.warning(f"OpenAI categorization failed, using best vector match: {openai_result.get('error', 'Unknown error')}")
                        
                        # Log with fallback method
                        self._log_categorization(
                            item_description, user_id, session_id, rfq_id,
                            best_match["client_category_name"],
                            best_match["confidence_score"], similarity_score,
                            "enhanced_vector_openai_fallback", processing_time
                        )
                        
                        return {
                            "success": True,
                            "method": "enhanced_vector_openai_fallback",
                            "client_category": best_match["client_category_name"],
                            "learning_category": {
                                "level_1": best_match["level_1_category"],
                                "level_2": best_match["level_2_category"], 
                                "level_3": best_match["level_3_category"],
                                "category_path": best_match["category_path"]
                            },
                            "confidence_score": best_match["confidence_score"],
                            "similarity_score": similarity_score,
                            "processing_time_ms": processing_time,
                            "learning_item_id": best_match["learning_item_id"],
                            "reasoning": f"Vector similarity match (OpenAI failed: {openai_result.get('error', 'Unknown')})",
                            "openai_error": openai_result.get("error", "Unknown error"),
                            "all_matches": [
                                {
                                    "client_category": meta["client_category_name"],
                                    "category_path": meta["category_path"],
                                    "similarity_score": max(0.0, min(1.0, 1.0 - dist)),
                                    "confidence_score": meta["confidence_score"]
                                }
                                for meta, dist in zip(results['metadatas'][0], results['distances'][0])
                                if max(0.0, min(1.0, 1.0 - dist)) >= 0.5  # Only show reasonable matches
                            ][:3]  # Top 3 matches
                        }
                else:
                    logger.info(f"Similarity {similarity_score:.3f} below threshold {similarity_threshold}, falling back")
            else:
                logger.info("No matches found in vector store, falling back")
            
            # Step 2: Fallback to existing auto-categorization service
            logger.info("Using fallback auto-categorization service")
            fallback_result = self.fallback_service.categorize_with_learning(
                item_description=item_description,
                user_id=user_id,
                session_id=session_id
            )
            
            if fallback_result.get("success"):
                # Enhanced result with fallback info - fix key mapping
                fallback_result["method"] = "enhanced_vector_fallback"
                fallback_result["fallback_used"] = True
                fallback_result["fallback_reason"] = f"Primary similarity {similarity_score:.3f} below threshold {similarity_threshold}" if 'similarity_score' in locals() else "No matches found in vector store"
                
                # Fix key mapping: fallback service returns "category" but we need "client_category"
                if "category" in fallback_result and "client_category" not in fallback_result:
                    fallback_result["client_category"] = fallback_result["category"]
                
                # Update learning taxonomy with the new categorization
                try:
                    self._update_learning_taxonomy(
                        item_description=item_description,
                        client_category=fallback_result["client_category"],
                        user_id=user_id
                    )
                    fallback_result["learning_updated"] = True
                    logger.info(f"Updated learning taxonomy with fallback result: {fallback_result['client_category']}")
                except Exception as e:
                    logger.warning(f"Failed to update learning taxonomy: {e}")
                    fallback_result["learning_updated"] = False
                    fallback_result["learning_error"] = str(e)
                
                return fallback_result
            else:
                # Both primary and fallback failed - create "Other" category entry
                processing_time = int((time.time() - start_time) * 1000)
                
                # Try to create fallback "Other" category entry
                try:
                    fallback_success = self._update_learning_taxonomy(
                        item_description=item_description,
                        client_category="Other",
                        user_id=user_id
                    )
                    
                    if fallback_success:
                        self._log_categorization(
                            item_description, user_id, session_id, rfq_id,
                            "Other", 0.3, 0.0, "enhanced_vector_fallback_other", processing_time
                        )
                        
                        return {
                            "success": True,
                            "method": "enhanced_vector_fallback_other",
                            "client_category": "Other",
                            "confidence_score": 0.3,
                            "processing_time_ms": processing_time,
                            "requires_review": True,
                            "message": "Categorized as 'Other' - requires manual review",
                            "fallback_reason": "Both primary and fallback categorization failed",
                            "learning_updated": True
                        }
                except Exception as e:
                    logger.warning(f"Failed to create 'Other' category fallback: {e}")
                
                # Final fallback - complete failure
                self._log_categorization(
                    item_description, user_id, session_id, rfq_id,
                    None, 0.0, 0.0, "enhanced_vector_failed", processing_time
                )
                
                return {
                    "success": False,
                    "method": "enhanced_vector_failed",
                    "error": "Both primary vector search and fallback categorization failed",
                    "primary_error": f"No good similarity matches found (best: {similarity_score:.3f})" if 'similarity_score' in locals() else "No matches found",
                    "fallback_error": fallback_result.get("error", "Unknown fallback error"),
                    "processing_time_ms": processing_time,
                    "suggestion": "Manual categorization required or expand reference database"
                }
        
        except Exception as e:
            processing_time = int((time.time() - start_time) * 1000)
            logger.error(f"Error in enhanced auto-categorization: {str(e)}")
            
            self._log_categorization(
                item_description, user_id, session_id, rfq_id,
                None, 0.0, 0.0, "enhanced_vector_error", processing_time
            )
            
            return {
                "success": False,
                "method": "enhanced_vector_error",
                "error": str(e),
                "processing_time_ms": processing_time
            }
    
    def get_category_suggestions(
        self, 
        item_description: str, 
        top_k: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get category suggestions for an item description.
        
        Args:
            item_description: Description to find suggestions for
            top_k: Number of suggestions to return
            
        Returns:
            List of category suggestions with similarity scores
        """
        try:
            results = self.collection.query(
                query_texts=[item_description],
                n_results=top_k,
                include=['documents', 'metadatas', 'distances'],
                where={"type": "category_item"}
            )
            
            if not results['documents'][0]:
                return []
            
            suggestions = []
            for i, metadata in enumerate(results['metadatas'][0]):
                # Clamp similarity score to reasonable range (0.0 to 1.0)
                raw_distance = results['distances'][0][i]
                similarity_score = max(0.0, min(1.0, 1.0 - raw_distance))
                
                # Only include reasonable suggestions
                if similarity_score >= 0.3:
                    suggestions.append({
                        "client_category": metadata["client_category_name"],
                        "learning_category": {
                            "level_1": metadata["level_1_category"],
                            "level_2": metadata["level_2_category"],
                            "level_3": metadata["level_3_category"],
                            "category_path": metadata["category_path"]
                        },
                        "similarity_score": similarity_score,
                        "confidence_score": metadata["confidence_score"],
                        "item_description": metadata["item_description"]
                    })
            
            return suggestions
            
        except Exception as e:
            logger.error(f"Error getting category suggestions: {str(e)}")
            return []
    
    def _update_learning_taxonomy(self, item_description: str, client_category: str, user_id: str) -> bool:
        """
        Update the learning taxonomy with a new item categorization.
        
        Args:
            item_description: Description of the item
            client_category: Client category assigned
            user_id: User ID for tracking
            
        Returns:
            True if successful, False otherwise
        """
        try:
            from .learning_categorization_service import LearningCategorizationService
            
            learning_service = LearningCategorizationService()
            
            # Use the learning categorization service to add this new item
            result = learning_service.categorize_and_learn(
                item_description=item_description,
                client_category=client_category,
                user_id=user_id,
                confidence_threshold=0.7  # Standard threshold for fallback learning
            )
            
            if result.get("success"):
                logger.info(f"Successfully updated learning taxonomy: {client_category} for '{item_description[:50]}...'")
                return True
            else:
                logger.warning(f"Learning taxonomy update failed: {result.get('error', 'Unknown error')}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating learning taxonomy: {e}")
            return False
    
    def _log_categorization(
        self, 
        input_description: str, 
        user_id: str,
        session_id: Optional[str], 
        rfq_id: Optional[str],
        predicted_category: Optional[str], 
        confidence_score: float,
        similarity_score: Optional[float], 
        method: str,
        processing_time: int
    ):
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
    
    def health_check(self) -> Dict[str, Any]:
        """Perform health check on the enhanced auto-categorization service."""
        try:
            # Check ChromaDB connection
            try:
                collection_count = self.collection.count()
                chromadb_status = "healthy"
            except Exception as e:
                chromadb_status = f"unhealthy: {str(e)}"
                collection_count = 0
            
            # Check fallback service
            try:
                fallback_stats = self.fallback_service.get_collection_stats()
                fallback_status = "healthy" if not fallback_stats.get("error") else "unhealthy"
            except Exception as e:
                fallback_status = f"unhealthy: {str(e)}"
            
            overall_status = "healthy" if all([
                chromadb_status == "healthy",
                fallback_status == "healthy"
            ]) else "unhealthy"
            
            return {
                "overall_status": overall_status,
                "primary_vector_store": {
                    "status": chromadb_status,
                    "collection_count": collection_count,
                    "chroma_path": self.chroma_path
                },
                "fallback_service": {
                    "status": fallback_status
                }
            }
            
        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            return {
                "overall_status": "unhealthy",
                "error": str(e)
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get service statistics."""
        try:
            # Get collection stats
            total_items = self.collection.count()
            
            # Try to get breakdown by type
            try:
                category_items = self.collection.query(
                    query_texts=["sample"],
                    n_results=1,
                    where={"type": "category_item"}
                )
                category_count = len(category_items['documents'][0]) if category_items['documents'][0] else 0
            except:
                category_count = "unknown"
            
            return {
                "unified_vector_store": {
                    "total_items": total_items,
                    "category_items": category_count,
                    "collection_name": "learning_taxonomy",
                    "embedding_model": "all-MiniLM-L6-v2"
                },
                "fallback_service": self.fallback_service.get_collection_stats()
            }
            
        except Exception as e:
            logger.error(f"Error getting stats: {str(e)}")
            return {"error": str(e)}