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


def sanitize_phone_number(phone: str) -> str:
    """Sanitize phone number by removing non-digit characters."""
    if not phone:
        return phone
    # Remove spaces, dashes, plus signs, parentheses
    sanitized = re.sub(r'[\s\-\+\(\)\.]', '', phone)
    # Remove country code if present (91 for India)
    if sanitized.startswith('91') and len(sanitized) == 12:
        sanitized = sanitized[2:]
    return sanitized


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
        sanitized = sanitize_phone_number(v)
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
        sanitized = sanitize_phone_number(v)
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
    org_uuid: Optional[str] = None
    orgUuid: Optional[str] = None
    orgId: Optional[str] = None
    

class UserDetailsSchema(BaseModel):
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

    @classmethod
    def from_api_response(cls, api_data: dict) -> "UserDetailsSchema":
        """Create UserDetailsSchema from API response."""
        # Debug logging to see raw API data
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Creating UserDetailsSchema from API data: {api_data}")

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

        return cls(
            id=user_id,
            name=api_data.get("fullName"),
            email=api_data.get("username"),
            self_client=api_data.get("selfClient", False),
            role=role,
            is_registered=bool(user_id),
            phone_number=phone,
            company_name=api_data.get("companyName"),
            unique_id=api_data.get("uniqueId"),
            org_id=api_data.get("org_uuid") or api_data.get("orgUuid") or api_data.get("orgId") or api_data.get("organizationId")
        )

    @classmethod
    def invalid_user(cls, user_phone: str) -> "UserDetailsSchema":
        """Returns a dummy user for test environments or invalid cases."""
        return cls(
            id="1428bbb9-a0ba-459d-b1e8-23d7c49455e8",
            name="shubham",
            email="shubham@mohap.ai",
            self_client=True,
            role=UserRole.UNKNOWN,
            is_registered=True,
            phone_number=user_phone,
            company_name="mohap ai solutin",

           
        )