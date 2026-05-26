import re
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from enum import Enum


# -------------------------------
# Enums
# -------------------------------

class UserRole(str, Enum):
    BUYER = "buyer"
    SELLER = "seller"
    UNKNOWN = "unknown" #fallback


# -------------------------------
# Validators (Regex)
# -------------------------------

# Comprehensive email validation
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)
# Sanitized phone number (removes spaces, dashes, plus signs)
PHONE_REGEX = re.compile(r"^[6-9]\d{9}$")
PINCODE_REGEX = re.compile(r"^\d{6}$")
# GSTIN Format: 2-digit state code + 5 letters (PAN) + 4 digits (PAN) + 1 letter (PAN) + entity code (1-9/A-Z) + Z + checksum
GSTIN_REGEX = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$")


def normalize_phone_number(phone: str, default_country_code: str = "91") -> str:
    """Normalize and format phone number to +<countrycode><number> format.
    
    - Strips spaces, dashes, parentheses, dots
    - Ensures +<countrycode> prefix
    - Defaults to +91 for 10-digit numbers (India)
    """
    if not phone:
        return ""

    # Remove unwanted characters
    phone_clean = re.sub(r'[\s\-()."\']', '', phone.strip())


    # If starts with +, assume already correct
    if phone_clean.startswith('+'):
        return phone_clean

    # If starts with country code without +, add +
    if phone_clean.startswith(default_country_code):
        return f"+{phone_clean}"

    # If 10-digit number, assume local number and add country code
    if len(phone_clean) == 10 and phone_clean.isdigit():
        return f"+{default_country_code}{phone_clean}"

    # Default: just prefix with + if missing
    if not phone_clean.startswith('+'):
        return f"+{default_country_code}{phone_clean}"

    return phone_clean




# -------------------------------
# Schemas
# -------------------------------

class BuyerRegistrationSchema(BaseModel):
    name: str = Field(..., description="Full name")
    companyName: str = Field(..., description="Organization Full Name( with Pvt Ltd./Ltd./LLP)")
    email: str = Field(..., description="Organization email")
    zipCode: str = Field(..., description="Pincode")
    organizationPhonenumber: Optional[str] = Field(None, description="Phone number")
    whatsApp: Optional[bool] = Field(None, description="WhatsApp flag")
    sourceType: str = "W"

    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        v = v.strip().title()
        if not re.match(r"^[A-Za-z\s.]+$", v):
            raise ValueError("Please use only letters and spaces (no numbers or special characters)")
        return v

    @field_validator("companyName")
    def validate_company(cls, v: str) -> str:
        v = v.strip().title()
        return v

    @field_validator("email")
    def validate_email(cls, v: str) -> str:
        if not v or len(v.strip()) == 0:
            raise ValueError("Email cannot be empty")
        v = v.strip().lower()
        if not EMAIL_REGEX.match(v):
            raise ValueError("Please enter a valid email address (e.g., name@company.com)")
        return v

    @field_validator("organizationPhonenumber")
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        sanitized = normalize_phone_number(v)
        if not PHONE_REGEX.match(sanitized):
            raise ValueError("Please enter a valid 10-digit Indian mobile number")
        return sanitized

    @field_validator("zipCode")
    def validate_zipcode(cls, v: str) -> str:
        if not PINCODE_REGEX.match(v):
            raise ValueError("Please enter a valid 6-digit Indian pincode")
        return v


class SellerRegistrationSchema(BaseModel):
    name: str = Field(..., description="Full name")
    companyName: str = Field(..., description="Organization Full Name( with Pvt Ltd./Ltd./LLP)")
    email: str = Field(..., description="Organization email")
    address1: str = Field(..., description="Location")
    zipCode: str = Field(..., description="Pincode")
    gstin: str = Field(..., description="GSTIN number")
    details: str = Field(..., description="Products or Services offered")
    organizationPhonenumber: Optional[str] = Field(None, description="Phone number")
    whatsApp: Optional[bool] = Field(None, description="WhatsApp flag")
    sourceType: str = "W"

    @field_validator("name")
    def validate_name(cls, v: str) -> str:
        v = v.strip().title()
        if not re.match(r"^[A-Za-z\s.]+$", v):
            raise ValueError("Please use only letters and spaces (no numbers or special characters)")
        return v

    @field_validator("companyName")
    def validate_company(cls, v: str) -> str:
        v = v.strip().title()
        return v

    @field_validator("address1")
    def validate_address(cls, v: str) -> str:
        v = v.strip().title()
        return v

    @field_validator("details")
    def validate_details(cls, v: str) -> str:
        v = v.strip()
        return v

    @field_validator("email")
    def validate_email(cls, v: str) -> str:
        if not v or len(v.strip()) == 0:
            raise ValueError("Email cannot be empty")
        v = v.strip().lower()
        if not EMAIL_REGEX.match(v):
            raise ValueError("Please enter a valid email address (e.g., name@company.com)")
        return v

    @field_validator("organizationPhonenumber")
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        sanitized = normalize_phone_number(v)
        if not PHONE_REGEX.match(sanitized):
            raise ValueError("Please enter a valid 10-digit Indian mobile number")
        return sanitized

    @field_validator("zipCode")
    def validate_zipcode(cls, v: str) -> str:
        if not PINCODE_REGEX.match(v):
            raise ValueError("Please enter a valid 6-digit Indian pincode")
        return v

    @field_validator("gstin")
    def validate_gstin(cls, v: str) -> str:
        v = v.strip().upper()
        if not GSTIN_REGEX.match(v):
            raise ValueError(
                "Invalid format. GSTIN should be 15 characters like: 27ABCDE1234F1Z5"
            )
        return v

class APIUserSchema(BaseModel):
    id: str
    username: Optional[str] = None
    fullName: Optional[str] = None
    selfClient: Optional[bool] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    companyName: Optional[str] = None
    uniqueId: Optional[str] = None
    orgId: Optional[str] = None
    verificationStatus: Optional[str] = None
    approved: Optional[bool] = None
    

class User(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    self_client: bool = False
    role: UserRole = UserRole.UNKNOWN
    is_registered: bool = False
    phone_number: Optional[str] = None
    company_name: Optional[str] = None
    unique_id: Optional[str] = None
    org_id: Optional[str] = None
    verification_status: Optional[str] = None
    approved: Optional[bool] = None
    otp_validated_at: Optional[float] = None  # Unix timestamp of last OTP validation

    @classmethod
    def from_api_response(cls, api_data: dict) -> "User":
        """Create User from API response."""
        # Ensure we're working with a dict
        if not isinstance(api_data, dict):
            if hasattr(api_data, 'dict'):
                api_data = api_data.dict()
            elif hasattr(api_data, '__dict__'):
                api_data = api_data.__dict__
            else:
                raise ValueError("api_data must be a dictionary or have dict() method")

        # Determine role
        self_client_value = api_data.get("selfClient")
        if self_client_value is True:
            role = UserRole.BUYER
        elif self_client_value is False:
            role = UserRole.SELLER
        else:
            role = UserRole.UNKNOWN

        # Handle both 'id' and 'userId' fields for Redis compatibility
        user_id = str(api_data.get("id") or api_data.get("userId", ""))
        if not user_id:
            raise ValueError("User ID is required")

        # Keep original phone format from API (with country code)
        phone = api_data.get("phone")
        # Don't sanitize - keep the original format for session consistency
        
        # Set is_registered to True by default
        verification_status = api_data.get("verificationStatus") or "PENDING_EMAIL_VERIFICATION"
        is_registered = True

        return cls(
            id=user_id,
            name=api_data.get("fullName"),
            email=api_data.get("username"),
            self_client=api_data.get("selfClient", False),
            role=role,
            is_registered=is_registered,
            phone_number=phone,
            company_name=api_data.get("companyName"),
            unique_id=api_data.get("uniqueId"),
            org_id=api_data.get("orgId"),
            verification_status=verification_status,
            approved=api_data.get("approved")
        )

    @classmethod
    def from_mixed_data(cls, data) -> "User":
        """Create User from either API response or User dict format."""
        # Handle None or empty data
        if not data:
            raise ValueError("Cannot create User from empty data")
        
        # If it's already a User object, return it
        if isinstance(data, cls):
            return data
            
        # Convert to dict if it's an object with dict method or __dict__
        if not isinstance(data, dict):
            if hasattr(data, 'dict'):
                data = data.dict()
            elif hasattr(data, '__dict__'):
                data = data.__dict__
            else:
                raise ValueError("Cannot convert data to dictionary format")
            
        # Check if it's already in User format (has 'name', 'email' fields)
        if 'name' in data and 'email' in data:
            # Set is_registered to True by default
            user_data = data.copy()
            user_data['is_registered'] = True
            return cls(**user_data)
        # Otherwise treat as API response format
        return cls.from_api_response(data)

    @classmethod
    def invalid_user(cls, user_phone: str) -> "User":
        """Returns a dummy user for test environments or invalid cases."""
        return cls(
            id="1428bbb9-a0ba-459d-b1e8-23d7c49455e8",
            name="shubham",
            email="shubham@mohap.ai",
            self_client=True,
            role=UserRole.UNKNOWN,
            is_registered=True,
            phone_number=user_phone,
            company_name="mohap ai solution"
        )