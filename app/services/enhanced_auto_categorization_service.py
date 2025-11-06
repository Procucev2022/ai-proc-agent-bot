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

    def _search_hierarchical_levels(
        self,
        item_description: str,
        similarity_threshold: float = 0.75,
        top_k: int = 5
    ) -> Dict[str, Any]:
        """
        Search through taxonomy levels hierarchically: Level 3 -> Level 2 -> Level 1.

        Args:
            item_description: Description to search for
            similarity_threshold: Minimum similarity threshold for accepting matches
            top_k: Number of results to consider at each level

        Returns:
            Dict containing best match info and which level provided it
        """
        logger.info(f"Starting hierarchical search for: '{item_description}'")

        # Define search levels from most specific to most general
        search_levels = [
            {"level": "level_3", "threshold": similarity_threshold},
            {"level": "level_2", "threshold": similarity_threshold * 0.85},  # Slightly lower threshold
            {"level": "level_1", "threshold": similarity_threshold * 0.75}   # Lowest threshold
        ]

        for level_config in search_levels:
            level = level_config["level"]
            threshold = level_config["threshold"]

            logger.info(f"Searching at {level} with threshold {threshold:.3f}")

            # Search at current level - filter to only items that have content at this level
            results = self.collection.query(
                query_texts=[item_description],
                n_results=top_k * 3,  # Get more results to filter
                include=['documents', 'metadatas', 'distances'],
                where={"type": "category_item"}
            )

            if not results['documents'][0] or len(results['documents'][0]) == 0:
                logger.info(f"No results found at {level}")
                continue

            # Filter results to only include items that have meaningful content at the target level
            level_matches = []
            for meta, dist in zip(results['metadatas'][0], results['distances'][0]):
                # Calculate similarity score (distance range 0-2 for cosine)
                similarity_score = max(0.0, min(1.0, 1.0 - (dist / 2.0)))

                # Check if this item has meaningful content at the target level
                level_category = meta.get(f"{level}_category", "").strip()

                # Only include items that:
                # 1. Have a non-empty category at this level
                # 2. Meet the similarity threshold
                # 3. If level 3, ensure it's not just copying level 2 content
                if level_category and similarity_score >= threshold:
                    # Additional validation for level 3 to ensure it's truly specific
                    if level == "level_3":
                        level_2_category = meta.get("level_2_category", "").strip()
                        # Skip if level 3 is identical to level 2 (not truly specific)
                        if level_category != level_2_category:
                            level_matches.append({
                                "metadata": meta,
                                "similarity_score": similarity_score,
                                "distance": dist,
                                "matched_level": level,
                                "level_category": level_category
                            })
                    else:
                        # For level 1 and 2, include if they have content
                        level_matches.append({
                            "metadata": meta,
                            "similarity_score": similarity_score,
                            "distance": dist,
                            "matched_level": level,
                            "level_category": level_category
                        })

            # Sort by similarity and take best matches
            level_matches.sort(key=lambda x: x["similarity_score"], reverse=True)

            # Only return results if we found meaningful matches at this level
            if level_matches:
                best_match = level_matches[0]
                logger.info(f"Found {len(level_matches)} valid matches at {level}, best similarity: {best_match['similarity_score']:.3f}")

                return {
                    "success": True,
                    "best_match": best_match["metadata"],
                    "similarity_score": best_match["similarity_score"],
                    "matched_level": level,
                    "all_level_matches": level_matches[:3],  # Top 3 for this level
                    "search_level": level
                }
            else:
                logger.info(f"No valid matches above threshold {threshold:.3f} at {level}")

        logger.info("No matches found at any level")
        return {
            "success": False,
            "reason": "No matches found at any taxonomy level",
            "matched_level": None
        }

    async def categorize_item(
        self,
        item_description: str,
        user_id: str,
        session_id: Optional[str] = None,
        rfq_id: Optional[str] = None,
        similarity_threshold: float = 0.75,
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
            # Step 1: Search unified vector store using hierarchical approach
            # First check collection status
            total_items = self.collection.count()
            logger.info(f"Enhanced categorization: Collection has {total_items} total items")

            # Use hierarchical search instead of single-level search
            hierarchical_result = self._search_hierarchical_levels(
                item_description, similarity_threshold, top_k
            )

            if hierarchical_result["success"]:
                # Found matches in learning taxonomy through hierarchical search
                best_match = hierarchical_result["best_match"]
                similarity_score = hierarchical_result["similarity_score"]
                matched_level = hierarchical_result["matched_level"]

                logger.info(f"Hierarchical match found at {matched_level}: similarity {similarity_score:.3f} for '{item_description}'")

                # Proceed with match found
                # Prepare top matches for OpenAI final selection from hierarchical results
                top_matches = []
                level_matches = hierarchical_result.get("all_level_matches", [])

                for match_info in level_matches:
                    meta = match_info["metadata"]
                    match_similarity = match_info["similarity_score"]
                    if match_similarity >= 0.3:  # Lower threshold for matches
                        top_matches.append({
                            "item": meta["item_description"],
                            "category": meta["client_category_name"],
                            "similarity_score": round(match_similarity, 4),
                            "matched_level": match_info["matched_level"]
                        })

                # Limit to top 3 matches for OpenAI
                top_matches = top_matches[:3]

                # Extract available categories from the matches
                available_categories = list(set([match['category'] for match in top_matches]))

                # Debug log the categories being passed to OpenAI
                logger.info(f"Available categories being passed to OpenAI: {available_categories}")
                logger.info(f"Top matches count: {len(top_matches)}")

                # Use OpenAI for final categorization decision with category constraints
                openai_result = await self.openai_service.categorize_with_similar_items(
                    item_description, top_matches, available_categories
                )

                processing_time = int((time.time() - start_time) * 1000)

                if openai_result.get("success"):
                    # Check if best match has learning taxonomy data
                    learning_match = None
                    if best_match.get("learning_item_id"):
                        learning_match = {
                            "learning_item_id": best_match.get("learning_item_id"),
                            "level_1_category": best_match.get("level_1_category"),
                            "level_2_category": best_match.get("level_2_category"),
                            "level_3_category": best_match.get("level_3_category"),
                            "category_path": best_match.get("category_path"),
                            "match_level": matched_level,
                            "learning_confidence": best_match.get("confidence_score", 0.0)
                        }

                    # Log successful categorization with learning match info if available
                    self._log_categorization(
                        item_description, user_id, session_id, rfq_id,
                        openai_result["category"],
                        openai_result.get("confidence", 0.8), similarity_score,
                        "enhanced_vector_openai", processing_time,
                        learning_match
                    )

                    result = {
                        "success": True,
                        "method": "enhanced_vector_openai",
                        "client_category": openai_result["category"],
                        "confidence_score": openai_result.get("confidence", 0.8),
                        "similarity_score": similarity_score,
                        "processing_time_ms": processing_time,
                        "reasoning": openai_result.get("reasoning", f"OpenAI selection from {len(top_matches)} similar items at {matched_level}"),
                        "openai_reasoning": openai_result.get("reasoning", ""),
                        "matched_taxonomy_level": matched_level,
                        "all_matches": [
                            {
                                "client_category": match_info["metadata"]["client_category_name"],
                                "category_path": match_info["metadata"]["category_path"],
                                "similarity_score": match_info["similarity_score"],
                                "confidence_score": match_info["metadata"]["confidence_score"],
                                "matched_level": match_info["matched_level"]
                            }
                            for match_info in level_matches
                            if match_info["similarity_score"] >= 0.5
                        ][:3]  # Top 3 matches
                    }

                    # Add learning taxonomy data if available (only the matched level)
                    if learning_match:
                        # Only populate the specific level where match was found
                        learning_category = {"level_1": None, "level_2": None, "level_3": None, "category_path": None}

                        if matched_level == "level_1":
                            learning_category["level_1"] = best_match.get("level_1_category")
                        elif matched_level == "level_2":
                            learning_category["level_2"] = best_match.get("level_2_category")
                        elif matched_level == "level_3":
                            learning_category["level_3"] = best_match.get("level_3_category")

                        learning_category["category_path"] = best_match.get("category_path")

                        result["learning_category"] = learning_category
                        result["learning_item_id"] = best_match.get("learning_item_id")

                    return result
                else:
                    # OpenAI failed, fallback to best vector match
                    logger.warning(f"OpenAI categorization failed, using best vector match: {openai_result.get('error', 'Unknown error')}")

                    # Check if best match has learning taxonomy data
                    learning_match = None
                    if best_match.get("learning_item_id"):
                        learning_match = {
                            "learning_item_id": best_match.get("learning_item_id"),
                            "level_1_category": best_match.get("level_1_category"),
                            "level_2_category": best_match.get("level_2_category"),
                            "level_3_category": best_match.get("level_3_category"),
                            "category_path": best_match.get("category_path"),
                            "match_level": matched_level,
                            "learning_confidence": best_match.get("confidence_score", 0.0)
                        }

                    # Log with fallback method with learning match info if available
                    self._log_categorization(
                        item_description, user_id, session_id, rfq_id,
                        best_match["client_category_name"],
                        best_match["confidence_score"], similarity_score,
                        "enhanced_vector_openai_fallback", processing_time,
                        learning_match
                    )

                    result = {
                        "success": True,
                        "method": "enhanced_vector_openai_fallback",
                        "client_category": best_match["client_category_name"],
                        "confidence_score": best_match["confidence_score"],
                        "similarity_score": similarity_score,
                        "processing_time_ms": processing_time,
                        "reasoning": f"Vector similarity match (OpenAI failed: {openai_result.get('error', 'Unknown')})",
                        "openai_error": openai_result.get("error", "Unknown error"),
                        "all_matches": [
                            {
                                "client_category": match_info["metadata"]["client_category_name"],
                                "category_path": match_info["metadata"]["category_path"],
                                "similarity_score": match_info["similarity_score"],
                                "confidence_score": match_info["metadata"]["confidence_score"],
                                "matched_level": match_info["matched_level"]
                            }
                            for match_info in level_matches
                            if match_info["similarity_score"] >= 0.5
                        ][:3]  # Top 3 matches
                    }

                    # Add learning taxonomy data if available (only the matched level)
                    if learning_match:
                        # Only populate the specific level where match was found
                        learning_category = {"level_1": None, "level_2": None, "level_3": None, "category_path": None}

                        if matched_level == "level_1":
                            learning_category["level_1"] = best_match.get("level_1_category")
                        elif matched_level == "level_2":
                            learning_category["level_2"] = best_match.get("level_2_category")
                        elif matched_level == "level_3":
                            learning_category["level_3"] = best_match.get("level_3_category")

                        learning_category["category_path"] = best_match.get("category_path")

                        result["learning_category"] = learning_category
                        result["learning_item_id"] = best_match.get("learning_item_id")

                    return result
            else:
                # No good matches found through hierarchical search
                logger.info(f"Hierarchical search failed: {hierarchical_result.get('reason', 'Unknown reason')}")

            # Step 2: Try learning service before fallback
            logger.info("Trying learning categorization service before fallback")
            learning_service_completed = False
            learning_taxonomy_data = None

            try:
                from .learning_categorization_service import LearningCategorizationService

                learning_service = LearningCategorizationService()

                # First check if there's an existing learning category
                existing_result = learning_service.check_existing_learning_category(item_description)

                if existing_result:
                    # Found existing learning category, now get client category from fallback service
                    logger.info("Found existing learning category, proceeding to fallback for client category")

                    # Store learning taxonomy info for later logging
                    learning_taxonomy_data = {
                        "learning_item_id": existing_result.get("learning_category_id"),
                        "level_1_category": existing_result.get("level_1"),
                        "level_2_category": existing_result.get("level_2"),
                        "level_3_category": existing_result.get("level_3"),
                        "category_path": existing_result.get("category_path"),
                        "match_level": "existing",
                        "learning_confidence": existing_result.get("confidence_score", 0.8)
                    }

                    # Continue to fallback service to get client category
                    learning_service_completed = True
                else:
                    logger.info("No existing learning category found, creating new one")
                    # No existing category, create new one with default "Other" client category
                    create_result = await learning_service.create_3_level_category(
                        item_description=item_description,
                        client_category="Other",  # Default to Other for new learning categories
                        similar_items=[],  # No similar items available
                        user_id=user_id,
                        session_id=session_id
                    )

                    if create_result.get("success"):
                        # Created new learning category, now get client category from fallback service
                        logger.info("Created new learning category, proceeding to fallback for client category")

                        # Store learning taxonomy info for later logging
                        learning_taxonomy_data = {
                            "learning_item_id": create_result.get("learning_category_id"),
                            "level_1_category": create_result.get("level_1"),
                            "level_2_category": create_result.get("level_2"),
                            "level_3_category": create_result.get("level_3"),
                            "category_path": create_result.get("category_path"),
                            "match_level": "new",
                            "learning_confidence": create_result.get("ai_confidence", 0.7)
                        }

                        # Continue to fallback service to get client category
                        learning_service_completed = True
                    else:
                        logger.info(f"Learning service create failed: {create_result.get('error', 'Unknown error')}")
                        learning_service_completed = False

            except Exception as e:
                logger.warning(f"Learning service error: {e}")
                learning_service_completed = False

            # Step 3: Fallback to existing auto-categorization service
            logger.info("Using fallback auto-categorization service")
            fallback_result = self.fallback_service.categorize_with_learning(
                item_description=item_description,
                user_id=user_id,
                session_id=session_id
            )
            
            if fallback_result.get("success"):
                processing_time = int((time.time() - start_time) * 1000)
                fallback_reason = hierarchical_result.get("reason", "No matches found in hierarchical taxonomy search")

                # Successfully received fallback result

                # Fix key mapping: fallback service returns nested structure
                if "auto_categorization" in fallback_result and fallback_result["auto_categorization"].get("success"):
                    auto_cat_result = fallback_result["auto_categorization"]
                    fallback_result["client_category"] = auto_cat_result.get("category", "Other")
                    fallback_result["confidence_score"] = auto_cat_result.get("confidence_score", 0.6)
                    fallback_result["similarity_score"] = None  # No similarity score in fallback
                elif "category" in fallback_result and "client_category" not in fallback_result:
                    # Handle flat structure (older format)
                    fallback_result["client_category"] = fallback_result["category"]
                else:
                    # Fallback if no category found
                    fallback_result["client_category"] = "Other"
                    fallback_result["confidence_score"] = 0.5
                    logger.warning("No category found in fallback result, defaulting to 'Other'")

                # Determine method and logging based on whether learning service was involved
                if learning_service_completed and learning_taxonomy_data:
                    # Learning service completed, log with learning taxonomy data
                    method = "enhanced_learning_fallback"
                    self._log_categorization(
                        item_description, user_id, session_id, rfq_id,
                        fallback_result["client_category"],
                        fallback_result.get("confidence_score", 0.6),
                        fallback_result.get("similarity_score"),
                        method, processing_time,
                        learning_taxonomy_data
                    )
                    logger.info(f"Learning service + fallback completed: {fallback_result['client_category']}")
                else:
                    # No learning service involvement, use regular fallback logging
                    method = "enhanced_vector_fallback"
                    self._log_fallback_categorization(
                        input_description=item_description,
                        user_id=user_id,
                        session_id=session_id,
                        rfq_id=rfq_id,
                        predicted_category=fallback_result["client_category"],
                        confidence_score=fallback_result.get("confidence_score", 0.5),
                        method=method,
                        processing_time=processing_time,
                        fallback_reason=fallback_reason
                    )

                # Update learning taxonomy with the new categorization for future learning
                try:
                    await self._update_learning_taxonomy(
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

                # Enhanced result with fallback info and learning taxonomy data if available
                if learning_service_completed and learning_taxonomy_data:
                    fallback_result["method"] = "enhanced_learning_fallback"
                    fallback_result["learning_category"] = {
                        "level_1": learning_taxonomy_data.get("level_1_category"),
                        "level_2": learning_taxonomy_data.get("level_2_category"),
                        "level_3": learning_taxonomy_data.get("level_3_category"),
                        "category_path": learning_taxonomy_data.get("category_path")
                    }
                    fallback_result["learning_item_id"] = learning_taxonomy_data.get("learning_item_id")
                    fallback_result["matched_taxonomy_level"] = learning_taxonomy_data.get("match_level")
                else:
                    fallback_result["method"] = "enhanced_vector_fallback"

                fallback_result["fallback_used"] = True
                fallback_result["fallback_reason"] = fallback_reason
                fallback_result["processing_time_ms"] = processing_time

                return fallback_result
            else:
                # Both primary and fallback failed - create "Other" category entry
                processing_time = int((time.time() - start_time) * 1000)
                
                # Try to create fallback "Other" category entry
                try:
                    fallback_success = await self._update_learning_taxonomy(
                        item_description=item_description,
                        client_category="Other",
                        user_id=user_id
                    )
                    
                    if fallback_success:
                        self._log_fallback_categorization(
                            input_description=item_description,
                            user_id=user_id,
                            session_id=session_id,
                            rfq_id=rfq_id,
                            predicted_category="Other",
                            confidence_score=0.3,
                            method="enhanced_vector_fallback_other",
                            processing_time=processing_time,
                            fallback_reason="Both primary and fallback categorization failed"
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
                self._log_fallback_categorization(
                    input_description=item_description,
                    user_id=user_id,
                    session_id=session_id,
                    rfq_id=rfq_id,
                    predicted_category=None,
                    confidence_score=0.0,
                    method="enhanced_vector_failed",
                    processing_time=processing_time,
                    fallback_reason="Both primary vector search and fallback categorization failed"
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
            
            self._log_fallback_categorization(
                input_description=item_description,
                user_id=user_id,
                session_id=session_id,
                rfq_id=rfq_id,
                predicted_category=None,
                confidence_score=0.0,
                method="enhanced_vector_error",
                processing_time=processing_time,
                fallback_reason=f"Exception occurred: {str(e)}"
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
    
    async def _update_learning_taxonomy(self, item_description: str, client_category: str, user_id: str) -> bool:
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

            # Use the existing create_3_level_category method to add this new item
            result = await learning_service.create_3_level_category(
                item_description=item_description,
                client_category=client_category,
                similar_items=[],  # No similar items in fallback scenario
                user_id=user_id,
                session_id=None  # Session ID not available in fallback
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
        processing_time: int,
        learning_match: Optional[Dict[str, Any]] = None
    ):
        """Log categorization attempt to database."""
        db = get_db_session()
        try:
            # Extract learning taxonomy information if provided
            learning_data = {}
            if learning_match:
                match_level = learning_match.get("match_level")

                # Only populate the specific level that provided the match
                learning_data = {
                    "learning_item_id": learning_match.get("learning_item_id"),
                    "learning_category_path": learning_match.get("category_path"),
                    "learning_match_level": match_level,
                    "learning_confidence": learning_match.get("learning_confidence")
                }

                # Populate only the specific level that matched
                if match_level == "level_1":
                    learning_data["learning_level_1"] = learning_match.get("level_1_category")
                    learning_data["learning_level_2"] = None
                    learning_data["learning_level_3"] = None
                elif match_level == "level_2":
                    learning_data["learning_level_1"] = None
                    learning_data["learning_level_2"] = learning_match.get("level_2_category")
                    learning_data["learning_level_3"] = None
                elif match_level == "level_3":
                    learning_data["learning_level_1"] = None
                    learning_data["learning_level_2"] = None
                    learning_data["learning_level_3"] = learning_match.get("level_3_category")
                else:
                    # Unknown match level, set all to None
                    learning_data["learning_level_1"] = None
                    learning_data["learning_level_2"] = None
                    learning_data["learning_level_3"] = None

            log_entry = AutoCategorizationLog(
                rfq_id=rfq_id,
                session_id=session_id,
                user_id=user_id,
                input_description=input_description,
                predicted_category=predicted_category,
                confidence_score=confidence_score,
                similarity_score=similarity_score,
                method_used=method,
                processing_time_ms=processing_time,
                **learning_data
            )
            db.add(log_entry)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to log categorization: {e}")
        finally:
            db.close()

    def _log_fallback_categorization(
        self,
        input_description: str,
        user_id: str,
        session_id: Optional[str],
        rfq_id: Optional[str],
        predicted_category: Optional[str],
        confidence_score: float,
        method: str,
        processing_time: int,
        fallback_reason: str
    ):
        """Log fallback categorization attempt (no learning taxonomy match found)."""
        db = get_db_session()
        try:
            log_entry = AutoCategorizationLog(
                rfq_id=rfq_id,
                session_id=session_id,
                user_id=user_id,
                input_description=input_description,
                predicted_category=predicted_category,
                confidence_score=confidence_score,
                similarity_score=None,  # No similarity score for fallback
                method_used=method,
                processing_time_ms=processing_time,
                # Learning taxonomy fields are intentionally left as NULL for fallback
                learning_item_id=None,
                learning_level_1=None,
                learning_level_2=None,
                learning_level_3=None,
                learning_category_path=None,
                learning_match_level="fallback",  # Indicates this was a fallback
                learning_confidence=None
            )
            db.add(log_entry)
            db.commit()
            logger.info(f"Logged fallback categorization: {method} - {fallback_reason}")
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to log fallback categorization: {e}")
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


if __name__ == "__main__":
    """Test the enhanced auto-categorization service with dummy data."""
    import logging

    logging.basicConfig(level=logging.INFO)

    # Initialize service
    service = EnhancedAutoCategorizationService()

    # Test items with different scenarios
    test_items = [
        {
            "description": "Power Systems",
            "user_id": "test_user_1",
            "session_id": "test_session_1",
            "rfq_id": "TEST_RFQ_001"
        }
        # {
        #     "description": "levitating pizza delivery drone",
        #     "user_id": "test_user_2",
        #     "session_id": "test_session_2",
        #     "rfq_id": "TEST_RFQ_002"
        # },
        # {
        #     "description": "Battries",
        #     "user_id": "test_user_3",
        #     "session_id": "test_session_3",
        #     "rfq_id": "TEST_RFQ_003"
        # },
        # {
        #     "description": "time traveling alarm clock",
        #     "user_id": "test_user_4",
        #     "session_id": "test_session_4",
        #     "rfq_id": "TEST_RFQ_004"
        # },
        # {
        #     "description": "anti-gravity yoga mat",
        #     "user_id": "test_user_5",
        #     "session_id": "test_session_5",
        #     "rfq_id": "TEST_RFQ_005"
        # },
        # {
        #     "description": "Cables",
        #     "user_id": "test_user_6",
        #     "session_id": "test_session_6",
        #     "rfq_id": "TEST_RFQ_006"
        # }
    ]

    print("Testing Enhanced Auto-Categorization Service")
    print("=" * 50)

    for i, item in enumerate(test_items, 1):
        print(f"\nTest {i}: '{item['description']}'")
        print("-" * 30)

        result = service.categorize_item(
            item_description=item["description"],
            user_id=item["user_id"],
            session_id=item["session_id"],
            rfq_id=item["rfq_id"]
        )

        print(f"Success: {result.get('success')}")
        print(f"Method: {result.get('method')}")
        print(f"Category: {result.get('client_category')}")
        print(f"Confidence: {result.get('confidence_score')}")
        print(f"Processing Time: {result.get('processing_time_ms')}ms")

        if result.get('learning_category'):
            print(f"Learning Category: {result.get('learning_category')}")

        if not result.get('success'):
            print(f"Error: {result.get('error')}")

    print("\n" + "=" * 50)
    print("Service Health Check:")
    health = service.health_check()
    print(f"Status: {health.get('overall_status')}")

    print("\nService Stats:")
    stats = service.get_stats()
    print(f"Stats: {stats}")