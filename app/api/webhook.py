"""
WhatsApp webhook endpoints for receiving and processing messages.

This module handles incoming WhatsApp messages through webhook endpoints.
It serves as the entry point for all user interactions, routing messages
to the appropriate chat service for processing and orchestration.

Key responsibilities:
- Receive WhatsApp webhook POST requests
- Validate webhook signatures and message formats
- Extract message content and user information
- Route messages to ChatService for processing
- Handle webhook verification for WhatsApp setup
- Return appropriate HTTP responses for webhook requirements
"""

from fastapi import APIRouter, Request, Query, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse, JSONResponse
from urllib.parse import unquote
import logging
from typing import Dict, Any, Optional
import json
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings
from app.services.chat_service import ChatService

router = APIRouter()
logger = logging.getLogger(__name__)
chat_service = ChatService()

# Initialize rate limiter for webhook endpoints
settings = get_settings()
limiter = Limiter(key_func=get_remote_address)


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    """
    Verify WhatsApp webhook during setup process.
    
    WhatsApp requires webhook verification with challenge response
    to confirm the endpoint is valid and controlled by the developer.
    """
    logger.info(f"Webhook verification attempt: mode={hub_mode}, token={hub_verify_token}")
    
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        logger.info("Webhook verification successful")
        return PlainTextResponse(content=hub_challenge)
    
    logger.warning("Webhook verification failed")
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/whatsapp")
@limiter.limit("60/minute")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Process incoming WhatsApp messages.
    
    This endpoint receives webhook calls from WhatsApp containing
    user messages, processes them through the chat service, and
    returns appropriate responses.
    """
    try:
        body = await request.body()
        logger.debug(f"Received webhook payload: {body.decode()}")
        
        # Parse webhook data
        webhook_data = await parse_webhook_data(request)
        
        if not webhook_data:
            logger.warning("No processable data in webhook")
            return JSONResponse(content={"status": "ok"})
        
        # Process message in background to avoid timeout
        background_tasks.add_task(process_message_async, webhook_data)
        
        # Return success immediately
        return JSONResponse(content={"status": "ok"})
        
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)


@router.post("/delivery")
async def handle_delivery_callback(request: Request):
    """
    Handle delivery status callbacks from WhatsApp.
    
    Processes delivery, read, and failed message status updates.
    """
    try:
        # Parse query parameters from URL
        query_params = dict(request.query_params)
        logger.info(f"Delivery callback received: {query_params}")
        
        status = query_params.get("qStatus")
        mobile = query_params.get("qMobile")
        msg_ref = query_params.get("qMsgRef")
        
        if status and mobile and msg_ref:
            logger.info(f"Message {msg_ref} to {mobile}: {status}")
            # Here you could update message status in database
        
        return JSONResponse(content={"status": "ok"})
        
    except Exception as e:
        logger.error(f"Error processing delivery callback: {e}")
        return JSONResponse(content={"status": "error"}, status_code=500)


async def parse_webhook_data(request: Request) -> Optional[Dict[str, Any]]:
    """
    Parse incoming webhook data based on content type.
    
    Handles both URL-encoded callbacks and JSON session messages.
    """
    content_type = request.headers.get("content-type", "")
    
    if "application/x-www-form-urlencoded" in content_type:
        # Handle URL-encoded webhook (user responses)
        form_data = await request.form()
        return parse_user_response_callback(dict(form_data))
    
    elif "application/json" in content_type:
        # Handle JSON webhook (if any)
        json_data = await request.json()
        return parse_json_webhook(json_data)
    
    else:
        # Try to parse as query parameters
        query_params = dict(request.query_params)
        if query_params:
            return parse_user_response_callback(query_params)
    
    return None


def parse_user_response_callback(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse user response callback from WhatsApp.
    
    Based on the documentation format:
    replytype, customernumber, replymessage, timestamp, wabanumber
    """
    try:
        reply_type = data.get("replytype")
        customer_number = data.get("customernumber")
        reply_message = data.get("replymessage")
        timestamp = data.get("timestamp")
        waba_number = data.get("wabanumber")
        
        if not all([reply_type, customer_number, reply_message]):
            return None
        
        # URL decode the reply message
        decoded_message = unquote(reply_message)
        
        # Handle different message types
        message_data = {
            "type": reply_type.lower(),
            "from": customer_number,
            "to": waba_number,
            "timestamp": timestamp,
            "content": decoded_message
        }
        
        # Parse JSON content for media messages
        if reply_type.upper() in ["IMAGE", "VIDEO", "DOCUMENT", "INTERACTIVE"]:
            try:
                message_data["content"] = json.loads(decoded_message)
            except json.JSONDecodeError:
                pass
        
        return message_data
        
    except Exception as e:
        logger.error(f"Error parsing user response callback: {e}")
        return None


def parse_json_webhook(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parse JSON webhook data if applicable.
    """
    # Add JSON webhook parsing logic if needed
    return data


async def process_message_async(webhook_data: Dict[str, Any]):
    """
    Process incoming message asynchronously.
    
    Routes the message through the chat service for processing.
    """
    try:
        logger.info(f"Processing message: {webhook_data}")
        
        # Extract message details
        message_type = webhook_data.get("type", "text")
        from_number = webhook_data.get("from")
        content = webhook_data.get("content")
        
        if not from_number or not content:
            logger.warning("Missing required message data")
            return
        
        # Handle document messages specifically
        if message_type.lower() == "document":
            await process_document_message(webhook_data)
        else:
            # Process regular messages through chat service
            await chat_service.process_message(
                user_phone=from_number,
                message_content=content,
                message_type=message_type
            )
        
    except Exception as e:
        logger.error(f"Error in async message processing: {e}")


async def process_document_message(webhook_data: Dict[str, Any]):
    """
    Process document message for Excel file uploads.
    
    Handles Excel file validation, processing, and routing to chat service.
    """
    try:
        from_number = webhook_data.get("from")
        content = webhook_data.get("content")
        
        if not isinstance(content, dict):
            logger.warning("Document message content is not a dictionary")
            return
        
        # Extract document information
        document_info = content.get("document", {})
        if not document_info:
            logger.warning("No document information found in message")
            return
        
        file_url = document_info.get("link")
        filename = document_info.get("filename", "")
        
        if not file_url:
            logger.warning("No file URL found in document message")
            return
        
        logger.info(f"Processing document upload: {filename} from {from_number}")
        
        # Check if it's an Excel file
        excel_extensions = ['.xlsx', '.xls', '.xlsm']
        is_excel = any(filename.lower().endswith(ext) for ext in excel_extensions)
        
        if is_excel:
            # Process as Excel file through chat service
            await chat_service.process_message(
                user_phone=from_number,
                message_content=content,
                message_type="excel_upload"
            )
        else:
            # Handle non-Excel documents
            await chat_service.process_message(
                user_phone=from_number,
                message_content=f"Received document: {filename}",
                message_type="document"
            )
        
    except Exception as e:
        logger.error(f"Error processing document message: {e}")


