#!/usr/bin/env python3
"""
Test Enhanced Vector System - End-to-End Results Testing

This script tests the final results of the enhanced vector system:
1. Auto-categorization using unified vector store
2. Seller matching using unified vector store
3. Fallback behavior when vector store unavailable

Usage:
    python test_enhanced_vector_system.py
"""

import sys
import os
import logging
from typing import Dict, Any, List

# Add the app directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from app.services.enhanced_auto_categorization_service import EnhancedAutoCategorizationService
from app.services.enhanced_seller_matching_service import EnhancedSellerMatchingService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class EnhancedVectorSystemTester:
    """Test the enhanced vector system end-to-end results."""
    
    def __init__(self):
        """Initialize the tester with test data."""
        self.test_items = [
            {
                "description": "Need 50 laptops for office use",
                "expected_category_contains": ["computer", "laptop", "technology", "electronics"],
                "delivery_location": {"lat": 12.97, "lng": 77.59, "city": "Bangalore", "state": "Karnataka"}
            },
            {
                "description": "Medical equipment for hospital - patient monitors",
                "expected_category_contains": ["medical", "healthcare", "equipment", "monitoring"],
                "delivery_location": {"lat": 19.07, "lng": 72.87, "city": "Mumbai", "state": "Maharashtra"}
            },
            {
                "description": "Office chairs and desks for new branch",
                "expected_category_contains": ["office", "furniture", "business"],
                "delivery_location": {"lat": 28.61, "lng": 77.23, "city": "Delhi", "state": "Delhi"}
            },
            {
                "description": "Industrial machinery for manufacturing",
                "expected_category_contains": ["industrial", "machinery", "manufacturing"],
                "delivery_location": {"lat": 22.57, "lng": 88.36, "city": "Kolkata", "state": "West Bengal"}
            }
        ]
        
        # Initialize services
        try:
            self.auto_categorization_service = EnhancedAutoCategorizationService()
            self.seller_matching_service = EnhancedSellerMatchingService()
            logger.info("✅ Enhanced services initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize enhanced services: {e}")
            self.auto_categorization_service = None
            self.seller_matching_service = None
    
    def test_auto_categorization(self) -> Dict[str, Any]:
        """Test enhanced auto-categorization with various item descriptions."""
        logger.info("🔍 Testing Enhanced Auto-Categorization...")
        
        if not self.auto_categorization_service:
            return {"success": False, "error": "Service not available"}
        
        results = {
            "total_tests": len(self.test_items),
            "successful_categorizations": 0,
            "failed_categorizations": 0,
            "fallback_used_count": 0,
            "primary_vector_used_count": 0,
            "results": []
        }
        
        for i, test_item in enumerate(self.test_items, 1):
            logger.info(f"\n--- Test {i}/{len(self.test_items)}: {test_item['description'][:50]}... ---")
            
            try:
                # Test categorization
                result = self.auto_categorization_service.categorize_item(
                    item_description=test_item["description"],
                    user_id="test_user",
                    session_id=f"test_session_{i}"
                )
                
                if result.get("success"):
                    results["successful_categorizations"] += 1
                    
                    # Check which method was used
                    method = result.get("method", "unknown")
                    if "fallback" in method:
                        results["fallback_used_count"] += 1
                        logger.info(f"✅ SUCCESS (FALLBACK): {result.get('client_category', 'No category')}")
                    else:
                        results["primary_vector_used_count"] += 1
                        logger.info(f"✅ SUCCESS (ENHANCED): {result.get('client_category', 'No category')}")
                        
                        # Show learning category if available
                        learning_cat = result.get("learning_category")
                        if learning_cat:
                            logger.info(f"   📂 Learning Category: {learning_cat.get('category_path', 'N/A')}")
                    
                    logger.info(f"   📊 Confidence: {result.get('confidence_score', 0):.2f}")
                    logger.info(f"   ⏱️  Processing Time: {result.get('processing_time_ms', 0)}ms")
                    
                    # Validate expected content
                    category_text = str(result.get('client_category', '')).lower()
                    learning_text = str(result.get('learning_category', {}).get('category_path', '')).lower()
                    combined_text = f"{category_text} {learning_text}"
                    
                    expected_found = any(expected.lower() in combined_text for expected in test_item['expected_category_contains'])
                    logger.info(f"   🎯 Expected content match: {'Yes' if expected_found else 'No'}")
                    
                else:
                    results["failed_categorizations"] += 1
                    logger.error(f"❌ FAILED: {result.get('error', 'Unknown error')}")
                
                results["results"].append({
                    "test_item": test_item["description"][:50] + "...",
                    "success": result.get("success", False),
                    "method": result.get("method", "unknown"),
                    "client_category": result.get("client_category"),
                    "confidence": result.get("confidence_score", 0),
                    "processing_time_ms": result.get("processing_time_ms", 0)
                })
                
            except Exception as e:
                results["failed_categorizations"] += 1
                logger.error(f"❌ EXCEPTION: {str(e)}")
                results["results"].append({
                    "test_item": test_item["description"][:50] + "...",
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def test_seller_matching(self) -> Dict[str, Any]:
        """Test enhanced seller matching for various item descriptions."""
        logger.info("\n\n🏪 Testing Enhanced Seller Matching...")
        
        if not self.seller_matching_service:
            return {"success": False, "error": "Service not available"}
        
        results = {
            "total_tests": len(self.test_items),
            "successful_matches": 0,
            "failed_matches": 0,
            "total_sellers_found": 0,
            "results": []
        }
        
        for i, test_item in enumerate(self.test_items, 1):
            logger.info(f"\n--- Test {i}/{len(self.test_items)}: {test_item['description'][:50]}... ---")
            
            try:
                # Test seller matching
                result = self.seller_matching_service.find_sellers_for_item(
                    item_description=test_item["description"],
                    delivery_location=test_item["delivery_location"],
                    max_distance_km=300,
                    max_sellers=10
                )
                
                if result.get("success"):
                    results["successful_matches"] += 1
                    sellers = result.get("sellers", [])
                    results["total_sellers_found"] += len(sellers)
                    
                    logger.info(f"✅ SUCCESS: Found {len(sellers)} sellers")
                    logger.info(f"   ⏱️  Processing Time: {result.get('search_details', {}).get('processing_time_ms', 0)}ms")
                    
                    # Show top 3 sellers
                    for j, seller in enumerate(sellers[:3], 1):
                        similarity = seller.get("category_match", {}).get("similarity_score", 0)
                        distance = seller.get("distance_km")
                        distance_text = f"{distance:.1f}km" if distance else "N/A"
                        
                        logger.info(f"   {j}. {seller.get('seller_name', 'Unknown')} - {similarity:.2f} similarity, {distance_text}")
                        logger.info(f"      📂 {seller.get('category_match', {}).get('learning_category', {}).get('category_path', 'N/A')}")
                        logger.info(f"      📞 {seller.get('phone_number', 'N/A')} | ⭐ {seller.get('ranking', 'N/A')}")
                    
                    if len(sellers) > 3:
                        logger.info(f"   ... and {len(sellers) - 3} more sellers")
                    
                else:
                    results["failed_matches"] += 1
                    logger.error(f"❌ FAILED: {result.get('error', 'Unknown error')}")
                
                results["results"].append({
                    "test_item": test_item["description"][:50] + "...",
                    "success": result.get("success", False),
                    "sellers_found": len(result.get("sellers", [])),
                    "processing_time_ms": result.get("search_details", {}).get("processing_time_ms", 0),
                    "top_seller": result.get("sellers", [{}])[0].get("seller_name") if result.get("sellers") else None
                })
                
            except Exception as e:
                results["failed_matches"] += 1
                logger.error(f"❌ EXCEPTION: {str(e)}")
                results["results"].append({
                    "test_item": test_item["description"][:50] + "...",
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def test_health_checks(self) -> Dict[str, Any]:
        """Test health checks of both enhanced services."""
        logger.info("\n\n🏥 Testing Service Health Checks...")
        
        health_results = {}
        
        # Test auto-categorization service health
        if self.auto_categorization_service:
            try:
                auto_health = self.auto_categorization_service.health_check()
                health_results["auto_categorization"] = auto_health
                status = auto_health.get("overall_status", "unknown")
                logger.info(f"Auto-Categorization Health: {status}")
                
                if status == "healthy":
                    logger.info("   ✅ Vector store accessible")
                    logger.info("   ✅ Fallback service available")
                else:
                    logger.warning("   ⚠️  Health check issues detected")
                    
            except Exception as e:
                health_results["auto_categorization"] = {"error": str(e)}
                logger.error(f"❌ Auto-categorization health check failed: {e}")
        
        # Test seller matching service health
        if self.seller_matching_service:
            try:
                seller_health = self.seller_matching_service.health_check()
                health_results["seller_matching"] = seller_health
                status = seller_health.get("overall_status", "unknown")
                logger.info(f"Seller Matching Health: {status}")
                
                if status == "healthy":
                    logger.info("   ✅ Vector store accessible")
                else:
                    logger.warning("   ⚠️  Health check issues detected")
                    
            except Exception as e:
                health_results["seller_matching"] = {"error": str(e)}
                logger.error(f"❌ Seller matching health check failed: {e}")
        
        return health_results
    
    def run_all_tests(self) -> Dict[str, Any]:
        """Run all tests and return comprehensive results."""
        logger.info("🚀 Starting Enhanced Vector System Tests")
        logger.info("=" * 60)
        
        # Run health checks first
        health_results = self.test_health_checks()
        
        # Run auto-categorization tests
        auto_results = self.test_auto_categorization()
        
        # Run seller matching tests
        seller_results = self.test_seller_matching()
        
        # Compile final results
        final_results = {
            "health_checks": health_results,
            "auto_categorization": auto_results,
            "seller_matching": seller_results,
            "overall_summary": {
                "services_healthy": len([h for h in health_results.values() if h.get("overall_status") == "healthy"]),
                "total_services": len(health_results),
                "categorization_success_rate": auto_results["successful_categorizations"] / max(auto_results["total_tests"], 1),
                "seller_matching_success_rate": seller_results["successful_matches"] / max(seller_results["total_tests"], 1),
                "primary_vector_usage_rate": auto_results["primary_vector_used_count"] / max(auto_results["successful_categorizations"], 1) if auto_results["successful_categorizations"] > 0 else 0
            }
        }
        
        # Print final summary
        logger.info("\n" + "=" * 60)
        logger.info("📊 FINAL TEST RESULTS SUMMARY")
        logger.info("=" * 60)
        
        summary = final_results["overall_summary"]
        logger.info(f"🏥 Service Health: {summary['services_healthy']}/{summary['total_services']} healthy")
        logger.info(f"🔍 Auto-Categorization: {auto_results['successful_categorizations']}/{auto_results['total_tests']} successful ({summary['categorization_success_rate']:.1%})")
        logger.info(f"   └─ Enhanced Vector Used: {auto_results['primary_vector_used_count']} times ({summary['primary_vector_usage_rate']:.1%})")
        logger.info(f"   └─ Fallback Used: {auto_results['fallback_used_count']} times")
        logger.info(f"🏪 Seller Matching: {seller_results['successful_matches']}/{seller_results['total_tests']} successful ({summary['seller_matching_success_rate']:.1%})")
        logger.info(f"   └─ Total Sellers Found: {seller_results['total_sellers_found']}")
        
        if summary["categorization_success_rate"] >= 0.8 and summary["seller_matching_success_rate"] >= 0.8:
            logger.info("\n🎉 OVERALL RESULT: ✅ SYSTEM WORKING WELL!")
        elif summary["categorization_success_rate"] >= 0.5 and summary["seller_matching_success_rate"] >= 0.5:
            logger.info("\n⚠️  OVERALL RESULT: 🟡 SYSTEM PARTIALLY WORKING")
        else:
            logger.info("\n❌ OVERALL RESULT: 🔴 SYSTEM NEEDS ATTENTION")
        
        return final_results

def main():
    """Main function to run the tests."""
    tester = EnhancedVectorSystemTester()
    results = tester.run_all_tests()
    
    # Return appropriate exit code
    summary = results.get("overall_summary", {})
    if summary.get("categorization_success_rate", 0) >= 0.8 and summary.get("seller_matching_success_rate", 0) >= 0.8:
        sys.exit(0)  # Success
    else:
        sys.exit(1)  # Issues detected

if __name__ == "__main__":
    main()