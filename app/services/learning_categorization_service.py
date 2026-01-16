"""
Learning categorization service for creating AI-driven 3-level product taxonomy.

This service implements the core learning categorization functionality that creates
and manages a 3-level hierarchical categorization system independent of client 
category mappings. It uses OpenAI to generate structured categorizations and 
maintains cross-references to client categories for analysis and validation.

Key responsibilities:
- Generate 3-level categorizations using OpenAI and similar items context
- Check for existing learning categories to avoid duplication
- Manage usage frequency and confidence scores
- Provide search and retrieval of learning categories
- Handle category validation and quality scoring
"""

import logging
import uuid
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func
from datetime import datetime

from ..database import get_db_session
from ..models import (
    LearningCategory, LearningCategoryItem, ClientCategoryMapping, 
    CategoryMapping, AutoCategorizationLog, LearningCategorySeller, Seller

)
from .openai_service import OpenAIService

logger = logging.getLogger(__name__)

class LearningCategorizationService:
    """
    Core service for AI-driven learning categorization system.
    
    Creates and manages 3-level product taxonomy using OpenAI analysis
    of item descriptions and similar items context.
    """
    
    def __init__(self):
        """Initialize the learning categorization service."""
        self.openai_service = OpenAIService()
    
    async def create_3_level_category(
        self, 
        item_description: str, 
        client_category: str, 
        similar_items: List[Dict[str, Any]] = None,
        user_id: str = None,
        session_id: str = None
    ) -> Dict[str, Any]:
        """
        Create a new 3-level learning category for an item.
        
        Uses OpenAI to analyze the item description and context from similar items
        to generate a structured 3-level categorization.
        
        Args:
            item_description: Description of the item to categorize
            client_category: Original client category for cross-reference
            similar_items: List of similar items for context
            user_id: User ID for audit trail
            session_id: Session ID for audit trail
            
        Returns:
            Dict with success status, learning category data, and metadata
        """
        db = get_db_session()
        try:
            # First check if we already have a learning category for this item
            existing_category = self.check_existing_learning_category(item_description)
            if existing_category:
                # Update usage frequency
                self.update_usage_frequency(existing_category["learning_category_id"])

                # Backfill client_category if current value is empty/"Other" and better value provided
                existing_client_cat = existing_category.get("client_category_name", "")
                if client_category and client_category != "Other":
                    if not existing_client_cat or existing_client_cat == "Other" or existing_client_cat == "":
                        learning_item_id = existing_category.get("learning_item_id")
                        if learning_item_id:
                            updated = self.update_client_category(learning_item_id, client_category)
                            if updated:
                                existing_category["client_category_name"] = client_category
                                logger.info(f"Backfilled client_category '{client_category}' for existing learning item")

                return {
                    "success": True,
                    "existing": True,
                    "learning_category": existing_category,
                    "message": "Using existing learning category"
                }
            
            # Generate 3-level categorization using OpenAI
            categorization_result = await self.openai_service.generate_3_level_categorization(
                item_description=item_description,
                similar_items=similar_items or [],
                client_category=client_category
            )
            
            # Close OpenAI client to prevent event loop errors
            self.openai_service.close_sync()
            
            if not categorization_result.get("success"):
                return {
                    "success": False,
                    "error": "Failed to generate 3-level categorization",
                    "details": categorization_result.get("error", "Unknown error")
                }

            # Extract categorization data
            categorization = categorization_result["categorization"]

            # De-duplicate: Check for hierarchy conflicts and resolve them
            categorization = self._deduplicate_category_hierarchy(
                db=db,
                level_1=categorization["level_1"],
                level_2=categorization["level_2"],
                level_3=categorization["level_3"]
            )
            logger.info(f"Post-dedup categories: L1={categorization['level_1']}, L2={categorization['level_2']}, L3={categorization['level_3']}")

            # Check if this exact 3-level category already exists
            existing_exact = db.query(LearningCategory).filter(
                and_(
                    LearningCategory.level_1_category == categorization["level_1"],
                    LearningCategory.level_2_category == categorization["level_2"],
                    LearningCategory.level_3_category == categorization["level_3"]
                )
            ).first()
            
            if existing_exact:
                learning_category = existing_exact
                # Update usage frequency
                learning_category.usage_frequency += 1
            else:
                # Create new learning category
                learning_category = LearningCategory(
                    id=str(uuid.uuid4()),
                    level_1_category=categorization["level_1"],
                    level_2_category=categorization["level_2"],
                    level_3_category=categorization["level_3"],
                    confidence_score=categorization_result.get("confidence_score", 0.8),
                    usage_frequency=1,
                    created_by="ai_learning",
                    ai_reasoning=categorization_result.get("reasoning", "")
                )
                db.add(learning_category)
                db.flush()  # Get the ID
            
            # Create learning category item
            learning_item = LearningCategoryItem(
                id=str(uuid.uuid4()),
                learning_category_id=learning_category.id,
                item_description=item_description,
                normalized_keywords=self._extract_keywords(item_description),
                confidence_score=categorization_result.get("confidence_score", 0.8),
                similarity_score=similar_items[0].get("similarity_score", 0.0) if similar_items else None,
                client_category_name=client_category
            )
            db.add(learning_item)
            db.flush()

            # Create cross-reference to client category if available
            client_mapping = None  # Initialize to avoid UnboundLocalError when client_category is empty
            if client_category:
                # Create cross-reference tracking directly (since we're now using item_category as primary source)
                category_path = f"{categorization['level_1']} > {categorization['level_2']} > {categorization['level_3']}"
                
                # Find existing CategoryMapping entry, or create a placeholder reference
                client_mapping = db.query(CategoryMapping).filter(
                    CategoryMapping.category == client_category
                ).first()
                
                if client_mapping:
                    learning_item.client_category_mapping_id = client_mapping.id
                    mapping_id = client_mapping.id
                else:
                    # Client category doesn't exist in CategoryMapping (expected with item_category primary source)
                    # We'll use a placeholder UUID for the mapping_id
                    mapping_id = str(uuid.uuid4())
                
                # Always create cross-reference tracking for client categories from item_category
                cross_ref = ClientCategoryMapping(
                    id=str(uuid.uuid4()),
                    learning_category_item_id=learning_item.id,
                    category_mapping_id=mapping_id,
                    learning_category_path=category_path,
                    client_category_name=client_category,
                    similarity_score=self._calculate_category_similarity(
                        category_path, client_category
                    ),
                    mapping_confidence=self._determine_mapping_confidence(
                        categorization_result.get("confidence_score", 0.8)
                    )
                )
                db.add(cross_ref)
            
            db.commit()
            
            # Log the learning categorization
            self._log_learning_categorization(
                item_description=item_description,
                learning_category_id=learning_category.id,
                client_category=client_category,
                confidence_score=categorization_result.get("confidence_score", 0.8),
                user_id=user_id,
                session_id=session_id
            )
            
            return {
                "success": True,
                "existing": False,
                "learning_category": {
                    "learning_category_id": learning_category.id,
                    "level_1_category": learning_category.level_1_category,
                    "level_2_category": learning_category.level_2_category,
                    "level_3_category": learning_category.level_3_category,
                    "confidence_score": float(learning_category.confidence_score),
                    "usage_frequency": learning_category.usage_frequency,
                    "ai_reasoning": learning_category.ai_reasoning
                },
                "learning_item_id": learning_item.id,
                "cross_reference_created": client_mapping is not None
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error creating 3-level category: {str(e)}")
            return {
                "success": False,
                "error": f"Database error: {str(e)}"
            }
        finally:
            db.close()
    
    def check_existing_learning_category(self, item_description: str) -> Optional[Dict[str, Any]]:
        """
        Check if a learning category already exists for this item description.
        
        Args:
            item_description: Item description to search for
            
        Returns:
            Dict with learning category data if found, None otherwise
        """
        db = get_db_session()
        try:
            # Look for exact or very similar item descriptions
            existing_item = db.query(LearningCategoryItem).filter(
                LearningCategoryItem.item_description == item_description
            ).first()
            
            if existing_item:
                learning_category = db.query(LearningCategory).filter(
                    LearningCategory.id == existing_item.learning_category_id
                ).first()
                
                if learning_category:
                    return {
                        "learning_category_id": learning_category.id,
                        "learning_item_id": existing_item.id,
                        "level_1_category": learning_category.level_1_category,
                        "level_2_category": learning_category.level_2_category,
                        "level_3_category": learning_category.level_3_category,
                        "confidence_score": float(learning_category.confidence_score),
                        "usage_frequency": learning_category.usage_frequency,
                        "client_category_name": existing_item.client_category_name
                    }
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking existing learning category: {str(e)}")
            return None
        finally:
            db.close()
    
    def update_usage_frequency(self, learning_category_id: str) -> bool:
        """
        Update usage frequency for a learning category.
        
        Args:
            learning_category_id: ID of the learning category to update
            
        Returns:
            True if successful, False otherwise
        """
        db = get_db_session()
        try:
            learning_category = db.query(LearningCategory).filter(
                LearningCategory.id == learning_category_id
            ).first()
            
            if learning_category:
                learning_category.usage_frequency += 1
                db.commit()
                return True
            
            return False
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error updating usage frequency: {str(e)}")
            return False
        finally:
            db.close()

    def update_client_category(self, learning_item_id: str, new_client_category: str) -> bool:
        """
        Update client_category_name for an existing learning item.

        This is used to backfill client categories when a better value is discovered
        (e.g., from fallback categorization after initial creation with "Other").

        Args:
            learning_item_id: ID of the LearningCategoryItem to update
            new_client_category: The new client category to set

        Returns:
            True if updated successfully, False otherwise
        """
        if not new_client_category or new_client_category == "Other":
            return False  # Don't update with empty or "Other"

        db = get_db_session()
        try:
            learning_item = db.query(LearningCategoryItem).filter(
                LearningCategoryItem.id == learning_item_id
            ).first()

            if not learning_item:
                logger.warning(f"Learning item {learning_item_id} not found for client category update")
                return False

            old_category = learning_item.client_category_name

            # Only update if current value is empty or "Other"
            if old_category and old_category != "Other" and old_category != "":
                logger.info(f"Learning item already has valid client_category: {old_category}, skipping update")
                return False

            learning_item.client_category_name = new_client_category
            db.commit()

            logger.info(f"Updated client_category_name from '{old_category}' to '{new_client_category}' for item {learning_item_id}")
            return True

        except Exception as e:
            db.rollback()
            logger.error(f"Error updating client category: {str(e)}")
            return False
        finally:
            db.close()

    def _deduplicate_category_hierarchy(
        self,
        db,
        level_1: str,
        level_2: str,
        level_3: str
    ) -> Dict[str, str]:
        """
        De-duplicate category hierarchy by checking for conflicts with existing categories.

        Resolves issues where:
        - A Level 1 category name appears as Level 2 elsewhere
        - A Level 2 category name appears as Level 1 elsewhere
        - Similar conflicts that break hierarchy consistency

        Args:
            db: Database session
            level_1: Generated Level 1 category
            level_2: Generated Level 2 category
            level_3: Generated Level 3 category

        Returns:
            Dict with corrected level_1, level_2, level_3 values
        """
        try:
            # Get all existing categories for conflict checking
            existing_categories = db.query(LearningCategory).all()

            if not existing_categories:
                # No existing categories, no conflicts possible
                return {"level_1": level_1, "level_2": level_2, "level_3": level_3}

            # Build lookup sets for each level
            existing_l1 = set()
            existing_l2 = set()
            existing_l3 = set()
            l2_to_l1_map = {}  # Maps L2 -> its parent L1 (with highest usage)
            l1_l2_pairs = {}   # Maps (L1, L2) -> usage count

            for cat in existing_categories:
                existing_l1.add(cat.level_1_category)
                existing_l2.add(cat.level_2_category)
                existing_l3.add(cat.level_3_category)

                # Track L2 to L1 mapping with usage frequency
                l2_key = cat.level_2_category
                if l2_key not in l2_to_l1_map:
                    l2_to_l1_map[l2_key] = (cat.level_1_category, cat.usage_frequency)
                else:
                    # Keep the L1 with higher usage
                    if cat.usage_frequency > l2_to_l1_map[l2_key][1]:
                        l2_to_l1_map[l2_key] = (cat.level_1_category, cat.usage_frequency)

                # Track L1-L2 pair usage
                pair_key = (cat.level_1_category, cat.level_2_category)
                l1_l2_pairs[pair_key] = l1_l2_pairs.get(pair_key, 0) + cat.usage_frequency

            corrected_l1 = level_1
            corrected_l2 = level_2
            corrected_l3 = level_3

            # Rule 1: If generated L1 exists as L2 elsewhere, check if it should stay as L2
            if level_1 in existing_l2 and level_1 not in existing_l1:
                # L1 is actually used as L2 - find its parent L1
                if level_1 in l2_to_l1_map:
                    parent_l1 = l2_to_l1_map[level_1][0]
                    logger.warning(f"De-dup: '{level_1}' exists as L2 under '{parent_l1}'. Promoting '{parent_l1}' to L1.")
                    corrected_l1 = parent_l1
                    corrected_l2 = level_1
                    # L3 stays as is or becomes what was L2
                    if level_2 != level_1:
                        corrected_l3 = level_2  # Push original L2 down to L3

            # Rule 2: If generated L2 exists as L1 elsewhere, use it as L1
            # Check if L2 is primarily used as L1 (more categories under it as L1)
            elif level_2 in existing_l1:
                # Count how many L2 categories exist under this as L1
                l1_usage_count = sum(1 for cat in existing_categories if cat.level_1_category == level_2)
                # Count how many times it appears as L2
                l2_usage_count = sum(1 for cat in existing_categories if cat.level_2_category == level_2)

                # If it's used more as L1 than L2, or only as L1, swap
                if l1_usage_count > l2_usage_count:
                    logger.warning(f"De-dup: '{level_2}' is primarily L1 ({l1_usage_count} as L1 vs {l2_usage_count} as L2). Swapping.")
                    corrected_l1 = level_2
                    corrected_l2 = level_1  # Swap
                    corrected_l3 = level_3

            # Rule 3: If L2 already exists under a DIFFERENT L1, use the existing L1
            elif level_2 in l2_to_l1_map:
                existing_parent_l1 = l2_to_l1_map[level_2][0]
                if existing_parent_l1 != level_1:
                    # Check which L1-L2 pair has higher usage
                    existing_pair_usage = l1_l2_pairs.get((existing_parent_l1, level_2), 0)
                    new_pair_usage = 0  # New pair has no usage yet

                    if existing_pair_usage > new_pair_usage:
                        logger.warning(f"De-dup: '{level_2}' already exists under '{existing_parent_l1}' (usage: {existing_pair_usage}). Using existing L1.")
                        corrected_l1 = existing_parent_l1

            # Rule 4: Fuzzy match L2 to consolidate similar category names
            # e.g., "Valves" vs "Valves & Fittings", "Cables & Wiring" vs "Cabling & Wiring"
            if corrected_l2 not in existing_l2:
                best_match = self._find_similar_l2(corrected_l2, existing_l2, l2_to_l1_map, corrected_l1)
                if best_match:
                    logger.warning(f"De-dup: '{corrected_l2}' similar to existing '{best_match}'. Using existing L2.")
                    corrected_l2 = best_match
                    # If the matched L2 belongs to a different L1, use that L1 too
                    if best_match in l2_to_l1_map:
                        matched_l1 = l2_to_l1_map[best_match][0]
                        if matched_l1 != corrected_l1:
                            logger.warning(f"De-dup: Using L1 '{matched_l1}' from matched L2 '{best_match}'.")
                            corrected_l1 = matched_l1

            if corrected_l1 != level_1 or corrected_l2 != level_2 or corrected_l3 != level_3:
                logger.info(f"De-dup corrected: [{level_1} > {level_2} > {level_3}] -> [{corrected_l1} > {corrected_l2} > {corrected_l3}]")

            return {
                "level_1": corrected_l1,
                "level_2": corrected_l2,
                "level_3": corrected_l3
            }

        except Exception as e:
            logger.error(f"Error in de-duplication: {str(e)}")
            # Return original values if de-dup fails
            return {"level_1": level_1, "level_2": level_2, "level_3": level_3}

    def _find_similar_l2(
        self,
        new_l2: str,
        existing_l2_set: set,
        l2_to_l1_map: Dict,
        preferred_l1: str,
        similarity_threshold: float = 0.70
    ) -> Optional[str]:
        """
        Find an existing L2 category that is similar to the new L2.

        Uses token-based similarity to catch variations like:
        - "Valves" vs "Valves & Fittings"
        - "Springs" vs "Mechanical Springs"
        - "Hydraulic Components" vs "Hydraulic & Pneumatic Components"

        Args:
            new_l2: The new L2 category name
            existing_l2_set: Set of existing L2 category names
            l2_to_l1_map: Mapping of L2 -> (L1, usage)
            preferred_l1: The L1 we'd prefer to match under
            similarity_threshold: Minimum similarity score (0-1) to consider a match

        Returns:
            Best matching existing L2 name, or None if no good match
        """
        from difflib import SequenceMatcher

        def normalize(s: str) -> str:
            """Normalize string for comparison."""
            return s.lower().replace('&', ' and ').replace('-', ' ').replace(',', ' ')

        def tokenize(s: str) -> set:
            """Extract meaningful tokens from category name."""
            normalized = normalize(s)
            # Remove common filler words
            stopwords = {'and', 'the', 'of', 'for', 'with', 'in', 'to', 'a'}
            tokens = set(normalized.split()) - stopwords
            return tokens

        def calculate_similarity(s1: str, s2: str) -> float:
            """Calculate similarity using both token overlap and sequence matching."""
            tokens1 = tokenize(s1)
            tokens2 = tokenize(s2)

            # Token-based Jaccard similarity
            if tokens1 and tokens2:
                intersection = tokens1 & tokens2
                union = tokens1 | tokens2
                jaccard = len(intersection) / len(union)
                is_subset = tokens1.issubset(tokens2) or tokens2.issubset(tokens1)
            else:
                jaccard = 0.0
                is_subset = False

            # Sequence-based similarity
            seq_ratio = SequenceMatcher(None, normalize(s1), normalize(s2)).ratio()

            # Subset bonus: when one category name contains all tokens of another
            # e.g., "Valves" -> "Valves & Fittings", "Springs" -> "Mechanical Springs"
            subset_bonus = 0.25 if is_subset else 0.0

            # Combined score (equal weights + subset bonus)
            combined = (jaccard * 0.5 + seq_ratio * 0.5) + subset_bonus
            return min(combined, 1.0)

        best_match = None
        best_score = 0.0
        best_same_l1 = False

        for existing_l2 in existing_l2_set:
            score = calculate_similarity(new_l2, existing_l2)

            if score >= similarity_threshold:
                # Check if this L2 is under the same L1 (preferred)
                same_l1 = False
                if existing_l2 in l2_to_l1_map:
                    same_l1 = l2_to_l1_map[existing_l2][0] == preferred_l1

                # Prefer matches under the same L1, then highest similarity
                if same_l1 and not best_same_l1:
                    best_match = existing_l2
                    best_score = score
                    best_same_l1 = True
                elif same_l1 == best_same_l1 and score > best_score:
                    best_match = existing_l2
                    best_score = score
                    best_same_l1 = same_l1

        if best_match:
            logger.debug(f"Fuzzy match: '{new_l2}' -> '{best_match}' (score: {best_score:.2f}, same_l1: {best_same_l1})")

        return best_match

    def get_learning_category_suggestions(
        self, 
        item_description: str, 
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Get learning category suggestions based on item description similarity.
        
        Args:
            item_description: Description to find suggestions for
            top_k: Number of suggestions to return
            
        Returns:
            List of learning category suggestions with similarity scores
        """
        db = get_db_session()
        try:
            # For now, use simple keyword matching
            # In future, this could use vector similarity
            keywords = self._extract_keywords(item_description)
            keyword_list = keywords.get("keywords", []) if keywords else []
            
            if not keyword_list:
                return []
            
            # Find learning categories with similar keywords
            suggestions = []
            learning_items = db.query(LearningCategoryItem).all()
            
            for item in learning_items:
                item_keywords = item.normalized_keywords
                if item_keywords and isinstance(item_keywords, dict):
                    item_keyword_list = item_keywords.get("keywords", [])
                    similarity = self._calculate_keyword_similarity(keyword_list, item_keyword_list)
                    
                    if similarity > 0.3:  # Minimum similarity threshold
                        learning_category = db.query(LearningCategory).filter(
                            LearningCategory.id == item.learning_category_id
                        ).first()
                        
                        if learning_category:
                            suggestions.append({
                                "learning_category_id": learning_category.id,
                                "level_1_category": learning_category.level_1_category,
                                "level_2_category": learning_category.level_2_category,
                                "level_3_category": learning_category.level_3_category,
                                "similarity_score": similarity,
                                "usage_frequency": learning_category.usage_frequency,
                                "confidence_score": float(learning_category.confidence_score)
                            })
            
            # Sort by similarity and usage frequency
            suggestions.sort(key=lambda x: (x["similarity_score"], x["usage_frequency"]), reverse=True)
            return suggestions[:top_k]
            
        except Exception as e:
            logger.error(f"Error getting learning category suggestions: {str(e)}")
            return []
        finally:
            db.close()
    
    async def validate_learning_category(
        self, 
        level_1: str, 
        level_2: str, 
        level_3: str, 
        item_description: str
    ) -> Dict[str, Any]:
        """
        Validate a 3-level learning category using OpenAI.
        
        Args:
            level_1: Level 1 category
            level_2: Level 2 category  
            level_3: Level 3 category
            item_description: Item description for context
            
        Returns:
            Dict with validation results
        """
        try:
            validation_result = await self.openai_service.validate_learning_category(
                level_1=level_1,
                level_2=level_2,
                level_3=level_3,
                item_description=item_description
            )
            
            # Close OpenAI client to prevent event loop errors
            self.openai_service.close_sync()
            
            return validation_result
            
        except Exception as e:
            logger.error(f"Error validating learning category: {str(e)}")
            return {
                "is_valid": False,
                "confidence_score": 0.0,
                "validation_issues": [f"Validation error: {str(e)}"]
            }
    
    def get_learning_category_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the learning categorization system.
        
        Returns:
            Dict with learning category statistics
        """
        db = get_db_session()
        try:
            total_categories = db.query(LearningCategory).count()
            total_items = db.query(LearningCategoryItem).count()
            total_cross_refs = db.query(ClientCategoryMapping).count()
            
            # Top level 1 categories by usage
            top_level_1 = db.query(
                LearningCategory.level_1_category,
                func.sum(LearningCategory.usage_frequency).label('total_usage')
            ).group_by(
                LearningCategory.level_1_category
            ).order_by(
                desc('total_usage')
            ).limit(10).all()
            
            # Average confidence score
            avg_confidence = db.query(
                func.avg(LearningCategory.confidence_score)
            ).scalar() or 0.0
            
            return {
                "total_learning_categories": total_categories,
                "total_learning_items": total_items,
                "total_cross_references": total_cross_refs,
                "average_confidence_score": float(avg_confidence),
                "top_level_1_categories": [
                    {"category": cat, "usage_count": int(usage)} 
                    for cat, usage in top_level_1
                ]
            }
            
        except Exception as e:
            logger.error(f"Error getting learning category stats: {str(e)}")
            return {
                "error": f"Failed to get stats: {str(e)}"
            }
        finally:
            db.close()
    
    def _extract_keywords(self, text: str) -> Dict[str, Any]:
        """
        Extract normalized keywords from text for similarity matching.
        
        Args:
            text: Text to extract keywords from
            
        Returns:
            Dict with extracted keywords and metadata
        """
        # Simple keyword extraction - can be enhanced with NLP
        import re
        
        # Clean and normalize text
        clean_text = re.sub(r'[^\w\s]', ' ', text.lower())
        words = [word.strip() for word in clean_text.split() if len(word.strip()) > 2]
        
        # Filter out common stop words
        stop_words = {
            'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had', 
            'her', 'was', 'one', 'our', 'out', 'day', 'get', 'has', 'him', 'his', 
            'how', 'man', 'new', 'now', 'old', 'see', 'two', 'way', 'who', 'boy', 
            'did', 'its', 'let', 'put', 'say', 'she', 'too', 'use'
        }
        
        keywords = [word for word in words if word not in stop_words]
        
        return {
            "keywords": keywords[:10],  # Limit to top 10 keywords
            "word_count": len(words),
            "unique_count": len(set(keywords))
        }
    
    def _calculate_keyword_similarity(self, keywords1: List[str], keywords2: List[str]) -> float:
        """
        Calculate similarity between two keyword lists.
        
        Args:
            keywords1: First keyword list
            keywords2: Second keyword list
            
        Returns:
            Similarity score between 0.0 and 1.0
        """
        if not keywords1 or not keywords2:
            return 0.0
        
        set1 = set(keywords1)
        set2 = set(keywords2)
        
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        
        return intersection / union if union > 0 else 0.0
    
    def _calculate_category_similarity(self, learning_path: str, client_category: str) -> float:
        """
        Calculate similarity between learning category path and client category.
        
        Args:
            learning_path: Learning category path (level1 > level2 > level3)
            client_category: Client category name
            
        Returns:
            Similarity score between 0.0 and 1.0
        """
        # Simple text similarity - can be enhanced with semantic similarity
        learning_words = set(learning_path.lower().replace('>', ' ').split())
        client_words = set(client_category.lower().split())
        
        intersection = len(learning_words.intersection(client_words))
        union = len(learning_words.union(client_words))
        
        return intersection / union if union > 0 else 0.0
    
    def _determine_mapping_confidence(self, ai_confidence: float) -> str:
        """
        Determine mapping confidence level based on AI confidence score.
        
        Args:
            ai_confidence: AI confidence score (0.0 to 1.0)
            
        Returns:
            Confidence level: 'high', 'medium', or 'low'
        """
        if ai_confidence >= 0.8:
            return 'high'
        elif ai_confidence >= 0.6:
            return 'medium'
        else:
            return 'low'
    
    def _log_learning_categorization(
        self, 
        item_description: str,
        learning_category_id: str,
        client_category: str,
        confidence_score: float,
        user_id: str = None,
        session_id: str = None
    ):
        """
        Log learning categorization attempt for monitoring.
        
        Args:
            item_description: Item description that was categorized
            learning_category_id: ID of the created learning category
            client_category: Original client category
            confidence_score: AI confidence score
            user_id: User ID for audit
            session_id: Session ID for audit
        """
        db = get_db_session()
        try:
            log_entry = AutoCategorizationLog(
                session_id=session_id,
                user_id=user_id or "learning_system",
                input_description=item_description,
                predicted_category=f"learning_category_{learning_category_id}",
                confidence_score=confidence_score,
                method_used="learning_3_level",
                processing_time_ms=0  # Will be updated by caller if needed
            )
            db.add(log_entry)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to log learning categorization: {e}")
        finally:
            db.close()