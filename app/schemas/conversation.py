"""
Conversation and session management Pydantic schemas.

This module contains schemas for conversation context, session management,
and workflow state tracking.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from enum import Enum


class WorkflowType(str, Enum):
    """Enum for workflow types."""
    PRODUCT_SEARCH = "product_search"
    RFQ_CREATION = "rfq_creation"
    GENERAL_INQUIRY = "general_inquiry"
    VENDOR_SEARCH = "vendor_search"


class ConversationOutcome(str, Enum):
    """Enum for conversation outcomes."""
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    ESCALATED = "escalated"
    TIMEOUT = "timeout"
    IN_PROGRESS = "in_progress"


class ConversationContextSchema(BaseModel):
    """
    Schema for conversation context and session data.
    
    Validates conversation state, extracted entities,
    and workflow progress information.
    """
    session_id: str = Field(..., description="Session identifier")
    external_user_id: str = Field(..., description="External user ID")
    workflow_type: Optional[WorkflowType] = Field(None, description="Current workflow type")
    workflow_state: Dict[str, Any] = Field(default_factory=dict, description="Workflow state data")
    extracted_entities: Dict[str, Any] = Field(default_factory=dict, description="Extracted entities")
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list, description="Message history")
    whatsapp_context: Optional[Dict[str, Any]] = Field(None, description="WhatsApp-specific context")
    
    @validator('session_id')
    def validate_session_id(cls, v):
        if len(v.strip()) < 1:
            raise ValueError('Session ID cannot be empty')
        return v.strip()
    
    @validator('external_user_id')
    def validate_external_user_id(cls, v):
        if len(v.strip()) < 1:
            raise ValueError('External user ID cannot be empty')
        return v.strip()
    
    class Config:
        extra = "allow"


class ConversationSessionSchema(BaseModel):
    """
    Schema for conversation session records.
    """
    session_id: str = Field(..., description="Session identifier")
    external_user_id: str = Field(..., description="External user ID")
    workflow_type: Optional[WorkflowType] = Field(None, description="Workflow type")
    outcome: Optional[ConversationOutcome] = Field(None, description="Session outcome")
    rfq_id: Optional[str] = Field(None, description="Associated RFQ ID")
    workflow_state: Dict[str, Any] = Field(default_factory=dict, description="Workflow state")
    conversation_history: List[Dict[str, Any]] = Field(default_factory=list, description="Message history")
    extracted_entities: Optional[Dict[str, Any]] = Field(None, description="Extracted entities")
    whatsapp_context: Optional[Dict[str, Any]] = Field(None, description="WhatsApp context")
    error_details: Optional[Dict[str, Any]] = Field(None, description="Error information")
    performance_metrics: Optional[Dict[str, Any]] = Field(None, description="Performance metrics")
    created_at: Optional[str] = Field(None, description="Creation timestamp")
    completed_at: Optional[str] = Field(None, description="Completion timestamp")
    
    class Config:
        extra = "allow"


class MessageSchema(BaseModel):
    """
    Schema for individual conversation messages.
    """
    message_id: str = Field(..., description="Message identifier")
    session_id: str = Field(..., description="Session identifier")
    sender: str = Field(..., description="Message sender (user/bot)")
    content: str = Field(..., description="Message content")
    timestamp: str = Field(..., description="Message timestamp")
    message_type: str = Field("text", description="Message type")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Message metadata")
    
    @validator('sender')
    def validate_sender(cls, v):
        if v not in ['user', 'bot', 'system']:
            raise ValueError('Sender must be user, bot, or system')
        return v
    
    @validator('content')
    def validate_content(cls, v):
        if len(v.strip()) < 1:
            raise ValueError('Message content cannot be empty')
        return v.strip()
    
    class Config:
        extra = "allow"


class EntityExtractionResultSchema(BaseModel):
    """
    Schema for entity extraction results.
    """
    entities: Dict[str, Any] = Field(default_factory=dict, description="Extracted entities")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="Extraction confidence")
    completeness: float = Field(0.0, ge=0.0, le=100.0, description="Completeness percentage")
    missing_required_fields: List[str] = Field(default_factory=list, description="Missing required fields")
    next_questions: List[str] = Field(default_factory=list, description="Next questions to ask")
    
    class Config:
        extra = "allow"


class WorkflowStateSchema(BaseModel):
    """
    Schema for workflow state tracking.
    """
    current_step: str = Field(..., description="Current workflow step")
    completed_steps: List[str] = Field(default_factory=list, description="Completed steps")
    pending_steps: List[str] = Field(default_factory=list, description="Pending steps")
    collected_data: Dict[str, Any] = Field(default_factory=dict, description="Collected data")
    validation_errors: List[str] = Field(default_factory=list, description="Validation errors")
    
    class Config:
        extra = "allow"