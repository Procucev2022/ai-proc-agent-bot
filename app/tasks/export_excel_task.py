"""
Export Excel background task.

This task checks for daily metrics Excel files and sends them to clients
via email using the B2B WhatsApp insight report template.
"""

import logging
import os
import base64
import asyncio
from typing import Dict, Any
from celery import shared_task
from datetime import datetime, date, timedelta

from app.services.email_service import EmailService
from app.services.enhanced_excel_report_service import EnhancedExcelReportService
from app.config import get_settings

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 300})
def send_excel_report_to_client(self, target_date: str = None):
    """
    Send daily metrics Excel report to client via email.
    
    Args:
        target_date: Date string in YYYY-MM-DD format. If None, uses yesterday.
        
    Returns:
        Dict with task execution results
    """
    try:
        settings = get_settings()
        logger.info("Starting export Excel report task")
        
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

        logger.info(f"Generating Excel report for date: {parsed_date}")
        
        # Generate Excel file using enhanced service
        try:
            # Create daily_metrics folder if it doesn't exist
            daily_metrics_dir = os.path.join(os.getcwd(), "daily_metrics")
            os.makedirs(daily_metrics_dir, exist_ok=True)
            
            # Generate output filename in daily_metrics folder
            output_filename = os.path.join(daily_metrics_dir, f"procurement_analytics_{parsed_date.strftime('%Y-%m-%d')}.xlsx")
            
            excel_service = EnhancedExcelReportService()
            excel_file_path = excel_service.generate_report(target_date=parsed_date, output_file=output_filename)
            logger.info(f"Excel report generated: {excel_file_path}")
        except Exception as e:
            logger.error(f"Failed to generate Excel report: {e}")
            return {
                "status": "failed",
                "target_date": str(parsed_date),
                "error": f"Excel generation failed: {str(e)}",
                "timestamp": datetime.utcnow().isoformat()
            }
        
        # Send email with Excel attachment (initialize API client within the async context)
        email_result = asyncio.run(_send_excel_to_client_with_init(excel_file_path, parsed_date))
        
        if email_result.get("status") == "Success":
            # Clean up the Excel file after successful email delivery
            try:
                os.remove(excel_file_path)
                logger.info(f"Excel file removed after successful delivery: {excel_file_path}")
            except Exception as e:
                logger.warning(f"Failed to remove Excel file {excel_file_path}: {e}")
            
            logger.info(f"Excel report sent successfully for {parsed_date}")
            return {
                "status": "completed",
                "target_date": str(parsed_date),
                "excel_file": excel_file_path,
                "email_result": email_result,
                "timestamp": datetime.utcnow().isoformat(),
                "message": f"Excel report sent successfully for {parsed_date}"
            }
        else:
            logger.error(f"Failed to send Excel report for {parsed_date}: {email_result}")
            return {
                "status": "failed",
                "target_date": str(parsed_date),
                "error": f"Email sending failed: {email_result.get('message', 'Unknown error')}",
                "email_result": email_result,
                "timestamp": datetime.utcnow().isoformat()
            }

    except Exception as e:
        logger.error(f"Export Excel report task failed: {e}")
        # Retry with exponential backoff
        raise self.retry(countdown=300)





async def _send_excel_to_client_with_init(excel_file_path: str, parsed_date: date) -> Dict[str, Any]:
    """
    Initialize API client and send Excel file to client via email.
    
    Args:
        excel_file_path: Path to the Excel file to send
        parsed_date: Date for the report
        
    Returns:
        Dict with email sending results
    """
    try:
        # Initialize API client within this async context
        from app.procucev_apis.procucev_api_client import init_procucev_api_client, close_procucev_api_client
        
        try:
            await init_procucev_api_client()
            logger.info("API client initialized for email task")
            
            # Now send the email
            result = await _send_excel_to_client(excel_file_path, parsed_date)
            
            return result
            
        finally:
            # Always close the API client session
            try:
                await close_procucev_api_client()
                logger.info("API client session closed")
            except Exception as e:
                logger.warning(f"Error closing API client: {e}")
        
    except Exception as e:
        logger.error(f"Failed to send Excel file to client: {e}")
        return {
            "status": "Failure",
            "message": "Failed to send Excel report",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


async def _send_excel_to_client(excel_file_path: str, parsed_date: date) -> Dict[str, Any]:
    """
    Send Excel file to client via email using B2B WhatsApp insight report template.
    
    Args:
        excel_file_path: Path to the Excel file to send
        parsed_date: Date for the report
        
    Returns:
        Dict with email sending results
    """
    try:
        settings = get_settings()
        email_service = EmailService()
        
        # Read Excel file and prepare attachment
        filename = os.path.basename(excel_file_path)
        with open(excel_file_path, 'rb') as f:
            file_content = f.read()
            file_base64 = base64.b64encode(file_content).decode('utf-8')
        
        # Format attachment with proper structure
        attachment_data = {
            "fileName": filename,
            "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "fileData": file_base64
        }
        
        logger.info(f"Attachment prepared: {filename}, size: {len(file_content)} bytes")
        
        # Prepare template variables
        template_variables = {
            "parsed_Date": parsed_date.strftime('%Y-%m-%d'),
            "sender_email": settings.email_report_sender or "shubham@mohap.ai",
            "support_email": settings.support_email or "support@procucev.com"
        }
        
        logger.info(f"Sending Excel report email for {parsed_date}")
        
        # Send email using B2B WhatsApp insight report template with attachment
        result = await email_service.send_email_by_template(
            template_name="B2B_whatsapp_insight_report",
            variables=template_variables,
            attachments=[attachment_data]
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to send Excel file to client: {e}")
        return {
            "status": "Failure",
            "message": "Failed to send Excel report",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }