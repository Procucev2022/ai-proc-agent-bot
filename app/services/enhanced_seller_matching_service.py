"""
Enhanced Seller Matching Service using unified vector store.

This service provides seller matching using the unified 3-level learning taxonomy
vector store. It searches for seller mappings based on item descriptions and
returns relevant sellers with their category matches and location information.

Key features:
- Search unified vector store for seller mappings
- Location-based filtering and distance calculation
- Ranking-based prioritization (Diamond > Platinum > Gold > Titanium)
- Comprehensive seller information with contact details
- Fallback to existing seller matching when needed
"""

import logging
import time
import json
from typing import Dict, Any, List, Optional
from math import radians, cos, sin, asin, sqrt

import chromadb
import chromadb.utils.embedding_functions as embedding_functions

from ..config import get_settings
from ..database import get_db_session
from ..models import Seller
from .openai_service import OpenAIService

logger = logging.getLogger(__name__)


class EnhancedSellerMatchingService:
    """
    Enhanced seller matching using unified 3-level taxonomy vector store.

    Provides fast seller discovery based on category matching and location.
    Uses ChromaDB server mode (HttpClient) for multi-worker deployments.
    """

    def __init__(self):
        """Initialize the enhanced seller matching service with ChromaDB server."""
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
            logger.info(f"EnhancedSellerMatchingService connected to ChromaDB server at {settings.chroma_host}:{settings.chroma_port}")
        except Exception as e:
            raise RuntimeError(
                f"ChromaDB server not available at {settings.chroma_host}:{settings.chroma_port}. "
                f"Start the server with: chroma run --host 0.0.0.0 --port {settings.chroma_port} --path ./chroma_db"
            ) from e

        # Get or create collection
        self.collection = self.chroma_client.get_or_create_collection(
            name="learning_taxonomy",
            embedding_function=self.embedding_function
        )

        # Initialize OpenAI service
        self.openai_service = OpenAIService()
    
    async def find_sellers_for_item(
        self,
        item_description: str,
        delivery_location: Optional[Dict[str, Any]] = None,
        max_distance_km: int = 200,
        max_sellers: int = 10,
        similarity_threshold: float = 0.0,
        ranking_priority: bool = True
    ) -> Dict[str, Any]:
        """
        Find sellers for an item using category-based vector search.
        
        Args:
            item_description: Description of item needing sellers
            delivery_location: Delivery location {lat, lng, city, state}
            max_distance_km: Maximum distance for seller search
            max_sellers: Maximum number of sellers to return
            similarity_threshold: Minimum similarity score to accept
            ranking_priority: Whether to prioritize by seller ranking
            
        Returns:
            Dict with matched sellers and search details
        """
        start_time = time.time()
        
        try:
            # Search unified vector store for seller mappings
            results = self.collection.query(
                query_texts=[item_description],
                n_results=max_sellers * 3,  # Get more to filter by location/distance
                include=['documents', 'metadatas', 'distances'],
                where={"type": "seller_mapping"}  # Only search seller mappings
            )
            
            if not results['documents'][0]:
                processing_time = int((time.time() - start_time) * 1000)
                return {
                    "success": False,
                    "error": "No sellers found for this item category",
                    "sellers": [],
                    "search_details": {
                        "item_description": item_description,
                        "processing_time_ms": processing_time,
                        "method": "enhanced_vector_seller_search"
                    }
                }
            
            logger.info(f"Found {len(results['documents'][0])} potential seller matches")
            
            # Process and filter sellers
            matched_sellers = []
            seen_sellers = set()  # Avoid duplicate sellers
            
            for i, metadata in enumerate(results['metadatas'][0]):
                try:
                    # Clamp similarity score to reasonable range (0.0 to 1.0)
                    raw_distance = results['distances'][0][i]
                    similarity_score = max(0.0, min(1.0, 1.0 - raw_distance))
                    
                    # Skip low similarity matches
                    if similarity_score < similarity_threshold:
                        continue
                    
                    seller_id = metadata["seller_id"]
                    
                    # Skip duplicate sellers (same seller might have multiple category mappings)
                    if seller_id in seen_sellers:
                        continue
                    seen_sellers.add(seller_id)
                    
                    # Parse seller location
                    seller_location = {}
                    if metadata.get("location"):
                        try:
                            seller_location = json.loads(metadata["location"]) if isinstance(metadata["location"], str) else metadata["location"]
                        except:
                            seller_location = {}
                    
                    # Calculate distance if delivery location provided
                    distance_km = None
                    within_coverage = True
                    
                    if delivery_location and seller_location.get("lat") and seller_location.get("lng"):
                        distance_km = self._calculate_distance(
                            delivery_location.get("lat", 0), 
                            delivery_location.get("lng", 0),
                            seller_location["lat"], 
                            seller_location["lng"]
                        )
                        
                        # Check if within max distance
                        if distance_km > max_distance_km:
                            within_coverage = False
                            continue  # Skip sellers outside max distance
                    
                    # Add seller to results
                    seller_info = {
                        "seller_id": seller_id,
                        "seller_name": metadata["seller_name"],
                        "phone_number": metadata["phone_number"],
                        "email": metadata["email"] or None,
                        "category_match": {
                            "original_category": metadata["original_category"],
                            "learning_category": {
                                "level_1": metadata["level_1_category"],
                                "level_2": metadata["level_2_category"],
                                "level_3": metadata["level_3_category"],
                                "category_path": metadata["category_path"]
                            },
                            "similarity_score": similarity_score,
                            "confidence_score": metadata["confidence_score"]
                        },
                        "location": seller_location,
                        "distance_km": distance_km,
                        "ranking": metadata["ranking"],
                        "within_coverage": within_coverage
                    }
                    
                    matched_sellers.append(seller_info)
                    
                except Exception as e:
                    logger.error(f"Error processing seller match {i}: {str(e)}")
                    continue
            
            # Sort sellers by priority for initial filtering
            if ranking_priority:
                # Sort by ranking priority first, then similarity
                matched_sellers.sort(
                    key=lambda x: (
                        self._ranking_priority(x["ranking"]),
                        x["category_match"]["similarity_score"],
                        -x["distance_km"] if x["distance_km"] else 0
                    ), 
                    reverse=True
                )
            else:
                # Sort by similarity and distance
                matched_sellers.sort(
                    key=lambda x: (
                        x["category_match"]["similarity_score"],
                        -x["distance_km"] if x["distance_km"] else 0
                    ), 
                    reverse=True
                )
            
            # Use OpenAI for intelligent seller selection if we have enough candidates
            if len(matched_sellers) > 1:
                logger.info(f"Using OpenAI to select best sellers from {len(matched_sellers)} candidates")
                
                # Pass top candidates to OpenAI for intelligent selection
                candidates_for_ai = matched_sellers[:max_sellers * 2]  # Give AI more options to choose from
                
                openai_result = await self.openai_service.select_best_sellers(
                    item_description=item_description,
                    candidate_sellers=candidates_for_ai,
                    max_sellers=max_sellers
                )
                
                if openai_result.get("success"):
                    # Use OpenAI-selected sellers
                    selected_sellers = openai_result["selected_sellers"]
                    logger.info(f"OpenAI selected {len(selected_sellers)} sellers with confidence {openai_result.get('confidence_score', 0):.3f}")
                    
                    processing_time = int((time.time() - start_time) * 1000)
                    
                    return {
                        "success": True,
                        "method": "enhanced_vector_openai_seller_search",
                        "sellers": selected_sellers,
                        "search_details": {
                            "item_description": item_description,
                            "total_found": len(selected_sellers),
                            "similarity_threshold": similarity_threshold,
                            "max_distance_km": max_distance_km,
                            "delivery_location": delivery_location,
                            "processing_time_ms": processing_time,
                            "ranking_priority": ranking_priority,
                            "openai_reasoning": openai_result.get("reasoning", ""),
                            "openai_confidence": openai_result.get("confidence_score", 0.8),
                            "total_candidates_considered": len(candidates_for_ai)
                        }
                    }
                else:
                    logger.warning(f"OpenAI seller selection failed: {openai_result.get('error', 'Unknown error')}, falling back to vector sorting")
            
            # Fallback to original sorting if OpenAI fails or insufficient candidates
            matched_sellers = matched_sellers[:max_sellers]
            
            processing_time = int((time.time() - start_time) * 1000)
            
            logger.info(f"Returning {len(matched_sellers)} matched sellers")
            
            return {
                "success": True,
                "method": "enhanced_vector_seller_search_fallback",
                "sellers": matched_sellers,
                "search_details": {
                    "item_description": item_description,
                    "total_found": len(matched_sellers),
                    "similarity_threshold": similarity_threshold,
                    "max_distance_km": max_distance_km,
                    "delivery_location": delivery_location,
                    "processing_time_ms": processing_time,
                    "ranking_priority": ranking_priority,
                    "fallback_reason": "OpenAI selection failed or insufficient candidates"
                }
            }
            
        except Exception as e:
            processing_time = int((time.time() - start_time) * 1000)
            logger.error(f"Error in enhanced seller matching: {str(e)}")
            
            return {
                "success": False,
                "method": "enhanced_vector_seller_search",
                "error": str(e),
                "sellers": [],
                "search_details": {
                    "processing_time_ms": processing_time
                }
            }
    
    def find_sellers_by_category_path(
        self, 
        category_path: str,
        delivery_location: Optional[Dict[str, Any]] = None,
        max_distance_km: int = 200,
        max_sellers: int = 10
    ) -> Dict[str, Any]:
        """
        Find sellers by exact category path match.
        
        Args:
            category_path: Exact category path (e.g., "Electronics > Computers > Laptops")
            delivery_location: Delivery location for distance filtering
            max_distance_km: Maximum distance for seller search
            max_sellers: Maximum number of sellers to return
            
        Returns:
            Dict with matched sellers
        """
        start_time = time.time()
        
        try:
            # Search for exact category path matches
            results = self.collection.query(
                query_texts=[category_path],
                n_results=max_sellers * 2,
                include=['documents', 'metadatas', 'distances'],
                where={
                    "$and": [
                        {"type": "seller_mapping"},
                        {"category_path": category_path}
                    ]
                }
            )
            
            if not results['documents'][0]:
                return {
                    "success": False,
                    "error": f"No sellers found for category path: {category_path}",
                    "sellers": [],
                    "processing_time_ms": int((time.time() - start_time) * 1000)
                }
            
            # Process sellers (similar to find_sellers_for_item but simpler)
            matched_sellers = []
            seen_sellers = set()
            
            for i, metadata in enumerate(results['metadatas'][0]):
                seller_id = metadata["seller_id"]
                
                if seller_id in seen_sellers:
                    continue
                seen_sellers.add(seller_id)
                
                # Parse location and calculate distance (same logic as above)
                seller_location = {}
                if metadata.get("location"):
                    try:
                        seller_location = json.loads(metadata["location"]) if isinstance(metadata["location"], str) else metadata["location"]
                    except:
                        seller_location = {}
                
                distance_km = None
                if delivery_location and seller_location.get("lat") and seller_location.get("lng"):
                    distance_km = self._calculate_distance(
                        delivery_location.get("lat", 0), 
                        delivery_location.get("lng", 0),
                        seller_location["lat"], 
                        seller_location["lng"]
                    )
                    
                    if distance_km > max_distance_km:
                        continue
                
                matched_sellers.append({
                    "seller_id": seller_id,
                    "seller_name": metadata["seller_name"],
                    "phone_number": metadata["phone_number"],
                    "email": metadata["email"] or None,
                    "original_category": metadata["original_category"],
                    "category_path": metadata["category_path"],
                    "location": seller_location,
                    "distance_km": distance_km,
                    "ranking": metadata["ranking"],
                    "confidence_score": metadata["confidence_score"]
                })
            
            # Sort by ranking and distance
            matched_sellers.sort(
                key=lambda x: (
                    self._ranking_priority(x["ranking"]),
                    -x["distance_km"] if x["distance_km"] else 0
                ), 
                reverse=True
            )
            
            matched_sellers = matched_sellers[:max_sellers]
            
            return {
                "success": True,
                "method": "enhanced_vector_category_exact_match",
                "sellers": matched_sellers,
                "category_path": category_path,
                "total_found": len(matched_sellers),
                "processing_time_ms": int((time.time() - start_time) * 1000)
            }
            
        except Exception as e:
            logger.error(f"Error finding sellers by category path: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "sellers": [],
                "processing_time_ms": int((time.time() - start_time) * 1000)
            }
    
    def get_seller_categories(self, seller_id: str) -> Dict[str, Any]:
        """
        Get all categories for a specific seller.
        
        Args:
            seller_id: Seller ID to get categories for
            
        Returns:
            Dict with seller categories and mappings
        """
        try:
            results = self.collection.query(
                query_texts=[""],  # Empty query, we're filtering by metadata
                n_results=100,
                include=['metadatas'],
                where={
                    "$and": [
                        {"type": "seller_mapping"},
                        {"seller_id": seller_id}
                    ]
                }
            )
            
            if not results['metadatas'][0]:
                return {
                    "success": False,
                    "error": f"No categories found for seller: {seller_id}",
                    "categories": []
                }
            
            categories = []
            for metadata in results['metadatas'][0]:
                categories.append({
                    "original_category": metadata["original_category"],
                    "learning_category": {
                        "level_1": metadata["level_1_category"],
                        "level_2": metadata["level_2_category"],
                        "level_3": metadata["level_3_category"],
                        "category_path": metadata["category_path"]
                    },
                    "confidence_score": metadata["confidence_score"]
                })
            
            return {
                "success": True,
                "seller_id": seller_id,
                "seller_name": results['metadatas'][0][0]["seller_name"],
                "categories": categories,
                "total_categories": len(categories)
            }
            
        except Exception as e:
            logger.error(f"Error getting seller categories: {str(e)}")
            return {
                "success": False,
                "error": str(e),
                "categories": []
            }
    
    def _calculate_distance(self, lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Calculate distance between two points using Haversine formula."""
        # Convert to radians
        lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
        
        # Haversine formula
        dlat = lat2 - lat1
        dlng = lng2 - lng1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
        c = 2 * asin(sqrt(a))
        r = 6371  # Radius of earth in kilometers
        
        return c * r
    
    def _ranking_priority(self, ranking: str) -> int:
        """Convert seller ranking to priority number for sorting."""
        ranking_map = {
            "Diamond": 4,
            "Platinum": 3,
            "Gold": 2,
            "Titanium": 1
        }
        return ranking_map.get(ranking, 0)
    
    def health_check(self) -> Dict[str, Any]:
        """Perform health check on the enhanced seller matching service."""
        try:
            # Check ChromaDB connection
            try:
                collection_count = self.collection.count()
                
                # Try to count seller mappings
                seller_results = self.collection.query(
                    query_texts=["test"],
                    n_results=1,
                    where={"type": "seller_mapping"}
                )
                seller_count = "available" if seller_results['documents'][0] else 0
                
                chromadb_status = "healthy"
            except Exception as e:
                chromadb_status = f"unhealthy: {str(e)}"
                collection_count = 0
                seller_count = 0
            
            return {
                "overall_status": chromadb_status,
                "unified_vector_store": {
                    "status": chromadb_status,
                    "total_collection_count": collection_count,
                    "seller_mappings_status": seller_count,
                    "chroma_path": self.chroma_path
                }
            }
            
        except Exception as e:
            logger.error(f"Health check failed: {str(e)}")
            return {
                "overall_status": "unhealthy",
                "error": str(e)
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get seller matching service statistics."""
        try:
            # Get collection stats
            total_items = self.collection.count()
            
            # Get database stats for additional context
            db = get_db_session()
            try:
                total_sellers = db.query(Seller).count()
                db.close()
            except:
                total_sellers = "unknown"
            
            return {
                "unified_vector_store": {
                    "total_items": total_items,
                    "collection_name": "learning_taxonomy",
                    "embedding_model": "all-MiniLM-L6-v2",
                    "chroma_path": self.chroma_path
                },
                "database_sellers": {
                    "total_sellers": total_sellers
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting stats: {str(e)}")
            return {"error": str(e)}