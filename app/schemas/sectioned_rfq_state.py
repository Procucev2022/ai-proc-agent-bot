"""
Sectioned RFQ State Schema

Pydantic models for managing sectioned RFQ creation workflow state.
Defines the structure for tracking progress through 4 sections:
- Date/Location
- Items
- Attachments
- Final Confirmation
"""

from enum import Enum
from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field
from datetime import datetime


class SectionType(str, Enum):
    """Enum for sectioned RFQ sections"""
    DATE_LOCATION = "date_location"
    ITEMS = "items"
    ATTACHMENTS = "attachments"
    FINAL_CONFIRMATION = "final_confirmation"


class SectionState(BaseModel):
    """State for individual section in sectioned RFQ workflow"""

    confirmed: bool = Field(default=False, description="Whether user has confirmed this section")
    data: Optional[Any] = Field(default=None, description="Section-specific data (dict for delivery, list for items)")
    retry_count: int = Field(default=0, ge=0, le=3, description="Number of format validation retry attempts (max 3)")
    awaiting_modification: bool = Field(default=False, description="Whether waiting for user to provide modified format")

    class Config:
        json_schema_extra = {
            "example": {
                "confirmed": False,
                "data": {"deliveryDate": "12 Nov 2025", "pincode": "411005", "city": "Pune", "state": "Maharashtra"},
                "retry_count": 0,
                "awaiting_modification": False
            }
        }


class SectionedRFQState(BaseModel):
    """Complete state for sectioned RFQ workflow"""

    active: bool = Field(default=False, description="Whether sectioned RFQ workflow is active")
    current_section: SectionType = Field(default=SectionType.DATE_LOCATION, description="Current section user is in")
    sections: Dict[str, SectionState] = Field(
        default_factory=lambda: {
            "date_location": SectionState(),
            "items": SectionState(),
            "attachments": SectionState(),
            "final_confirmation": SectionState()
        },
        description="State for each section"
    )
    pending_restart: bool = Field(default=False, description="Whether restart confirmation is pending")
    initial_extraction_done: bool = Field(default=False, description="Whether initial entity extraction has been performed")
    from_excel: bool = Field(default=False, description="Whether RFQ originated from Excel upload (allows >5 items)")

    class Config:
        json_schema_extra = {
            "example": {
                "active": True,
                "current_section": "date_location",
                "sections": {
                    "date_location": {
                        "confirmed": False,
                        "data": None,
                        "retry_count": 0,
                        "awaiting_modification": False
                    },
                    "items": {
                        "confirmed": False,
                        "data": None,
                        "retry_count": 0,
                        "awaiting_modification": False
                    },
                    "attachments": {
                        "confirmed": False,
                        "data": None,
                        "retry_count": 0,
                        "awaiting_modification": False
                    },
                    "final_confirmation": {
                        "confirmed": False,
                        "data": None,
                        "retry_count": 0,
                        "awaiting_modification": False
                    }
                },
                "pending_restart": False,
                "initial_extraction_done": False
            }
        }

    def get_section_state(self, section: SectionType) -> SectionState:
        """Get state for specific section"""
        return self.sections.get(section.value, SectionState())

    def update_section_state(self, section: SectionType, state: SectionState):
        """Update state for specific section"""
        self.sections[section.value] = state

    def is_section_confirmed(self, section: SectionType) -> bool:
        """Check if section is confirmed"""
        section_state = self.get_section_state(section)
        return section_state.confirmed

    def get_next_section(self, current: SectionType) -> Optional[SectionType]:
        """Get next section in workflow"""
        section_order = [
            SectionType.DATE_LOCATION,
            SectionType.ITEMS,
            SectionType.ATTACHMENTS,
            SectionType.FINAL_CONFIRMATION
        ]

        try:
            current_idx = section_order.index(current)
            if current_idx < len(section_order) - 1:
                return section_order[current_idx + 1]
        except ValueError:
            pass

        return None

    def get_previous_section(self, current: SectionType) -> Optional[SectionType]:
        """Get previous section in workflow"""
        section_order = [
            SectionType.DATE_LOCATION,
            SectionType.ITEMS,
            SectionType.ATTACHMENTS,
            SectionType.FINAL_CONFIRMATION
        ]

        try:
            current_idx = section_order.index(current)
            if current_idx > 0:
                return section_order[current_idx - 1]
        except ValueError:
            pass

        return None

    def reset_section(self, section: SectionType):
        """Reset section to initial state"""
        self.sections[section.value] = SectionState()

    def reset_all_sections(self):
        """Reset all sections to initial state"""
        for section in SectionType:
            self.reset_section(section)
        self.current_section = SectionType.DATE_LOCATION
        self.pending_restart = False
        self.initial_extraction_done = False

    def to_dict(self) -> dict:
        """Convert to dictionary for storage in session.workflow_state"""
        return {
            "active": self.active,
            "current_section": self.current_section.value,
            "sections": {
                key: {
                    "confirmed": state.confirmed,
                    "data": state.data,
                    "retry_count": state.retry_count,
                    "awaiting_modification": state.awaiting_modification
                }
                for key, state in self.sections.items()
            },
            "pending_restart": self.pending_restart,
            "initial_extraction_done": self.initial_extraction_done,
            "from_excel": self.from_excel
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SectionedRFQState':
        """Create from dictionary stored in session.workflow_state"""
        sections = {}
        for key, state_data in data.get("sections", {}).items():
            sections[key] = SectionState(**state_data)

        return cls(
            active=data.get("active", False),
            current_section=SectionType(data.get("current_section", "date_location")),
            sections=sections,
            pending_restart=data.get("pending_restart", False),
            initial_extraction_done=data.get("initial_extraction_done", False),
            from_excel=data.get("from_excel", False)
        )


# Helper function to initialize sectioned RFQ state
def initialize_sectioned_rfq_state() -> dict:
    """
    Initialize new sectioned RFQ state structure.

    Returns:
        Dictionary ready to be stored in session.workflow_state["sectioned_rfq"]
    """
    state = SectionedRFQState()
    return state.to_dict()
