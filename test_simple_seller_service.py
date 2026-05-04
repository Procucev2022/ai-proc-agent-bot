#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple focused test for the current seller_service.py implementation.
"""

import sys
import os

def test_current_implementation():
    """Test the current seller_service.py implementation."""
    print("Testing current seller_service.py implementation...")
    
    try:
        # Read the seller service file
        with open('app/services/seller_service.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find the _process_rfq_email_requests method
        lines = content.split('\n')
        in_method = False
        method_lines = []
        
        for line in lines:
            if 'def _process_rfq_email_requests(' in line:
                in_method = True
            
            if in_method:
                method_lines.append(line)
                
                # End of method (next method starts or class ends)
                if line.strip().startswith('def ') and 'def _process_rfq_email_requests(' not in line:
                    break
                if line.strip().startswith('class ') and in_method:
                    break
        
        method_content = '\n'.join(method_lines)
        
        print("Found _process_rfq_email_requests method")
        print("=" * 60)
        
        # Check for acknowledgment message sending
        ack_send_lines = [line.strip() for line in method_lines if 'ack_message' in line and 'send_message' in line]
        status_send_lines = [line.strip() for line in method_lines if 'status_message' in line and 'send_message' in line]
        
        print(f"Acknowledgment message sends: {len(ack_send_lines)}")
        for line in ack_send_lines:
            print(f"  - {line}")
        
        print(f"Status message sends: {len(status_send_lines)}")
        for line in status_send_lines:
            print(f"  - {line}")
        
        # Check the order and parameters
        print("\nAnalysis:")
        
        # Look for the issue: sending ack_message instead of status_message at the end
        wrong_send = any('send_message(user.phone_number, ack_message)' in line for line in method_lines)
        correct_send = any('send_message(user.phone_number, status_message' in line for line in method_lines)
        
        if wrong_send:
            print("❌ ISSUE FOUND: Sending ack_message instead of status_message at the end")
            print("   This will send the acknowledgment message twice!")
        
        if correct_send:
            print("✅ Found correct status_message sending")
        
        # Check for clear_pending_reply usage
        clear_pending_false = any('clear_pending_reply=False' in line for line in method_lines)
        
        if clear_pending_false:
            print("✅ Found clear_pending_reply=False usage")
        else:
            print("❌ No clear_pending_reply=False found")
        
        return not wrong_send and correct_send
        
    except FileNotFoundError:
        print("❌ seller_service.py not found")
        return False
    except Exception as e:
        print(f"❌ Error analyzing file: {e}")
        return False

def test_message_flow_logic():
    """Test the logical flow of messages."""
    print("\nTesting message flow logic...")
    
    try:
        with open('app/services/seller_service.py', 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find the method and analyze the flow
        method_start = content.find('def _process_rfq_email_requests(')
        if method_start == -1:
            print("❌ Method not found")
            return False
        
        # Get the method content (rough extraction)
        method_end = content.find('\n    def ', method_start + 1)
        if method_end == -1:
            method_end = len(content)
        
        method_content = content[method_start:method_end]
        
        # Check the logical flow
        steps = []
        
        if 'ack_message = await self.response_helpers.generate_seller_contextual_response(ack_context)' in method_content:
            steps.append("1. Generate acknowledgment message")
        
        if 'batch_result = await self.seller_api_service.send_rfq_email(' in method_content:
            steps.append("2. Send RFQ email batch")
        
        if 'status_message = await self.response_helpers.generate_seller_contextual_response(status_context)' in method_content:
            steps.append("3. Generate status message")
        
        # Check what gets sent
        if 'await self.whatsapp_service.send_message(user.phone_number, ack_message)' in method_content:
            steps.append("4. Send acknowledgment message")
        
        if 'await self.whatsapp_service.send_message(user.phone_number, status_message' in method_content:
            steps.append("5. Send status message")
        
        print("Current flow:")
        for step in steps:
            print(f"  {step}")
        
        # The correct flow should be:
        # 1. Generate ack message
        # 2. Send ack message (optional, for immediate feedback)
        # 3. Process batch
        # 4. Generate status message
        # 5. Send status message with clear_pending_reply=False
        
        expected_steps = [
            "1. Generate acknowledgment message",
            "2. Send RFQ email batch", 
            "3. Generate status message"
        ]
        
        has_correct_flow = all(step in steps for step in expected_steps)
        
        if has_correct_flow:
            print("✅ Basic flow structure is correct")
        else:
            print("❌ Flow structure has issues")
        
        return has_correct_flow
        
    except Exception as e:
        print(f"❌ Error testing flow: {e}")
        return False

def suggest_fix():
    """Suggest the correct implementation."""
    print("\n" + "="*60)
    print("SUGGESTED FIX:")
    print("="*60)
    
    print("""
The issue in your code is on this line:
    await self.whatsapp_service.send_message(user.phone_number, ack_message)

It should be:
    await self.whatsapp_service.send_message(user.phone_number, status_message, clear_pending_reply=False)

Current flow:
1. Generate ack_message ✅
2. Process RFQ batch ✅  
3. Generate status_message ✅
4. Send ack_message again ❌ (WRONG!)

Correct flow should be:
1. Generate ack_message ✅
2. Send ack_message (optional, for immediate feedback)
3. Process RFQ batch ✅
4. Generate status_message ✅
5. Send status_message with clear_pending_reply=False ✅

The fix is to change the last send_message call to send status_message instead of ack_message.
""")

def run_simple_test():
    """Run the simple focused test."""
    print("="*60)
    print("SIMPLE SELLER SERVICE TEST")
    print("="*60)
    
    test1_passed = test_current_implementation()
    test2_passed = test_message_flow_logic()
    
    print("\n" + "="*60)
    print("RESULTS:")
    print("="*60)
    
    if test1_passed and test2_passed:
        print("✅ All tests passed - Implementation looks correct!")
    else:
        print("❌ Issues found in implementation")
        suggest_fix()
    
    return test1_passed and test2_passed

if __name__ == "__main__":
    success = run_simple_test()
    sys.exit(0 if success else 1)