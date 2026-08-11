from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.common import (
    BaseResponseSchema,
    ContactSchema,
    ErrorResponseSchema,
    FileUploadSchema,
    LocationSchema,
    PaginationSchema,
    StatusEnum,
    SuccessResponseSchema,
    TimestampMixin,
    ValidationErrorSchema,
)
from app.schemas.conversation import (
    ConversationContextSchema,
    ConversationOutcome,
    ConversationSessionSchema,
    EntityExtractionResultSchema,
    MessageSchema,
    WorkflowStateSchema,
    WorkflowType,
)
from app.schemas.sectioned_rfq_state import (
    SectionState,
    SectionType,
    SectionedRFQState,
    initialize_sectioned_rfq_state,
)
from app.schemas.seller import (
    BackgroundJobInfo,
    BackgroundJobStatus,
    BatchNotificationResponse,
    CategorizationStatistics,
    ErrorResponse,
    JobStatusEnum,
    MockAPIStatistics,
    MockDataGenerationRequest,
    MockDataGenerationResponse,
    NotificationResult,
    PaginationInfo,
    RFQApprovalRequest,
    RFQProcessingResponse,
    RFQStatusEnum,
    SellerInfo,
    SellerListResponse,
    SellerNotificationRequest,
    SellerRankingEnum,
    SellerSelectionMetadata,
    SellerSelectionResponse,
    SystemConfigUpdate,
    SystemConfiguration,
    ValidationErrorResponse,
    WebhookDeliveryStatus,
    WebhookResponse,
)
from app.schemas.user import (
    APIUserSchema,
    BuyerRegistrationSchema,
    SellerRegistrationSchema,
    User,
    UserRole,
    normalize_phone_number,
)
from app.schemas.vendor import (
    VendorProfileSchema,
    VendorQuerySchema,
    VendorRFQAssignmentSchema,
    VendorResponseSchema,
    VendorSearchRequestSchema,
)
from app.schemas.whatsapp import (
    WhatsAppContactSchema,
    WhatsAppIncomingMessageSchema,
    WhatsAppMessageSchema,
    WhatsAppOutgoingMessageSchema,
    WhatsAppTextMessageSchema,
)


def _seller_info(**overrides) -> SellerInfo:
    values = {
        "seller_id": "seller-1",
        "seller_name": "Seller One",
        "phone_number": "9876543210",
        "email": "seller@example.com",
        "categories": ["hardware"],
        "location": {"state": "Maharashtra", "city": "Pune", "pincode": "411005"},
        "subscription_credits": 4,
        "ranking": "Gold",
        "last_active_at": datetime(2025, 1, 1),
    }
    values.update(overrides)
    return SellerInfo(**values)


def _selection_metadata() -> SellerSelectionMetadata:
    return SellerSelectionMetadata(
        rfq_id="rfq-1",
        categories_searched=["hardware"],
        delivery_location={"state": "Maharashtra", "city": "Pune", "pincode": "411005"},
        initial_category_matches=3,
        geo_filtered_count=2,
        subscribed_available=1,
        unsubscribed_available=1,
        config_used={"radius": 25},
        selection_timestamp="2025-01-01T00:00:00Z",
    )


def test_common_defaults_validators_and_serialization():
    now = datetime(2025, 1, 2)
    response = BaseResponseSchema(success=True, message="ok", data={"id": 1}, error="", extra="allowed")
    assert response.data == {"id": 1} and response.extra == "allowed"
    assert isinstance(response.timestamp, datetime)
    assert BaseResponseSchema(success=False, message="bad").error is None
    assert "timestamp" in response.model_dump()
    with pytest.raises(ValidationError):
        BaseResponseSchema(message="missing success")

    assert PaginationSchema().model_dump() == {"page": 1, "page_size": 10, "total": None}
    assert PaginationSchema(page=1, page_size=100, total=8, extra_field=True).extra_field is True
    for kwargs in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        with pytest.raises(ValidationError):
            PaginationSchema(**kwargs)

    error = ErrorResponseSchema(error_code="E1", error_message="bad", details={"field": "x"}, extra=1)
    assert error.details == {"field": "x"} and error.extra == 1
    assert isinstance(error.timestamp, datetime)
    assert SuccessResponseSchema(message="done").status == "success"
    assert SuccessResponseSchema(status="ok", message="done", data=[1]).data == [1]
    validation = ValidationErrorSchema(field="name", error="invalid", value=4, extra="x")
    assert validation.value == 4 and validation.extra == "x"
    assert TimestampMixin().created_at is None and TimestampMixin().updated_at is None
    created = TimestampMixin(created_at=now, updated_at=now)
    assert created.created_at == now
    assert {StatusEnum.ACTIVE, StatusEnum.INACTIVE, StatusEnum.PENDING, StatusEnum.COMPLETED, StatusEnum.FAILED, StatusEnum.CANCELLED} == {
        "active", "inactive", "pending", "completed", "failed", "cancelled"
    }

    assert LocationSchema(state="M", city="Pune", pincode="411005", address="street", extra=1).address == "street"
    for pincode in ("12345", "1234567", "12A456"):
        with pytest.raises(ValidationError):
            LocationSchema(state="M", city="Pune", pincode=pincode)
    assert ContactSchema(name="A", email=None).email is None
    assert ContactSchema(name="A", email="a@example.com", phone="1", extra=True).phone == "1"
    for email in ("bad", "a@bad"):
        with pytest.raises(ValidationError):
            ContactSchema(name="A", email=email)
    assert FileUploadSchema(filename="a.txt", file_path="/tmp/a.txt", file_size=2, mime_type="text/plain", uploaded_at=now, extra=1).uploaded_at == now
    assert isinstance(FileUploadSchema(filename="a", file_path="p", file_size=0, mime_type="x").uploaded_at, datetime)
    with pytest.raises(ValidationError):
        FileUploadSchema(filename="a", file_path="p", file_size=1)


def test_conversation_enums_defaults_validators_and_invalid_inputs():
    context = ConversationContextSchema(
        session_id=" session ",
        external_user_id=" external ",
        workflow_type="rfq_creation",
        workflow_state={"step": 1},
        extracted_entities={"city": "Pune"},
        conversation_history=[{"role": "user"}],
        whatsapp_context={"wa": True},
        extra="allowed",
    )
    assert context.session_id == "session"
    assert context.external_user_id == "external"
    assert context.workflow_type is WorkflowType.RFQ_CREATION
    assert context.extra == "allowed"
    defaults = ConversationContextSchema(session_id="s", external_user_id="u")
    assert defaults.workflow_state == {} and defaults.conversation_history == []
    assert defaults.workflow_state is not ConversationContextSchema(session_id="s", external_user_id="u").workflow_state
    for field in ("session_id", "external_user_id"):
        values = {"session_id": "s", "external_user_id": "u", field: "   "}
        with pytest.raises(ValidationError):
            ConversationContextSchema(**values)
    with pytest.raises(ValidationError):
        ConversationContextSchema(session_id="s", external_user_id="u", workflow_type="unknown")
    with pytest.raises(ValidationError):
        ConversationContextSchema(session_id="s")

    session = ConversationSessionSchema(
        session_id="s", external_user_id="u", workflow_type="registration", outcome="completed",
        rfq_id="r", workflow_state={"x": 1}, conversation_history=[{"x": 1}],
        extracted_entities={"a": 1}, whatsapp_context={"b": 2}, error_details={"e": "x"},
        performance_metrics={"duration": 1}, created_at="now", completed_at="later", extra=1,
    )
    assert session.workflow_type is WorkflowType.REGISTRATION
    assert session.outcome is ConversationOutcome.COMPLETED and session.extra == 1
    assert ConversationSessionSchema(session_id="s", external_user_id="u").workflow_state == {}
    with pytest.raises(ValidationError):
        ConversationSessionSchema(external_user_id="u")
    with pytest.raises(ValidationError):
        ConversationSessionSchema(session_id="s")
    for value in ("completed", "abandoned", "escalated", "timeout", "in_progress"):
        assert ConversationSessionSchema(session_id="s", external_user_id="u", outcome=value).outcome.value == value

    message = MessageSchema(
        message_id="m", session_id="s", sender="system", content="  hello  ", timestamp="t",
        metadata={"x": 1}, message_type="image", extra=True,
    )
    assert message.content == "hello" and message.message_type == "image" and message.extra is True
    assert MessageSchema(message_id="m", session_id="s", sender="user", content="x", timestamp="t").message_type == "text"
    for sender in ("user", "bot", "system"):
        assert MessageSchema(message_id="m", session_id="s", sender=sender, content="x", timestamp="t").sender == sender
    with pytest.raises(ValidationError):
        MessageSchema(message_id="m", session_id="s", sender="admin", content="x", timestamp="t")
    with pytest.raises(ValidationError):
        MessageSchema(message_id="m", session_id="s", sender="user", content="   ", timestamp="t")
    with pytest.raises(ValidationError):
        MessageSchema(message_id="m", sender="user", content="x", timestamp="t")

    extraction = EntityExtractionResultSchema(
        entities={"x": 1}, confidence=1, completeness=100,
        missing_required_fields=["city"], next_questions=["Where?"], extra=1,
    )
    assert extraction.confidence == 1 and extraction.completeness == 100 and extraction.extra == 1
    assert EntityExtractionResultSchema().entities == {}
    assert EntityExtractionResultSchema().entities is not EntityExtractionResultSchema().entities
    for kwargs in ({"confidence": -0.01}, {"confidence": 1.01}, {"completeness": -0.01}, {"completeness": 100.01}):
        with pytest.raises(ValidationError):
            EntityExtractionResultSchema(**kwargs)
    state = WorkflowStateSchema(current_step="items", completed_steps=["date"], pending_steps=["x"], collected_data={"a": 1}, validation_errors=["bad"], extra=1)
    assert state.completed_steps == ["date"] and state.extra == 1
    with pytest.raises(ValidationError):
        WorkflowStateSchema()


def test_sectioned_rfq_state_transitions_round_trip_and_invalid_data():
    default = SectionedRFQState()
    assert set(default.sections) == {s.value for s in SectionType}
    assert default.sections["items"] is not default.sections["attachments"]
    assert default.current_section is SectionType.DATE_LOCATION
    assert SectionedRFQState(current_section="items").current_section is SectionType.ITEMS
    with pytest.raises(ValidationError):
        SectionState(retry_count=-1)
    with pytest.raises(ValidationError):
        SectionState(retry_count=4)
    assert SectionState(data=[1], confirmed=True, retry_count=3, awaiting_modification=True).data == [1]
    with pytest.raises(ValidationError):
        SectionedRFQState(current_section="invalid")

    state = SectionedRFQState(active=True, pending_restart=True, initial_extraction_done=True, from_excel=True)
    assert state.get_section_state(SectionType.ITEMS).confirmed is False
    assert state.get_section_state(SectionType("items")).confirmed is False
    state.sections = {"items": SectionState(confirmed=True, data={"x": 1})}
    assert state.get_section_state(SectionType.ITEMS).confirmed
    assert state.get_section_state(SectionType.ATTACHMENTS).confirmed is False
    replacement = SectionState(confirmed=True, retry_count=2)
    state.update_section_state(SectionType.ITEMS, replacement)
    assert state.get_section_state(SectionType.ITEMS) is replacement
    assert state.is_section_confirmed(SectionType.ITEMS)
    assert state.is_section_confirmed(SectionType.ATTACHMENTS) is False

    assert state.get_next_section(SectionType.DATE_LOCATION) is SectionType.ITEMS
    assert state.get_next_section(SectionType.ITEMS) is SectionType.ATTACHMENTS
    assert state.get_next_section(SectionType.ATTACHMENTS) is SectionType.FINAL_CONFIRMATION
    assert state.get_next_section(SectionType.FINAL_CONFIRMATION) is None
    assert state.get_next_section("not-a-section") is None
    assert state.get_previous_section(SectionType.DATE_LOCATION) is None
    assert state.get_previous_section(SectionType.ITEMS) is SectionType.DATE_LOCATION
    assert state.get_previous_section(SectionType.ATTACHMENTS) is SectionType.ITEMS
    assert state.get_previous_section(SectionType.FINAL_CONFIRMATION) is SectionType.ATTACHMENTS
    assert state.get_previous_section("not-a-section") is None

    state.reset_section(SectionType.ITEMS)
    assert state.get_section_state(SectionType.ITEMS) == SectionState()
    state.sections["date_location"] = SectionState(confirmed=True, data={"date": "tomorrow"}, retry_count=2, awaiting_modification=True)
    state.current_section = SectionType.FINAL_CONFIRMATION
    state.reset_all_sections()
    assert state.active and state.from_excel
    assert state.current_section is SectionType.DATE_LOCATION
    assert state.pending_restart is False and state.initial_extraction_done is False
    assert all(section == SectionState() for section in state.sections.values())

    state = SectionedRFQState(active=True, current_section=SectionType.ITEMS, pending_restart=True, initial_extraction_done=True, from_excel=True)
    state.update_section_state(SectionType.ITEMS, SectionState(confirmed=True, data={"items": 2}, retry_count=1, awaiting_modification=True))
    dumped = state.to_dict()
    assert dumped["current_section"] == "items" and dumped["from_excel"] is True
    assert dumped["sections"]["items"] == {"confirmed": True, "data": {"items": 2}, "retry_count": 1, "awaiting_modification": True}
    restored = SectionedRFQState.from_dict(dumped)
    assert restored.to_dict() == dumped
    partial = SectionedRFQState.from_dict({"active": True, "current_section": "attachments", "sections": {"attachments": {"retry_count": 2}}})
    assert partial.active and partial.current_section is SectionType.ATTACHMENTS
    assert set(partial.sections) == {"attachments"} and partial.sections["attachments"].retry_count == 2
    assert SectionedRFQState.from_dict({}).from_excel is False
    with pytest.raises(ValueError):
        SectionedRFQState.from_dict({"current_section": "bad"})
    with pytest.raises(ValidationError):
        SectionedRFQState.from_dict({"sections": {"items": {"retry_count": 4}}})
    initial = initialize_sectioned_rfq_state()
    assert initial["current_section"] == "date_location" and len(initial["sections"]) == 4


def test_seller_request_response_admin_testing_and_webhook_schemas():
    approval_values = {
        "rfq_id": "r", "rfq_title": "Title", "categories": ["a"],
        "delivery_location": {"state": "M", "city": "Pune", "pincode": "411005"},
        "approval_timestamp": datetime(2025, 1, 1), "deadline": date(2025, 2, 1),
        "quantity_info": "10", "division": "IT", "approved_by": "u",
    }
    assert RFQApprovalRequest(**approval_values).deadline == date(2025, 2, 1)
    with pytest.raises(ValidationError):
        RFQApprovalRequest(**{**approval_values, "categories": []})
    for missing in ("state", "city", "pincode"):
        location = approval_values["delivery_location"].copy()
        location.pop(missing)
        with pytest.raises(ValidationError):
            RFQApprovalRequest(**{**approval_values, "delivery_location": location})
    for pincode in ("41100A", "41100", "4110055"):
        with pytest.raises(ValidationError):
            RFQApprovalRequest(**{**approval_values, "delivery_location": {"state": "M", "city": "Pune", "pincode": pincode}})
    assert SellerNotificationRequest(seller_id="s", rfq_data={"items": []}).rfq_data == {"items": []}
    with pytest.raises(ValidationError):
        SellerNotificationRequest(seller_id="s")
    allowed = {"MAX_SUBSCRIBED_SELLERS_PER_RFQ": 1, "MAX_UNSUBSCRIBED_SELLERS_PER_RFQ": 2, "MAX_TIME_SINCE_LAST_MESSAGE_HOURS": 3, "MAX_TIME_SINCE_LAST_ACTIVE_HOURS": 4, "GEO_DISTANCE_RADIUS_KM": 5}
    assert SystemConfigUpdate(config_updates=allowed).config_updates == allowed
    assert SystemConfigUpdate(config_updates={}).config_updates == {}
    with pytest.raises(ValidationError):
        SystemConfigUpdate(config_updates={"bad": 1})

    seller = _seller_info(ranking="Diamond", distance_km=1.5)
    assert seller.ranking is SellerRankingEnum.diamond
    metadata = _selection_metadata()
    selection = SellerSelectionResponse(subscribed_sellers=[seller], unsubscribed_sellers=[], total_selected=1, selection_metadata=metadata)
    assert selection.selection_metadata.rfq_id == "rfq-1"
    notification = NotificationResult(seller_id="s", seller_name="S", success=True, message_id="m", error=None)
    batch = BatchNotificationResponse(successful=1, failed=0, total=1, details=[notification])
    processing = RFQProcessingResponse(success=True, job_id="j", rfq_id="r", processing_time_seconds=1.2, sellers_selected=1, subscribed_sellers=1, unsubscribed_sellers=0, notifications_sent=1, notifications_failed=0, selection_metadata=metadata, notification_details=batch)
    assert processing.notification_details.details[0].message_id == "m"
    assert SellerInfo(ranking="Platinum", **{k: v for k, v in seller.model_dump().items() if k != "ranking"}).ranking is SellerRankingEnum.platinum
    assert SellerInfo(ranking="Titanium", **{k: v for k, v in seller.model_dump().items() if k != "ranking"}).ranking is SellerRankingEnum.titanium
    with pytest.raises(ValidationError):
        SellerInfo(ranking="unknown", **{k: v for k, v in seller.model_dump().items() if k != "ranking"})

    config = SystemConfiguration(configuration={"x": 1}, last_updated=None, total_parameters=1)
    job = BackgroundJobInfo(job_id="j", rfq_id="r", status="processing", stage=None, started_at="now", duration_seconds=0)
    assert job.status is JobStatusEnum.processing
    status = BackgroundJobStatus(statistics={}, active_jobs=[job], service_status="ok")
    stats = CategorizationStatistics(database_statistics={}, job_statistics={}, processing_statistics={}, configuration={})
    assert status.active_jobs[0].job_id == "j" and stats.configuration == {} and config.last_updated is None
    assert {value.value for value in JobStatusEnum} == {"pending", "processing", "completed", "failed"}
    assert {value.value for value in RFQStatusEnum} == {"collecting", "ready", "submitted", "failed"}

    assert MockDataGenerationRequest().model_dump() == {"num_sellers": 60, "num_rfqs": 20, "clear_existing": False}
    assert MockDataGenerationRequest(num_sellers=1, num_rfqs=100, clear_existing=True).clear_existing
    for kwargs in ({"num_sellers": 0}, {"num_sellers": 201}, {"num_rfqs": 0}, {"num_rfqs": 101}):
        with pytest.raises(ValidationError):
            MockDataGenerationRequest(**kwargs)
    generated = MockDataGenerationResponse(success=True, generation_time_seconds=1, data_created={"sellers": 1}, seller_distribution={}, rfq_distribution={})
    assert generated.success
    assert MockAPIStatistics(mock_api_statistics={}, service_status="ok").service_status == "ok"
    assert PaginationInfo(total=2, limit=1, offset=0, has_more=True).has_more
    assert SellerListResponse(sellers=[seller], pagination=PaginationInfo(total=1, limit=10, offset=0, has_more=False)).sellers[0].seller_id == "seller-1"
    assert ErrorResponse(error="bad").success is False
    assert ValidationErrorResponse(validation_errors=[{"field": "x"}]).error == "Validation failed"
    last_attempt = datetime(2025, 1, 1)
    delivery = WebhookDeliveryStatus(webhook_id="w", rfq_id="r", delivery_status="pending", attempts=1, last_attempt=last_attempt, next_retry=None)
    webhook = WebhookResponse(success=True, message="ok", webhook_id="w", processed_at=last_attempt)
    assert delivery.next_retry is None and webhook.webhook_id == "w"


def test_user_phone_registration_adapters_and_defaults():
    assert normalize_phone_number("") == ""
    assert normalize_phone_number(" (987) 654-3210.'\" ") == "+919876543210"
    assert normalize_phone_number("+441234567890") == "+441234567890"
    assert normalize_phone_number("441234567890", default_country_code="44") == "+441234567890"
    assert normalize_phone_number("9876543210") == "+919876543210"
    assert normalize_phone_number("abcdefghi1") == "+91abcdefghi1"
    assert normalize_phone_number("123", default_country_code="1") == "+123"

    buyer = BuyerRegistrationSchema(name=" ada. lovelace ", companyName=" acme ltd ", email=" A+tag@EXAMPLE.COM ", zipCode="411005", organizationPhonenumber=None, whatsApp=True)
    assert buyer.name == "Ada. Lovelace" and buyer.companyName == "Acme Ltd" and buyer.email == "a+tag@example.com"
    assert buyer.organizationPhonenumber is None and buyer.sourceType == "W"
    assert BuyerRegistrationSchema(name="A", companyName="C", email="a@b.com", zipCode="411005", organizationPhonenumber=None).organizationPhonenumber is None
    for kwargs in (
        {"name": "A1"}, {"email": "   "}, {"email": "bad"}, {"zipCode": "123"}, {"organizationPhonenumber": "123"},
    ):
        values = {"name": "A", "companyName": "C", "email": "a@b.com", "zipCode": "411005"}
        values.update(kwargs)
        with pytest.raises(ValidationError):
            BuyerRegistrationSchema(**values)

    seller_values = {"name": " ada. lovelace ", "companyName": " acme ltd ", "email": " SELLER@EXAMPLE.COM ", "address1": " pune ", "zipCode": "411005", "gstin": "27abcde1234f1z5", "details": "  Products and services  "}
    seller = SellerRegistrationSchema(**seller_values, organizationPhonenumber=None, whatsApp=False)
    assert seller.name == "Ada. Lovelace" and seller.address1 == "Pune" and seller.details == "Products and services" and seller.gstin == "27ABCDE1234F1Z5"
    assert SellerRegistrationSchema(**{**seller_values, "details": "   "}, organizationPhonenumber=None).details == ""
    for kwargs in ({"name": "A1"}, {"email": "bad"}, {"zipCode": "123"}, {"gstin": "bad"}, {"organizationPhonenumber": "123"}):
        values = {**seller_values, "organizationPhonenumber": None}
        values.update(kwargs)
        with pytest.raises(ValidationError):
            SellerRegistrationSchema(**values)
    assert SellerRegistrationSchema(**seller_values, organizationPhonenumber=None).sourceType == "W"

    api = APIUserSchema(id="id", username="u", fullName="Full", selfClient=True, email="e", phone="p", companyName="C", uniqueId="uid", orgId="oid", verificationStatus="VERIFIED", approved=True)
    assert api.fullName == "Full"
    assert APIUserSchema(id="id").approved is None
    with pytest.raises(ValidationError):
        APIUserSchema()
    assert User(id="id").role is UserRole.UNKNOWN and User(id="id").is_registered is False
    explicit = User(id="id", role="buyer", self_client=True, is_registered=True, otp_validated_at=1.5)
    assert explicit.role is UserRole.BUYER and explicit.otp_validated_at == 1.5

    class DictAdapter:
        def dict(self):
            return {"id": "dict-id", "selfClient": False, "verificationStatus": "VERIFIED"}

    assert User.from_api_response(DictAdapter()).id == "dict-id"
    assert User.from_api_response(SimpleNamespace(id="namespace-id", selfClient=False)).id == "namespace-id"
    with pytest.raises(ValueError):
        User.from_api_response(3)
    preferred = User.from_api_response({"id": "preferred", "userId": "fallback", "selfClient": False, "verificationStatus": "DONE", "phone": "+91 1"})
    assert preferred.id == "preferred" and preferred.phone_number == "+91 1"
    assert User.from_api_response({"userId": 7, "selfClient": False}).id == "7"
    for value, role in ((True, UserRole.BUYER), (False, UserRole.SELLER)):
        converted = User.from_api_response({"id": "x", "selfClient": value})
        assert converted.role is role and converted.is_registered
    assert User.from_api_response({"id": "x"}).role is UserRole.UNKNOWN
    with pytest.raises(ValidationError):
        User.from_api_response({"id": "x", "selfClient": None})
    coerced = User.from_api_response({"id": "x", "selfClient": "yes"})
    assert coerced.role is UserRole.UNKNOWN and coerced.self_client is True
    assert User.from_api_response({"id": "x", "verificationStatus": "VERIFIED"}).verification_status == "VERIFIED"
    assert User.from_api_response({"id": "x", "verificationStatus": ""}).verification_status == "PENDING_EMAIL_VERIFICATION"
    with pytest.raises(ValueError):
        User.from_api_response({"id": "", "userId": ""})

    existing = User(id="existing")
    assert User.from_mixed_data(existing) is existing
    assert User.from_mixed_data({"name": "Name", "email": "e", "id": "u"}).is_registered
    assert User.from_mixed_data({"name": "Name", "id": "u"}).id == "u"
    assert User.from_mixed_data(DictAdapter()).id == "dict-id"
    assert User.from_mixed_data(SimpleNamespace(id="namespace-id")).id == "namespace-id"
    with pytest.raises(ValueError):
        User.from_mixed_data(None)
    with pytest.raises(ValueError):
        User.from_mixed_data({})
    with pytest.raises(ValueError):
        User.from_mixed_data(3)
    invalid = User.invalid_user("phone")
    assert invalid.id == "1428bbb9-a0ba-459d-b1e8-23d7c49455e8" and invalid.role is UserRole.UNKNOWN and invalid.phone_number == "phone"


def test_vendor_aliases_validators_profiles_and_queries():
    assert VendorSearchRequestSchema().vendor_category is None
    alias = VendorSearchRequestSchema(category="tools", location="Pune", division="sales", vendorcategory="industrial", extra=1)
    assert alias.vendor_category == "industrial" and alias.extra == 1
    by_name = VendorSearchRequestSchema(vendor_category="named")
    assert by_name.model_dump(by_alias=True)["vendorcategory"] == "named"
    both = VendorSearchRequestSchema(vendorcategory="alias", vendor_category="name")
    assert both.vendor_category == "alias"

    response = VendorResponseSchema(id="v", vendorId="legacy", companyName="Co", email="a@b.com", city="Pune", mobileNo="1", otherEmails=["b@c.com"], extra=True)
    assert response.vendor_id == "legacy" and response.extra is True
    assert response.model_dump(by_alias=True)["companyName"] == "Co"
    assert VendorResponseSchema(id="v", company_name="Co", email="@.", city="Pune").email == "@."
    for email in ("bad", "a@bad"):
        with pytest.raises(ValidationError):
            VendorResponseSchema(id="v", companyName="Co", email=email, city="Pune")
    with pytest.raises(ValidationError):
        VendorResponseSchema(id="v", companyName="Co", email="a@b.com")

    profile = VendorProfileSchema(id="v", company_name="Co", email="not-an-email", city="Pune", state="M", mobile_no="1", other_emails=["x"], categories=["a"], services=["s"], geographic_coverage=["west"], created_at="today", extra=1)
    assert profile.email == "not-an-email" and profile.extra == 1
    assert VendorProfileSchema(id="v", company_name="Co", email="e", city="Pune").state is None
    with pytest.raises(ValidationError):
        VendorProfileSchema(id="v", company_name="Co", city="Pune")

    assert VendorRFQAssignmentSchema(vendor={"id": "v"}, rfq={"id": "r"}, extra=1).vendor["id"] == "v"
    for field in ("vendor", "rfq"):
        values = {"vendor": {"id": "v"}, "rfq": {"id": "r"}}
        values[field] = {}
        with pytest.raises(ValidationError):
            VendorRFQAssignmentSchema(**values)
    assert VendorRFQAssignmentSchema(vendor={"id": ""}, rfq={"id": ""}).vendor["id"] == ""
    with pytest.raises(ValidationError):
        VendorRFQAssignmentSchema(vendor=[], rfq={"id": "r"})

    query = VendorQuerySchema(vendor={"id": "v"}, rfq={"id": "r"}, query="  hello  ", extra=1)
    assert query.query == "hello" and query.extra == 1
    assert VendorQuerySchema(vendor={"id": "v"}, rfq={"id": "r"}, query="12345").query == "12345"
    with pytest.raises(ValidationError):
        VendorQuerySchema(vendor={"id": "v"}, rfq={"id": "r"}, query=" 1234 ")


def test_whatsapp_validators_aliases_defaults_and_invalid_inputs():
    valid = WhatsAppMessageSchema(object="whatsapp_business_account", entry=[], extra=True)
    assert valid.entry == [] and valid.extra is True
    assert WhatsAppMessageSchema(object="whatsapp_business_account", entry=[{"id": 1}]).entry[0]["id"] == 1
    with pytest.raises(ValidationError):
        WhatsAppMessageSchema(object="other", entry=[])
    with pytest.raises(ValidationError):
        WhatsAppMessageSchema(object="whatsapp_business_account")

    assert WhatsAppContactSchema(wa_id="123").profile is None
    assert WhatsAppContactSchema(wa_id="123", profile={"name": "A"}, extra=1).profile == {"name": "A"}
    assert WhatsAppTextMessageSchema(body="").body == ""
    with pytest.raises(ValidationError):
        WhatsAppContactSchema()
    with pytest.raises(ValidationError):
        WhatsAppTextMessageSchema()

    allowed = ("text", "image", "audio", "video", "document", "location", "contacts")
    for message_type in allowed:
        incoming = WhatsAppIncomingMessageSchema(id="m", **{"from": "123"}, timestamp="t", type=message_type, text={"body": "hi"}, extra=1)
        assert incoming.type == message_type and incoming.from_ == "123" and incoming.extra == 1
    incoming = WhatsAppIncomingMessageSchema(id="m", from_="123", timestamp="t", type="text")
    assert incoming.text is None and incoming.model_dump(by_alias=True)["from"] == "123"
    with pytest.raises(ValidationError):
        WhatsAppIncomingMessageSchema(id="m", **{"from": "123"}, timestamp="t", type="sticker")
    with pytest.raises(ValidationError):
        WhatsAppIncomingMessageSchema(id="m", timestamp="t", type="text")

    assert WhatsAppOutgoingMessageSchema(to="1234567890").model_dump() == {"messaging_product": "whatsapp", "to": "1234567890", "type": "text", "text": None}
    outgoing = WhatsAppOutgoingMessageSchema(to="12345678901", messaging_product="custom", type="image", text={"link": "x"}, extra=1)
    assert outgoing.messaging_product == "custom" and outgoing.extra == 1
    for phone in ("", "123456789", "+1234567890", "12345abcde"):
        with pytest.raises(ValidationError):
            WhatsAppOutgoingMessageSchema(to=phone)
    with pytest.raises(ValidationError):
        WhatsAppOutgoingMessageSchema()
