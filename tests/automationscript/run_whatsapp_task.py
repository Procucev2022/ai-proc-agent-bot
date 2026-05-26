#!/usr/bin/env python3
"""
Manual runner for WhatsApp Report Automation Task
"""

import sys
import os
import asyncio
from datetime import datetime, date, timedelta

# Add the app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app.tasks.whatsapp_report_automation_task import run_whatsapp_report_automation_async

def run_task_manually(target_date=None):
    """Run the WhatsApp report automation task manually"""
    print("Starting WhatsApp Report Automation Task...")
    print(f"Target date: {target_date or 'yesterday'}")
    
    try:
        # Create a mock self object for the task
        class MockSelf:
            def retry(self, countdown=300):
                raise Exception("Task failed and would retry")
        
        mock_self = MockSelf()
        
        # Run the async task
        result = asyncio.run(run_whatsapp_report_automation_async(mock_self, target_date))
        
        print("\n" + "="*50)
        print("TASK COMPLETED")
        print("="*50)
        print(f"Status: {result.get('status', 'unknown')}")
        print(f"Target Date: {result.get('target_date', 'unknown')}")
        
        if result.get('status') == 'completed':
            print(f"Analytics Sessions: {result.get('analytics_sessions', 0)}")
            print(f"Steps Completed: {', '.join(result.get('steps_completed', []))}")
            print(f"Message: {result.get('message', '')}")
        else:
            print(f"Error: {result.get('error', 'Unknown error')}")
            print(f"Failed Step: {result.get('step', 'unknown')}")
        
        print(f"Timestamp: {result.get('timestamp', '')}")
        
        return result
        
    except Exception as e:
        print(f"\nTask execution failed: {e}")
        import traceback
        traceback.print_exc()
        return {"status": "failed", "error": str(e)}

if __name__ == "__main__":
    # Get target date from command line argument if provided
    target_date = None
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
        print(f"Using provided date: {target_date}")
    
    result = run_task_manually(target_date)
    
    # Exit with appropriate code
    if result.get('status') == 'completed':
        sys.exit(0)
    else:
        sys.exit(1)