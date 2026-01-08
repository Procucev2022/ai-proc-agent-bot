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
    created_by: Optional[str] = Field(None, description="Created by user")
    created_ts: Optional[datetime] = Field(None, alias="createdTS", description="Creation timestamp")
    itemcode: Optional[str] = Field(None, description="Item code")
    
    @validator('quantity')
    def validate_quantity(cls, v):
        # Convert string to float if needed
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                raise ValueError('Quantity must be a valid number')
        if v <= 0:
            raise ValueError('Quantity must be greater than 0')
        if v > 10000000:
            raise ValueError('Please limit quantity to 1,00,00,000 pieces/units')
        return v
    
    @validator('unit_of_measures')
    def validate_unit(cls, v):
        valid_units = ['pcs', 'kg', 'grams', 'liters', 'meters', 'sqft', 'boxes', 'sets']
        if v.lower() not in valid_units:
            # Allow any unit but log for reference
            pass
        return v
    
    class Config:
        populate_by_name = True
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
        populate_by_name = True
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
    project_desc: Optional[str] = Field(None, alias="projectDesc", description="Project description (auto-populated by system)")
    delivery_date: datetime = Field(..., alias="deliveryDate", description="Required delivery date")
    division: Optional[str] = Field(None, description="Division/department (auto-populated via categorization)")
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
    
    
    class Config:
        populate_by_name = True
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
        populate_by_name = True
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
    field collection and progress tracking. Supports division auto-population.
    """
    # Core mandatory fields
    project_desc: Optional[str] = Field(None, description="Project description (auto-populated by system)")
    delivery_date: Optional[datetime] = Field(None, description="Required delivery date")
    division: Optional[str] = Field(None, description="Division/department")
    user_id: Optional[str] = Field(None, description="User ID")
    organization_id: Optional[str] = Field(None, description="Organization ID")
    
    # Date validation flag
    date_validation_error: bool = Field(False, description="Flag indicating if there's a date validation error")
    
    
    # Item details (at least one item required)
    items: List[Dict[str, Any]] = Field(default_factory=list, description="RFQ items")
    
    # Delivery location (at least one required)
    delivery_locations: List[Dict[str, Any]] = Field(default_factory=list, description="Delivery locations")
    
    # Optional fields
    remarks: Optional[str] = Field(None, description="Additional remarks")
    vendors: List[Dict[str, Any]] = Field(default_factory=list, description="Selected vendors")
    preferred_brand: Optional[str] = Field(None, description="Preferred brand")
    attachments: List[Dict[str, Any]] = Field(default_factory=list, description="Document attachments")
    
    
    def _is_invalid_description(self, desc: str) -> bool:
        """
        Check if description is actually a unit of measure, generic term, or otherwise invalid.

        Returns True if the description should be considered invalid/missing.
        """
        if not desc:
            return True

        desc_lower = desc.lower().strip()

        # Invalid: Units of measure that should not be treated as product descriptions
        UNIT_KEYWORDS = {
            'pcs', 'pc', 'pieces', 'piece',
            'kg', 'kgs', 'kilogram', 'kilograms',
            'g', 'grams', 'gram',
            'liters', 'liter', 'l', 'lt',
            'meters', 'meter', 'm', 'mt',
            'boxes', 'box',
            'sets', 'set',
            'units', 'unit',
            'sqft', 'sqm'
        }

        # Invalid: Too generic terms that don't describe actual products
        GENERIC_TERMS = {
            'item', 'items',
            'product', 'products',
            'thing', 'things',
            'stuff',
            'something'
        }

        # Invalid: Just numbers
        if desc_lower.isdigit():
            return True

        # Invalid: Common units or generic terms
        if desc_lower in UNIT_KEYWORDS or desc_lower in GENERIC_TERMS:
            return True

        return False

    def get_missing_mandatory_fields(self) -> List[str]:
        """Return list of missing mandatory fields based on GMT API requirements."""
        missing = []

        # User-providable mandatory fields (from GMT API)
        # Note: project_desc is now auto-populated by system, not required from user
        if not self.delivery_date or self.date_validation_error:
            missing.append("delivery_date")

        # Division is now auto-populated via categorization service - not required from user

        if not self.items:
            missing.append("items")
        else:
            # Check individual item fields for completeness
            for i, item in enumerate(self.items):
                description = item.get("description", "").strip() if item.get("description") else ""

                # Check if description is missing OR invalid (unit of measure, generic term, etc.)
                if not description or self._is_invalid_description(description):
                    missing.append(f"item_{i}_description")

                quantity = item.get("quantity")
                if not quantity:
                    missing.append(f"item_{i}_quantity")
                else:
                    # Convert to float for comparison if it's a string
                    try:
                        quantity_val = float(quantity) if isinstance(quantity, str) else quantity
                        if quantity_val <= 0:
                            missing.append(f"item_{i}_quantity")
                    except (ValueError, TypeError):
                        missing.append(f"item_{i}_quantity")
        
        if not self.delivery_locations:
            missing.append("delivery_locations")
        else:
            # Check individual delivery location fields
            for i, location in enumerate(self.delivery_locations):
                if not location.get("state"):
                    missing.append(f"delivery_location_{i}_state")
                if not location.get("city"):
                    missing.append(f"delivery_location_{i}_city")
                if not location.get("pincode"):
                    missing.append(f"delivery_location_{i}_pincode")
            
        # Note: user_id and organization_id are system fields, auto-populated
        return missing
    
    def get_missing_optional_fields(self) -> List[str]:
        """Return list of missing optional fields that could enhance the RFQ."""
        missing = []
        
        if not self.preferred_brand:
            missing.append("preferred_brand")
        if not self.remarks:
            missing.append("remarks")
        if not self.attachments:
            missing.append("attachments")
            
        return missing
    
    def get_completeness_percentage(self) -> float:
        """Calculate completeness percentage based on filled fields."""
        total_fields = 9  # 6 mandatory + 3 optional (project_desc is auto-populated)
        filled_fields = 0

        # Check mandatory fields (project_desc excluded as it's auto-populated)
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
        if self.remarks:
            filled_fields += 1
        if self.vendors:
            filled_fields += 1
        if self.attachments:
            filled_fields += 1
            
        return (filled_fields / total_fields) * 100
    
    def is_complete(self) -> bool:
        """Check if all mandatory fields are filled."""
        return len(self.get_missing_mandatory_fields()) == 0
    
    def get_mandatory_questions(self) -> List[str]:
        """Generate user-friendly questions for missing mandatory fields with smart field grouping."""
        missing = self.get_missing_mandatory_fields()
        questions = []

        # Field groups for related fields that should be asked together
        FIELD_GROUPS = {
            "delivery_address": {
                "fields": ["delivery_location_0_state", "delivery_location_0_city", "delivery_location_0_pincode"],
                "group_question": "Where should the items be delivered? Please provide the complete address (State, City, and Pincode)",
                "individual_questions": {
                    "delivery_location_0_state": "What is the delivery state?",
                    "delivery_location_0_city": "What is the delivery city?",
                    "delivery_location_0_pincode": "What is the delivery pincode?"
                }
            },
            "item_details": {
                "fields": ["item_0_description", "item_0_quantity"],
                "group_question": "What are the item details? Please provide the description and quantity needed",
                "individual_questions": {
                    "item_0_description": "What is the item description?",
                    "item_0_quantity": "How many items do you need (quantity)?"
                }
            }
        }

        # Question mapping for standalone fields
        standalone_question_map = {
            "delivery_date": "What is the required delivery date?",
            "division_confirmation": "Please confirm the division for this request",
            "division_selection": "Which division or department is this for?",
            "items": "What items do you need for your RFQ?",
            "delivery_locations": "Where should the items be delivered?"
        }

        # Process field groups first
        processed_fields = set()

        for group_name, group_config in FIELD_GROUPS.items():
            group_fields = group_config["fields"]
            missing_in_group = [f for f in missing if f in group_fields]

            if missing_in_group:
                if len(missing_in_group) >= 2:
                    # Multiple fields missing in group - ask group question
                    questions.append(group_config["group_question"])
                    processed_fields.update(group_fields)
                else:
                    # Only one field missing - ask specific question
                    missing_field = missing_in_group[0]
                    questions.append(group_config["individual_questions"][missing_field])
                    processed_fields.add(missing_field)

        # Process standalone fields
        for missing_field in missing:
            if missing_field in processed_fields:
                continue

            if missing_field == "delivery_date":
                questions.append(standalone_question_map["delivery_date"])
            elif missing_field == "division_confirmation":
                questions.append(standalone_question_map["division_confirmation"])
            elif missing_field == "division":
                questions.append(standalone_question_map["division_selection"])
            elif missing_field == "items":
                questions.append(standalone_question_map["items"])
            elif missing_field == "delivery_locations":
                questions.append(standalone_question_map["delivery_locations"])
            else:
                # Handle dynamic field names that don't match exact patterns
                if missing_field.startswith("item_") and "_description" in missing_field:
                    if not any("item details" in q.lower() for q in questions):
                        questions.append("What is the item description?")
                elif missing_field.startswith("item_") and "_quantity" in missing_field:
                    if not any("quantity" in q.lower() for q in questions):
                        questions.append("How many items do you need (quantity)?")
                elif missing_field.startswith("delivery_location_") and "_state" in missing_field:
                    if not any("address" in q.lower() or "delivered" in q.lower() for q in questions):
                        questions.append("What is the delivery state?")
                elif missing_field.startswith("delivery_location_") and "_city" in missing_field:
                    if not any("address" in q.lower() or "delivered" in q.lower() for q in questions):
                        questions.append("What is the delivery city?")
                elif missing_field.startswith("delivery_location_") and "_pincode" in missing_field:
                    if not any("address" in q.lower() or "delivered" in q.lower() for q in questions):
                        questions.append("What is the delivery pincode?")

        # Remove duplicates while preserving order
        return list(dict.fromkeys(questions))
    
    
    
    def get_optional_questions(self) -> List[str]:
        """Generate questions for optional fields."""
        questions = []

        if not self.attachments:
            questions.append("Would you like to add any specification documents or images to your RFQ? ")
        
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
    
    def set_date_validation_error(self, has_error: bool = True):
        """Set the date validation error flag."""
        self.date_validation_error = has_error
    
    def has_date_validation_error(self) -> bool:
        """Check if there's a date validation error."""
        return self.date_validation_error
    
    def get_combined_questions(self, include_optional: bool = True) -> Dict[str, List[str]]:
        """
        Generate both mandatory and optional questions in one response.
        Returns a dict with 'mandatory' and 'optional' question lists.
        """
        mandatory_questions = self.get_mandatory_questions()
        optional_questions = self.get_optional_questions() if include_optional else []
        
        return {
            "mandatory": mandatory_questions,
            "optional": optional_questions,
            "has_mandatory": len(mandatory_questions) > 0,
            "has_optional": len(optional_questions) > 0
        }
    
    class Config:
        extra = "allow"


class RFQUpdateSchema(BaseModel):
    """
    Schema for updating existing RFQ records.

    Allows partial updates to RFQ fields during the collection process.
    """
    project_desc: Optional[str] = Field(None, description="Project description (auto-populated by system)")
    delivery_date: Optional[datetime] = Field(None, description="Required delivery date")
    division: Optional[str] = Field(None, description="Division/department")
    remarks: Optional[str] = Field(None, description="Additional remarks")
    items: Optional[List[Dict[str, Any]]] = Field(None, description="RFQ items")
    delivery_locations: Optional[List[Dict[str, Any]]] = Field(None, description="Delivery locations")
    vendors: Optional[List[Dict[str, Any]]] = Field(None, description="Selected vendors")
    
    class Config:
        extra = "allow"