#!/bin/bash

# Simple rate limiting test using curl
# This script makes multiple requests to test rate limiting

BASE_URL="http://localhost:8000"
CHAT_ENDPOINT="$BASE_URL/api/chat"
HEALTH_ENDPOINT="$BASE_URL/health"

echo "Testing Rate Limiting with curl"
echo "================================"

# Test health endpoint (100/hour limit)
echo "Testing /health endpoint..."
for i in {1..5}; do
    echo "Request $i:"
    curl -s -w "Status: %{http_code} | Time: %{time_total}s\n" \
         -H "Content-Type: application/json" \
         "$HEALTH_ENDPOINT" \
         -o /dev/null
    sleep 0.1
done

echo ""
echo "Testing /api/chat endpoint (20/minute limit)..."
echo "Making 25 requests quickly to trigger rate limiting..."

# Test chat endpoint (20/minute limit) - this should trigger rate limiting
for i in {1..25}; do
    echo "Chat Request $i:"
    curl -s -w "Status: %{http_code} | Time: %{time_total}s\n" \
         -H "Content-Type: application/json" \
         -d '{"message": "Hello", "phone": "919876543229"}' \
         "$CHAT_ENDPOINT" \
         -o /dev/null
    sleep 0.05
done

echo ""
echo "Testing with detailed headers..."
echo "Making one request to see rate limit headers:"

curl -v -X POST "$CHAT_ENDPOINT" \
     -H "Content-Type: application/json" \
     -d '{"message": "Hello", "phone": "919876543229"}' \
     2>&1 | grep -E "(HTTP|X-RateLimit|Retry-After)"

echo ""
echo "Rate limiting test completed!"
echo "Look for HTTP 429 status codes in the output above"
echo "Check for X-RateLimit-* headers in the detailed output"