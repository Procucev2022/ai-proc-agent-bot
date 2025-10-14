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
import os
from typing import Dict, Any, Optional
import json
from slowapi import Limiter
from slowapi.util import get_remote_address
from logging.handlers import RotatingFileHandler
from datetime import datetime

from app.config import get_settings
from app.services.chat_service import ChatService

router = APIRouter()
logger = logging.getLogger(__name__)
chat_service = ChatService()

# Set up WhatsApp webhook payload logger
webhook_payload_logger = logging.getLogger("whatsapp_webhook")
webhook_payload_logger.setLevel(logging.DEBUG)

# Create whatsapp_logs directory if it doesn't exist
log_dir = "whatsapp_logs"
if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# Add file handler if not already present
if not webhook_payload_logger.handlers:
    log_file = os.path.join(log_dir, f"whatsapp_webhook_{datetime.now().strftime('%Y-%m-%d')}.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    webhook_payload_logger.addHandler(file_handler)

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
        logger.error(f"Critical error processing webhook: {e}")
        
        # Try to extract user phone for technical failure notification
        user_phone = None
        try:
            body = await request.body()
            webhook_data = await parse_webhook_data(request)
            if webhook_data:
                user_phone = webhook_data.get("from")
        except:
            pass
        
        # Send technical failure message if we have user phone
        if user_phone:
            from app.utils.technical_failure_handler import handle_technical_failure
            try:
                await handle_technical_failure(
                    user_phone=user_phone,
                    error_message=f"Webhook processing error: {str(e)}",
                    error_type="Webhook Error"
                )
            except:
                pass  # Don't let notification failure break webhook response
        
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)


@router.get("/delivery")
@router.post("/delivery")
async def handle_delivery_callback(request: Request):
    """
    Handle delivery status callbacks from WhatsApp based on ICS documentation.

    Expected ICS format:
    ?qStatus=STATUS&qMobile=MOBILENO&qMsgRef=MESSAGEID&qDTime=DATETIME&SMSMSGID=SMSMSGID&SENDERID=SENDERID&NOTES=NOTES

    Sample: ?qStatus=read&qMobile=919036149941&qMsgRef=80890044684334105532272789261449227204&qDTime=2025-09-13%2013%3A53%3A25.980895&SMSMSGID=test13sep&NOTES=NA
    """
    try:
        # Parse query parameters from URL
        query_params = dict(request.query_params)
        logger.info(f"ICS delivery callback received: {query_params}")

        # Extract ICS delivery callback fields
        status = query_params.get("qStatus")
        mobile = query_params.get("qMobile")
        msg_ref = query_params.get("qMsgRef")
        date_time = query_params.get("qDTime")
        sms_msg_id = query_params.get("SMSMSGID")
        sender_id = query_params.get("SENDERID")
        notes = query_params.get("NOTES")

        if status and mobile and msg_ref:
            logger.info(f"ICS delivery status - Message {msg_ref} to {mobile}: {status} at {date_time}")
            if notes and notes != "NA":
                logger.info(f"Delivery notes: {notes}")

            # Here you could update message status in database
            # Example: await update_message_status(msg_ref, status, date_time)
        else:
            logger.warning(f"Missing required delivery callback fields: status={status}, mobile={mobile}, msg_ref={msg_ref}")

        return JSONResponse(content={"status": "ok"})

    except Exception as e:
        logger.error(f"Error processing ICS delivery callback: {e}")
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
    Parse user response callback from WhatsApp based on ICS documentation.

    Expected format from ICS:
    replytype, customernumber, replymessage, timestamp, wabanumber, mid, smsgid

    Sample: ?replytype=TEXT&customernumber=919036149941&replymessage=I+want+laptop&timestamp=2025-09-13%2013%3A54%3A22.125300&wabanumber=917090170855&mid=80890044684334105532272789261449227204&smsgid=NA
    """
    try:
        reply_type = data.get("replytype")
        customer_number = data.get("customernumber")
        reply_message = data.get("replymessage")
        timestamp = data.get("timestamp")
        waba_number = data.get("wabanumber")
        mid = data.get("mid")
        smsgid = data.get("smsgid")

        # Log received data for debugging
        logger.info(f"Parsing ICS webhook - Type: {reply_type}, From: {customer_number}, Message: {reply_message[:50] if reply_message else 'None'}...")

        # Check essential fields - only customer_number and reply_message are mandatory
        if not customer_number:
            logger.warning("Missing customer number in ICS webhook data")
            return None

        if not reply_message:
            logger.warning("Missing reply message in ICS webhook data")
            return None

        # URL decode the reply message (+ signs should become spaces)
        decoded_message = unquote(reply_message.replace('+', ' '))

        # Handle different message types based on ICS documentation
        message_data = {
            "type": reply_type.lower() if reply_type else "text",
            "from": customer_number,
            "to": waba_number,
            "timestamp": timestamp,
            "content": decoded_message,
            "message_id": mid,
            "sms_id": smsgid
        }

        # Parse JSON content for media/interactive messages per ICS documentation
        if reply_type and reply_type.upper() in ["IMAGE", "VIDEO", "DOCUMENT", "INTERACTIVE"]:
            try:
                # ICS sends JSON data URL-encoded for media messages
                message_data["content"] = json.loads(decoded_message)
                logger.info(f"Successfully parsed {reply_type.upper()} message content as JSON")

                # Log media message payload to separate file
                webhook_payload_logger.info("="*80)
                webhook_payload_logger.info(f"MEDIA WEBHOOK RECEIVED - Type: {reply_type.upper()}")
                webhook_payload_logger.info(f"From: {customer_number}")
                webhook_payload_logger.info(f"Timestamp: {timestamp}")
                webhook_payload_logger.info(f"Message ID: {mid}")
                webhook_payload_logger.debug(f"Full Content: {message_data['content']}")

                # Log file details for document/image
                if reply_type.upper() in ["IMAGE", "DOCUMENT"]:
                    content_obj = message_data['content']
                    media_key = "image" if reply_type.upper() == "IMAGE" else "document"
                    if isinstance(content_obj, dict) and media_key in content_obj:
                        media_info = content_obj[media_key]
                        webhook_payload_logger.info(f"File URL: {media_info.get('link', 'N/A')}")
                        webhook_payload_logger.info(f"Filename: {media_info.get('filename', 'N/A')}")
                        webhook_payload_logger.info(f"MIME Type: {media_info.get('mime_type', 'N/A')}")

                webhook_payload_logger.info("="*80)

            except json.JSONDecodeError:
                # Keep as string if JSON parsing fails
                logger.info(f"Keeping {reply_type.upper()} message content as string")
                webhook_payload_logger.warning(f"Failed to parse {reply_type.upper()} content as JSON")
                pass

        logger.info(f"Successfully parsed ICS webhook: Type={message_data['type']}, From={message_data['from']}")
        return message_data

    except Exception as e:
        logger.error(f"Error parsing ICS user response callback: {e}")
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


