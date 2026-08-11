from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models import ConversationSession, ConversationOutcome, SessionState, UserType, WorkflowType
from app.schemas.common import ContactSchema, LocationSchema, PaginationSchema
from app.schemas.conversation import ConversationContextSchema, MessageSchema
from app.schemas.rfq import RFQCreateRequestSchema, RFQDeliveryLocationSchema, RFQItemSchema, RFQValidationSchema
from app.schemas.sectioned_rfq_state import SectionState, SectionType, SectionedRFQState, initialize_sectioned_rfq_state
from app.schemas.seller import RFQApprovalRequest, SystemConfigUpdate
from app.schemas.user import BuyerRegistrationSchema, SellerRegistrationSchema, User, UserRole, normalize_phone_number
from app.schemas.vendor import VendorQuerySchema, VendorResponseSchema
from app.schemas.whatsapp import WhatsAppIncomingMessageSchema, WhatsAppMessageSchema, WhatsAppOutgoingMessageSchema


def test_common_and_conversation_schemas():
    assert PaginationSchema(page=2, page_size=20, extra="ok").page == 2
    with pytest.raises(ValidationError):
        PaginationSchema(page=0)
    assert LocationSchema(state="M", city="Pune", pincode="411005").pincode == "411005"
    with pytest.raises(ValidationError):
        LocationSchema(state="M", city="Pune", pincode="123")
    with pytest.raises(ValidationError):
        ContactSchema(name="A", email="bad")
    context = ConversationContextSchema(session_id=" s ", external_user_id=" u ")
    assert context.session_id == "s"
    with pytest.raises(ValidationError):
        ConversationContextSchema(session_id=" ", external_user_id="u")
    assert MessageSchema(message_id="1", session_id="s", sender="user", content=" hi ", timestamp="now").content == "hi"
    with pytest.raises(ValidationError):
        MessageSchema(message_id="1", session_id="s", sender="bad", content="x")


def test_rfq_schemas_and_validation_helpers():
    item = RFQItemSchema(description="Laptop", quantity="2", unit_of_measures="pcs")
    assert item.quantity == 2
    with pytest.raises(ValidationError):
        RFQItemSchema(description="x", quantity=0, unit_of_measures="pcs")
    with pytest.raises(ValidationError):
        RFQItemSchema(description="x", quantity=10000001, unit_of_measures="pcs")
    with pytest.raises(ValidationError):
        RFQDeliveryLocationSchema(state="Maharashtra", city="Pune", pincode="123")
    future = datetime.now() + timedelta(days=2)
    request = RFQCreateRequestSchema(
        createdBy="u", deliveryDate=future, user="u", org={"id": "o"},
        rfqItem=[{"description": "Laptop", "quantity": 1, "unit_of_measures": "pcs"}],
        clientdeliverylocationrfq=[{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}],
    )
    assert request.rfq_item[0].description == "Laptop"
    schema = RFQValidationSchema(items=[{"description": "Laptop", "quantity": 2}], delivery_locations=[{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}], delivery_date=future)
    assert schema.is_complete()
    assert schema.get_missing_optional_fields()
    schema.set_date_validation_error()
    assert schema.has_date_validation_error()
    assert schema.get_next_questions()
    assert schema.get_combined_questions(include_optional=False)["optional"] == []
    incomplete = RFQValidationSchema(items=[{"description": "kg", "quantity": 0}], delivery_locations=[{}])
    assert incomplete.get_missing_mandatory_fields()


def test_section_state_seller_vendor_user_and_whatsapp_schemas():
    state = SectionedRFQState()
    assert state.get_next_section(SectionType.DATE_LOCATION) == SectionType.ITEMS
    assert state.get_previous_section(SectionType.DATE_LOCATION) is None
    state.update_section_state(SectionType.ITEMS, SectionState(confirmed=True))
    assert state.is_section_confirmed(SectionType.ITEMS)
    state.reset_all_sections()
    restored = SectionedRFQState.from_dict(initialize_sectioned_rfq_state())
    assert restored.to_dict()["current_section"] == "date_location"
    with pytest.raises(ValidationError):
        SectionState(retry_count=4)
    approval = RFQApprovalRequest(rfq_id="1", rfq_title="x", categories=["a"], delivery_location={"state":"M", "city":"P", "pincode":"411005"}, approval_timestamp=datetime.now())
    assert approval.rfq_id == "1"
    with pytest.raises(ValidationError):
        SystemConfigUpdate(config_updates={"bad": 1})
    assert VendorResponseSchema(id="v", companyName="Co", email="a@b.com", city="P").email == "a@b.com"
    with pytest.raises(ValidationError):
        VendorQuerySchema(vendor={"id":"v"}, rfq={"id":"r"}, query="x")
    assert normalize_phone_number("98765 43210") == "+919876543210"
    assert normalize_phone_number("+919876543210") == "+919876543210"
    buyer = BuyerRegistrationSchema(name=" Ada ", companyName=" Acme ", email="A@B.COM", zipCode="411005")
    assert buyer.name == "Ada" and buyer.email == "a@b.com"
    with pytest.raises(ValidationError):
        BuyerRegistrationSchema(name="A1", companyName="C", email="a@b.com", zipCode="411005")
    seller = SellerRegistrationSchema(name="Ada", companyName="Acme", email="a@b.com", address1="Pune", zipCode="411005", gstin="27ABCDE1234F1Z5", details="x")
    assert seller.gstin == "27ABCDE1234F1Z5"
    user = User.from_api_response({"id":"1", "selfClient":True, "username":"a@b.com"})
    assert user.role == UserRole.BUYER
    assert User.from_mixed_data(user) is user
    assert User.invalid_user("123").is_registered
    assert WhatsAppMessageSchema(object="whatsapp_business_account", entry=[]).object == "whatsapp_business_account"
    assert WhatsAppIncomingMessageSchema(id="1", **{"from":"1234567890"}, timestamp="t", type="text").type == "text"
    assert WhatsAppOutgoingMessageSchema(to="1234567890").messaging_product == "whatsapp"


def test_model_enum_validators():
    session = ConversationSession(session_id="s", external_user_id="u", workflow_state={}, conversation_history=[], retention_date=datetime.now().date())
    assert session.validate_workflow_type("workflow_type", "rfq_creation") == WorkflowType.rfq_creation
    assert session.validate_workflow_type("workflow_type", "bad") is None
    assert session.validate_outcome("outcome", "completed") == ConversationOutcome.completed
    assert session.validate_outcome("outcome", "bad") is None
    assert session.validate_user_type("user_type", "seller") == UserType.seller
    assert session.validate_user_type("user_type", "bad") == UserType.unknown
    assert session.validate_session_state("session_state", "active") == SessionState.active
    assert session.validate_session_state("session_state", "bad") == SessionState.active
