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



async def _send_excel_reports_with_api_session(excel_files: list, parsed_date: date) -> Dict[str, Any]:
    """
    Initialize API client and send multiple Excel files to client via email.
    
    Args:
        excel_files: List of paths to Excel files to send
        parsed_date: Date for the report
        
    Returns:
        Dict with email sending results
    """
    try:
        from app.procucev_apis.procucev_api_client import init_procucev_api_client, close_procucev_api_client
        
        try:
            await init_procucev_api_client()
            logger.info("API client initialized for email task")
            
            result = await _send_excel_email_reports(excel_files, parsed_date)
            return result
            
        finally:
            try:
                await close_procucev_api_client()
                logger.info("API client session closed")
            except Exception as e:
                logger.warning(f"Error closing API client: {e}")
        
    except Exception as e:
        logger.error(f"Failed to send Excel files to client: {e}")
        return {
            "status": "Failure",
            "message": "Failed to send Excel reports",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


async def _send_excel_email_reports(excel_files: list, parsed_date: date) -> Dict[str, Any]:
    """
    Send multiple Excel files to client via email.
    
    Args:
        excel_files: List of paths to Excel files to send
        parsed_date: Date for the report
        
    Returns:
        Dict with email sending results
    """
    try:
        settings = get_settings()
        email_service = EmailService()
        
        # Prepare multiple attachments
        attachments = []
        for excel_file_path in excel_files:
            if os.path.exists(excel_file_path):
                filename = os.path.basename(excel_file_path)
                with open(excel_file_path, 'rb') as f:
                    file_content = f.read()
                    file_base64 = base64.b64encode(file_content).decode('utf-8')
                
                attachment_data = {
                    "fileName": filename,
                    "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "fileData": file_base64
                }
                attachments.append(attachment_data)
                logger.info(f"Attachment prepared: {filename}, size: {len(file_content)} bytes")
        
        template_variables = {
            "parsed_Date": parsed_date.strftime('%Y-%m-%d'),
            "sender_email": settings.email_report_sender or "priyasoniy17@gmail.com",
            "support_email": settings.support_email or "support@procucev.com",
            "WHATSAPP_REPORT_EMAILS": os.getenv('WHATSAPP_REPORT_EMAILS', 'priyasoniy17@gmail.com,shubham@mohap.ai')
        }
        
        logger.info(f"Sending {len(attachments)} Excel files for {parsed_date}")
        
        result = await email_service.send_email_by_template(
            template_name="B2B_whatsapp_insight_report",
            variables=template_variables,
            attachments=attachments
        )
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to send Excel files to client: {e}")
        return {
            "status": "Failure",
            "message": "Failed to send Excel reports",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }

