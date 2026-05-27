#!/usr/bin/env python3
"""
Entity Service Test Script
Tests entity extraction functionality with predefined queries and evaluates completeness.
"""

import sys
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from app.services.entity_service import EntityService


# Test results tracking
test_results = {
    "total_queries": 0,
    "successful_extractions": 0,
    "api_failures": 0,
    "complete_extractions": 0,
    "partial_extractions": 0
}


def load_test_inputs():
    """Load test queries from input file."""
    inputs_file = project_root / "tests" / "inputs" / "entity_extraction_queries.txt"
    with open(inputs_file, 'r') as f:
        queries = [line.strip() for line in f if line.strip()]
    return queries


def test_single_query(entity_service, query, query_number):
    """Test a single query and evaluate results."""
    print(f"\n{'='*60}")
    print(f"TEST {query_number}: {query}")
    print(f"{'='*60}")
    
    test_results["total_queries"] += 1
    
    try:
        # Test entity extraction
        result = entity_service.extract_entities(query, {}, "buy_something")
        test_results["successful_extractions"] += 1
        
        # Display results
        print(f"Extracted Entities: {result.get('entities', {})}")
        print(f"Completeness: {result.get('completeness', 0)}%")
        print(f"Confidence: {result.get('confidence', 0)}%")
        
        # Test completeness check
        completeness = entity_service.check_entity_completeness(result, "buy_something")
        
        print(f"Is Complete: {completeness.get('is_complete', False)}")
        print(f"Missing Fields: {completeness.get('missing_required_fields', [])}")
        print(f"Next Questions: {completeness.get('next_questions', [])}")
        
        # Track completeness
        if completeness.get('is_complete', False):
            test_results["complete_extractions"] += 1
        else:
            test_results["partial_extractions"] += 1
            
    except Exception as e:
        print(f"ERROR: {str(e)}")
        test_results["api_failures"] += 1


def main():
    """Run all entity extraction tests."""
    print("Entity Service Unit Tests")
    print("========================")
    
    # Load test inputs
    try:
        queries = load_test_inputs()
        print(f"Loaded {len(queries)} test queries")
    except FileNotFoundError:
        print("ERROR: entity_extraction_queries.txt not found in tests/inputs/")
        return
    
    # Initialize entity service
    try:
        entity_service = EntityService()
        print("Entity service initialized successfully")
    except Exception as e:
        print(f"Failed to initialize entity service: {e}")
        return
    
    # Test each query
    for i, query in enumerate(queries, 1):
        test_single_query(entity_service, query, i)
    
    # Print final summary
    print_final_summary()


def print_final_summary():
    """Print final test summary and evaluation."""
    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    
    total = test_results["total_queries"]
    successful = test_results["successful_extractions"]
    api_failures = test_results["api_failures"]
    complete = test_results["complete_extractions"]
    partial = test_results["partial_extractions"]
    
    print(f"Total Queries: {total}")
    print(f"Successful Extractions: {successful}/{total} ({successful/total*100:.1f}%)")
    print(f"API Failures: {api_failures}/{total} ({api_failures/total*100:.1f}%)")
    print(f"Complete Extractions: {complete}/{total} ({complete/total*100:.1f}%)")
    print(f"Partial Extractions: {partial}/{total} ({partial/total*100:.1f}%)")
    
    print(f"\n{'='*60}")
    print("EVALUATION")
    print(f"{'='*60}")
    
    if api_failures == 0:
        print("PASS: All queries successfully processed")
    else:
        print(f"FAIL: {api_failures} API failures occurred")
    
    if complete > 0:
        print(f"PASS: {complete} queries had complete entity extraction")
    else:
        print("WARNING: No queries had complete entity extraction")
    
    success_rate = successful / total * 100 if total > 0 else 0
    if success_rate >= 90:
        print(f"OVERALL STATUS: PASS - Entity service performing well ({success_rate:.1f}%)")
    elif success_rate >= 70:
        print(f"OVERALL STATUS: PARTIAL PASS - Some issues detected ({success_rate:.1f}%)")
    else:
        print(f"OVERALL STATUS: FAIL - Significant issues require attention ({success_rate:.1f}%)")
    
    print(f"{'='*60}")


if __name__ == "__main__":
    main()