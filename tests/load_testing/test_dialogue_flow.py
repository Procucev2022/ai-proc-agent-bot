#!/usr/bin/env python3
"""
Dialogue flow load testing script.

This script tests the multi-turn conversation capabilities of the chat service
by simulating realistic procurement conversations with multiple messages.
"""

import asyncio
import time
import json
import logging
import random
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from enum import Enum

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.services.chat_service import ChatService

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ConversationStage(Enum):
    """Stages of procurement conversation."""
    INITIAL_REQUEST = "initial_request"
    CLARIFICATION = "clarification"
    SPECIFICATION = "specification"
    CONFIRMATION = "confirmation"
    COMPLETION = "completion"
    MODIFICATION = "modification"

@dataclass
class ConversationMessage:
    """Single message in a conversation."""
    stage: ConversationStage
    message_type: str
    content: str
    expected_response_pattern: str
    delay_before: float = 0.0  # Delay before sending message

@dataclass
class ConversationFlow:
    """Complete conversation flow definition."""
    flow_id: str
    description: str
    user_phone: str
    messages: List[ConversationMessage]
    expected_duration: float

@dataclass
class ConversationResult:
    """Result of a single conversation test."""
    flow_id: str
    user_phone: str
    success: bool
    total_duration: float
    messages_sent: int
    messages_completed: int
    error_message: Optional[str] = None
    stage_timings: Dict[str, float] = None
    responses: List[Dict[str, Any]] = None

@dataclass
class DialogueFlowResults:
    """Overall dialogue flow test results."""
    total_conversations: int
    successful_conversations: int
    failed_conversations: int
    average_conversation_duration: float
    min_conversation_duration: float
    max_conversation_duration: float
    conversations_per_minute: float
    test_duration: float
    success_rate: float
    stage_performance: Dict[str, Dict[str, float]]

class DialogueFlowTester:
    """Load tester for dialogue flows."""
    
    def __init__(self):
        self.chat_service = ChatService()
        self.conversation_results: List[ConversationResult] = []
    
    def get_conversation_flows(self) -> List[ConversationFlow]:
        """Get predefined conversation flows for testing."""
        flows = []
        
        # Flow 1: Simple RFQ creation
        flows.append(ConversationFlow(
            flow_id="simple_rfq",
            description="Simple RFQ creation flow",
            user_phone="+919876543001",
            messages=[
                ConversationMessage(
                    stage=ConversationStage.INITIAL_REQUEST,
                    message_type="text",
                    content="I need 50 laptops for my office",
                    expected_response_pattern="clarification"
                ),
                ConversationMessage(
                    stage=ConversationStage.CLARIFICATION,
                    message_type="text",
                    content="Dell laptops with 16GB RAM and SSD",
                    expected_response_pattern="specification",
                    delay_before=1.0
                ),
                ConversationMessage(
                    stage=ConversationStage.SPECIFICATION,
                    message_type="text",
                    content="Needed by March 15th, deliver to Bangalore",
                    expected_response_pattern="confirmation",
                    delay_before=2.0
                ),
                ConversationMessage(
                    stage=ConversationStage.CONFIRMATION,
                    message_type="text",
                    content="Yes, please create the RFQ",
                    expected_response_pattern="completion",
                    delay_before=1.0
                )
            ],
            expected_duration=30.0
        ))
        
        # Flow 2: Complex multi-product RFQ
        flows.append(ConversationFlow(
            flow_id="multi_product_rfq",
            description="Multi-product RFQ creation",
            user_phone="+919876543002",
            messages=[
                ConversationMessage(
                    stage=ConversationStage.INITIAL_REQUEST,
                    message_type="text",
                    content="I need office furniture - 25 desks, 30 chairs, and 5 meeting tables",
                    expected_response_pattern="clarification"
                ),
                ConversationMessage(
                    stage=ConversationStage.CLARIFICATION,
                    message_type="text",
                    content="Ergonomic office chairs, wooden desks with drawers, conference tables for 8 people",
                    expected_response_pattern="specification",
                    delay_before=2.0
                ),
                ConversationMessage(
                    stage=ConversationStage.SPECIFICATION,
                    message_type="text",
                    content="Delivery by April 1st to Mumbai office, budget around 5 lakhs",
                    expected_response_pattern="confirmation",
                    delay_before=3.0
                ),
                ConversationMessage(
                    stage=ConversationStage.CONFIRMATION,
                    message_type="text",
                    content="Yes, create all three RFQs",
                    expected_response_pattern="completion",
                    delay_before=1.0
                )
            ],
            expected_duration=45.0
        ))
        
        # Flow 3: RFQ with modifications
        flows.append(ConversationFlow(
            flow_id="rfq_with_modifications",
            description="RFQ creation with modifications",
            user_phone="+919876543003",
            messages=[
                ConversationMessage(
                    stage=ConversationStage.INITIAL_REQUEST,
                    message_type="text",
                    content="I want to buy 100 printers",
                    expected_response_pattern="clarification"
                ),
                ConversationMessage(
                    stage=ConversationStage.CLARIFICATION,
                    message_type="text",
                    content="Laser printers, black and white, for office use",
                    expected_response_pattern="specification",
                    delay_before=1.5
                ),
                ConversationMessage(
                    stage=ConversationStage.SPECIFICATION,
                    message_type="text",
                    content="Needed by February 28th, deliver to Delhi",
                    expected_response_pattern="confirmation",
                    delay_before=2.0
                ),
                ConversationMessage(
                    stage=ConversationStage.MODIFICATION,
                    message_type="text",
                    content="Actually, make it 150 printers and change delivery to Chennai",
                    expected_response_pattern="confirmation",
                    delay_before=1.0
                ),
                ConversationMessage(
                    stage=ConversationStage.CONFIRMATION,
                    message_type="text",
                    content="Yes, proceed with the updated RFQ",
                    expected_response_pattern="completion",
                    delay_before=1.0
                )
            ],
            expected_duration=60.0
        ))
        
        # Flow 4: Interactive flow with buttons
        flows.append(ConversationFlow(
            flow_id="interactive_flow",
            description="Interactive flow with button responses",
            user_phone="+919876543004",
            messages=[
                ConversationMessage(
                    stage=ConversationStage.INITIAL_REQUEST,
                    message_type="text",
                    content="I need IT equipment for new office setup",
                    expected_response_pattern="clarification"
                ),
                ConversationMessage(
                    stage=ConversationStage.CLARIFICATION,
                    message_type="text",
                    content="20 computers, 5 servers, networking equipment",
                    expected_response_pattern="specification",
                    delay_before=2.0
                ),
                ConversationMessage(
                    stage=ConversationStage.SPECIFICATION,
                    message_type="text",
                    content="High-end workstations, Dell servers, Cisco networking, by March 30th",
                    expected_response_pattern="confirmation",
                    delay_before=3.0
                ),
                ConversationMessage(
                    stage=ConversationStage.CONFIRMATION,
                    message_type="interactive",
                    content='{"type": "button_reply", "button_reply": {"id": "confirm_rfq", "title": "Confirm RFQ"}}',
                    expected_response_pattern="completion",
                    delay_before=1.0
                )
            ],
            expected_duration=40.0
        ))
        
        # Flow 5: Status inquiry flow
        flows.append(ConversationFlow(
            flow_id="status_inquiry",
            description="RFQ status inquiry flow",
            user_phone="+919876543005",
            messages=[
                ConversationMessage(
                    stage=ConversationStage.INITIAL_REQUEST,
                    message_type="text",
                    content="What's the status of my RFQ for office supplies?",
                    expected_response_pattern="status_response"
                ),
                ConversationMessage(
                    stage=ConversationStage.CLARIFICATION,
                    message_type="text",
                    content="RFQ ID was GMT123456",
                    expected_response_pattern="status_details",
                    delay_before=1.0
                ),
                ConversationMessage(
                    stage=ConversationStage.SPECIFICATION,
                    message_type="text",
                    content="Thank you for the update",
                    expected_response_pattern="acknowledgment",
                    delay_before=1.0
                )
            ],
            expected_duration=15.0
        ))
        
        return flows
    
    async def run_conversation(self, flow: ConversationFlow) -> ConversationResult:
        """Run a single conversation flow."""
        logger.info(f"Starting conversation flow: {flow.flow_id}")
        
        start_time = time.time()
        stage_timings = {}
        responses = []
        
        try:
            for i, message in enumerate(flow.messages):
                # Apply delay before sending message
                if message.delay_before > 0:
                    await asyncio.sleep(message.delay_before)
                
                stage_start = time.time()
                
                # Send message
                response = await self.chat_service.process_message(
                    user_phone=flow.user_phone,
                    message_content=message.content,
                    message_type=message.message_type
                )
                
                stage_end = time.time()
                stage_duration = stage_end - stage_start
                
                # Record stage timing
                stage_timings[f"{message.stage.value}_{i}"] = stage_duration
                
                # Record response
                responses.append({
                    "message_index": i,
                    "stage": message.stage.value,
                    "response": response,
                    "duration": stage_duration
                })
                
                logger.info(f"  Stage {message.stage.value}: {stage_duration:.2f}s")
                
                # Check if there was an error
                if response.get("status") == "error":
                    logger.warning(f"Error in stage {message.stage.value}: {response.get('error')}")
            
            end_time = time.time()
            total_duration = end_time - start_time
            
            return ConversationResult(
                flow_id=flow.flow_id,
                user_phone=flow.user_phone,
                success=True,
                total_duration=total_duration,
                messages_sent=len(flow.messages),
                messages_completed=len(responses),
                stage_timings=stage_timings,
                responses=responses
            )
            
        except Exception as e:
            end_time = time.time()
            total_duration = end_time - start_time
            
            return ConversationResult(
                flow_id=flow.flow_id,
                user_phone=flow.user_phone,
                success=False,
                total_duration=total_duration,
                messages_sent=len(flow.messages),
                messages_completed=len(responses),
                error_message=str(e),
                stage_timings=stage_timings,
                responses=responses
            )
    
    async def run_concurrent_conversations(self, num_conversations: int = 10, max_concurrent: int = 5) -> DialogueFlowResults:
        """Run multiple conversations concurrently."""
        logger.info(f"Starting concurrent dialogue flow test with {num_conversations} conversations")
        
        flows = self.get_conversation_flows()
        
        # Create semaphore to limit concurrent conversations
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def limited_conversation(flow: ConversationFlow) -> ConversationResult:
            async with semaphore:
                return await self.run_conversation(flow)
        
        # Create tasks for concurrent execution
        tasks = []
        start_time = time.time()
        
        for i in range(num_conversations):
            # Select flow and create unique phone number
            base_flow = flows[i % len(flows)]
            
            # Create a copy with unique phone number
            unique_flow = ConversationFlow(
                flow_id=f"{base_flow.flow_id}_{i:03d}",
                description=base_flow.description,
                user_phone=f"+91987654{i:04d}",
                messages=base_flow.messages,
                expected_duration=base_flow.expected_duration
            )
            
            task = asyncio.create_task(limited_conversation(unique_flow))
            tasks.append(task)
        
        # Wait for all conversations to complete
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
        
        self.conversation_results.extend(successful_results)
        self.conversation_results.extend([r for r in failed_results if hasattr(r, 'flow_id')])
        
        # Calculate statistics
        durations = [r.total_duration for r in successful_results]
        
        if durations:
            avg_duration = sum(durations) / len(durations)
            min_duration = min(durations)
            max_duration = max(durations)
        else:
            avg_duration = min_duration = max_duration = 0
        
        conversations_per_minute = (num_conversations / total_duration) * 60 if total_duration > 0 else 0
        success_rate = len(successful_results) / num_conversations if num_conversations > 0 else 0
        
        # Calculate stage performance
        stage_performance = self._calculate_stage_performance(successful_results)
        
        return DialogueFlowResults(
            total_conversations=num_conversations,
            successful_conversations=len(successful_results),
            failed_conversations=len(failed_results),
            average_conversation_duration=avg_duration,
            min_conversation_duration=min_duration,
            max_conversation_duration=max_duration,
            conversations_per_minute=conversations_per_minute,
            test_duration=total_duration,
            success_rate=success_rate,
            stage_performance=stage_performance
        )
    
    def _calculate_stage_performance(self, results: List[ConversationResult]) -> Dict[str, Dict[str, float]]:
        """Calculate performance metrics for each conversation stage."""
        stage_performance = {}
        
        for result in results:
            if not result.stage_timings:
                continue
                
            for stage_key, duration in result.stage_timings.items():
                stage_name = stage_key.split('_')[0]  # Extract stage name
                
                if stage_name not in stage_performance:
                    stage_performance[stage_name] = []
                
                stage_performance[stage_name].append(duration)
        
        # Calculate stats for each stage
        stage_stats = {}
        for stage_name, durations in stage_performance.items():
            if durations:
                stage_stats[stage_name] = {
                    "avg_duration": sum(durations) / len(durations),
                    "min_duration": min(durations),
                    "max_duration": max(durations),
                    "count": len(durations)
                }
        
        return stage_stats
    
    async def run_stress_test(self, num_conversations: int = 20, max_concurrent: int = 10) -> DialogueFlowResults:
        """Run stress test with higher load."""
        logger.info(f"Starting dialogue flow stress test with {num_conversations} conversations")
        
        return await self.run_concurrent_conversations(num_conversations, max_concurrent)
    
    def save_results(self, results: DialogueFlowResults, filename: str = "dialogue_flow_results.json"):
        """Save test results to file."""
        try:
            results_dict = {
                "test_results": asdict(results),
                "conversation_results": [asdict(r) for r in self.conversation_results],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(filename, 'w') as f:
                json.dump(results_dict, f, indent=2, default=str)
            
            logger.info(f"Test results saved to {filename}")
            
        except Exception as e:
            logger.error(f"Error saving results: {e}")
    
    def print_results(self, results: DialogueFlowResults):
        """Print test results to console."""
        print("\n" + "="*60)
        print("DIALOGUE FLOW TEST RESULTS")
        print("="*60)
        print(f"Total Conversations: {results.total_conversations}")
        print(f"Successful: {results.successful_conversations}")
        print(f"Failed: {results.failed_conversations}")
        print(f"Success Rate: {results.success_rate:.2%}")
        print(f"Test Duration: {results.test_duration:.2f} seconds")
        print(f"Conversations per Minute: {results.conversations_per_minute:.2f}")
        print(f"Average Conversation Duration: {results.average_conversation_duration:.2f}s")
        print(f"Min Conversation Duration: {results.min_conversation_duration:.2f}s")
        print(f"Max Conversation Duration: {results.max_conversation_duration:.2f}s")
        
        if results.stage_performance:
            print("\nSTAGE PERFORMANCE:")
            print("-" * 40)
            for stage, stats in results.stage_performance.items():
                print(f"{stage.upper()}:")
                print(f"  Average: {stats['avg_duration']:.2f}s")
                print(f"  Min: {stats['min_duration']:.2f}s")
                print(f"  Max: {stats['max_duration']:.2f}s")
                print(f"  Count: {stats['count']}")
        
        print("="*60)

async def main():
    """Main function to run dialogue flow tests."""
    tester = DialogueFlowTester()
    
    print("Starting Dialogue Flow Load Tests...")
    
    # Test 1: Basic dialogue flow test
    print("\n1. Running basic dialogue flow test...")
    basic_results = await tester.run_concurrent_conversations(num_conversations=5, max_concurrent=3)
    tester.print_results(basic_results)
    tester.save_results(basic_results, "basic_dialogue_flow_results.json")
    
    # Test 2: Concurrent dialogue flows
    print("\n2. Running concurrent dialogue flows test...")
    concurrent_results = await tester.run_concurrent_conversations(num_conversations=10, max_concurrent=5)
    tester.print_results(concurrent_results)
    tester.save_results(concurrent_results, "concurrent_dialogue_flow_results.json")
    
    # Test 3: Stress test
    print("\n3. Running dialogue flow stress test...")
    stress_results = await tester.run_stress_test(num_conversations=15, max_concurrent=8)
    tester.print_results(stress_results)
    tester.save_results(stress_results, "dialogue_flow_stress_results.json")
    
    print("\nAll dialogue flow tests completed!")

if __name__ == "__main__":
    asyncio.run(main())