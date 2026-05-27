"""
Pydantic schemas for the seller recommendation system API.

This module defines the request/response schemas for all seller recommendation
endpoints including RFQ processing, seller selection, notifications, and
admin functions.

The schemas provide data validation, serialization, and API documentation
for the seller recommendation system.
"""

from pydantic import BaseModel, Field, validator
from typing import Dict, List, Any, Optional, Union
from datetime import datetime, date
from enum import Enum

# ================================
# ENUMS
# ================================

class SellerRankingEnum(str, Enum):
    """Seller ranking categories."""
    diamond = "Diamond"
    platinum = "Platinum"  
    gold = "Gold"
    titanium = "Titanium"

class RFQStatusEnum(str, Enum):
    """RFQ status values."""
    collecting = "collecting"
    ready = "ready"
    submitted = "submitted"
    failed = "failed"

class JobStatusEnum(str, Enum):
    """Background job status values."""
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"

# ================================
# REQUEST SCHEMAS
# ================================

class RFQApprovalRequest(BaseModel):
    """Request schema for RFQ approval webhook."""
    
    rfq_id: str = Field(..., description="RFQ identifier")
    rfq_title: str = Field(..., description="RFQ title")
    categories: List[str] = Field(..., description="List of RFQ categories")
    delivery_location: Dict[str, str] = Field(..., description="Delivery location with state/city/pincode")
    quantity_info: Optional[str] = Field(None, description="Quantity information")
    deadline: Optional[date] = Field(None, description="RFQ deadline")
    division: Optional[str] = Field(None, description="Business division")
    approval_timestamp: datetime = Field(..., description="When RFQ was approved")
    approved_by: Optional[str] = Field(None, description="Who approved the RFQ")
    
    @validator('categories')
    def categories_must_not_be_empty(cls, v):
        if not v:
            raise ValueError('Categories list cannot be empty')
        return v
    
    @validator('delivery_location')
    def validate_location(cls, v):
        required_fields = ['state', 'city', 'pincode']
        for field in required_fields:
            if field not in v:
                raise ValueError(f'Delivery location must contain {field}')
        # Validate pincode format
        pincode = v.get('pincode', '')
        if not pincode.isdigit() or len(pincode) != 6:
            raise ValueError('Pincode must be 6 digits')
        return v

class SellerNotificationRequest(BaseModel):
    """Request schema for manual seller notification."""
    
    seller_id: str = Field(..., description="Seller identifier")
    rfq_data: Dict[str, Any] = Field(..., description="RFQ information")
    
class SystemConfigUpdate(BaseModel):
    """Request schema for system configuration updates."""
    
    config_updates: Dict[str, Any] = Field(..., description="Configuration key-value updates")
    
    @validator('config_updates')
    def validate_config_keys(cls, v):
        allowed_keys = {
            'MAX_SUBSCRIBED_SELLERS_PER_RFQ',
            'MAX_UNSUBSCRIBED_SELLERS_PER_RFQ', 
            'MAX_TIME_SINCE_LAST_MESSAGE_HOURS',
            'MAX_TIME_SINCE_LAST_ACTIVE_HOURS',
            'GEO_DISTANCE_RADIUS_KM'
        }
        
        for key in v.keys():
            if key not in allowed_keys:
                raise ValueError(f'Invalid configuration key: {key}')
        
        return v

# ================================
# RESPONSE SCHEMAS
# ================================

class SellerInfo(BaseModel):
    """Schema for seller information."""
    
    seller_id: str
    seller_name: str
    phone_number: str
    email: Optional[str]
    categories: List[str]
    location: Dict[str, Union[str, float]]
    subscription_credits: int
    ranking: SellerRankingEnum
    last_active_at: Optional[datetime]
    distance_km: Optional[float] = Field(None, description="Distance from delivery location")

class SellerSelectionMetadata(BaseModel):
    """Schema for seller selection metadata."""
    
    rfq_id: str
    categories_searched: List[str]
    delivery_location: Dict[str, str]
    initial_category_matches: int
    geo_filtered_count: int
    subscribed_available: int
    unsubscribed_available: int
    config_used: Dict[str, Any]
    selection_timestamp: str

class SellerSelectionResponse(BaseModel):
    """Response schema for seller selection results."""
    
    subscribed_sellers: List[SellerInfo]
    unsubscribed_sellers: List[SellerInfo]
    total_selected: int
    selection_metadata: SellerSelectionMetadata

class NotificationResult(BaseModel):
    """Schema for notification sending result."""
    
    seller_id: str
    seller_name: str
    success: bool
    message_id: Optional[str]
    error: Optional[str]

class BatchNotificationResponse(BaseModel):
    """Response schema for batch notification results."""
    
    successful: int
    failed: int
    total: int
    details: List[NotificationResult]

class RFQProcessingResponse(BaseModel):
    """Response schema for RFQ processing results."""
    
    success: bool
    job_id: str
    rfq_id: str
    processing_time_seconds: float
    sellers_selected: int
    subscribed_sellers: int
    unsubscribed_sellers: int
    notifications_sent: int
    notifications_failed: int
    selection_metadata: SellerSelectionMetadata
    notification_details: BatchNotificationResponse

# ================================
# ADMIN SCHEMAS
# ================================

class SystemConfiguration(BaseModel):
    """Schema for system configuration."""
    
    configuration: Dict[str, Any]
    last_updated: Optional[str]
    total_parameters: int

class BackgroundJobInfo(BaseModel):
    """Schema for background job information."""
    
    job_id: str
    rfq_id: str
    status: JobStatusEnum
    stage: Optional[str]
    started_at: str
    duration_seconds: float

class BackgroundJobStatus(BaseModel):
    """Schema for background job status and statistics."""
    
    statistics: Dict[str, Any]
    active_jobs: List[BackgroundJobInfo]
    service_status: str

class CategorizationStatistics(BaseModel):
    """Schema for seller categorization statistics."""
    
    database_statistics: Dict[str, Any]
    job_statistics: Dict[str, Any]
    processing_statistics: Dict[str, Any]
    configuration: Dict[str, Any]

# ================================
# TESTING SCHEMAS
# ================================

class MockDataGenerationRequest(BaseModel):
    """Request schema for mock data generation."""
    
    num_sellers: int = Field(60, ge=1, le=200, description="Number of sellers to create")
    num_rfqs: int = Field(20, ge=1, le=100, description="Number of RFQs to create")
    clear_existing: bool = Field(False, description="Clear existing data first")

class MockDataGenerationResponse(BaseModel):
    """Response schema for mock data generation results."""
    
    success: bool
    generation_time_seconds: float
    data_created: Dict[str, int]
    seller_distribution: Dict[str, Any]
    rfq_distribution: Dict[str, Any]

class MockAPIStatistics(BaseModel):
    """Schema for mock API statistics."""
    
    mock_api_statistics: Dict[str, Any]
    service_status: str

# ================================
# PAGINATION SCHEMAS
# ================================

class PaginationInfo(BaseModel):
    """Schema for pagination information."""
    
    total: int
    limit: int
    offset: int
    has_more: bool

class SellerListResponse(BaseModel):
    """Response schema for paginated seller list."""
    
    sellers: List[SellerInfo]
    pagination: PaginationInfo

# ================================
# ERROR SCHEMAS
# ================================

class ErrorResponse(BaseModel):
    """Schema for API error responses."""
    
    success: bool = False
    error: str
    error_code: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

class ValidationErrorResponse(BaseModel):
    """Schema for validation error responses."""
    
    success: bool = False
    error: str = "Validation failed"
    validation_errors: List[Dict[str, Any]]

# ================================
# WEBHOOK SCHEMAS
# ================================

class WebhookDeliveryStatus(BaseModel):
    """Schema for webhook delivery status."""
    
    webhook_id: str
    rfq_id: str
    delivery_status: str  # delivered, failed, pending
    attempts: int
    last_attempt: datetime
    next_retry: Optional[datetime]

class WebhookResponse(BaseModel):
    """Standard webhook response schema."""
    
    success: bool
    message: str
    webhook_id: str
    processed_at: datetime