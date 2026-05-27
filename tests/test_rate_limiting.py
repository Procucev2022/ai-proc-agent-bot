#!/usr/bin/env python3
"""
Rate limiting test script.

This script tests the rate limiting functionality by making rapid requests
to the API endpoints and verifying that rate limits are enforced properly.
"""

import asyncio
import aiohttp
import time
import json
from typing import Dict, List

# Test configuration
BASE_URL = "http://localhost:8000"
ENDPOINTS = {
    "chat": {
        "url": f"{BASE_URL}/api/chat",
        "method": "POST",
        "data": {"message": "Hello", "phone": "919876543229"},
        "expected_limit": 20  # 20/minute
    },
    "upload": {
        "url": f"{BASE_URL}/api/upload-excel",
        "method": "POST",
        "data": {"phone": "919876543229"},
        "files": {"file": ("test.xlsx", b"fake excel content", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        "expected_limit": 5  # 5/minute
    },
    "health": {
        "url": f"{BASE_URL}/health",
        "method": "GET",
        "expected_limit": 100  # 100/hour default
    }
}


async def make_request(session: aiohttp.ClientSession, endpoint_config: Dict) -> Dict:
    """Make a single request to an endpoint."""
    try:
        if endpoint_config["method"] == "GET":
            async with session.get(endpoint_config["url"]) as response:
                return {
                    "status": response.status,
                    "headers": dict(response.headers),
                    "timestamp": time.time()
                }
        elif endpoint_config["method"] == "POST":
            if "files" in endpoint_config:
                # Handle file upload
                data = aiohttp.FormData()
                for key, value in endpoint_config["data"].items():
                    data.add_field(key, value)
                
                filename, content, content_type = endpoint_config["files"]["file"]
                data.add_field("file", content, filename=filename, content_type=content_type)
                
                async with session.post(endpoint_config["url"], data=data) as response:
                    return {
                        "status": response.status,
                        "headers": dict(response.headers),
                        "timestamp": time.time()
                    }
            else:
                # Handle JSON request
                async with session.post(endpoint_config["url"], json=endpoint_config["data"]) as response:
                    return {
                        "status": response.status,
                        "headers": dict(response.headers),
                        "timestamp": time.time()
                    }
    except Exception as e:
        return {
            "error": str(e),
            "timestamp": time.time()
        }


async def test_rate_limiting(endpoint_name: str, num_requests: int = 30, delay: float = 0.1):
    """Test rate limiting for a specific endpoint."""
    print(f"\n=== Testing {endpoint_name} endpoint ===")
    
    endpoint_config = ENDPOINTS[endpoint_name]
    results = []
    
    async with aiohttp.ClientSession() as session:
        # Make rapid requests
        for i in range(num_requests):
            result = await make_request(session, endpoint_config)
            results.append(result)
            
            print(f"Request {i+1:2d}: Status {result.get('status', 'ERROR')}", end="")
            
            # Show rate limit headers if available
            headers = result.get("headers", {})
            if "x-ratelimit-limit" in headers:
                print(f" | Limit: {headers['x-ratelimit-limit']}", end="")
            if "x-ratelimit-remaining" in headers:
                print(f" | Remaining: {headers['x-ratelimit-remaining']}", end="")
            if "retry-after" in headers:
                print(f" | Retry-After: {headers['retry-after']}", end="")
            
            print()
            
            # Small delay between requests
            await asyncio.sleep(delay)
    
    # Analyze results
    analyze_results(endpoint_name, results)


def analyze_results(endpoint_name: str, results: List[Dict]):
    """Analyze the test results."""
    print(f"\n--- Analysis for {endpoint_name} ---")
    
    success_count = sum(1 for r in results if r.get("status") == 200)
    rate_limited_count = sum(1 for r in results if r.get("status") == 429)
    error_count = sum(1 for r in results if "error" in r)
    
    print(f"Total requests: {len(results)}")
    print(f"Successful (200): {success_count}")
    print(f"Rate limited (429): {rate_limited_count}")
    print(f"Errors: {error_count}")
    
    # Show first few rate limited responses
    rate_limited = [r for r in results if r.get("status") == 429]
    if rate_limited:
        print(f"\nFirst rate limited response headers:")
        headers = rate_limited[0].get("headers", {})
        for key, value in headers.items():
            if "rate" in key.lower() or "retry" in key.lower():
                print(f"  {key}: {value}")


async def test_different_ips():
    """Test that rate limiting works per IP by simulating different IPs."""
    print("\n=== Testing Different IP Addresses ===")
    
    # This would require running through a proxy or using different network interfaces
    # For now, we'll just show the concept
    print("Note: This test requires proxy setup or multiple network interfaces")
    print("In a real scenario, requests from different IPs should have separate rate limits")


def test_redis_connection():
    """Test Redis connection for rate limiting storage."""
    print("\n=== Testing Redis Connection ===")
    
    try:
        import redis
        from app.config import get_settings
        
        settings = get_settings()
        r = redis.from_url(settings.redis_url)
        r.ping()
        print("Redis connection successful")
        
        # Check for rate limiting keys
        keys = r.keys("rate_limit:*")
        print(f"Found {len(keys)} rate limiting keys in Redis")
        
        if keys:
            print("Sample keys:")
            for key in keys[:5]:
                value = r.get(key)
                ttl = r.ttl(key)
                print(f"  {key.decode()}: {value.decode() if value else 'None'} (TTL: {ttl}s)")
                
    except Exception as e:
        print(f"Redis connection failed: {e}")
        print("Rate limiting will fall back to in-memory storage")


async def run_all_tests():
    """Run all rate limiting tests."""
    print("Starting Rate Limiting Tests")
    print("=" * 50)
    
    # Test Redis connection first
    test_redis_connection()
    
    # Test each endpoint
    await test_rate_limiting("health", num_requests=10, delay=0.05)
    await test_rate_limiting("chat", num_requests=25, delay=0.1)
    
    # Note: Upload test might fail if server isn't configured for file uploads
    print("\nUpload test may fail if server isn't running or configured properly")
    try:
        await test_rate_limiting("upload", num_requests=8, delay=0.2)
    except Exception as e:
        print(f"Upload test failed: {e}")
    
    print("\nRate limiting tests completed!")
    print("\nTo run these tests:")
    print("1. Start your FastAPI server: uvicorn app.main:app --reload")
    print("2. Run this script: python test_rate_limiting.py")


if __name__ == "__main__":
    asyncio.run(run_all_tests())