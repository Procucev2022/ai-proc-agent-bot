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

from ..config import get_settings
from ..database import get_db_session
from ..models import AutoCategorizationLog
from .auto_categorization_service import AutoCategorizationService
from ..database import execute_remote_query
from .openai_service import OpenAIService

logger = logging.getLogger(__name__)


class EnhancedAutoCategorizationService:
    """
    Enhanced auto-categorization using unified 3-level taxonomy vector store.

    Provides fast categorization with fallback to existing service when needed.
    Uses ChromaDB server mode (HttpClient) for multi-worker deployments.
    """

    def __init__(self):
        """Initialize the enhanced auto-categorization service with ChromaDB server."""
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
            logger.info(f"EnhancedAutoCategorizationService connected to ChromaDB server at {settings.chroma_host}:{settings.chroma_port}")
        except Exception as e:
            raise RuntimeError(
                f"ChromaDB server not available at {settings.chroma_host}:{settings.chroma_port}. "
                f"Start the server with: chroma run --host 0.0.0.0 --port {settings.chroma_port} --path ./chroma_db"
            ) from e

        # Get or create collections
        self.collection = self.chroma_client.get_or_create_collection(
            name="learning_taxonomy",
            embedding_function=self.embedding_function
        )

        # Category names collection for hybrid search
        self.category_collection = self.chroma_client.get_or_create_collection(
            name="category_names",
            embedding_function=self.embedding_function
        )

        # Initialize fallback service
        self.fallback_service = AutoCategorizationService()

        # Initialize OpenAI service
        self.openai_service = OpenAIService()

    def _build_enhanced_description(self, item_description: str, hierarchy: Dict[str, Any]) -> str:
        """
        Build enhanced description for fallback search with deduplication.

        Combines item description with hierarchy levels (L3, L2) for better
        similarity matching, avoiding duplicate terms.

        Args:
            item_description: Original item description (e.g., "Batteries")
            hierarchy: Dict with level_1_category, level_2_category, level_3_category

        Returns:
            Enhanced description string (e.g., "Batteries Power Systems")
        """
        # Start with the original item description
        parts = [item_description.strip()]
        item_lower = item_description.lower().strip()

        # Use "or ''" to handle cases where key exists but value is None
        l3 = (hierarchy.get("level_3_category") or "").strip()
        if l3 and l3.lower() != item_lower:
            parts.append(l3)

        l2 = (hierarchy.get("level_2_category") or "").strip()
        if l2 and l2.lower() != item_lower and l2.lower() != l3.lower():
            parts.append(l2)
        # Join all parts with spaces and return
        return " ".join(parts)

    def _keyword_lookup_source_of_truth(self, item_description: str, top_k: int = 5) -> Dict[str, Any]:
        """
        Keyword search against the item_category source of truth table.

        Complements vector similarity by finding exact/partial name matches
        that embedding-based search may miss (e.g. "Caustic Soda" vs "Carbonated Drink").

        Args:
            item_description: Item description to search for
            top_k: Max results to return

        Returns:
            Dict with matched categories and consensus info
        """
        try:
            from collections import Counter

            search_term = item_description.strip()
            category_votes = Counter()

            # 1. Search by item name — try full term, then progressively shorter phrases
            words = search_term.split()
            item_results = []
            for window_size in range(len(words), 0, -1):
                for start in range(len(words) - window_size + 1):
                    phrase = " ".join(words[start:start + window_size])
                    if len(phrase) < 3:
                        continue
                    phrase_items = execute_remote_query(
                        """
                        SELECT item, category, COUNT(*) as freq
                        FROM item_category
                        WHERE LOWER(item) LIKE LOWER(:q)
                        AND category IS NOT NULL AND category != ''
                        GROUP BY item, category
                        ORDER BY freq DESC
                        LIMIT :limit
                        """,
                        {"q": f"%{phrase}%", "limit": top_k}
                    )
                    if phrase_items:
                        item_results.extend(phrase_items)
                if item_results:
                    break  # found matches at this window size

            for r in item_results:
                category_votes[r["category"]] += r.get("freq", 1) * 2  # double weight for item match

            # 2. Search by category name — try full term first, then individual words
            category_results = execute_remote_query(
                """
                SELECT category, COUNT(*) as freq
                FROM item_category
                WHERE LOWER(category) LIKE LOWER(:q)
                AND category IS NOT NULL AND category != ''
                GROUP BY category
                ORDER BY freq DESC
                LIMIT :limit
                """,
                {"q": f"%{search_term}%", "limit": top_k}
            )

            if not category_results:
                # Try sliding window phrases against category names
                # Multi-word phrases use LIKE (substring match)
                # Single words use exact category name match only to avoid noise
                #   e.g. "Coil" matches "Coils" but "Wall" won't match "PUF Wall Partition"
                words = search_term.split()

                for window_size in range(len(words), 0, -1):
                    for start in range(len(words) - window_size + 1):
                        phrase = " ".join(words[start:start + window_size])
                        if len(phrase) < 3:
                            continue

                        if window_size >= 2:
                            # Multi-word: substring match is safe
                            phrase_results = execute_remote_query(
                                """
                                SELECT category, COUNT(*) as freq
                                FROM item_category
                                WHERE LOWER(category) LIKE LOWER(:q)
                                AND category IS NOT NULL AND category != ''
                                GROUP BY category
                                ORDER BY freq DESC
                                LIMIT :limit
                                """,
                                {"q": f"%{phrase}%", "limit": top_k}
                            )
                        else:
                            # Single word: only match if the word IS the category name
                            # (case-insensitive, allow plural: "Coil" matches "Coils")
                            phrase_results = execute_remote_query(
                                """
                                SELECT category, COUNT(*) as freq
                                FROM item_category
                                WHERE (LOWER(category) = LOWER(:exact)
                                   OR LOWER(category) = LOWER(:plural))
                                AND category IS NOT NULL AND category != ''
                                GROUP BY category
                                ORDER BY freq DESC
                                LIMIT :limit
                                """,
                                {"exact": phrase, "plural": phrase + "s", "limit": top_k}
                            )

                        if phrase_results:
                            category_results.extend(phrase_results)
                    # Try ALL windows at this size before moving to smaller
                    if category_results:
                        break  # found matches at this window size, stop going smaller

            for r in category_results:
                category_votes[r["category"]] += r.get("freq", 1)

            if not category_votes:
                return {"success": False, "reason": "No keyword matches"}

            best_category, best_count = category_votes.most_common(1)[0]
            total = sum(category_votes.values())
            match_source = "item+category" if item_results and category_results else (
                "item" if item_results else "category"
            )

            logger.info(
                f"Keyword lookup for '{search_term}': found '{best_category}' "
                f"({best_count}/{total} votes, source={match_source})"
            )

            return {
                "success": True,
                "category": best_category,
                "consensus": best_count / total if total > 0 else 0,
                "total_matches": total,
                "match_source": match_source,
                "all_categories": dict(category_votes),
                "matches": item_results or category_results,
            }

        except Exception as e:
            logger.warning(f"Keyword lookup failed: {e}")
            return {"success": False, "reason": str(e)}

    def _cross_validate_with_fallback(
        self,
        item_description: str,
        learning_category: str,
        learning_similarity: float
    ) -> Dict[str, Any]:
        """
        Cross-validate learning taxonomy result with fallback service.

        When learning_taxonomy returns a medium-confidence match, verify it against
        the fallback service (category_items collection) which has complete coverage.

        Args:
            item_description: Original item description
            learning_category: Category predicted by learning_taxonomy
            learning_similarity: Similarity score from learning_taxonomy

        Returns:
            Dict with validation result and recommended category
        """
        try:
            # Step 1: Keyword lookup against source of truth (exact/partial name match)
            keyword_result = self._keyword_lookup_source_of_truth(item_description)
            if keyword_result.get("success"):
                keyword_category = keyword_result["category"]
                keyword_consensus = keyword_result.get("consensus", 0)

                if keyword_category.lower() == learning_category.lower():
                    return {
                        "validated": True,
                        "use_learning": True,
                        "reason": f"Keyword lookup confirms learning category",
                        "fallback_category": keyword_category,
                        "fallback_similarity": 1.0,
                    }
                elif keyword_consensus >= 0.3:
                    # Keyword match disagrees — keyword matches are high quality, trust with lower consensus
                    logger.info(
                        f"Keyword lookup override: '{keyword_category}' "
                        f"(consensus={keyword_consensus:.2f}) over learning '{learning_category}'"
                    )
                    return {
                        "validated": False,
                        "use_learning": False,
                        "reason": f"Keyword lookup: {keyword_category} (consensus={keyword_consensus:.2f})",
                        "fallback_category": keyword_category,
                        "fallback_similarity": 1.0,
                        "recommended_category": keyword_category,
                    }

            # Step 2: Vector similarity fallback (when keyword search finds nothing)
            TOP_K = 5
            fallback_results = self.fallback_service.collection.query(
                query_texts=[item_description],
                n_results=TOP_K,
                include=['metadatas', 'distances']
            )

            if not fallback_results['metadatas'][0]:
                return {
                    "validated": True,
                    "use_learning": True,
                    "reason": "No fallback results available"
                }

            # Majority voting: count categories across top-k results, weighted by similarity
            from collections import Counter
            category_votes = Counter()
            best_fallback_similarity = 0.0

            for meta, dist in zip(fallback_results['metadatas'][0], fallback_results['distances'][0]):
                cat = meta.get("category", "")
                if not cat:
                    continue
                sim = max(0.0, min(1.0, 1.0 - (dist / 2.0)))
                category_votes[cat] += sim  # weight votes by similarity
                if sim > best_fallback_similarity:
                    best_fallback_similarity = sim

            if not category_votes:
                return {
                    "validated": True,
                    "use_learning": True,
                    "reason": "No fallback categories found"
                }

            # Get the top voted category from fallback
            fallback_category, fallback_weight = category_votes.most_common(1)[0]
            fallback_similarity = best_fallback_similarity

            # Compare categories (case-insensitive)
            categories_match = learning_category.lower() == fallback_category.lower()

            if categories_match:
                return {
                    "validated": True,
                    "use_learning": True,
                    "reason": "Both services agree",
                    "fallback_category": fallback_category,
                    "fallback_similarity": fallback_similarity
                }
            else:
                # Categories disagree — only prefer fallback if it has BOTH:
                # 1. Higher similarity than learning
                # 2. Strong consensus (majority of top-k votes)
                total_weight = sum(category_votes.values())
                fallback_consensus = fallback_weight / total_weight if total_weight > 0 else 0

                prefer_fallback = (
                    fallback_similarity > learning_similarity and
                    fallback_consensus >= 0.5
                )

                logger.warning(
                    f"Cross-validation disagreement: learning='{learning_category}' "
                    f"(sim={learning_similarity:.3f}), fallback='{fallback_category}' "
                    f"(sim={fallback_similarity:.3f}, consensus={fallback_consensus:.2f}). "
                    f"Prefer fallback: {prefer_fallback}"
                )

                return {
                    "validated": False,
                    "use_learning": not prefer_fallback,
                    "reason": f"Disagreement: learning={learning_category}, fallback={fallback_category}",
                    "fallback_category": fallback_category,
                    "fallback_similarity": fallback_similarity,
                    "fallback_consensus": fallback_consensus,
                    "recommended_category": fallback_category if prefer_fallback else learning_category
                }

        except Exception as e:
            logger.warning(f"Cross-validation failed: {e}")
            return {
                "validated": True,
                "use_learning": True,
                "reason": f"Cross-validation error: {str(e)}"
            }

    def _search_by_category_name(
        self,
        item_description: str,
        top_k: int = 5
    ) -> Dict[str, Any]:
        """
        Search for matching categories by comparing item description directly to category names.

        This provides a complementary signal to item-based search:
        - Item-based: "What existing items are similar to this description?"
        - Category-based: "What category names are semantically similar to this description?"

        Args:
            item_description: Description to search for
            top_k: Number of category results to return

        Returns:
            Dict with category matches and similarity scores
        """
        try:
            # Check if category collection has data
            category_count = self.category_collection.count()
            if category_count == 0:
                logger.warning("Category names collection is empty - hybrid search unavailable")
                return {"success": False, "reason": "Category collection empty", "matches": []}

            # Query category names directly
            results = self.category_collection.query(
                query_texts=[item_description],
                n_results=top_k,
                include=['documents', 'metadatas', 'distances']
            )

            if not results['documents'][0]:
                return {"success": False, "reason": "No category matches found", "matches": []}

            matches = []
            for doc, meta, dist in zip(
                results['documents'][0],
                results['metadatas'][0],
                results['distances'][0]
            ):
                similarity = max(0.0, min(1.0, 1.0 - (dist / 2.0)))
                matches.append({
                    "category_name": doc,
                    "similarity": similarity,
                    "item_count": meta.get("item_count", 0)
                })

            logger.info(f"Category-based search found {len(matches)} matches, "
                       f"best: '{matches[0]['category_name']}' (sim={matches[0]['similarity']:.3f})")

            return {
                "success": True,
                "matches": matches,
                "best_match": matches[0] if matches else None
            }

        except Exception as e:
            logger.warning(f"Category-based search failed: {e}")
            return {"success": False, "reason": str(e), "matches": []}

    def _hybrid_category_selection(
        self,
        item_based_category: str,
        item_based_similarity: float,
        category_based_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Combine item-based and category-based search results for final category selection.

        Strategy:
        1. If both agree on top category -> high confidence
        2. If item-based has high similarity (>0.85) -> trust item-based
        3. If category-based has significantly higher similarity -> consider override
        4. Use voting/weighting for borderline cases

        Args:
            item_based_category: Category from item-based search
            item_based_similarity: Similarity score from item-based search
            category_based_results: Results from category-based search

        Returns:
            Dict with final category selection and reasoning
        """
        result = {
            "final_category": item_based_category,
            "confidence_boost": 0.0,
            "method": "item_based_only",
            "agreement": False,
            "reasoning": ""
        }

        if not category_based_results.get("success") or not category_based_results.get("matches"):
            result["reasoning"] = "Category-based search unavailable, using item-based result"
            return result

        category_matches = category_based_results["matches"]
        best_category_match = category_matches[0]
        category_based_name = best_category_match["category_name"]
        category_based_similarity = best_category_match["similarity"]

        # Check if categories agree (case-insensitive)
        categories_agree = item_based_category.lower() == category_based_name.lower()

        if categories_agree:
            # Both methods agree - boost confidence
            result["agreement"] = True
            result["confidence_boost"] = 0.1  # Add 10% confidence
            result["method"] = "hybrid_agreement"
            result["reasoning"] = f"Both item-based and category-based search agree on '{item_based_category}'"
            logger.info(f"Hybrid agreement: both methods selected '{item_based_category}'")
            return result

        # Categories disagree - analyze which to trust
        ITEM_TRUST_THRESHOLD = 0.85
        CATEGORY_OVERRIDE_THRESHOLD = 0.6

        if item_based_similarity >= ITEM_TRUST_THRESHOLD:
            # High item-based similarity - trust it
            result["method"] = "hybrid_item_trusted"
            result["reasoning"] = (
                f"Item-based similarity ({item_based_similarity:.3f}) >= {ITEM_TRUST_THRESHOLD}, "
                f"trusting '{item_based_category}' over category-based '{category_based_name}'"
            )
            return result

        # Check if category-based match is strong enough to consider
        if category_based_similarity >= CATEGORY_OVERRIDE_THRESHOLD:
            # Category-based has reasonable match - check if it's in top item-based results
            # For now, we'll log the disagreement but still use item-based
            # (This could be enhanced to do more sophisticated voting)

            # Check if category-based best match appears in top 3 item results
            category_in_item_results = any(
                m["category_name"].lower() == item_based_category.lower()
                for m in category_matches[:3]
            )

            if not category_in_item_results and category_based_similarity > item_based_similarity:
                # Category-based has higher similarity and item-based category not in top category matches
                # This might indicate item-based matched a wrong item
                result["final_category"] = category_based_name
                result["method"] = "hybrid_category_override"
                result["reasoning"] = (
                    f"Category-based '{category_based_name}' (sim={category_based_similarity:.3f}) "
                    f"overrode item-based '{item_based_category}' (sim={item_based_similarity:.3f})"
                )
                logger.info(f"Hybrid override: category-based '{category_based_name}' over item-based '{item_based_category}'")
                return result

        result["method"] = "hybrid_item_preferred"
        result["reasoning"] = (
            f"Item-based '{item_based_category}' (sim={item_based_similarity:.3f}) preferred over "
            f"category-based '{category_based_name}' (sim={category_based_similarity:.3f})"
        )
        return result

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
        Categorize item using a 3-step pipeline:

        1. Keyword lookup (SQL LIKE on item_category) — always runs first
        2. Taxonomy lookup (learning_taxonomy vector search) — if found,
           combine with keyword results and send to OpenAI for final pick
        3. Fallback (category_items vector search) — if taxonomy misses,
           combine with keyword results and send to OpenAI

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
            # ── Step 1: Keyword lookup (always runs) ───────────────────────
            keyword_result = self._keyword_lookup_source_of_truth(item_description, top_k=5)
            keyword_matches = []
            if keyword_result.get("success"):
                # Convert keyword results to OpenAI-compatible format
                # All keyword categories are equal candidates — use uniform score
                # so OpenAI judges purely on semantic fit, not source of truth volume
                for cat in keyword_result.get("all_categories", {}):
                    keyword_matches.append({
                        "item": f"[keyword match: '{item_description}' found in category '{cat}']",
                        "category": cat,
                        "similarity_score": 0.95,
                        "matched_level": "keyword",
                    })
                logger.info(f"Keyword lookup: {keyword_result['category']} (consensus={keyword_result.get('consensus', 0):.2f})")

            # ── Step 2: Taxonomy lookup (learning_taxonomy) ────────────────
            hierarchical_result = self._search_hierarchical_levels(
                item_description, similarity_threshold, top_k
            )

            if hierarchical_result["success"]:
                # Build top matches from taxonomy
                taxonomy_matches = []
                for match_info in hierarchical_result.get("all_level_matches", []):
                    meta = match_info["metadata"]
                    taxonomy_matches.append({
                        "item": meta.get("item_description", ""),
                        "category": meta.get("client_category_name", ""),
                        "similarity_score": round(match_info["similarity_score"], 4),
                        "matched_level": match_info["matched_level"],
                    })

                # Combine keyword + taxonomy for OpenAI
                all_matches = keyword_matches + taxonomy_matches
                all_matches = [m for m in all_matches if m["category"] and m["category"] != "Other"]

                if not all_matches:
                    # No usable candidates — skip to fallback
                    logger.info("Taxonomy matched but no valid categories, falling through to fallback")
                else:
                    available_categories = list(set(m["category"] for m in all_matches))

                    # HIGH SIMILARITY SHORTCUT: skip LLM if taxonomy match is very strong
                    best_sim = hierarchical_result["similarity_score"]
                    best_cat = hierarchical_result["best_match"].get("client_category_name", "")
                    if best_sim >= 0.90 and best_cat and best_cat != "Other":
                        processing_time = int((time.time() - start_time) * 1000)
                        logger.info(f"High similarity ({best_sim:.3f}), using '{best_cat}' directly")

                        self._log_categorization(
                            item_description, user_id, session_id, rfq_id,
                            best_cat, 0.95, best_sim,
                            "enhanced_taxonomy_high_similarity", processing_time, None
                        )
                        return {
                            "success": True,
                            "method": "enhanced_taxonomy_high_similarity",
                            "client_category": best_cat,
                            "confidence_score": 0.95,
                            "similarity_score": best_sim,
                            "processing_time_ms": processing_time,
                            "reasoning": f"High similarity match ({best_sim:.3f}), LLM call skipped",
                            "all_matches": all_matches[:5],
                        }

                    # Send to OpenAI with combined keyword + taxonomy results
                    openai_result = await self.openai_service.categorize_with_similar_items(
                        item_description, all_matches[:5], available_categories
                    )
                    processing_time = int((time.time() - start_time) * 1000)

                    if openai_result.get("success") and openai_result.get("category") != "Other":
                        selected = openai_result["category"]
                        confidence = openai_result.get("confidence", 0.8)

                        # Update taxonomy if LLM picked a different category
                        if selected.lower() != best_cat.lower():
                            try:
                                await self._update_learning_taxonomy(
                                    item_description=item_description,
                                    client_category=selected,
                                    user_id=user_id,
                                )
                                logger.info(f"Updated taxonomy: '{best_cat}' -> '{selected}' for '{item_description[:50]}'")
                            except Exception as e:
                                logger.warning(f"Taxonomy update failed: {e}")

                        self._log_categorization(
                            item_description, user_id, session_id, rfq_id,
                            selected, confidence, best_sim,
                            "enhanced_taxonomy_openai", processing_time, None
                        )
                        return {
                            "success": True,
                            "method": "enhanced_taxonomy_openai",
                            "client_category": selected,
                            "confidence_score": confidence,
                            "similarity_score": best_sim,
                            "processing_time_ms": processing_time,
                            "reasoning": openai_result.get("reasoning", ""),
                            "openai_reasoning": openai_result.get("reasoning", ""),
                            "all_matches": all_matches[:5],
                        }

            # ── Step 3: Fallback (category_items via AutoCategorizationService) ──
            logger.info("Taxonomy miss — using fallback auto-categorization service")
            fallback_matches = self.fallback_service._get_similar_items(item_description)

            # Combine keyword + fallback results
            all_matches = keyword_matches + [
                {**m, "matched_level": "fallback"} for m in fallback_matches
            ]
            all_matches = [m for m in all_matches if m["category"] and m["category"] != "Other"]

            if all_matches:
                available_categories = list(set(m["category"] for m in all_matches))

                openai_result = await self.openai_service.categorize_with_similar_items(
                    item_description, all_matches[:5], available_categories
                )
                processing_time = int((time.time() - start_time) * 1000)

                if openai_result.get("success"):
                    selected = openai_result["category"]
                    confidence = openai_result.get("confidence", 0.7)
                    best_sim = fallback_matches[0]["similarity_score"] if fallback_matches else None

                    # Update learning taxonomy for future hits
                    if selected != "Other":
                        try:
                            await self._update_learning_taxonomy(
                                item_description=item_description,
                                client_category=selected,
                                user_id=user_id,
                            )
                        except Exception as e:
                            logger.warning(f"Learning taxonomy update failed: {e}")

                    self._log_fallback_categorization(
                        item_description, user_id, session_id, rfq_id,
                        selected, confidence,
                        "enhanced_fallback_openai", processing_time,
                        "Taxonomy miss, used keyword + fallback vector search"
                    )
                    return {
                        "success": True,
                        "method": "enhanced_fallback_openai",
                        "client_category": selected,
                        "confidence_score": confidence,
                        "similarity_score": best_sim,
                        "processing_time_ms": processing_time,
                        "reasoning": openai_result.get("reasoning", ""),
                        "openai_reasoning": openai_result.get("reasoning", ""),
                        "all_matches": all_matches[:5],
                        "learning_updated": selected != "Other",
                    }

            # ── Nothing worked ─────────────────────────────────────────────
            processing_time = int((time.time() - start_time) * 1000)
            self._log_fallback_categorization(
                item_description, user_id, session_id, rfq_id,
                "Other", 0.3, "enhanced_no_match", processing_time,
                "No matches from keyword, taxonomy, or fallback"
            )
            return {
                "success": True,
                "method": "enhanced_no_match",
                "client_category": "Other",
                "confidence_score": 0.3,
                "similarity_score": None,
                "processing_time_ms": processing_time,
                "reasoning": "No matches found from any source",
                "requires_review": True,
            }

        except Exception as e:
            processing_time = int((time.time() - start_time) * 1000)
            logger.error(f"Error in enhanced auto-categorization: {e}")
            return {
                "success": False,
                "method": "enhanced_error",
                "error": str(e),
                "processing_time_ms": processing_time,
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