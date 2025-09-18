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
GSTIN_REGEX = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$")


def normalize_phone_number(phone: str, default_country_code: str = "91") -> str:
    """Normalize and format phone number to +<countrycode><number> format.
    
    - Strips spaces, dashes, parentheses, dots
    - Ensures +<countrycode> prefix
    - Defaults to +91 for 10-digit numbers (India)
    """
    if not phone:
        return ""

    # Remove unwanted characters
    phone_clean = re.sub(r'[\s\-\(\)\.\"']', '', phone.strip())

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
    company_name: str = Field(..., description="Company name")
    email: str = Field(..., description="Organization email")
    pincode: str = Field(..., description="Pincode")
    organizationPhonenumber: Optional[str] = Field(None, description="Phone number")
    whatsapp: Optional[bool] = Field(None, description="WhatsApp flag")
    source_type: str = "W"

    @field_validator("email")
    def validate_email(cls, v: str) -> str:
        if not v or len(v.strip()) == 0:
            raise ValueError("Email cannot be empty")
        v = v.strip().lower()
        if len(v) > 254:  # RFC 5321 limit
            raise ValueError("Email address too long")
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("organizationPhonenumber")
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        sanitized = normalize_phone_number(v)
        if not PHONE_REGEX.match(sanitized):
            raise ValueError("Invalid phone number format")
        return sanitized

    @field_validator("pincode")
    def validate_pincode(cls, v: str) -> str:
        if not PINCODE_REGEX.match(v):
            raise ValueError("Pincode must be 6 digits")
        return v


class SellerRegistrationSchema(BaseModel):
    full_name: str
    company_name: str
    email: str
    location: str
    pincode: str
    gstin: str
    products_services: str
    organizationPhonenumber: Optional[str] = None
    whatsapp: Optional[bool] = None
    source_type: str = "W"

    @field_validator("email")
    def validate_email(cls, v: str) -> str:
        if not v or len(v.strip()) == 0:
            raise ValueError("Email cannot be empty")
        v = v.strip().lower()
        if len(v) > 254:  # RFC 5321 limit
            raise ValueError("Email address too long")
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("organizationPhonenumber")
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        sanitized = normalize_phone_number(v)
        if not PHONE_REGEX.match(sanitized):
            raise ValueError("Invalid phone number format")
        return sanitized

    @field_validator("pincode")
    def validate_pincode(cls, v: str) -> str:
        if not PINCODE_REGEX.match(v):
            raise ValueError("Pincode must be 6 digits")
        return v

    @field_validator("gstin")
    def validate_gstin(cls, v: str) -> str:
        if not GSTIN_REGEX.match(v):
            raise ValueError("Invalid GSTIN format")
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

    @classmethod
    def from_api_response(cls, api_data: dict) -> "User":
        """Create User from API response."""
        # Debug logging to see raw API data
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Creating User from API data: {api_data}")

        # Determine role
        match api_data.get("selfClient"):
            case True:
                role = UserRole.BUYER
            case False:
                role = UserRole.SELLER
            case _:
                role = UserRole.UNKNOWN

        # Handle both 'id' and 'userId' fields for Redis compatibility
        user_id = str(api_data.get("id") or api_data.get("userId", ""))
        if not user_id:
            raise ValueError("User ID is required")

        # Keep original phone format from API (with country code)
        phone = api_data.get("phone")
        # Don't sanitize - keep the original format for session consistency
        
        # Check verification status for is_registered flag
        verification_status = api_data.get("verificationStatus") or "PENDING_EMAIL_VERIFICATION"
        is_registered = verification_status == "EMAIL_VERIFIED"

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
            verification_status=verification_status
        )

    @classmethod
    def from_mixed_data(cls, data: dict) -> "User":
        """Create User from either API response or User dict format."""
        # Check if it's already in User format (has 'name', 'email' fields)
        if 'name' in data and 'email' in data:
            # Ensure is_registered follows verification_status rule
            user_data = data.copy()
            verification_status = user_data.get('verification_status', 'PENDING_EMAIL_VERIFICATION')
            user_data['is_registered'] = verification_status == 'EMAIL_VERIFIED'
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