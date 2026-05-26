#!/usr/bin/env python3
"""
Intent Service Demonstration Script.
Tests intent classification with predefined queries and evaluates results against expected criteria.

SUCCESS CRITERIA:
- All queries must be classified (no API failures)
- High-confidence queries (>80%) should not require clarification
- Multi-intent ambiguity should be detected for purchase-related queries
- Fallback classification should be rare (<20% of queries)
"""

import sys
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.services.intent_service import IntentService


# Test results tracking
test_results = {
    "total_queries": 0,
    "successful_classifications": 0,
    "fallback_classifications": 0,
    "high_confidence_requiring_clarification": 0,
    "multi_intent_detected": 0,
    "api_failures": 0
}


def load_test_inputs():
    """Load test queries from input file."""
    inputs_file = project_root / "tests" / "inputs" / "intent_classification_queries.txt"
    with open(inputs_file, 'r') as f:
        queries = [line.strip() for line in f if line.strip()]
    return queries


def test_single_query(intent_service, query, query_number):
    """Test a single query and evaluate results against criteria."""
    print(f"\n{'='*60}")
    print(f"TEST {query_number}: {query}")
    print(f"{'='*60}")
    
    test_results["total_queries"] += 1
    
    try:
        result = intent_service.classify_intent(query)
        
        # Track successful classification
        test_results["successful_classifications"] += 1
        
        # Display results
        print(f"Intent: {result.get('intent', 'N/A')}")
        print(f"Confidence: {result.get('confidence', 'N/A')}%")
        print(f"Routing Decision: {result.get('routing_decision', 'N/A')}")
        print(f"Next Action: {result.get('next_action', 'N/A')}")
        print(f"Requires Clarification: {result.get('requires_clarification', 'N/A')}")
        print(f"Threshold Met: {result.get('threshold_met', 'N/A')}")
        
        # Show all intent scores if available
        if result.get('all_intent_scores'):
            print("All Intent Scores:")
            for intent, score in result['all_intent_scores'].items():
                print(f"  {intent}: {score}%")
        
        # Show ambiguous intents if detected
        if result.get('ambiguous_intents'):
            print(f"Ambiguous Intents: {', '.join(result['ambiguous_intents'])}")
            test_results["multi_intent_detected"] += 1
        
        if result.get('reasoning'):
            print(f"Reasoning: {result['reasoning']}")
        
        # Evaluate against criteria
        evaluate_result(result, query)
            
    except Exception as e:
        print(f"ERROR: {str(e)}")
        test_results["api_failures"] += 1


def evaluate_result(result, query):
    """Evaluate result against success criteria."""
    confidence = result.get('confidence', 0)
    requires_clarification = result.get('requires_clarification', False)
    is_fallback = result.get('is_fallback', False)
    
    # Track fallback usage
    if is_fallback:
        test_results["fallback_classifications"] += 1
        print("WARNING CRITERIA: Fallback classification used")
    
    # Check for high confidence requiring clarification (potential issue)
    if confidence > 80 and requires_clarification:
        test_results["high_confidence_requiring_clarification"] += 1
        print("FAIL CRITERIA: High confidence but requires clarification")
    
    # Positive indicators
    if confidence > 80 and not requires_clarification:
        print("PASS CRITERIA: High confidence, clear routing")
    
    if result.get('ambiguous_intents'):
        print("PASS CRITERIA: Multi-intent ambiguity detected appropriately")


def main():
    """Run all intent classification tests."""
    print("Intent Service Unit Tests")
    print("========================")
    
    # Load test inputs
    try:
        queries = load_test_inputs()
        print(f"Loaded {len(queries)} test queries")
    except FileNotFoundError:
        print("ERROR: intent_classification_queries.txt not found in tests/inputs/")
        return
    
    # Initialize intent service
    try:
        intent_service = IntentService()
        print("Intent service initialized successfully")
    except Exception as e:
        print(f"Failed to initialize intent service: {e}")
        return
    
    # Test each query
    for i, query in enumerate(queries, 1):
        test_single_query(intent_service, query, i)
    
    # Final evaluation and summary
    print_final_summary()


def print_final_summary():
    """Print final test summary and pass/fail evaluation."""
    print(f"\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    
    total = test_results["total_queries"]
    successful = test_results["successful_classifications"] 
    fallbacks = test_results["fallback_classifications"]
    api_failures = test_results["api_failures"]
    high_conf_clarification = test_results["high_confidence_requiring_clarification"]
    multi_intent = test_results["multi_intent_detected"]
    
    print(f"Total Queries: {total}")
    print(f"Successful Classifications: {successful}/{total} ({successful/total*100:.1f}%)")
    print(f"API Failures: {api_failures}/{total} ({api_failures/total*100:.1f}%)")
    print(f"Fallback Classifications: {fallbacks}/{total} ({fallbacks/total*100:.1f}%)")
    print(f"High Confidence Requiring Clarification: {high_conf_clarification}")
    print(f"Multi-Intent Ambiguity Detected: {multi_intent}")
    
    print(f"\n{'='*60}")
    print("PASS/FAIL EVALUATION")
    print(f"{'='*60}")
    
    # Evaluate against criteria
    criteria_passed = 0
    total_criteria = 4
    
    # Criterion 1: All queries classified (no API failures)
    if api_failures == 0:
        print("PASS: All queries successfully classified")
        criteria_passed += 1
    else:
        print(f"FAIL: {api_failures} API failures occurred")
    
    # Criterion 2: High-confidence queries should not require clarification
    if high_conf_clarification == 0:
        print("PASS: No high-confidence queries required clarification")
        criteria_passed += 1
    else:
        print(f"FAIL: {high_conf_clarification} high-confidence queries required clarification")
    
    # Criterion 3: Multi-intent ambiguity detection (expect at least 1)
    if multi_intent > 0:
        print("PASS: Multi-intent ambiguity was detected")
        criteria_passed += 1
    else:
        print("WARNING: No multi-intent ambiguity detected (expected for purchase queries)")
    
    # Criterion 4: Fallback should be rare (<20%)
    fallback_rate = fallbacks / total * 100 if total > 0 else 0
    if fallback_rate < 20:
        print(f"PASS: Fallback rate is acceptable ({fallback_rate:.1f}% < 20%)")
        criteria_passed += 1
    else:
        print(f"FAIL: Fallback rate too high ({fallback_rate:.1f}% >= 20%)")
    
    print(f"\n{'='*60}")
    print(f"OVERALL RESULT: {criteria_passed}/{total_criteria} criteria passed")
    
    if criteria_passed == total_criteria:
        print("OVERALL STATUS: PASS - Intent service performing well")
    elif criteria_passed >= total_criteria * 0.75:
        print("OVERALL STATUS: PARTIAL PASS - Minor issues detected")
    else:
        print("OVERALL STATUS: FAIL - Significant issues require attention")
    
    print(f"{'='*60}")


if __name__ == "__main__":
    main()