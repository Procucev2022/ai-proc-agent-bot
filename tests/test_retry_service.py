"""
Tests for the retry service functionality.

Tests the retry mechanism with various failure scenarios,
success cases, and configuration options.
"""

import asyncio
import sys
from pathlib import Path

# Add the project root to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pytest
from unittest.mock import AsyncMock
from app.tools.retry_service import RetryService


class TestRetryService:
    """Test cases for the RetryService."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.retry_service = RetryService(max_retries=2, initial_delay=0.1)
    
    @pytest.mark.asyncio
    async def test_retry_success_on_first_attempt(self):
        """Test successful function call on first attempt."""
        async def successful_function():
            return {"success": True, "data": "test"}
        
        result = await self.retry_service.retry_with_backoff(successful_function)
        
        assert result["success"] is True
        assert result["attempts"] == 1
        assert result["result"]["success"] is True
    
    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self):
        """Test successful function call after some failures."""
        call_count = 0
        
        async def eventually_successful_function():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise Exception("Temporary failure")
            return {"success": True, "data": "test"}
        
        result = await self.retry_service.retry_with_backoff(eventually_successful_function)
        
        assert result["success"] is True
        assert result["attempts"] == 2
        assert call_count == 2
    
    @pytest.mark.asyncio
    async def test_retry_max_attempts_exceeded(self):
        """Test function that fails all retry attempts."""
        call_count = 0
        
        async def always_failing_function():
            nonlocal call_count
            call_count += 1
            raise Exception("Persistent failure")
        
        result = await self.retry_service.retry_with_backoff(always_failing_function)
        
        assert result["success"] is False
        assert result["attempts"] == 3  # max_retries + 1
        assert call_count == 3
        assert "Persistent failure" in result["error"]
    
    @pytest.mark.asyncio
    async def test_retry_with_unsuccessful_result(self):
        """Test function that returns unsuccessful result."""
        async def unsuccessful_function():
            return {"success": False, "error": "Function failed"}
        
        result = await self.retry_service.retry_with_backoff(unsuccessful_function)
        
        assert result["success"] is False
        assert result["attempts"] == 3  # Should retry on unsuccessful results
    
    @pytest.mark.asyncio
    async def test_retry_configuration(self):
        """Test retry service with custom configuration."""
        custom_retry_service = RetryService(max_retries=1, initial_delay=0.05)
        
        call_count = 0
        
        async def failing_function():
            nonlocal call_count
            call_count += 1
            raise Exception("Test failure")
        
        result = await custom_retry_service.retry_with_backoff(failing_function)
        
        assert result["success"] is False
        assert result["attempts"] == 2  # max_retries(1) + 1
        assert call_count == 2


@pytest.mark.asyncio
async def test_retry_service_integration():
    """Integration test for retry service."""
    retry_service = RetryService(max_retries=1, initial_delay=0.01)
    
    # Mock a WhatsApp-like function
    attempt_count = 0
    
    async def mock_whatsapp_send():
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            # First attempt fails
            return {"success": False, "error": "Network timeout"}
        else:
            # Second attempt succeeds
            return {"success": True, "message_id": "12345"}
    
    result = await retry_service.retry_with_backoff(mock_whatsapp_send)
    
    assert result["success"] is True
    assert result["attempts"] == 2
    assert attempt_count == 2
    assert result["result"]["message_id"] == "12345"


if __name__ == "__main__":
    # Run tests manually
    async def run_tests():
        test_service = TestRetryService()
        
        print("Running retry service tests...")
        
        test_service.setup_method()
        await test_service.test_retry_success_on_first_attempt()
        print("✓ Success on first attempt test passed")
        
        test_service.setup_method()
        await test_service.test_retry_success_after_failures()
        print("✓ Success after failures test passed")
        
        test_service.setup_method()
        await test_service.test_retry_max_attempts_exceeded()
        print("✓ Max attempts exceeded test passed")
        
        test_service.setup_method()
        await test_service.test_retry_with_unsuccessful_result()
        print("✓ Unsuccessful result test passed")
        
        test_service.setup_method()
        await test_service.test_retry_configuration()
        print("✓ Custom configuration test passed")
        
        await test_retry_service_integration()
        print("✓ Integration test passed")
        
        print("\nAll tests passed! 🎉")
    
    asyncio.run(run_tests())