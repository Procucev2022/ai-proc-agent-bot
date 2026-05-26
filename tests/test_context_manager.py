"""
Context Manager Testing Suite
Tests different scenarios and data types with the context manager
"""

from app.context.context_controller import session_context, user_context, param_context
from app.context.context_manager import context_manager
import json

def test_basic_operations():
    """Test basic CRUD operations"""
    print("=== Basic Operations Test ===")
    request_id = "basic-001"
    
    # Set different data types
    user_context.set(request_id, {"id": "u001", "name": "John", "role": "buyer"})
    session_context.set(request_id, {"workflow": "rfq", "step": 1, "active": True})
    param_context.set(request_id, ["laptop", "mouse", "keyboard"])
    
    print(f"User: {user_context.get(request_id)}")
    print(f"Session: {session_context.get(request_id)}")
    print(f"Params: {param_context.get(request_id)}")
    
    # Update operations
    user_context.update(request_id, {"last_seen": "2024-01-15"})
    session_context.update(request_id, {"step": 2})
    
    print(f"Updated User: {user_context.get(request_id)}")
    print(f"Updated Session: {session_context.get(request_id)}")

def test_complex_data_types():
    """Test with complex nested data structures"""
    print("\n=== Complex Data Types Test ===")
    request_id = "complex-002"
    
    # Nested dictionaries
    user_data = {
        "profile": {
            "personal": {"name": "Alice", "email": "alice@test.com"},
            "business": {"company": "TechCorp", "role": "procurement_manager"}
        },
        "preferences": {
            "categories": ["electronics", "office_supplies"],
            "budget_range": {"min": 1000, "max": 50000}
        }
    }
    
    # Complex session state
    session_data = {
        "rfq_workflow": {
            "current_step": "vendor_selection",
            "completed_steps": ["product_search", "requirement_gathering"],
            "data_collected": {
                "products": [
                    {"name": "Laptop", "qty": 10, "specs": {"ram": "16GB", "storage": "512GB"}},
                    {"name": "Monitor", "qty": 5, "specs": {"size": "24inch", "resolution": "4K"}}
                ],
                "delivery": {"date": "2024-02-15", "location": "Mumbai"}
            }
        }
    }
    
    user_context.set(request_id, user_data)
    session_context.set(request_id, session_data)
    
    print(f"Complex User Data: {json.dumps(user_context.get(request_id), indent=2)}")
    print(f"Complex Session Data: {json.dumps(session_context.get(request_id), indent=2)}")
    
    # Update nested data
    session_context.update(request_id, {
        "rfq_workflow": {
            **session_context.get(request_id)["rfq_workflow"],
            "current_step": "price_negotiation"
        }
    })
    
    print(f"Updated Session Step: {session_context.get(request_id)['rfq_workflow']['current_step']}")

def test_multiple_requests():
    """Test concurrent request handling"""
    print("\n=== Multiple Requests Test ===")
    
    requests = ["req-001", "req-002", "req-003"]
    
    for i, req_id in enumerate(requests):
        user_context.set(req_id, {"id": f"user_{i}", "phone": f"91987654321{i}"})
        session_context.set(req_id, {"workflow": f"workflow_{i}", "timestamp": f"2024-01-{15+i}"})
        param_context.set(req_id, {"search_term": f"product_{i}", "filters": {"price": i*1000}})
    
    # Verify isolation
    for req_id in requests:
        print(f"Request {req_id}:")
        print(f"  User: {user_context.get(req_id)}")
        print(f"  Session: {session_context.get(req_id)}")
        print(f"  Params: {param_context.get(req_id)}")

def test_data_types_variety():
    """Test different Python data types"""
    print("\n=== Data Types Variety Test ===")
    request_id = "types-003"
    
    # String
    context_manager.set(request_id, "string_data", "Hello World")
    
    # Integer
    context_manager.set(request_id, "int_data", 42)
    
    # Float
    context_manager.set(request_id, "float_data", 3.14159)
    
    # Boolean
    context_manager.set(request_id, "bool_data", True)
    
    # List
    context_manager.set(request_id, "list_data", [1, "two", 3.0, {"four": 4}])
    
    # Dictionary
    context_manager.set(request_id, "dict_data", {"key1": "value1", "nested": {"key2": "value2"}})
    
    # None
    context_manager.set(request_id, "none_data", None)
    
    # Print all types
    data_types = ["string_data", "int_data", "float_data", "bool_data", "list_data", "dict_data", "none_data"]
    for data_type in data_types:
        value = context_manager.get(request_id, data_type)
        print(f"{data_type}: {value} (type: {type(value).__name__})")

def test_cleanup_operations():
    """Test cleanup and deletion operations"""
    print("\n=== Cleanup Operations Test ===")
    request_id = "cleanup-004"
    
    # Set up data
    user_context.set(request_id, {"id": "cleanup_user"})
    session_context.set(request_id, {"workflow": "cleanup_test"})
    param_context.set(request_id, {"temp": "data"})
    
    print("Before cleanup:")
    print(f"  User: {user_context.get(request_id)}")
    print(f"  Session: {session_context.get(request_id)}")
    print(f"  Params: {param_context.get(request_id)}")
    
    # Delete individual contexts
    user_context.delete(request_id)
    print(f"After user delete: {user_context.get(request_id)}")
    
    # Clear entire request context
    context_manager.clear(request_id)
    print("After full clear:")
    print(f"  User: {user_context.get(request_id)}")
    print(f"  Session: {session_context.get(request_id)}")
    print(f"  Params: {param_context.get(request_id)}")

def test_rfq_workflow_simulation():
    """Simulate a complete RFQ workflow using context"""
    print("\n=== RFQ Workflow Simulation ===")
    request_id = "rfq-workflow-005"
    
    # Step 1: User starts RFQ
    user_context.set(request_id, {
        "phone": "919876543210",
        "name": "Procurement Manager",
        "company": "ABC Corp"
    })
    
    session_context.set(request_id, {
        "workflow": "rfq_creation",
        "step": "product_search",
        "rfq_data": {}
    })
    
    print("Step 1 - RFQ Started:")
    print(f"  User: {user_context.get(request_id)}")
    print(f"  Session: {session_context.get(request_id)}")
    
    # Step 2: Product selection
    session_context.update(request_id, {
        "step": "product_selection",
        "rfq_data": {
            "products": [{"name": "Laptop", "category": "Electronics"}]
        }
    })
    
    print("\nStep 2 - Product Selected:")
    print(f"  Session: {session_context.get(request_id)}")
    
    # Step 3: Quantity and specs
    current_session = session_context.get(request_id)
    current_session["rfq_data"]["products"][0].update({
        "quantity": 50,
        "specifications": {"RAM": "16GB", "Storage": "512GB SSD"}
    })
    session_context.update(request_id, {
        "step": "specifications",
        "rfq_data": current_session["rfq_data"]
    })
    
    print("\nStep 3 - Specifications Added:")
    print(f"  RFQ Data: {json.dumps(session_context.get(request_id)['rfq_data'], indent=2)}")
    
    # Step 4: Vendor selection
    session_context.update(request_id, {
        "step": "vendor_selection",
        "selected_vendors": ["Vendor A", "Vendor B", "Vendor C"]
    })
    
    print("\nStep 4 - Vendors Selected:")
    print(f"  Vendors: {session_context.get(request_id)['selected_vendors']}")

if __name__ == "__main__":
    print("Context Manager Testing Suite")
    print("=" * 50)
    
    test_basic_operations()
    test_complex_data_types()
    test_multiple_requests()
    test_data_types_variety()
    test_cleanup_operations()
    test_rfq_workflow_simulation()
    
    print("\n" + "=" * 50)
    print("All tests completed!")