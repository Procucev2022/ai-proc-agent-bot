from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import ConversationOutcome, WorkflowType
from app.schemas.rfq import RFQValidationSchema
from app.schemas.user import BuyerRegistrationSchema, SellerRegistrationSchema
from app.services.helpers.attachment_helpers import AttachmentHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.excel_confirmation_helpers import ExcelConfirmationHelpers
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.response_helpers import ResponseHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.helpers.support_helpers import SupportHelpers
from app.services.handlers.auth_registration_intent_switch import AuthRegistrationIntentSwitch
from app.services.handlers.authentication_orchestrator import AuthenticationOrchestrator
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.products_array_handler import ProductsArrayHandler
from app.services.handlers.purchase_intent_handler import PurchaseIntentHandler
from app.services.handlers.purchase_workflow_handler import PurchaseWorkflowHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.handlers.seller_rfq_interest_handler import SellerRFQInterestHandler


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


def session(**state):
    return SimpleNamespace(
        session_id="session-1",
        external_user_id="user-1",
        workflow_type=None,
        workflow_state=state,
        conversation_history={"openai_messages": [], "metadata": [], "messages": []},
        outcome=None,
        created_at=None,
        last_activity_at=None,
        product_items=[],
        extracted_entities={},
        rfq_ids=[],
        rfq_id=None,
        retention_date=None,
        user_type=None,
        completed_at=None,
        bfs_products_searched=[],
        bfs_search_count=0,
        bfs_price_accepted=[],
        bfs_counter_offers=[],
        products_bid_for=[],
        bids_received=[],
        bids_accepted=[],
        counter_offers_made=[],
        counter_offers_accepted=[],
        rfqs_with_response=[],
    )


def user(role="buyer"):
    return SimpleNamespace(
        id="user-1", org_id="org-1", phone_number="+919999999999",
        role=role, is_registered=True,
    )


# ---------------------------------------------------------------------------
# Pure helpers and mocked external boundaries
# ---------------------------------------------------------------------------


def test_attachment_validation_urls_and_session_state(monkeypatch):
    assert AttachmentHelpers.validate_attachment_type("quote.PDF", "application/pdf") == {"valid": True}
    assert not AttachmentHelpers.validate_attachment_type("quote.exe", "application/pdf")["valid"]
    assert not AttachmentHelpers.validate_attachment_type("quote.pdf", "application/x-unknown")["valid"]

    settings = SimpleNamespace(WHATSAPP_MEDIA_DOWNLOAD_URL="https://media.example/download")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    assert AttachmentHelpers._construct_media_download_url("media-1") == "https://media.example/download/media-1"
    assert AttachmentHelpers._construct_media_download_url("https://other/file") == "https://other/file"
    assert AttachmentHelpers._construct_media_download_url("https://media.sendmsg.in/wamessage/media/old-id") == "https://media.example/download/old-id"

    s = session(pending_attachments=[{"file_name": "old", "status": "rejected"}], extracted_entities=[{"attachments": [{"file_name": "approved"}]}])
    added = AttachmentHelpers.add_attachment_to_session(s, {"file_name": "new", "status": "pending"})
    assert added["success"] and added["count"] == 2
    assert s.workflow_state["awaiting_attachment_decision"]
    assert AttachmentHelpers.get_attachment_summary(s)["approved_files"] == ["approved"]

    full = session(extracted_entities=[{"attachments": [{"file_name": str(i)} for i in range(4)]}])
    assert not AttachmentHelpers.add_attachment_to_session(full, {"file_name": "five"})["success"]


def test_attachment_approval_rejection_and_sync():
    s = session(
        pending_attachments=[{"file_name": "a"}, {"file_name": "bad", "status": "rejected"}],
        pending_rfq={"entities": {}}, pending_optional_rfq={"entities": {}},
        pending_combined_rfq={"combined_schema": {}}, pending_optional_combined_rfq={"combined_schema": {}},
    )
    assert AttachmentHelpers.approve_pending_attachment(s, "a")
    assert s.workflow_state["pending_attachments"] == []
    assert s.workflow_state["extracted_entities"][0]["attachments"][0]["file_name"] == "a"
    assert s.workflow_state["pending_rfq"]["entities"]["attachments"]
    assert s.workflow_state["pending_combined_rfq"]["combined_schema"]["attachments"]
    assert not AttachmentHelpers.approve_pending_attachment(session())
    assert AttachmentHelpers.reject_pending_attachments(s)
    assert not s.workflow_state["awaiting_attachment_decision"]


@pytest.mark.asyncio
async def test_attachment_download_success_size_http_timeout_and_exception(monkeypatch):
    response = SimpleNamespace(status=200, headers={"Content-Type": "application/pdf"}, read=AsyncMock(return_value=b"pdf"), text=AsyncMock())
    get_context = AsyncContext(response)
    client = SimpleNamespace(get=MagicMock(return_value=get_context))
    monkeypatch.setattr("app.services.helpers.attachment_helpers.aiohttp.ClientSession", lambda: AsyncContext(client))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(WHATSAPP_FROM_NUMBER="9199", WHATSAPP_MEDIA_DOWNLOAD_URL="https://media.example/download"))
    result = await AttachmentHelpers.download_and_encode_attachment("media", "quote.pdf")
    assert result["success"] and result["attachment"]["file_content"] == base64.b64encode(b"pdf").decode()

    response.read.return_value = b"x" * (AttachmentHelpers.MAX_FILE_SIZE_BYTES + 1)
    result = await AttachmentHelpers.download_and_encode_attachment("media", "large.pdf")
    assert not result["success"] and "exceeds" in result["error"]

    response.status = 404
    response.text.return_value = "missing"
    result = await AttachmentHelpers.download_and_encode_attachment("media", "quote.pdf")
    assert result["error"] == "Failed to download file: HTTP 404"

    import aiohttp
    timeout_error = type("TimeoutError", (Exception,), {})
    monkeypatch.setattr("app.services.helpers.attachment_helpers.aiohttp.ClientTimeout", timeout_error)
    client.get.side_effect = timeout_error()
    result = await AttachmentHelpers.download_and_encode_attachment("media", "quote.pdf")
    assert result["error"] == "Timeout downloading file"
    client.get.side_effect = RuntimeError("network")
    result = await AttachmentHelpers.download_and_encode_attachment("media", "quote.pdf")
    assert "network" in result["error"]


def test_authentication_helpers_messages_payloads_and_extractors(monkeypatch):
    assert AuthenticationHelpers.extract_user_details([]) is None
    assert AuthenticationHelpers.extract_user_details([{"id": 1}, {"id": 2, "selfClient": True}])["id"] == 2
    assert AuthenticationHelpers.extract_user_details(["bad"]) is None
    assert AuthenticationHelpers.format_email_list(["a@x", "b@x"]) == "1. a@x\n2. b@x"
    assert AuthenticationHelpers.validate_otp_format("12 34")
    assert AuthenticationHelpers.validate_otp_format("123456")
    assert not AuthenticationHelpers.validate_otp_format("123")
    assert not AuthenticationHelpers.validate_otp_format("12ab34")
    assert AuthenticationHelpers.extract_emails_from_user_data({"email": ["a@x", "bad", "b@y"]}) == ["a@x", "b@y"]
    assert AuthenticationHelpers.extract_emails_from_user_data({"email": "x@y"}) == ["x@y"]

    message = AuthenticationHelpers.generate_registration_message(BuyerRegistrationSchema, "buyer")
    assert "*Full name*" in message and "*Organization email*" in message
    assert "address1" not in message
    questions = AuthenticationHelpers.generate_registration_questions_dynamic(
        SellerRegistrationSchema, {"name": "alex smith", "email": "a@x"}, ["zipCode", "address1", "details"], "bad value"
    )
    assert "Alex" in questions and "pincode" in questions and "Validation Error" in questions
    assert "remaining registration details" in AuthenticationHelpers.generate_registration_questions_dynamic(BuyerRegistrationSchema, {}, [], None)
    confirmation = AuthenticationHelpers.generate_confirmation_message_dynamic(BuyerRegistrationSchema, {"name": "Alex", "email": "a@x"})
    assert "Alex" in confirmation and "N/A" not in confirmation

    monkeypatch.setattr("app.schemas.user.normalize_phone_number", lambda value: "+919999")
    payload = AuthenticationHelpers.build_registration_payload_dynamic(
        BuyerRegistrationSchema, {"name": "Alex", "companyName": "Acme", "email": "a@x", "zipCode": "560001", "organizationPhonenumber": "999"}, "999"
    )
    assert payload["organizationPhonenumber"] == "+919999" and payload["details"] == "Registered via WhatsApp bot"


@pytest.mark.asyncio
async def test_authentication_validate_entities_all_external_pincode_paths(monkeypatch):
    entities = {"name": " alex smith ", "companyName": " acme ", "email": " A@X.COM ", "zipCode": "560001"}
    monkeypatch.setattr("app.services.helpers.authentication_helpers.get_location_from_pincode_async", AsyncMock(return_value={"city": "Bengaluru"}))
    validated, error = await AuthenticationHelpers.validate_entities(entities, BuyerRegistrationSchema)
    assert validated["name"] == "Alex Smith" and validated["email"] == "a@x.com" and error is None

    for value, lookup, expected in [
        ("12", AsyncMock(return_value=None), "exactly 15"),
        ("27ABCDE1234F1Z!", AsyncMock(return_value=None), "Invalid format"),
        ("bad", AsyncMock(return_value=None), "6-digit"),
        ("560001", AsyncMock(return_value=None), "not a valid"),
        ("560001", AsyncMock(side_effect=RuntimeError("lookup")), "couldn't verify"),
    ]:
        data = {"email": "not-an-email", "gstin": value, "zipCode": value if value == "bad" else "560001"}
        monkeypatch.setattr("app.services.helpers.authentication_helpers.get_location_from_pincode_async", lookup)
        _, error = await AuthenticationHelpers.validate_entities(data, SellerRegistrationSchema)
        assert error and (expected in error or "valid email" in error)


def test_chat_service_helper_transform_stage_and_context(monkeypatch):
    data = ChatServiceHelpers.transform_entities_to_schema({
        "projectDesc": "Project", "deliveryDate": "2026-01-02", "division": "IT",
        "description": "Laptop", "quantity": 2, "state": "Karnataka", "city": "Bengaluru", "pincode": "560001",
        "remarks": "urgent", "brand": "HP", "attachments": [{"file_name": "x"}],
    })
    assert data["project_desc"] == "Project" and data["items"][0]["unit_of_measures"] == "unit(s)"
    assert data["delivery_locations"][0]["city"] == "Bengaluru" and data["preferred_brand"] == "HP"
    assert ChatServiceHelpers.transform_entities_to_schema({"deliveryDate": "not a date"}) == {}
    combined = ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products([
        {"entities": {"description": "Laptop", "quantity": 1, "deliveryDate": "2026-01-02", "state": "Karnataka", "city": "Bengaluru", "pincode": "560001"}},
        {"entities": {"description": "Chair", "quantity": 2, "date_validation_error": True}},
    ])
    assert len(combined.items) == 2 and combined.date_validation_error
    assert ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products([]) is None

    for state, expected in [
        ({"sectioned_rfq": {"active": True, "current_section": "items"}}, "collecting_items"),
        ({"pending_rfq": {"entities": {}}}, "confirming"), ({"pending_optional_rfq": {"entities": {}}}, "optional_fields"),
        ({"incomplete_products": [{}]}, "collecting_details"), ({"extracted_entities": [{}]}, "processing_multiple"),
        ({"extracted_entities": []}, "collecting"), ({"extracted_entities": [{"x": 1}]}, "processing_multiple"),
        ({}, "completed"),
    ]:
        s = session(**state)
        if state == {}:
            s.outcome = "done"
        assert ChatServiceHelpers.determine_conversation_stage(s) == expected


@pytest.mark.asyncio
async def test_chat_service_context_role_lookup_and_fallback(monkeypatch):
    s = session(extracted_entities=[{"x": 1}])
    s.conversation_history = {"metadata": [{"role": "assistant", "content": "last"}]}
    redis = AsyncMock()
    redis.get.return_value = {"role": "buyer"}
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    context = await ChatServiceHelpers.build_conversation_context(s, "hello")
    assert context["bot_last_message"] == "last" and context["user_role"] == "buyer"
    s.external_user_id = None
    assert (await ChatServiceHelpers.build_conversation_context(s, "hello"))["user_role"] is None
    s.external_user_id = "u"
    redis.get.side_effect = RuntimeError("redis")
    assert (await ChatServiceHelpers.build_conversation_context(s, "hello"))["user_role"] is None
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(session(), "current", {"original_message": "original"}) == "original"


def test_excel_helpers_and_confirmation_helpers():
    items = [{"ItemDescription": " Laptop ", "Specification": None, "Quantity": 2, "Uom": None, "Remarks": ""}, {"ItemDescription": "", "Quantity": None}]
    entities = ExcelHelpers.convert_excel_to_entities(items)
    assert entities[0]["description"] == "Laptop" and entities[0]["quantity"] == "2" and entities[0]["unit_of_measure"] == "pcs"
    assert ExcelHelpers.calculate_excel_completeness(items) == 50
    assert ExcelHelpers.calculate_excel_completeness([]) == 0
    assert "didn't find" in ExcelHelpers.generate_excel_summary({"filename": "x.xlsx", "total_items": 0})
    assert "Laptop" in ExcelHelpers.generate_excel_summary({"filename": "x.xlsx", "total_items": 1, "items": items})
    assert "No items found" in ExcelHelpers.identify_missing_fields([])
    assert "Item descriptions" in ExcelHelpers.identify_missing_fields(items)
    context = ExcelHelpers.prepare_excel_context({"success": True, "filename": "x", "total_items": 1, "items": items, "rfqs": [{"deliveryDate": "tomorrow", "pincode": "1", "state": "S", "city": "C"}]}, "9199")
    assert context["workflow_type"] == "excel_rfq_upload" and context["city"] == "C"
    assert ExcelHelpers.should_complete_immediately(100, items) is False
    schema = ExcelHelpers.create_excel_validation_schema_from_items([{"ItemDescription": "x", "Specification": "s", "Uom": "pcs", "Quantity": 1, "S.No": "1", "Remarks": "r"}])
    assert schema.is_excel_complete()
    assert "looks good" in ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {}}})[0]
    assert "empty or corrupted" in " ".join(ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [], "headers": []}}))

    stats = ExcelConfirmationHelpers.generate_confirmation_message({"total_rows": 2, "identified_for_rfq": 2, "extracted": 1, "skipped": 1, "has_missing_items": True, "skipped_items_summary": "row 2"}, "x.xlsx")
    assert "Missing quantity" in stats and "row 2" in stats
    assert "All items processed" in ExcelConfirmationHelpers.generate_confirmation_message({"extracted": 2}, "x.xlsx")
    s = SimpleNamespace(workflow_state=None)
    ExcelConfirmationHelpers.save_excel_confirmation_data(s, {"rfqs": [], "items": [{"ItemDescription": "x", "Quantity": 1}], "filename": "x"})
    assert ExcelConfirmationHelpers.get_excel_confirmation_data(s)["ready_for_multiple_rfq_creation"]
    assert ExcelConfirmationHelpers.prepare_multiple_rfq_data(ExcelConfirmationHelpers.get_excel_confirmation_data(s))[0]["products"][0]["description"] == "x"
    assert "Created: 1 RFQ" in ExcelConfirmationHelpers.format_rfq_creation_summary([{"products": [{"description": "x", "quantity": 1}]}])
    ExcelConfirmationHelpers.clear_excel_confirmation_data(s)
    assert ExcelConfirmationHelpers.get_excel_confirmation_data(s) == {}


def test_session_helpers_time_ids_activity_and_serialization(monkeypatch):
    now = datetime(2025, 1, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.helpers.session_helpers.utc_now", lambda: now)
    assert SessionHelpers.generate_session_id("9", "uuid").startswith("whatsapp_9_20250102_")
    assert SessionHelpers.generate_session_id("9", "daily") == "whatsapp_9_20250102"
    assert "2025W01" in SessionHelpers.generate_session_id("9", "weekly")
    assert SessionHelpers.generate_session_id("9", "persistent").endswith("persistent")
    assert SessionHelpers.generate_session_id("9", "uuid").startswith("whatsapp_9_20250102_")
    assert SessionHelpers.generate_session_id("9", "other").endswith("20250102")

    s = session()
    s.bfs_products_searched = None; s.bfs_price_accepted = None; s.bfs_counter_offers = None
    SessionHelpers.update_bfs_activity(s, {"searched_products": ["l"], "accepted_prices": [1], "counter_offers": [2]})
    assert s.bfs_search_count == 1
    s.products_bid_for = None; s.bids_received = None; s.bids_accepted = None; s.counter_offers_made = None; s.counter_offers_accepted = None; s.rfqs_with_response = None
    SessionHelpers.update_bidding_activity(s, {"products_bid_for": ["l"], "rfqs_with_response": ["r", "r"]})
    assert s.rfqs_with_response == ["r"]
    s.rfq_ids = []; s.rfq_id = None
    assert SessionHelpers.calculate_session_averages(s).avg_products_per_rfq == 0
    s.rfq_ids = ["r"]; s.product_items = [{"category": "IT"}]; s.extracted_entities = {"description": "Laptop"}
    assert SessionHelpers.calculate_session_averages(s).avg_categories_per_rfq == 2
    class E(Enum):
        X = "x"
    cleaned = SessionHelpers.clean_for_json_serialization({"e": E.X, "d": date(2025, 1, 1), "t": (1, object())})
    assert cleaned["e"] == "x" and cleaned["d"] == "2025-01-01" and isinstance(cleaned["t"], list)
    assert SessionHelpers.should_use_summary_aware_extraction("refer to earlier") is False


@pytest.mark.asyncio
async def test_session_expiry_redis_timestamp_and_notification(monkeypatch):
    assert not await SessionHelpers.is_session_expired(None)
    settings = SimpleNamespace(redis_session_storage_enabled=True, session_timeout_minutes=60)
    redis = AsyncMock(); redis.session_exists.return_value = True
    monkeypatch.setattr("app.services.helpers.session_helpers.get_settings", lambda: settings)
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    assert not await SessionHelpers.is_session_expired(session())
    redis.session_exists.return_value = False
    assert await SessionHelpers.is_session_expired(session())
    settings.redis_session_storage_enabled = False
    s = session(); s.created_at = None
    assert await SessionHelpers.is_session_expired(s)
    s.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr("app.services.helpers.session_helpers.is_expired", lambda *_: (True, now if 'now' in locals() else s.created_at, s.created_at))
    assert await SessionHelpers.is_session_expired(s)

    expired = session(); expired.created_at = datetime.now(timezone.utc) - timedelta(hours=3); expired.conversation_history = {"openai_messages": [{}, {}]}
    monkeypatch.setattr(SessionHelpers, "is_session_expired", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.helpers.session_helpers.utc_now", lambda: datetime.now(timezone.utc))
    assert await SessionHelpers.should_send_expiration_message(expired)
    expired.workflow_state["last_expiry_notification"] = "bad"
    assert await SessionHelpers.should_send_expiration_message(expired)
    fresh = session(); fresh.created_at = datetime.now(timezone.utc); fresh.conversation_history = {"openai_messages": [{}, {}]}
    assert not await SessionHelpers.should_send_expiration_message(fresh)


@pytest.mark.asyncio
async def test_session_expiry_reset_preserves_auth_profile_and_appends(monkeypatch):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.helpers.session_helpers.utc_now", lambda: now)
    s = session(profile_selection_stage="select", profile_options=[1], old="clear")
    s.workflow_type = WorkflowType.authentication; s.extracted_entities = {"x": 1}; s.conversation_history = {"messages": [1]}
    db = MagicMock()
    result = await SessionHelpers.handle_session_expiry(s, db)
    assert result.workflow_type == WorkflowType.authentication and "profile_selection_stage" in result.workflow_state
    assert result.conversation_history["openai_messages"] == [] and result.outcome is None
    db.append_session_data.assert_called_once()
    s.workflow_type = WorkflowType.rfq_creation
    await SessionHelpers.handle_session_expiry(s, db)
    assert s.workflow_type is None


def test_response_helpers_fallbacks_and_openai_paths(monkeypatch):
    helper = ResponseHelpers.__new__(ResponseHelpers)
    helper.settings = SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal", support_email="support@x", support_contact_info="help@x", rfq_max_allowed=2)
    client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(output_text=" AI response "))))
    helper.openai_service = SimpleNamespace(default_model="model", client=client, _load_prompt=MagicMock(return_value="instructions"))
    import asyncio
    assert asyncio.run(helper._generate_common_seller_response("state", "type", "context", "fallback")) == "AI response"
    client.responses.create.return_value = SimpleNamespace(output_text="")
    assert asyncio.run(helper._generate_common_seller_response("state", "type", {}, "fallback")) == "fallback"
    client.responses.create.side_effect = RuntimeError("ai")
    assert asyncio.run(helper._generate_common_seller_response("state", "type", {}, "fallback")) == "fallback"
    helper.openai_service.generate_contextual_response = AsyncMock(side_effect=RuntimeError("down"))
    assert asyncio.run(helper.generate_contextual_response({}, ["question"])) == "Thank you for the information! question"
    assert asyncio.run(helper.generate_rfq_status_contextual_response({})) is None
    assert helper._get_email_status_fallback({"successful_emails": 1, "total_requested": 2}).startswith("✅ Sent")
    assert helper._get_email_status_fallback({"successful_emails": 0, "total_requested": 2}).startswith("❌")
    assert "Insufficient Credits" in helper._get_email_status_with_errors_openai_fallback({"successful_emails": 1, "total_requested": 3, "error_analysis": {"total_failed": 2, "error_counts": {"NO_CREDITS": 1}, "error_categories": {"NO_CREDITS": ["r1"]}}})
    assert helper._get_fallback_message("unknown", "buyer")["buttons"]
    assert helper._get_fallback_message("unknown", "seller")["buttons"]
    assert helper._get_fallback_message("unknown", None)["buttons"] is None
    assert "Laptop" in helper._format_single_rfq({"rfq_id": "r", "category": ["IT"], "submission_date": "today", "location": "Bengaluru", "project_description": "Laptop"}, 1)


@pytest.mark.asyncio
async def test_summarization_history_redis_rich_entities_and_completion(monkeypatch):
    redis_client = MagicMock(); redis_client.incr.return_value = 7
    monkeypatch.setattr("redis.from_url", lambda *_args, **_kwargs: redis_client)
    s = session(pending_rfq={"x": 1}, complete_products=[1], incomplete_products=[2], extracted_entities=[{"description": "Laptop"}], user_preferences={"brand": "HP"}, conversation_stage="collecting")
    s.product_items = [{"category": "IT"}]; s.interaction_metrics = {"count": 1}; s.seller_responses = [{"ok": 1}]; s.rfq_metadata = {"x": 1}
    SummarizationHelpers.add_to_conversation_history(s, "user", "I need a laptop", intent="buy", confidence=.9)
    SummarizationHelpers.add_to_conversation_history(s, "assistant", {"image": True}, "image")
    assert len(s.conversation_history["messages"]) == 2 and redis_client.expire.called
    before = len(s.conversation_history["messages"])
    async def pending_message():
        return "later"
    coroutine_message = pending_message()
    SummarizationHelpers.add_to_conversation_history(s, "user", coroutine_message)
    coroutine_message.close()
    assert len(s.conversation_history["messages"]) == before
    rich = SummarizationHelpers.extract_rich_entities_for_summary(s)
    assert "rfq_details" in rich and "session_product_items" in rich
    data = SummarizationHelpers.prepare_enhanced_summary_data(s, rich)
    assert data["products_count"] == 1 and data["workflow_stage_reached"] == "rfq_confirmation"
    assert "User specified" in SummarizationHelpers._extract_key_decisions([{"sender": "user", "content": "yes"}], {})[0]
    chat, daily = AsyncMock(), AsyncMock()
    await SummarizationHelpers.handle_session_completion_async(chat, daily, {"session_id": "s", "user_id": "u", "rfq_ids": ["r"]})
    chat.generate_session_summary.assert_awaited_once(); daily.generate_daily_summary.assert_awaited_once_with("u")




def test_support_helpers_template_and_missing_implementation_paths(tmp_path):
    helper = SupportHelpers.__new__(SupportHelpers)
    helper.templates_dir = tmp_path
    helper.email_service = SimpleNamespace(send_email=AsyncMock(return_value={"status": "Success"}))
    assert helper.load_template("missing") is None
    (tmp_path / "ok.json").write_text('{"subject": "Hello {name}"}', encoding="utf-8")
    assert helper.load_template("ok")["subject"] == "Hello {name}"
    with pytest.raises(AttributeError):
        helper.integrate_template_and_data({"subject": "Hi {name}"}, {"name": "A"})


# ---------------------------------------------------------------------------
# Handler orchestration with all service/database/network boundaries mocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_products_array_handler_routes_empty_incomplete_complete_and_errors(monkeypatch):
    h = ProductsArrayHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    s = session()
    result = await h.handle_products_array(user(), s, "x", [], global_supplementary_fields={"state": "K"})
    assert result["status"] == "need_product_description"
    h._track_product_categories = AsyncMock()
    h._categorize_products_by_completeness = AsyncMock(return_value=([{"entities": {"description": "x"}, "missing_fields": ["delivery_date"]}], []))
    h._handle_incomplete_products = AsyncMock(return_value={"status": "incomplete"})
    assert (await h.handle_products_array(user(), s, "x", [{"description": "x"}]))["status"] == "incomplete"
    h._categorize_products_by_completeness.return_value = ([], [{"index": 1, "entities": {"description": "x"}}])
    h._handle_complete_products = AsyncMock(return_value={"status": "complete"})
    assert (await h.handle_products_array(user(), s, "x", [{"description": "x"}]))["status"] == "complete"
    h._track_product_categories.side_effect = RuntimeError("bad")
    assert (await h.handle_products_array(user(), s, "x", [{"description": "x"}]))["status"] == "error"

@pytest.mark.asyncio
async def test_products_array_questions_and_single_multiple_confirmation(monkeypatch):
    h = ProductsArrayHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    schema = MagicMock()
    schema.get_combined_questions.return_value = {"mandatory": ["What delivery date?", None], "optional": [], "has_mandatory": True, "has_optional": False}
    monkeypatch.setattr("app.services.handlers.products_array_handler.ChatServiceHelpers.create_rfq_schema_from_entities", lambda *_args: schema)
    questions, _ = await h._generate_clarification_questions([
        {"index": 1, "entities": {"description": "laptop"}, "missing_fields": ["delivery_date"]},
        {"index": 2, "entities": {}, "missing_fields": ["delivery_date"]},
    ], 2)
    assert questions == ["What delivery date?"]
    schema.get_combined_questions.return_value = {"mandatory": ["Delivery date?", "Quantity"], "optional": [], "has_mandatory": True, "has_optional": False}
    questions, _ = await h._generate_clarification_questions([{"index": 1, "entities": {"description": "laptop", "date_validation_error": True}, "missing_fields": ["item_0_quantity"]}], 1)
    assert "Quantity" in questions
    fake = MagicMock(); fake.get_optional_questions.return_value = ["Add docs?"]; fake.model_dump.return_value = {}
    monkeypatch.setattr("app.services.handlers.products_array_handler.ChatServiceHelpers.create_rfq_schema_from_entities", lambda *_args: fake)
    h.whatsapp_service.send_configurable_buttons = AsyncMock(); h.session_manager.save_session = AsyncMock()
    result = await h._handle_single_complete_product(user(), session(), "x", {"index": 1, "entities": {"description": "x"}}, [])
    assert result["status"] == "optional_fields_inquiry"
    fake.get_optional_questions.return_value = []
    h.response_helpers.generate_rfq_summary_and_confirmation = AsyncMock(return_value="summary")
    result = await h._handle_single_complete_product(user(), session(), "x", {"index": 1, "entities": {"description": "x"}}, [])
    assert result["status"] == "single_product_confirmation"


@pytest.mark.asyncio
async def test_auth_registration_switch_choices_and_account_parsing(monkeypatch):
    wa = AsyncMock(); h = AuthRegistrationIntentSwitch.__new__(AuthRegistrationIntentSwitch); h.whatsapp_service = wa; h.openai_service = MagicMock()
    s = session(user_type="buyer"); s.workflow_type = "authentication"
    assert await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    s.workflow_state["pending_auth_reg_switch"] = {"new_intent": "sell_something"}
    assert not await h.should_handle_auth_reg_switch(s, "sell_something", "seller")
    assert h._get_target_combination("register_seller", "seller") == "registration_seller"
    s.workflow_state.pop("pending_auth_reg_switch")
    assert (await h.handle_auth_reg_switch_choice("9", s, "sell", "sell_something", "seller"))["status"] == "auth_reg_switch_choice_presented"
    assert (await h.handle_auth_reg_switch_response("9", s, "continue"))["status"] == "continue_current_workflow"
    await h.handle_auth_reg_switch_choice("9", s, "sell", "sell_something", "seller")
    assert (await h.handle_auth_reg_switch_response("9", s, "1"))["status"] == "continue_current_workflow"
    await h.handle_auth_reg_switch_choice("9", s, "sell", "sell_something", "seller")
    assert (await h.handle_auth_reg_switch_response("9", s, "exit"))["status"] == "exit_requested"
    assert await h._analyze_switch_choice("ambiguous") == "unclear"
    h.openai_service.generate_response.return_value = "switch_to_new"
    assert await h._analyze_switch_choice("perhaps new") == "switch_to_new"
    options = [{"number": 1, "email": "a@x", "text": "1 a@x"}, {"number": 2, "email": "b@x", "text": "2 b@x"}]
    assert (await h._parse_account_selection("2", options))["email"] == "b@x"
    assert (await h._parse_account_selection("a@x please", options))["number"] == 1
    assert await h._parse_account_selection("9", options) is None


@pytest.mark.asyncio
async def test_authentication_orchestrator_flow_routing_and_redirect(monkeypatch):
    h = AuthenticationOrchestrator.__new__(AuthenticationOrchestrator)
    h.whatsapp_service = AsyncMock(); h.authentication_service = AsyncMock(); h.registration_service = AsyncMock(); h.support_service = AsyncMock(); h.intent_service = AsyncMock(); h.chat_service = None
    h.auth_reg_switch = AsyncMock(); h.profile_selection_service = AsyncMock()
    s = session(); h.authentication_service.validate_token.return_value = None
    h.profile_selection_service.handle_profile_selection.return_value = {"status": "selected"}
    result = await h.authentication_orchestrator_flow("9", "buy", s, {"intent": "buy_something", "confidence": 90})
    assert result["status"] == "selected"
    h.authentication_service.validate_token.return_value = {"is_registered": True, "id": "u", "name": "A", "email": "a@x"}
    assert (await h.authentication_orchestrator_flow("9", "buy", s, {"intent": "buy_something", "confidence": 90})).id == "u"
    h.authentication_service.validate_token.side_effect = RuntimeError("bad")
    h.support_service.redirect_to_support.return_value = {"status": "support"}
    assert (await h.authentication_orchestrator_flow("9", "x", s, {"intent": "other", "confidence": 90}))["status"] == "support"
    h.authentication_service.validate_token.side_effect = None
    h.registration_service.initiate_registration.return_value = {"ok": True}
    result = await h._redirect_to_registration_flow("9", s, "seller")
    assert result["status"] == "redirected_to_registration" and s.workflow_type == WorkflowType.registration
    h.whatsapp_service.send_message = AsyncMock()
    assert (await h._handle_auth_clarification_request("9", "x"))["status"] == "clarification_sent"


@pytest.mark.asyncio
async def test_confirmation_handler_button_parse_fallback_and_completion(monkeypatch):
    h = ConfirmationHandler.__new__(ConfirmationHandler); h.whatsapp_service = AsyncMock(); h.cancel_service = None; h.session_manager = None; h.response_helpers = AsyncMock(); h.confirmation_service = AsyncMock(); h._bfs_search_handler = None
    h._handle_rfq_acceptance = AsyncMock(return_value={"status": "accepted"}); h._handle_rfq_modification = AsyncMock(return_value={"status": "modified"}); h._proceed_to_confirmation_from_optional = AsyncMock(return_value={"status": "continued"}); h._handle_restart_workflow = AsyncMock(return_value={"status": "restarted"})
    assert (await h.handle_confirmation_button(user(), session(), "unknown"))["status"] == "unknown_button"
    assert (await h.handle_confirmation_button(user(), session(), "confirm_rfq"))["status"] == "accepted"
    assert (await h.handle_confirmation_button(user(), session(), "no_rfq"))["status"] == "modified"
    h.confirmation_service.parse_confirmation.return_value = "yes"
    assert (await h.handle_pending_confirmations(user(), session(), "yes", {}))["status"] == "accepted"
    h.confirmation_service.parse_confirmation.return_value = "unclear"
    h.response_helpers.generate_contextual_response.return_value = "clarify"
    assert (await h.handle_pending_confirmations(user(), session(), "hmm", {}))["status"] == "confirmation_clarification_requested"
    assert (await h.handle_optional_fields_response(user(), session(), "skip"))["status"] == "continued"
    h._merge_optional_fields_and_confirm = AsyncMock(return_value={"status": "modified"})
    assert (await h.handle_optional_fields_response(user(), session(), "add specs"))["status"] == "modified"
    assert h._extract_product_descriptions_from_rfq([{ "success": True, "rfq_data": {"items": [{"product_name": "Laptop"}]}}]) == ["Laptop"]


@pytest.mark.asyncio
async def test_bfs_search_handler_extraction_payload_results_and_buttons(monkeypatch):
    h = BFSSearchHandler.__new__(BFSSearchHandler); h.whatsapp_service = AsyncMock(); h.session_manager = AsyncMock(); h.openai_service = AsyncMock(); h.auto_categorization_service = AsyncMock(); h.bfs_api_service = AsyncMock()
    s = session(); u = user()
    h.openai_service.extract_entities.return_value = {"success": True, "products": [{"description": "Laptop"}, {"description": ""}]}
    assert await h._extract_entities("laptop") == [{"description": "Laptop"}]
    h.auto_categorization_service.categorize_item.return_value = {"success": True, "category": "Other"}
    assert (await h._build_api_payload([{ "description": "Laptop"}], "u", "s"))[0]["category"] == []
    h.bfs_api_service.search_bfs_items.return_value = {"success": True, "data": [{"description": "Laptop", "availableQuantity": 2, "sellPrice": 3}]}
    result = await h.handle_bfs_search(u, s, "laptop")
    assert result["status"] == "bfs_search_completed" and s.workflow_state["bfs_results"]
    await h._send_bfs_results(u, s, [], suppress_raise_rfq_on_no_results=True)
    h.bfs_api_service.search_bfs_items.return_value = {"success": False, "error": "down"}
    assert (await h.handle_bfs_search(u, s, "laptop"))["status"] == "bfs_search_failed"
    assert (await h.handle_button(u, s, "unknown"))["status"] == "unknown_bfs_button"


@pytest.mark.asyncio
async def test_bfs_bid_retry_and_submission_error_paths(monkeypatch):
    h = BFSSearchHandler.__new__(BFSSearchHandler); h.whatsapp_service = AsyncMock(); h.session_manager = AsyncMock(); h.bfs_api_service = AsyncMock()
    s = session(bfs_results=[{"id": "i", "description": "x", "sellPrice": 1}]); u = user()
    monkeypatch.setattr("app.services.handlers.bfs_search_handler.parse_bid_format", lambda *_: {"error": "bad"})
    assert (await h.handle_bid_format_input(u, s, "bad"))["status"] == "bfs_bid_format_invalid"
    s.workflow_state["bfs_bid_retry_count"] = 2
    assert (await h.handle_bid_format_input(u, s, "bad"))["status"] == "bfs_bid_max_retries"
    s.workflow_state = {"bfs_bid_items": [{"price": 2, "quantity": 1, "original_item": {"id": "i", "sellPrice": 1}}]}
    redis = AsyncMock(); redis.retrieve.return_value = SimpleNamespace(org_id=None, id=None)
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    assert (await h._submit_bids_to_api(u, s))["status"] == "bfs_bid_missing_user_data"
    s.workflow_state["bfs_bid_items"] = []
    assert (await h._submit_bids_to_api(u, s))["status"] == "bfs_bid_no_items"


@pytest.mark.asyncio
async def test_seller_auth_mixin_auth_switch_lookup_and_otp(monkeypatch):
    h = SellerRFQInterestHandler.__new__(SellerRFQInterestHandler); h.whatsapp_service = AsyncMock(); h.authentication_service = AsyncMock(); h.session_manager = AsyncMock(); h.otp_service = AsyncMock(); h.auth_redis_service = AsyncMock(); h.openai_service = AsyncMock()
    h.auth_redis_service.retrieve.return_value = None
    assert (await h.check_seller_auth_state("+9", "s"))["state"] == "not_auth"
    h.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=True)
    assert (await h.check_seller_auth_state("+9", "s"))["account_type"] == "buyer"
    h.auth_redis_service.retrieve.return_value = SimpleNamespace(self_client=False, org_id="s", id="u")
    assert (await h.check_seller_auth_state("+9", "s"))["state"] == "correct_seller"
    assert await h._parse_switch_choice("maybe") == "unclear"
    h.openai_service.generate_response.return_value = "switch"
    assert await h._parse_switch_choice("perhaps") == "switch"
    s = session(target_seller_id="s"); current = SimpleNamespace(self_client=True, email="a@x", dict=lambda: {"email": "a@x"})
    assert (await h.prompt_seller_account_switch("9", s, current))["status"] == "switch_prompt_sent"
    h.authentication_service.clear_user_token = AsyncMock(); h.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    assert (await h.handle_seller_switch_response("9", s, "1"))["status"] == "otp"
    h.authentication_service.user_authenticate.return_value = {"success": True, "response": [{"selfClient": False, "orgId": "s", "username": "seller@x"}]}
    assert (await h.get_seller_info("9", "s"))[0] == "seller@x"
    h.get_seller_info = AsyncMock(return_value=("seller@x", {"id": "u"})); h.initiate_seller_auth = SellerAuthMixin.initiate_seller_auth.__get__(h, type(h)); h.otp_service.send_otp.return_value = {"status": "otp_sent"}
    assert (await h.initiate_seller_auth("9", s, "s"))["status"] == "otp_sent"
    h.otp_service.send_otp.return_value = {"status": "failed", "error": "down"}
    assert (await h.initiate_seller_auth("9", s, "s"))["status"] == "otp_send_failed"


@pytest.mark.asyncio
async def test_seller_interest_flow_and_purchase_handlers(monkeypatch):
    h = SellerRFQInterestHandler.__new__(SellerRFQInterestHandler); h.whatsapp_service = AsyncMock(); h.session_manager = AsyncMock(); h.auth_redis_service = AsyncMock(); h.settings = SimpleNamespace(procucev_rfq_details_url="https://portal")
    response = SimpleNamespace(success=True, message_id="m", error=None); h.whatsapp_service.send_configurable_buttons.return_value = response
    h.check_seller_auth_state = AsyncMock(return_value={"state": "correct_seller"})
    monkeypatch.setattr("app.services.seller_notification_service.SellerNotificationService", lambda: SimpleNamespace(get_intermediate_rfq_buttons=lambda *_: []))
    assert (await h.handle_rfq_interest_click("9", "r", "s", session()))["status"] == "intermediate_buttons_sent"
    s = session(rfq_id="r", target_seller_id="s")
    assert (await h.handle_check_details_click("9", "r", "s", s))["status"] == "check_details_sent"
    assert (await h.handle_otp_validated("9", session()))["status"] == "error"

    p = PurchaseWorkflowHandler.__new__(PurchaseWorkflowHandler); p.entity_service = AsyncMock(); p.whatsapp_service = AsyncMock()
    assert await p._handle_products_array(user(), session(), "x", []) is None
    assert await p._handle_single_entity(user(), session(), "x", {}) is None
    pi = PurchaseIntentHandler(AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock())
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    pi.chat_summary_service.load_user_context.return_value = []
    pi.entity_service.extract_entities.return_value = {"entities": {}}
    assert (await pi.handle_purchase_intent(user(), session(), "x"))["status"] == "no_new_entities"
    pi.entity_service.extract_entities.return_value = {"products": [{"description": "laptop"}]}
    pi.products_array_handler.handle_products_array.return_value = {"status": "products"}
    assert (await pi.handle_purchase_intent(user(), session(), "x"))["status"] == "products"


@pytest.mark.asyncio
async def test_format_modification_and_purchase_error_branches(monkeypatch):
    h = FormatModificationHandler(AsyncMock())
    s = session(); u = user()
    monkeypatch.setattr("app.services.handlers.format_modification_handler.WorkflowManager.is_awaiting_modification", lambda *_: (False, None))
    assert (await h.handle_format_modification("x", s, u))["status"] == "error"
    monkeypatch.setattr("app.services.handlers.format_modification_handler.WorkflowManager.is_awaiting_modification", lambda *_: (True, "unknown"))
    assert "Unknown" in (await h.handle_format_modification("x", s, u))["message"]
    # The current constructor omits session_manager; the public error path remains deterministic and is caught.
    monkeypatch.setattr("app.services.handlers.format_modification_handler.WorkflowManager.is_awaiting_modification", lambda *_: (True, "delivery"))
    assert (await h.handle_format_modification("x", s, u))["status"] == "error"
    h.session_manager = AsyncMock()
    monkeypatch.setattr("app.services.handlers.format_modification_handler.WorkflowManager.get_delivery_details", lambda *_: {"delivery_date": "2026", "pincode": "1", "city": "C", "state": "S"})
    await h._replace_products_array(s, [{"description": "x"}])
    assert s.workflow_state["extracted_entities"][0]["city"] == "C"
    pi = PurchaseIntentHandler.__new__(PurchaseIntentHandler); pi.whatsapp_service = AsyncMock(); pi.session_manager = AsyncMock()
    assert (await pi._handle_quantity_limit_violations(u, s, {"quantity_violations": [{"description": "x", "quantity": 10000001}]}))["status"] == "quantity_limit_violation"
    assert (await pi._handle_error_response(RuntimeError("x"), "9"))["status"] == "error"
