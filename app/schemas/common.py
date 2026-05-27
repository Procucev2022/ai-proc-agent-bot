"""
Common Pydantic schemas and base classes.

This module contains shared schemas, base classes, and common validation utilities
used across the application.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, Any, Dict, List
from datetime import datetime


class BaseResponseSchema(BaseModel):
    """
    Base schema for API responses.
    
    Provides common response structure with status, message, and data fields.
    """
    success: bool = Field(..., description="Whether the request was successful")
    message: str = Field(..., description="Response message")
    data: Optional[Any] = Field(None, description="Response data")
    error: Optional[str] = Field(None, description="Error message if any")
    timestamp: Optional[datetime] = Field(default_factory=datetime.now, description="Response timestamp")
    
    class Config:
        extra = "allow"


class PaginationSchema(BaseModel):
    """
    Schema for pagination parameters.
    """
    page: int = Field(1, ge=1, description="Page number")
    page_size: int = Field(10, ge=1, le=100, description="Items per page")
    total: Optional[int] = Field(None, description="Total items count")
    
    class Config:
        extra = "allow"


class ErrorResponseSchema(BaseModel):
    """
    Schema for error responses.
    """
    error_code: str = Field(..., description="Error code")
    error_message: str = Field(..., description="Error message")
    details: Optional[Dict[str, Any]] = Field(None, description="Error details")
    timestamp: datetime = Field(default_factory=datetime.now, description="Error timestamp")
    
    class Config:
        extra = "allow"


class SuccessResponseSchema(BaseModel):
    """
    Schema for success responses.
    """
    status: str = Field("success", description="Response status")
    message: str = Field(..., description="Success message")
    data: Optional[Any] = Field(None, description="Response data")
    
    class Config:
        extra = "allow"


class ValidationErrorSchema(BaseModel):
    """
    Schema for validation errors.
    """
    field: str = Field(..., description="Field name with error")
    error: str = Field(..., description="Error message")
    value: Optional[Any] = Field(None, description="Invalid value")
    
    class Config:
        extra = "allow"


class TimestampMixin(BaseModel):
    """
    Mixin for adding timestamp fields to schemas.
    """
    created_at: Optional[datetime] = Field(None, description="Creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")
    
    class Config:
        extra = "allow"


class StatusEnum:
    """
    Common status enums used across the application.
    """
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LocationSchema(BaseModel):
    """
    Schema for location information.
    """
    state: str = Field(..., description="State")
    city: str = Field(..., description="City")
    pincode: str = Field(..., description="Pincode")
    address: Optional[str] = Field(None, description="Full address")
    
    @validator('pincode')
    def validate_pincode(cls, v):
        if not v.isdigit() or len(v) != 6:
            raise ValueError('Pincode must be 6 digits')
        return v
    
    class Config:
        extra = "allow"


class ContactSchema(BaseModel):
    """
    Schema for contact information.
    """
    name: str = Field(..., description="Contact name")
    email: Optional[str] = Field(None, description="Email address")
    phone: Optional[str] = Field(None, description="Phone number")
    
    @validator('email')
    def validate_email(cls, v):
        if v and ('@' not in v or '.' not in v):
            raise ValueError('Invalid email format')
        return v
    
    class Config:
        extra = "allow"


class FileUploadSchema(BaseModel):
    """
    Schema for file upload information.
    """
    filename: str = Field(..., description="Original filename")
    file_path: str = Field(..., description="Stored file path")
    file_size: int = Field(..., description="File size in bytes")
    mime_type: str = Field(..., description="MIME type")
    uploaded_at: datetime = Field(default_factory=datetime.now, description="Upload timestamp")
    
    class Config:
        extra = "allow"