"""
Vendor search and management Pydantic schemas.

This module contains schemas for vendor search, responses, and vendor management.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any


class VendorSearchRequestSchema(BaseModel):
    """
    Schema for vendor search API requests.
    
    Validates vendor search parameters including product categories,
    locations, and filtering criteria.
    """
    category: Optional[str] = Field(None, description="Product category")
    location: Optional[str] = Field(None, description="Geographic location")
    division: Optional[str] = Field(None, description="Division/department")
    vendor_category: Optional[str] = Field(None, alias="vendorcategory", description="Vendor category")
    
    class Config:
        populate_by_name = True
        extra = "allow"


class VendorResponseSchema(BaseModel):
    """
    Schema for vendor search API responses.
    
    Defines the structure of vendor information returned
    in search results and recommendations.
    """
    id: str = Field(..., description="Vendor ID")
    vendor_id: Optional[str] = Field(None, alias="vendorId", description="Vendor identifier")
    company_name: str = Field(..., alias="companyName", description="Company name")
    email: str = Field(..., description="Primary email")
    city: str = Field(..., description="City location")
    mobile_no: Optional[str] = Field(None, alias="mobileNo", description="Mobile number")
    other_emails: Optional[List[str]] = Field(None, alias="otherEmails", description="Additional emails")
    
    @validator('email')
    def validate_email(cls, v):
        if '@' not in v or '.' not in v:
            raise ValueError('Invalid email format')
        return v
    
    class Config:
        populate_by_name = True
        extra = "allow"


class VendorProfileSchema(BaseModel):
    """
    Schema for detailed vendor profile information.
    """
    id: str = Field(..., description="Vendor ID")
    vendor_id: Optional[str] = Field(None, description="Vendor identifier")
    company_name: str = Field(..., description="Company name")
    email: str = Field(..., description="Primary email")
    city: str = Field(..., description="City location")
    state: Optional[str] = Field(None, description="State location")
    mobile_no: Optional[str] = Field(None, description="Mobile number")
    other_emails: Optional[List[str]] = Field(None, description="Additional emails")
    categories: Optional[List[str]] = Field(None, description="Vendor categories")
    services: Optional[List[str]] = Field(None, description="Vendor services")
    geographic_coverage: Optional[List[str]] = Field(None, description="Geographic coverage")
    created_at: Optional[str] = Field(None, description="Profile creation timestamp")
    
    class Config:
        extra = "allow"


class VendorRFQAssignmentSchema(BaseModel):
    """
    Schema for vendor RFQ assignment requests.
    """
    vendor: Dict[str, str] = Field(..., description="Vendor details")
    rfq: Dict[str, str] = Field(..., description="RFQ details")
    
    @validator('vendor')
    def validate_vendor(cls, v):
        if 'id' not in v:
            raise ValueError('Vendor ID is required')
        return v
    
    @validator('rfq')
    def validate_rfq(cls, v):
        if 'id' not in v:
            raise ValueError('RFQ ID is required')
        return v
    
    class Config:
        extra = "allow"


class VendorQuerySchema(BaseModel):
    """
    Schema for vendor query requests.
    """
    vendor: Dict[str, str] = Field(..., description="Vendor details")
    rfq: Dict[str, str] = Field(..., description="RFQ details")
    query: str = Field(..., description="Query text")
    
    @validator('query')
    def validate_query(cls, v):
        if len(v.strip()) < 5:
            raise ValueError('Query must be at least 5 characters long')
        return v.strip()
    
    class Config:
        extra = "allow"