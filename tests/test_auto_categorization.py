#!/usr/bin/env python3
"""
Comprehensive test script for auto-categorization system.

This script tests:
1. Database connectivity and table creation
2. ChromaDB integration
3. Embedding generation
4. Vector similarity search
5. OpenAI categorization
6. End-to-end categorization flow
7. Error handling and edge cases
"""

import os
import sys
import time
import asyncio
from typing import Dict, List
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.auto_categorization_service import AutoCategorizationService
from app.database import get_db_session
from app.models import CategoryMapping, AutoCategorizationLog

class AutoCategorizationTester:
    """Comprehensive tester for auto-categorization system."""
    
    def __init__(self):
        self.auto_cat_service = None
        self.test_results = []
        self.start_time = time.time()
    
    def run_all_tests(self):
        """Run all test suites."""
        print("🧪 Starting Comprehensive Auto-Categorization Tests\n")
        
        try:
            # Test suites
            self.test_database_connectivity()
            self.test_chromadb_integration()
            self.test_embedding_generation()
            self.test_sample_data_exists()
            self.test_categorization_accuracy()
            self.test_edge_cases()
            self.test_performance()
            self.test_logging()
            
            # Summary
            self.print_test_summary()
            
        except Exception as e:
            print(f"❌ Test suite failed: {e}")
            sys.exit(1)
    
    def test_database_connectivity(self):
        """Test database connectivity and table existence."""
        print("1️⃣ Testing Database Connectivity...")
        
        try:
            db = get_db_session()
            
            # Test CategoryMapping table
            mapping_count = db.query(CategoryMapping).count()
            self.log_test("Database connection", True, f"Connected, {mapping_count} mappings found")
            
            # Test AutoCategorizationLog table
            log_count = db.query(AutoCategorizationLog).count()
            self.log_test("AutoCategorizationLog table", True, f"{log_count} existing logs")
            
            db.close()
            
        except Exception as e:
            self.log_test("Database connectivity", False, str(e))
        
        print()
    
    def test_chromadb_integration(self):
        """Test ChromaDB integration."""
        print("2️⃣ Testing ChromaDB Integration...")
        
        try:
            self.auto_cat_service = AutoCategorizationService()
            
            # Test collection initialization
            stats = self.auto_cat_service.get_collection_stats()
            if "error" in stats:
                self.log_test("ChromaDB collection", False, stats["error"])
            else:
                item_count = stats.get("count", 0)
                self.log_test("ChromaDB collection", True, f"{item_count} items in collection")
            
        except Exception as e:
            self.log_test("ChromaDB integration", False, str(e))
        
        print()
    
    def test_embedding_generation(self):
        """Test sentence transformer embedding functionality."""
        print("3️⃣ Testing Sentence Transformer Embeddings...")
        
        if not self.auto_cat_service:
            self.auto_cat_service = AutoCategorizationService()
        
        try:
            # Test that embedding function is properly initialized
            embedding_function = self.auto_cat_service.embedding_function
            if hasattr(embedding_function, 'model_name'):
                model_name = embedding_function.model_name
                self.log_test("Embedding function initialization", True, f"Using model: {model_name}")
            else:
                self.log_test("Embedding function initialization", False, "Embedding function not properly initialized")
            
            # Test that ChromaDB can handle text queries (embeddings generated internally)
            test_text = "blue ballpoint pens for office use"
            try:
                # Test by performing a simple query - this will internally generate embeddings
                results = self.auto_cat_service.collection.query(
                    query_texts=[test_text],
                    n_results=1,
                    include=['documents']
                )
                self.log_test("Sentence transformer embedding", True, "Query executed successfully with automatic embeddings")
            except Exception as query_e:
                self.log_test("Sentence transformer embedding", False, f"Query failed: {query_e}")
        
        except Exception as e:
            self.log_test("Embedding generation test", False, str(e))
        
        print()
    
    def test_sample_data_exists(self):
        """Test that sample data exists in both MySQL and ChromaDB."""
        print("4️⃣ Testing Sample Data...")
        
        try:
            db = get_db_session()
            mapping_count = db.query(CategoryMapping).count()
            db.close()
            
            if mapping_count == 0:
                self.log_test("Sample data in MySQL", False, "No category mappings found")
                print("   💡 Run: python setup_auto_categorization.py")
                return
            else:
                self.log_test("Sample data in MySQL", True, f"{mapping_count} category mappings")
            
            # Test ChromaDB data
            if self.auto_cat_service:
                stats = self.auto_cat_service.get_collection_stats()
                chroma_count = stats.get("count", 0)
                
                if chroma_count == 0:
                    self.log_test("Sample data in ChromaDB", False, "No items in ChromaDB collection")
                    print("   💡 Run: python create_sample_category_data.py")
                else:
                    self.log_test("Sample data in ChromaDB", True, f"{chroma_count} items in ChromaDB")
        
        except Exception as e:
            self.log_test("Sample data check", False, str(e))
        
        print()
    
    def test_categorization_accuracy(self):
        """Test categorization accuracy with known test cases."""
        print("5️⃣ Testing Categorization Accuracy...")
        
        if not self.auto_cat_service:
            self.log_test("Categorization accuracy", False, "AutoCategorizationService not initialized")
            print()
            return
        
        # Test cases with expected categories (matching actual migrated data)
        test_cases = [
            {
                "description": "blue ballpoint pens for office",
                "expected": "Admin & IT",
                "confidence_threshold": 0.6
            },
            {
                "description": "safety helmet for construction workers",
                "expected": "Occupational Safety & Health",  # Using actual category from migrated data
                "confidence_threshold": 0.6
            },
            {
                "description": "CNC lathe machine for manufacturing",
                "expected": "CAPEX - Equipment & Machinery",  # Using actual category from migrated data
                "confidence_threshold": 0.6
            },
            {
                "description": "cardboard boxes for shipping",
                "expected": "Packing Material",  # Using actual category from migrated data
                "confidence_threshold": 0.6
            },
            {
                "description": "steel sheets for fabrication",
                "expected": "Raw Materials",
                "confidence_threshold": 0.6
            }
        ]
        
        correct_predictions = 0
        total_tests = len(test_cases)
        
        for i, test_case in enumerate(test_cases, 1):
            try:
                result = self.auto_cat_service.categorize_item(
                    item_description=test_case["description"],
                    user_id="test_user",
                    session_id=f"accuracy_test_{i}"
                )
                
                if result["success"]:
                    predicted = result["category"]
                    confidence = result.get("confidence_score", 0)
                    expected = test_case["expected"]
                    
                    is_correct = predicted == expected and confidence >= test_case["confidence_threshold"]
                    
                    if is_correct:
                        correct_predictions += 1
                        status = "✅"
                    else:
                        status = "❌"
                    
                    print(f"   {status} '{test_case['description'][:30]}...'")
                    print(f"      Expected: {expected} | Got: {predicted} | Confidence: {confidence:.2f}")
                else:
                    print(f"   ❌ '{test_case['description'][:30]}...' - Failed: {result.get('reason', 'Unknown')}")
            
            except Exception as e:
                print(f"   ❌ '{test_case['description'][:30]}...' - Error: {e}")
        
        accuracy = (correct_predictions / total_tests) * 100
        self.log_test("Categorization accuracy", accuracy >= 60, f"{accuracy:.1f}% ({correct_predictions}/{total_tests})")
        
        print()
    
    def test_edge_cases(self):
        """Test edge cases and error handling."""
        print("6️⃣ Testing Edge Cases...")
        
        if not self.auto_cat_service:
            self.log_test("Edge cases", False, "AutoCategorizationService not initialized")
            print()
            return
        
        edge_cases = [
            ("", "Empty description"),
            ("xyz", "Very short description"),
            ("a" * 1000, "Very long description"),
            ("🎉🔥💯", "Only emojis"),
            ("completely unknown futuristic quantum widget", "Unknown item")
        ]
        
        passed_edge_cases = 0
        
        for description, case_name in edge_cases:
            try:
                result = self.auto_cat_service.categorize_item(
                    item_description=description,
                    user_id="test_user",
                    session_id=f"edge_case_{case_name.replace(' ', '_')}"
                )
                
                # Edge cases should either succeed or fail gracefully
                if result.get("success") or result.get("reason"):
                    passed_edge_cases += 1
                    print(f"   ✅ {case_name}: Handled gracefully")
                else:
                    print(f"   ❌ {case_name}: Unexpected response format")
            
            except Exception as e:
                print(f"   ❌ {case_name}: Unhandled exception - {e}")
        
        self.log_test("Edge case handling", passed_edge_cases == len(edge_cases), 
                     f"{passed_edge_cases}/{len(edge_cases)} handled correctly")
        
        print()
    
    def test_performance(self):
        """Test performance and response times."""
        print("7️⃣ Testing Performance...")
        
        if not self.auto_cat_service:
            self.log_test("Performance", False, "AutoCategorizationService not initialized")
            print()
            return
        
        try:
            # Test response time
            start_time = time.time()
            result = self.auto_cat_service.categorize_item(
                item_description="laptop computer for programming",
                user_id="test_user",
                session_id="performance_test"
            )
            end_time = time.time()
            
            response_time = (end_time - start_time) * 1000  # Convert to ms
            processing_time = result.get("processing_time_ms", 0)
            
            # Performance thresholds
            acceptable_time = 10000  # 10 seconds
            is_fast_enough = response_time < acceptable_time
            
            self.log_test("Response time", is_fast_enough, 
                         f"{response_time:.0f}ms total, {processing_time}ms internal")
            
            if not is_fast_enough:
                print(f"   ⚠️  Response time exceeded {acceptable_time}ms threshold")
        
        except Exception as e:
            self.log_test("Performance test", False, str(e))
        
        print()
    
    def test_logging(self):
        """Test that categorization attempts are properly logged."""
        print("8️⃣ Testing Logging...")
        
        try:
            # Count logs before test
            db = get_db_session()
            initial_count = db.query(AutoCategorizationLog).count()
            db.close()
            
            # Perform categorization
            if self.auto_cat_service:
                result = self.auto_cat_service.categorize_item(
                    item_description="test item for logging",
                    user_id="test_user",
                    session_id="logging_test"
                )
                
                # Count logs after test
                db = get_db_session()
                final_count = db.query(AutoCategorizationLog).count()
                db.close()
                
                logs_created = final_count - initial_count
                
                if logs_created > 0:
                    self.log_test("Categorization logging", True, f"{logs_created} new log entries")
                else:
                    self.log_test("Categorization logging", False, "No log entries created")
            else:
                self.log_test("Categorization logging", False, "Service not initialized")
        
        except Exception as e:
            self.log_test("Logging test", False, str(e))
        
        print()
    
    def log_test(self, test_name: str, passed: bool, details: str = ""):
        """Log test result."""
        status = "✅ PASS" if passed else "❌ FAIL"
        self.test_results.append({"name": test_name, "passed": passed, "details": details})
        print(f"   {status} {test_name}: {details}")
    
    def print_test_summary(self):
        """Print comprehensive test summary."""
        total_time = time.time() - self.start_time
        
        print("=" * 60)
        print("📊 TEST SUMMARY")
        print("=" * 60)
        
        passed = sum(1 for result in self.test_results if result["passed"])
        total = len(self.test_results)
        
        print(f"🎯 Overall: {passed}/{total} tests passed ({(passed/total)*100:.1f}%)")
        print(f"⏱️  Total time: {total_time:.1f}s")
        print()
        
        # Detailed results
        for result in self.test_results:
            status = "✅" if result["passed"] else "❌"
            print(f"{status} {result['name']}: {result['details']}")
        
        print()
        
        if passed == total:
            print("🎉 All tests passed! Auto-categorization system is working correctly.")
        elif passed / total >= 0.8:
            print("⚠️  Most tests passed. Review failed tests above.")
        else:
            print("🚨 Multiple tests failed. System needs attention.")
            
        print("\n📋 Recommendations:")
        if passed < total:
            print("- Review failed tests above")
            print("- Check database connectivity")
            print("- Verify API keys and configuration")
            print("- Run setup_auto_categorization.py if sample data is missing")

def main():
    """Run comprehensive tests."""
    tester = AutoCategorizationTester()
    tester.run_all_tests()

if __name__ == "__main__":
    main()