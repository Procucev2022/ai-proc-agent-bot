"""
WhatsApp Report Automation Task.

This task orchestrates the sequential execution of three existing scripts:
1. conversation_analytics_service.py - Generates analytics data
2. enhanced_excel_report_service.py - Generates Excel report and saves to disk
3. export_excel_task.py - Sends the generated Excel report via email

The task ensures each step completes before the next starts and is not parallel.
"""

import logging
import os
import asyncio
import pandas as pd
import base64
from typing import Dict, Any
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from datetime import datetime, date, timedelta

from app.services.conversation_analytics_service import ConversationAnalyticsService
from app.services.enhanced_excel_report_service import EnhancedExcelReportService
from app.tasks.export_excel_task import  _send_excel_reports_with_api_session
from app.services.email_service import EmailService
from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    queue='report_automation',
    priority=5,  # Medium priority
    time_limit=7200,        # 2 hrs max (runaway protection - extended)
    soft_time_limit=5400,   # 1.5 hrs soft limit (increased to allow completion)
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3, 'countdown': 300}
)
def run_whatsapp_report_automation(self, target_date: str = None):
    """
    Run WhatsApp report automation orchestrator task.
    
    Executes three scripts sequentially:
    1. Conversation analytics service
    2. Enhanced Excel report service  
    3. Export Excel task (email sending)
    
    Args:
        target_date: Date string in YYYY-MM-DD format. If None, uses yesterday.
        
    Returns:
        Dict with task execution results
    """
    return asyncio.run(run_whatsapp_report_automation_async(self, target_date))


async def run_whatsapp_report_automation_async(self, target_date: str = None):
    """Async implementation of the WhatsApp report automation task."""
    try:
        settings = get_settings()
        logger.info("Starting WhatsApp report automation orchestrator task")
        
        # Parse target date
        if target_date:
            try:
                parsed_date = datetime.strptime(target_date, '%Y-%m-%d').date()
            except ValueError as e:
                logger.error(f"Invalid date format '{target_date}': {e}")
                return {
                    "status": "failed",
                    "error": f"Invalid date format '{target_date}'. Use YYYY-MM-DD",
                    "timestamp": datetime.utcnow().isoformat()
                }
        else:
            # Default to yesterday
            parsed_date = date.today() - timedelta(days=1)

        logger.info(f"Running WhatsApp report automation for date: {parsed_date}")
        
        # Create reportStore directory if it doesn't exist
        report_store_dir = os.path.join(os.getcwd(), "app", "reportStore")
        os.makedirs(report_store_dir, exist_ok=True)
        
        # Step 1: Run conversation analytics service
        logger.info("Step 1: Running conversation analytics service")
        try:
            analytics_service = ConversationAnalyticsService()
            analytics_result = await analytics_service.analyze_daily_conversations(parsed_date)
            
            if not analytics_result.get('success', False):
                logger.error(f"Conversation analytics failed: {analytics_result.get('error', 'Unknown error')}")
                return {
                    "status": "failed",
                    "step": "conversation_analytics",
                    "target_date": str(parsed_date),
                    "error": f"Conversation analytics failed: {analytics_result.get('error', 'Unknown error')}",
                    "timestamp": datetime.utcnow().isoformat()
                }
            
            logger.info(f"Step 1 completed: Analyzed {analytics_result.get('total_sessions', 0)} sessions")
            
        except Exception as e:
            logger.error(f"Step 1 failed - Conversation analytics error: {e}")
            return {
                "status": "failed",
                "step": "conversation_analytics",
                "target_date": str(parsed_date),
                "error": f"Conversation analytics error: {str(e)}",
                "timestamp": datetime.utcnow().isoformat()
            }
        
        # Step 2: Generate Excel report using enhanced service
        logger.info("Step 2: Generating Excel report")
        try:
            # Generate output filename in reportStore folder
            output_filename = os.path.join(report_store_dir, f"Daily_Analytics_{parsed_date.strftime('%Y-%m-%d')}.xlsx")

            
            excel_service = EnhancedExcelReportService()
            excel_file_path = excel_service.generate_report(target_date=parsed_date, output_file=output_filename)
            
            # Create additional CSV file with sessions DataFrame
            sessions_filename = os.path.join(report_store_dir, f"Daily_Chats_{parsed_date.strftime('%Y-%m-%d')}.csv")

            sessions_df = analytics_result.get('sessions_df', pd.DataFrame(columns=["created_at", "phone_number", "conversation_history"]))

            # If sessions_df exists but has no columns, enforce column names
            if sessions_df.empty and sessions_df.columns.tolist() == []:
                sessions_df = pd.DataFrame(
                    columns=["created_at", "phone_number", "conversation_history"]
                )

            # Always create CSV (even if empty)
            sessions_df.to_csv(sessions_filename, index=False)

            if sessions_df.empty:
                logger.warning(f"Sessions CSV created but contains no data: {sessions_filename}")
            else:
                logger.info(f"Sessions CSV file created: {sessions_filename}")
            
            if not os.path.exists(excel_file_path):
                logger.error(f"Excel file was not created: {excel_file_path}")
                return {
                    "status": "failed",
                    "step": "excel_generation",
                    "target_date": str(parsed_date),
                    "error": f"Excel file was not created: {excel_file_path}",
                    "timestamp": datetime.utcnow().isoformat()
                }
            
            logger.info(f"Step 2 completed: Excel report generated at {excel_file_path}")
            
        except Exception as e:
            logger.error(f"Step 2 failed - Excel generation error: {e}")
            return {
                "status": "failed",
                "step": "excel_generation",
                "target_date": str(parsed_date),
                "error": f"Excel generation error: {str(e)}",
                "timestamp": datetime.utcnow().isoformat()
            }
        
        # Step 3: Send Excel report via email
        logger.info("Step 3: Sending Excel report via email")
        try:
            # Prepare list of files to send
            files_to_send = [excel_file_path]
            if 'sessions_filename' in locals() and os.path.exists(sessions_filename):
                files_to_send.append(sessions_filename)

            
            # Send all files in a single email
            email_result = await _send_excel_reports_with_api_session(files_to_send, parsed_date)
            
            if email_result.get("status") != "Success":
                logger.error(f"Email sending failed: {email_result}")
                return {
                    "status": "failed",
                    "step": "email_sending",
                    "target_date": str(parsed_date),
                    "excel_files": files_to_send,
                    "error": f"Email sending failed: {email_result.get('message', 'Unknown error')}",
                    "email_result": email_result,
                    "timestamp": datetime.utcnow().isoformat()
                }
            
            logger.info(f"Step 3 completed: {len(files_to_send)} Excel files sent via email successfully")
            
            # Clean up the Excel files after successful email delivery
            for file_path in files_to_send:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        logger.info(f"Excel file removed after successful delivery: {file_path}")
                except Exception as e:
                    logger.warning(f"Failed to remove Excel file {file_path}: {e}")
            
        except Exception as e:
            logger.error(f"Step 3 failed - Email sending error: {e}")
            return {
                "status": "failed",
                "step": "email_sending",
                "target_date": str(parsed_date),
                "excel_file": excel_file_path,
                "error": f"Email sending error: {str(e)}",
                "timestamp": datetime.utcnow().isoformat()
            }
        
        # All steps completed successfully
        logger.info(f"WhatsApp report automation completed successfully for {parsed_date}")
        return {
            "status": "completed",
            "target_date": str(parsed_date),
            "steps_completed": ["conversation_analytics", "excel_generation", "email_sending"],
            "analytics_sessions": analytics_result.get('total_sessions', 0),
            "excel_file": excel_file_path,
            "email_result": email_result,
            "timestamp": datetime.utcnow().isoformat(),
            "message": f"WhatsApp report automation completed successfully for {parsed_date}"
        }

    except SoftTimeLimitExceeded:
        logger.error("WhatsApp report automation task exceeded soft time limit (1 hour) - stopping gracefully")
        # Don't retry on timeout - just return status to avoid 3x restarts = 4hrs continuous
        return {
            "status": "timeout",
            "error": "Task exceeded maximum execution time",
            "timestamp": datetime.utcnow().isoformat()
        }
    
    except Exception as e:
        logger.error(f"WhatsApp report automation task failed: {e}")
        # Rely on autoretry_for - will retry up to 3 times with 300s countdown
        raise
