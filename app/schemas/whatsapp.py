"""
WhatsApp webhook and message Pydantic schemas.

This module contains schemas for validating WhatsApp Business API webhook messages
and related WhatsApp communication structures.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any


class WhatsAppMessageSchema(BaseModel):
    """
    Schema for incoming WhatsApp webhook messages.
    
    Validates the structure and content of webhook messages
    received from WhatsApp Business API.
    """
    object: str = Field(..., description="Webhook object type")
    entry: List[Dict[str, Any]] = Field(..., description="Webhook entry data")
    
    @validator('object')
    def validate_object_type(cls, v):
        if v != 'whatsapp_business_account':
            raise ValueError('Invalid webhook object type')
        return v
    
    class Config:
        extra = "allow"


class WhatsAppContactSchema(BaseModel):
    """
    Schema for WhatsApp contact information.
    """
    profile: Optional[Dict[str, str]] = Field(None, description="Contact profile")
    wa_id: str = Field(..., description="WhatsApp ID")
    
    class Config:
        extra = "allow"


class WhatsAppTextMessageSchema(BaseModel):
    """
    Schema for WhatsApp text message content.
    """
    body: str = Field(..., description="Message body text")
    
    class Config:
        extra = "allow"


class WhatsAppIncomingMessageSchema(BaseModel):
    """
    Schema for incoming WhatsApp messages.
    """
    id: str = Field(..., description="Message ID")
    from_: str = Field(..., alias="from", description="Sender phone number")
    timestamp: str = Field(..., description="Message timestamp")
    type: str = Field(..., description="Message type")
    text: Optional[WhatsAppTextMessageSchema] = Field(None, description="Text message content")
    
    @validator('type')
    def validate_message_type(cls, v):
        valid_types = ['text', 'image', 'audio', 'video', 'document', 'location', 'contacts']
        if v not in valid_types:
            raise ValueError(f'Invalid message type: {v}')
        return v
    
    class Config:
        populate_by_name = True
        extra = "allow"


class WhatsAppOutgoingMessageSchema(BaseModel):
    """
    Schema for outgoing WhatsApp messages.
    """
    messaging_product: str = Field("whatsapp", description="Messaging product")
    to: str = Field(..., description="Recipient phone number")
    type: str = Field("text", description="Message type")
    text: Optional[Dict[str, str]] = Field(None, description="Text message content")
    
    @validator('to')
    def validate_phone_number(cls, v):
        if not v.isdigit() or len(v) < 10:
            raise ValueError('Invalid phone number format')
        return v
    
    class Config:
        extra = "allow"