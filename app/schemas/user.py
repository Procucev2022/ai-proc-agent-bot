"""
User registration and authentication Pydantic schemas.

This module contains schemas for user management, registration, and authentication.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional
from enum import Enum


class UserRole(str, Enum):
    """Enum for user roles."""
    BUYER = "buyer"
    VENDOR = "vendor"
    CATEGORY_MANAGER = "category_manager"


class UserRegistrationSchema(BaseModel):
    """
    Schema for user registration requests.
    
    Validates user registration data including contact information,
    company details, and role assignments.
    """
    phone_number: str = Field(..., description="WhatsApp phone number")
    name: str = Field(..., description="User's full name")
    company_name: Optional[str] = Field(None, description="Company name")
    role: UserRole = Field(UserRole.BUYER, description="User role")
    email: Optional[str] = Field(None, description="Email address")
    
    @validator('phone_number')
    def validate_phone_number(cls, v):
        # Remove any non-digit characters
        digits_only = ''.join(filter(str.isdigit, v))
        if len(digits_only) < 10:
            raise ValueError('Phone number must have at least 10 digits')
        return v
    
    @validator('email')
    def validate_email(cls, v):
        if v and ('@' not in v or '.' not in v):
            raise ValueError('Invalid email format')
        return v
    
    @validator('name')
    def validate_name(cls, v):
        if len(v.strip()) < 2:
            raise ValueError('Name must be at least 2 characters long')
        return v.strip()
    
    class Config:
        extra = "allow"


class UserProfileSchema(BaseModel):
    """
    Schema for user profile information.
    """
    id: str = Field(..., description="User ID")
    phone_number: str = Field(..., description="WhatsApp phone number")
    name: str = Field(..., description="User's full name")
    company_name: Optional[str] = Field(None, description="Company name")
    role: UserRole = Field(..., description="User role")
    email: Optional[str] = Field(None, description="Email address")
    created_at: Optional[str] = Field(None, description="Account creation timestamp")
    is_active: bool = Field(True, description="Whether user account is active")
    
    class Config:
        extra = "allow"


class UserUpdateSchema(BaseModel):
    """
    Schema for updating user profile information.
    """
    name: Optional[str] = Field(None, description="User's full name")
    company_name: Optional[str] = Field(None, description="Company name")
    email: Optional[str] = Field(None, description="Email address")
    
    @validator('email')
    def validate_email(cls, v):
        if v and ('@' not in v or '.' not in v):
            raise ValueError('Invalid email format')
        return v
    
    @validator('name')
    def validate_name(cls, v):
        if v and len(v.strip()) < 2:
            raise ValueError('Name must be at least 2 characters long')
        return v.strip() if v else v
    
    class Config:
        extra = "allow"