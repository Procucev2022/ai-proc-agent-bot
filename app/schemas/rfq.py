"""
RFQ (Request for Quotation) Pydantic schemas.

This module contains all schemas related to RFQ creation, validation, and management
based on the GMT API structure and business requirements.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime


class RFQItemSchema(BaseModel):
    """
    Schema for individual RFQ items.
    
    Validates item details including description, quantity, and measurements.
    """
    description: str = Field(..., description="Item description")
    quantity: float = Field(..., gt=0, description="Quantity required")
    unit_of_measures: str = Field(..., description="Unit of measurement (pcs, kg, etc.)")
    brand: Optional[str] = Field(None, description="Preferred brand")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    serial_no: Optional[float] = Field(None, description="Serial number for ordering")
    category: Optional[str] = Field(None, description="Item category")
    created_by: Optional[str] = Field(None, description="Created by user")
    created_ts: Optional[datetime] = Field(None, alias="createdTS", description="Creation timestamp")
    itemcode: Optional[str] = Field(None, description="Item code")
    
    @validator('quantity')
    def validate_quantity(cls, v):
        if v <= 0:
            raise ValueError('Quantity must be greater than 0')
        return v
    
    @validator('unit_of_measures')
    def validate_unit(cls, v):
        valid_units = ['pcs', 'kg', 'grams', 'liters', 'meters', 'sqft', 'boxes', 'sets']
        if v.lower() not in valid_units:
            # Allow any unit but log for reference
            pass
        return v
    
    class Config:
        allow_population_by_field_name = True
        extra = "allow"


class RFQDeliveryLocationSchema(BaseModel):
    """
    Schema for RFQ delivery location.
    
    Validates delivery address details including state, city, and pincode.
    """
    state: str = Field(..., description="Delivery state")
    city: str = Field(..., description="Delivery city")
    pincode: str = Field(..., description="Delivery pincode")
    
    @validator('pincode')
    def validate_pincode(cls, v):
        if not v.isdigit() or len(v) != 6:
            raise ValueError('Pincode must be 6 digits')
        return v
    
    @validator('state')
    def validate_state(cls, v):
        # List of Indian states for validation
        indian_states = [
            'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chhattisgarh',
            'Goa', 'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jharkhand', 'Karnataka',
            'Kerala', 'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya', 'Mizoram',
            'Nagaland', 'Odisha', 'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu', 
            'Telangana', 'Tripura', 'Uttar Pradesh', 'Uttarakhand', 'West Bengal'
        ]
        if v not in indian_states:
            # Allow any state but could be used for validation
            pass
        return v
    
    class Config:
        extra = "allow"


class RFQVendorSchema(BaseModel):
    """
    Schema for RFQ vendor selection.
    
    Validates vendor details for RFQ assignment.
    """
    id: str = Field(..., description="Vendor ID")
    email: str = Field(..., description="Vendor email")
    other_emails: Optional[List[str]] = Field(None, alias="otherEmails", description="Additional vendor emails")
    vendor_id: Optional[str] = Field(None, alias="vendorId", description="Vendor identifier")
    
    @validator('email')
    def validate_email(cls, v):
        if '@' not in v or '.' not in v:
            raise ValueError('Invalid email format')
        return v
    
    class Config:
        allow_population_by_field_name = True
        extra = "allow"


class RFQOrganizationSchema(BaseModel):
    """
    Schema for RFQ organization details.
    
    Validates organization information for RFQ creation.
    """
    id: str = Field(..., description="Organization ID")
    
    class Config:
        extra = "allow"


class RFQCreateRequestSchema(BaseModel):
    """
    Schema for RFQ creation requests.
    
    Validates complete RFQ creation data including all required fields
    and business rule constraints based on GMT API structure.
    """
    # Required fields
    created_by: str = Field(..., alias="createdBy", description="User who created the RFQ")
    project_desc: str = Field(..., alias="projectDesc", description="Project description")
    delivery_date: datetime = Field(..., alias="deliveryDate", description="Required delivery date")
    division: str = Field(..., description="Division/department")
    user: str = Field(..., description="User ID")
    org: RFQOrganizationSchema = Field(..., description="Organization details")
    rfq_item: List[RFQItemSchema] = Field(..., alias="rfqItem", min_items=1, description="RFQ items")
    client_delivery_location_rfq: List[RFQDeliveryLocationSchema] = Field(
        ..., 
        alias="clientdeliverylocationrfq", 
        min_items=1, 
        description="Delivery locations"
    )
    
    # Optional fields
    no_pr_flag: bool = Field(True, alias="noPrFlag", description="No PR flag")
    vendors: List[RFQVendorSchema] = Field(default_factory=list, description="Selected vendors")
    remarks: Optional[str] = Field("", description="Additional remarks")
    rfq_document: List[Dict[str, Any]] = Field(default_factory=list, alias="rfqDocument", description="RFQ documents")
    category: Optional[str] = Field(None, description="Product category")
    
    @validator('delivery_date')
    def validate_delivery_date(cls, v):
        if v <= datetime.now():
            raise ValueError('Delivery date must be in the future')
        return v
    
    @validator('rfq_item')
    def validate_rfq_items(cls, v):
        if not v:
            raise ValueError('At least one RFQ item is required')
        return v
    
    @validator('client_delivery_location_rfq')
    def validate_delivery_locations(cls, v):
        if not v:
            raise ValueError('At least one delivery location is required')
        return v
    
    @validator('division')
    def validate_division(cls, v):
        # Valid divisions from GMT API
        valid_divisions = [
            "Admin & IT", "CAPEX - Equipment & Machinery", "Civil & Electrical Works",
            "Electrical - Engineering Items", "Logistics", "Mechanical - Engineering Items",
            "Occuptional Safety & Health", "Packing Material", "Professional Services",
            "Raw Materials"
        ]
        if v not in valid_divisions:
            # Allow any division but log for reference
            pass
        return v
    
    class Config:
        allow_population_by_field_name = True
        extra = "allow"


class RFQStatusResponseSchema(BaseModel):
    """
    Schema for RFQ status and update responses.
    
    Defines the structure of RFQ status information and
    workflow progress updates from GMT API.
    """
    status_code: str = Field(..., alias="statusCode", description="Response status code")
    message: str = Field(..., description="Response message")
    error_msg: Optional[str] = Field(None, alias="errorMsg", description="Error message if any")
    timestamp: str = Field(..., description="Response timestamp")
    status: str = Field(..., description="Response status")
    type: Optional[str] = Field(None, description="Response type")
    
    class Config:
        allow_population_by_field_name = True
        extra = "allow"


class ExcelValidationSchema(BaseModel):
    """
    Schema for Excel-specific field validation and completeness checking.
    
    Focuses only on Excel target fields: ['S.No', 'ItemDescription', 'Specification', 'Uom', 'Quantity', 'Remarks']
    Used when processing Excel files to ask only relevant questions about missing Excel fields.
    """
    # Excel-specific fields (matching target_columns from excel_processing_service)
    serial_no: Optional[str] = Field(None, description="Serial number (S.No)")
    item_description: Optional[str] = Field(None, description="Item description")
    specification: Optional[str] = Field(None, description="Item specification")
    uom: Optional[str] = Field(None, description="Unit of measurement")
    quantity: Optional[str] = Field(None, description="Quantity required")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    
    def get_missing_required_excel_fields(self) -> List[str]:
        """Return list of missing required Excel fields."""
        missing = []
        
        # Required fields for Excel processing (from excel_processing_service)
        if not self.item_description:
            missing.append("item_description")
        if not self.specification:
            missing.append("specification")
        if not self.uom:
            missing.append("uom")
        if not self.quantity:
            missing.append("quantity")
            
        return missing
    
    def get_missing_optional_excel_fields(self) -> List[str]:
        """Return list of missing optional Excel fields."""
        missing = []
        
        if not self.serial_no:
            missing.append("serial_no")
        if not self.remarks:
            missing.append("remarks")
            
        return missing
    
    def get_excel_completion_percentage(self) -> float:
        """Calculate completion percentage based on Excel fields."""
        total_fields = 6  # All Excel fields
        filled_fields = 0
        
        if self.serial_no:
            filled_fields += 1
        if self.item_description:
            filled_fields += 1
        if self.specification:
            filled_fields += 1
        if self.uom:
            filled_fields += 1
        if self.quantity:
            filled_fields += 1
        if self.remarks:
            filled_fields += 1
            
        return (filled_fields / total_fields) * 100
    
    def is_excel_complete(self) -> bool:
        """Check if all required Excel fields are filled."""
        return len(self.get_missing_required_excel_fields()) == 0
    
    def get_excel_questions(self) -> List[str]:
        """Generate questions for missing Excel fields."""
        missing = self.get_missing_required_excel_fields()
        questions = []
        
        if "item_description" in missing:
            questions.append("What is the item description? Please provide a detailed description of the item.")
        
        if "specification" in missing:
            questions.append("What are the specifications for this item? Please provide detailed specifications.")
        
        if "uom" in missing:
            questions.append("What is the unit of measurement? (e.g., pcs, kg, liters, meters)")
        
        if "quantity" in missing:
            questions.append("What quantity do you need? Please specify the number.")
        
        # Add optional field questions if required fields are complete
        if not missing:
            optional_missing = self.get_missing_optional_excel_fields()
            if "serial_no" in optional_missing:
                questions.append("Would you like to provide a serial number for this item?")
            if "remarks" in optional_missing:
                questions.append("Do you have any additional remarks or notes for this item?")
        
        return questions
    
    def get_next_excel_questions(self) -> List[str]:
        """Generate intelligent next questions based on missing Excel fields."""
        return self.get_excel_questions()
    
    class Config:
        extra = "allow"


class RFQValidationSchema(BaseModel):
    """
    Schema for RFQ field validation and completeness checking.
    
    Tracks which fields are mandatory vs optional and validates
    field values according to business rules. Used for intelligent
    field collection and progress tracking.
    """
    # Core mandatory fields
    project_desc: Optional[str] = Field(None, description="Project description")
    delivery_date: Optional[datetime] = Field(None, description="Required delivery date")
    division: Optional[str] = Field(None, description="Division/department")
    user_id: Optional[str] = Field(None, description="User ID")
    organization_id: Optional[str] = Field(None, description="Organization ID")
    
    # Item details (at least one item required)
    items: List[Dict[str, Any]] = Field(default_factory=list, description="RFQ items")
    
    # Delivery location (at least one required)
    delivery_locations: List[Dict[str, Any]] = Field(default_factory=list, description="Delivery locations")
    
    # Optional fields
    category: Optional[str] = Field(None, description="Product category")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    vendors: List[Dict[str, Any]] = Field(default_factory=list, description="Selected vendors")
    preferred_brand: Optional[str] = Field(None, description="Preferred brand")
    
    def get_missing_mandatory_fields(self) -> List[str]:
        """Return list of missing mandatory fields based on GMT API requirements."""
        missing = []
        
        # User-providable mandatory fields (from GMT API)
        if not self.project_desc:
            missing.append("project_desc")
        if not self.delivery_date:
            missing.append("delivery_date")
        if not self.division:
            missing.append("division")
        if not self.items:
            missing.append("items")
        if not self.delivery_locations:
            missing.append("delivery_locations")
            
        # Note: user_id and organization_id are system fields, auto-populated
        return missing
    
    def get_missing_optional_fields(self) -> List[str]:
        """Return list of missing optional fields that could enhance the RFQ."""
        missing = []
        
        if not self.category:
            missing.append("category")
        if not self.preferred_brand:
            missing.append("preferred_brand")
        if not self.remarks:
            missing.append("remarks")
            
        return missing
    
    def get_completeness_percentage(self) -> float:
        """Calculate completeness percentage based on filled fields."""
        total_fields = 10  # 7 mandatory + 3 optional
        filled_fields = 0
        
        # Check mandatory fields
        if self.project_desc:
            filled_fields += 1
        if self.delivery_date:
            filled_fields += 1
        if self.division:
            filled_fields += 1
        if self.user_id:
            filled_fields += 1
        if self.organization_id:
            filled_fields += 1
        if self.items:
            filled_fields += 1
        if self.delivery_locations:
            filled_fields += 1
            
        # Check optional fields
        if self.category:
            filled_fields += 1
        if self.remarks:
            filled_fields += 1
        if self.vendors:
            filled_fields += 1
            
        return (filled_fields / total_fields) * 100
    
    def is_complete(self) -> bool:
        """Check if all mandatory fields are filled."""
        return len(self.get_missing_mandatory_fields()) == 0
    
    def get_mandatory_questions(self) -> List[str]:
        """Generate ALL mandatory field questions at once."""
        missing = self.get_missing_mandatory_fields()
        questions = []
        
        if "project_desc" in missing:
            questions.append("What product or service do you need to procure? Please provide a detailed description.")
        
        if "delivery_date" in missing:
            questions.append("When do you need this delivered? Please provide a specific date.")
        
        if "division" in missing:
            questions.append("Which division or department is this for? (e.g., Admin & IT, CAPEX - Equipment & Machinery, etc.)")
        
        if "items" in missing:
            questions.append("Can you provide details about the items you need? Include quantity and unit of measurement.")
        
        if "delivery_locations" in missing:
            questions.append("Where should this be delivered? Please provide the complete address with state, city, and pincode.")
        
        return questions
    
    def get_optional_questions(self) -> List[str]:
        """Generate questions for optional fields."""
        questions = []
        
        if not self.category and self.project_desc:
            questions.append("What category does this product fall under?")
        
        if not self.preferred_brand and self.items:
            questions.append("Do you have any preferred brand or specifications?")
        
        if not self.remarks:
            questions.append("Do you have any additional remarks or special requirements?")
        
        return questions
    
    def get_next_questions(self) -> List[str]:
        """Generate intelligent next questions based on missing fields."""
        # First, check if we have any mandatory fields missing
        mandatory_questions = self.get_mandatory_questions()
        if mandatory_questions:
            return mandatory_questions  # Return ALL mandatory questions at once
        
        # If all mandatory fields are complete, ask about optional fields
        optional_questions = self.get_optional_questions()
        if optional_questions:
            return optional_questions
        
        return []  # No questions needed if everything is complete
    
    class Config:
        extra = "allow"


class RFQUpdateSchema(BaseModel):
    """
    Schema for updating existing RFQ records.
    
    Allows partial updates to RFQ fields during the collection process.
    """
    project_desc: Optional[str] = Field(None, description="Project description")
    delivery_date: Optional[datetime] = Field(None, description="Required delivery date")
    division: Optional[str] = Field(None, description="Division/department")
    category: Optional[str] = Field(None, description="Product category")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    items: Optional[List[Dict[str, Any]]] = Field(None, description="RFQ items")
    delivery_locations: Optional[List[Dict[str, Any]]] = Field(None, description="Delivery locations")
    vendors: Optional[List[Dict[str, Any]]] = Field(None, description="Selected vendors")
    
    class Config:
        extra = "allow"