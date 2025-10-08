"""
Context Manager Unit Tests
Different testing samples for context functionality
"""

from .context_manager import context_manager
from .context_controller import session_context, user_context, param_context

def test_string_context():
    """Test string data storage"""
    request_id = "str-001"
    context_manager.set(request_id, "message", "Hello WhatsApp Bot")
    assert context_manager.get(request_id, "message") == "Hello WhatsApp Bot"
    print("✓ String context test passed")

def test_dict_context():
    """Test dictionary data storage"""
    request_id = "dict-002"
    user_data = {"phone": "919876543210", "name": "Test User", "role": "buyer"}
    user_context.set(request_id, user_data)
    retrieved = user_context.get(request_id)
    assert retrieved["phone"] == "919876543210"
    print("✓ Dictionary context test passed")

def test_list_context():
    """Test list data storage"""
    request_id = "list-003"
    products = ["laptop", "mouse", "keyboard", "monitor"]
    param_context.set(request_id, products)
    assert len(param_context.get(request_id)) == 4
    print("✓ List context test passed")

def test_nested_context():
    """Test nested data structures"""
    request_id = "nested-004"
    rfq_data = {
        "rfq_id": "RFQ001",
        "items": [
            {"name": "Laptop", "qty": 10, "specs": {"ram": "16GB"}},
            {"name": "Mouse", "qty": 20, "specs": {"type": "wireless"}}
        ],
        "delivery": {"date": "2024-02-15", "location": "Mumbai"}
    }
    session_context.set(request_id, rfq_data)
    retrieved = session_context.get(request_id)
    assert retrieved["items"][0]["specs"]["ram"] == "16GB"
    print("✓ Nested context test passed")

def test_update_context():
    """Test context update functionality"""
    request_id = "update-005"
    initial_data = {"step": 1, "workflow": "rfq"}
    session_context.set(request_id, initial_data)
    
    session_context.update(request_id, {"step": 2, "status": "active"})
    updated = session_context.get(request_id)
    
    assert updated["step"] == 2
    assert updated["workflow"] == "rfq"
    assert updated["status"] == "active"
    print("✓ Update context test passed")

def test_multiple_requests():
    """Test isolation between different requests"""
    req1, req2 = "multi-001", "multi-002"
    
    user_context.set(req1, {"name": "User1", "phone": "111"})
    user_context.set(req2, {"name": "User2", "phone": "222"})
    
    assert user_context.get(req1)["name"] == "User1"
    assert user_context.get(req2)["name"] == "User2"
    print("✓ Multiple requests isolation test passed")

def test_cleanup():
    """Test context cleanup"""
    request_id = "cleanup-006"
    
    user_context.set(request_id, {"temp": "data"})
    session_context.set(request_id, {"temp": "session"})
    
    # Clear all context for request
    context_manager.clear(request_id)
    
    assert user_context.get(request_id) is None
    assert session_context.get(request_id) is None
    print("✓ Cleanup test passed")

def test_boolean_numeric():
    """Test boolean and numeric data types"""
    request_id = "types-007"
    
    context_manager.set(request_id, "is_active", True)
    context_manager.set(request_id, "count", 42)
    context_manager.set(request_id, "price", 99.99)
    
    assert context_manager.get(request_id, "is_active") is True
    assert context_manager.get(request_id, "count") == 42
    assert context_manager.get(request_id, "price") == 99.99
    print("✓ Boolean/Numeric types test passed")

def run_all_tests():
    """Run all context tests"""
    print("Running Context Manager Tests...")
    print("-" * 40)
    
    test_string_context()
    test_dict_context()
    test_list_context()
    test_nested_context()
    test_update_context()
    test_multiple_requests()
    test_cleanup()
    test_boolean_numeric()
    
    print("-" * 40)
    print("All context tests completed successfully!")

if __name__ == "__main__":
    run_all_tests()