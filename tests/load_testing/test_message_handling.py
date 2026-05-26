#!/usr/bin/env python3
"""
Basic load testing script for message handling.

This script tests the basic message processing capabilities of the chat service
by sending multiple concurrent messages and measuring response times.
"""

import asyncio
import time
import json
import logging
from typing import Dict, List, Any
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor
import statistics

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.services.chat_service import ChatService
from app.services.whatsapp_webhook_service import WhatsAppWebhookService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class TestMessage:
    """Test message structure for load testing."""
    phone_number: str
    message_type: str
    content: str
    expected_response_type: str

@dataclass
class TestResult:
    """Result structure for individual test."""
    message_id: str
    phone_number: str
    success: bool
    response_time: float
    error_message: str = None
    response_data: Dict[str, Any] = None

@dataclass
class LoadTestResults:
    """Overall load test results."""
    total_messages: int
    successful_messages: int
    failed_messages: int
    average_response_time: float
    min_response_time: float
    max_response_time: float
    messages_per_second: float
    test_duration: float
    error_rate: float

class MessageLoadTester:
    """Load tester for message handling."""
    
    def __init__(self):
        self.chat_service = ChatService()
        self.webhook_service = WhatsAppWebhookService()
        self.test_results: List[TestResult] = []
    
    def get_test_messages(self) -> List[TestMessage]:
        """Get list of test messages for load testing."""
        base_messages = [
            TestMessage(
                phone_number="+919876543210",
                message_type="text",
                content="I need 100 laptops for my office",
                expected_response_type="buy_something"
            ),
            TestMessage(
                phone_number="+919876543211",
                message_type="text", 
                content="Hello, can you help me with procurement?",
                expected_response_type="general_inquiry"
            ),
            TestMessage(
                phone_number="+919876543212",
                message_type="text",
                content="I want to buy 50 desks and 25 chairs",
                expected_response_type="buy_something"
            ),
            TestMessage(
                phone_number="+919876543213",
                message_type="text",
                content="What's the status of my RFQ?",
                expected_response_type="rfq_status_check"
            ),
            TestMessage(
                phone_number="+919876543214",
                message_type="text",
                content="I need office supplies - pens, paper, staplers",
                expected_response_type="buy_something"
            ),
            TestMessage(
                phone_number="+919876543215",
                message_type="text",
                content="Can you tell me about your services?",
                expected_response_type="general_inquiry"
            ),
            TestMessage(
                phone_number="+919876543216",
                message_type="text",
                content="I want to purchase 200 units of LED monitors",
                expected_response_type="buy_something"
            ),
            TestMessage(
                phone_number="+919876543217",
                message_type="interactive",
                content='{"type": "button_reply", "button_reply": {"id": "confirm_rfq", "title": "Confirm"}}',
                expected_response_type="confirmation_response"
            ),
            TestMessage(
                phone_number="+919876543218",
                message_type="text",
                content="Need printer cartridges urgently",
                expected_response_type="buy_something"
            ),
            TestMessage(
                phone_number="+919876543219",
                message_type="text",
                content="Help me create an RFQ for IT equipment",
                expected_response_type="buy_something"
            )
        ]
        
        # Generate multiple variations of each message for load testing
        test_messages = []
        for i in range(5):  # 5 variations
            for msg in base_messages:
                test_messages.append(TestMessage(
                    phone_number=f"+9198765432{i:02d}",
                    message_type=msg.message_type,
                    content=msg.content,
                    expected_response_type=msg.expected_response_type
                ))
        
        return test_messages
    
    async def send_message_async(self, message: TestMessage, message_id: str) -> TestResult:
        """Send a single message and measure response time."""
        start_time = time.time()
        
        try:
            # Test direct chat service
            response = await self.chat_service.process_message(
                user_phone=message.phone_number,
                message_content=message.content,
                message_type=message.message_type
            )
            
            end_time = time.time()
            response_time = end_time - start_time
            
            return TestResult(
                message_id=message_id,
                phone_number=message.phone_number,
                success=True,
                response_time=response_time,
                response_data=response
            )
            
        except Exception as e:
            end_time = time.time()
            response_time = end_time - start_time
            
            return TestResult(
                message_id=message_id,
                phone_number=message.phone_number,
                success=False,
                response_time=response_time,
                error_message=str(e)
            )
    
    async def run_concurrent_test(self, num_messages: int = 50, max_concurrent: int = 10) -> LoadTestResults:
        """Run concurrent message load test."""
        logger.info(f"Starting concurrent load test with {num_messages} messages, {max_concurrent} concurrent")
        
        test_messages = self.get_test_messages()
        
        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def limited_send(message: TestMessage, message_id: str) -> TestResult:
            async with semaphore:
                return await self.send_message_async(message, message_id)
        
        # Create tasks for concurrent execution
        tasks = []
        start_time = time.time()
        
        for i in range(num_messages):
            message = test_messages[i % len(test_messages)]
            message_id = f"msg_{i:04d}"
            
            task = asyncio.create_task(limited_send(message, message_id))
            tasks.append(task)
        
        # Wait for all tasks to complete
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        end_time = time.time()
        total_duration = end_time - start_time
        
        # Process results
        successful_results = []
        failed_results = []
        
        for result in results:
            if isinstance(result, Exception):
                failed_results.append(result)
            elif result.success:
                successful_results.append(result)
            else:
                failed_results.append(result)
        
        # Calculate statistics
        response_times = [r.response_time for r in successful_results]
        
        if response_times:
            avg_response_time = statistics.mean(response_times)
            min_response_time = min(response_times)
            max_response_time = max(response_times)
        else:
            avg_response_time = min_response_time = max_response_time = 0
        
        messages_per_second = num_messages / total_duration if total_duration > 0 else 0
        error_rate = len(failed_results) / num_messages if num_messages > 0 else 0
        
        self.test_results.extend(successful_results)
        self.test_results.extend([r for r in failed_results if hasattr(r, 'message_id')])
        
        return LoadTestResults(
            total_messages=num_messages,
            successful_messages=len(successful_results),
            failed_messages=len(failed_results),
            average_response_time=avg_response_time,
            min_response_time=min_response_time,
            max_response_time=max_response_time,
            messages_per_second=messages_per_second,
            test_duration=total_duration,
            error_rate=error_rate
        )
    
    async def run_webhook_test(self, num_messages: int = 30) -> LoadTestResults:
        """Test webhook processing performance."""
        logger.info(f"Starting webhook load test with {num_messages} messages")
        
        test_messages = self.get_test_messages()
        
        async def send_webhook_message(message: TestMessage, message_id: str) -> TestResult:
            start_time = time.time()
            
            try:
                webhook_data = {
                    "type": message.message_type,
                    "from": message.phone_number,
                    "content": message.content
                }
                
                response = await self.webhook_service.process_webhook(webhook_data)
                
                end_time = time.time()
                response_time = end_time - start_time
                
                return TestResult(
                    message_id=message_id,
                    phone_number=message.phone_number,
                    success=response.get("status") == "success",
                    response_time=response_time,
                    response_data=response
                )
                
            except Exception as e:
                end_time = time.time()
                response_time = end_time - start_time
                
                return TestResult(
                    message_id=message_id,
                    phone_number=message.phone_number,
                    success=False,
                    response_time=response_time,
                    error_message=str(e)
                )
        
        # Run webhook tests sequentially to avoid overwhelming the service
        results = []
        start_time = time.time()
        
        for i in range(num_messages):
            message = test_messages[i % len(test_messages)]
            message_id = f"webhook_msg_{i:04d}"
            
            result = await send_webhook_message(message, message_id)
            results.append(result)
            
            # Small delay between requests
            await asyncio.sleep(0.1)
        
        end_time = time.time()
        total_duration = end_time - start_time
        
        # Process results
        successful_results = [r for r in results if r.success]
        failed_results = [r for r in results if not r.success]
        
        response_times = [r.response_time for r in successful_results]
        
        if response_times:
            avg_response_time = statistics.mean(response_times)
            min_response_time = min(response_times)
            max_response_time = max(response_times)
        else:
            avg_response_time = min_response_time = max_response_time = 0
        
        messages_per_second = num_messages / total_duration if total_duration > 0 else 0
        error_rate = len(failed_results) / num_messages if num_messages > 0 else 0
        
        return LoadTestResults(
            total_messages=num_messages,
            successful_messages=len(successful_results),
            failed_messages=len(failed_results),
            average_response_time=avg_response_time,
            min_response_time=min_response_time,
            max_response_time=max_response_time,
            messages_per_second=messages_per_second,
            test_duration=total_duration,
            error_rate=error_rate
        )
    
    def save_results(self, results: LoadTestResults, filename: str = "load_test_results.json"):
        """Save test results to file."""
        try:
            results_dict = {
                "test_results": asdict(results),
                "individual_results": [asdict(r) for r in self.test_results],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(filename, 'w') as f:
                json.dump(results_dict, f, indent=2)
            
            logger.info(f"Test results saved to {filename}")
            
        except Exception as e:
            logger.error(f"Error saving results: {e}")
    
    def print_results(self, results: LoadTestResults):
        """Print test results to console."""
        print("\n" + "="*60)
        print("LOAD TEST RESULTS")
        print("="*60)
        print(f"Total Messages: {results.total_messages}")
        print(f"Successful: {results.successful_messages}")
        print(f"Failed: {results.failed_messages}")
        print(f"Error Rate: {results.error_rate:.2%}")
        print(f"Test Duration: {results.test_duration:.2f} seconds")
        print(f"Messages per Second: {results.messages_per_second:.2f}")
        print(f"Average Response Time: {results.average_response_time:.3f}s")
        print(f"Min Response Time: {results.min_response_time:.3f}s")
        print(f"Max Response Time: {results.max_response_time:.3f}s")
        print("="*60)

async def main():
    """Main function to run load tests."""
    tester = MessageLoadTester()
    
    print("Starting Message Handling Load Tests...")
    
    # Test 1: Concurrent message processing
    print("\n1. Running concurrent message test...")
    concurrent_results = await tester.run_concurrent_test(num_messages=50, max_concurrent=10)
    tester.print_results(concurrent_results)
    tester.save_results(concurrent_results, "concurrent_test_results.json")
    
    # Test 2: Webhook processing
    print("\n2. Running webhook processing test...")
    webhook_results = await tester.run_webhook_test(num_messages=30)
    tester.print_results(webhook_results)
    tester.save_results(webhook_results, "webhook_test_results.json")
    
    # Test 3: Stress test with higher concurrency
    print("\n3. Running stress test...")
    stress_results = await tester.run_concurrent_test(num_messages=100, max_concurrent=20)
    tester.print_results(stress_results)
    tester.save_results(stress_results, "stress_test_results.json")
    
    print("\nAll load tests completed!")

if __name__ == "__main__":
    asyncio.run(main())