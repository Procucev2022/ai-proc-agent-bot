"""Deterministic round-three coverage for residual handlers and helpers."""

from __future__ import annotations

import base64
import importlib
import logging.handlers
import time
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import app.services.handlers.bfs_search_handler as bfs_mod
import app.services.handlers.format_modification_handler as format_mod
import app.services.handlers.seller_auth_mixin as seller_mod
import app.services.helpers.attachment_helpers as attachment_mod
import app.services.helpers.authentication_helpers as authentication_mod
import app.services.helpers.chat_service_helpers as chat_mod
import app.services.helpers.excel_confirmation_helpers as excel_confirmation_mod
import app.services.helpers.excel_helpers as excel_mod
import app.services.helpers.session_helpers as session_mod
import app.services.helpers.summarization_helpers as summarization_mod
import app.services.helpers.support_helpers as support_mod
from app.models import ConversationOutcome, WorkflowType
from app.schemas.user import BuyerRegistrationSchema
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.format_modification_handler import FormatModificationHandler
from app.services.handlers.seller_auth_mixin import SellerAuthMixin
from app.services.helpers.attachment_helpers import AttachmentHelpers
from app.services.helpers.authentication_helpers import AuthenticationHelpers
from app.services.helpers.chat_service_helpers import ChatServiceHelpers
from app.services.helpers.excel_confirmation_helpers import ExcelConfirmationHelpers
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.helpers.support_helpers import SupportHelpers


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


def make_session(**state):
    return SimpleNamespace(
        session_id="round3-session",
        external_user_id="round3-user",
        phone_number="+919999999999",
        workflow_type=None,
        workflow_state=dict(state),
        conversation_history={"openai_messages": [], "metadata": [], "messages": []},
        extracted_entities={},
        product_items=[],
        outcome=None,
        retention_date=None,
        created_at=None,
        completed_at=None,
        last_activity_at=None,
        rfq_ids=[],
        rfq_id=None,
        user_type=None,
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


def make_user(role="buyer", **overrides):
    values = {
        "id": "user-1",
        "org_id": "org-1",
        "phone_number": "+919999999999",
        "role": role,
        "is_registered": True,
        "email": "buyer@example.com",
        "self_client": role == "buyer",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def bare(cls, **attrs):
    instance = cls.__new__(cls)
    for name, value in attrs.items():
        setattr(instance, name, value)
    return instance


@pytest.mark.asyncio
async def test_bfs_constructor_buttons_payload_and_post_bid_menus(monkeypatch):
    openai = MagicMock(name="openai")
    categorizer = MagicMock(name="categorizer")
    bfs_api = MagicMock(name="bfs_api")
    monkeypatch.setattr(bfs_mod, "OpenAIService", lambda: openai)
    monkeypatch.setattr(bfs_mod, "get_auto_categorization_service_async", AsyncMock(return_value=categorizer))
    monkeypatch.setattr(bfs_mod, "get_bfs_api_service", lambda: bfs_api)

    whatsapp = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(),
    )
    session_manager = SimpleNamespace(save_session=AsyncMock())
    handler = BFSSearchHandler(whatsapp, session_manager)
    assert handler.openai_service is openai
    assert handler.bfs_api_service is bfs_api
    # Constructing the handler must not build the categorization service: that loads a
    # Sentence Transformer model and would block the event loop mid-request.
    assert handler.auto_categorization_service is None
    assert await handler._get_categorization_service() is categorizer
    # Resolved once, then reused.
    assert await handler._get_categorization_service() is categorizer
    assert bfs_mod.get_auto_categorization_service_async.await_count == 1

    session = make_session(bfs_searched_products=["pump"], bfs_results=[{"id": "i"}])
    result = await handler.handle_button(make_user(), session, "bfs_raise_rfq")
    assert result == {"status": "bfs_activate_rfq"}
    assert session.workflow_state == {"bfs_rfq_products": ["pump"]}
    session_manager.save_session.assert_awaited_once()

    empty = make_session()
    assert await handler.handle_button(make_user(), empty, "bfs_raise_rfq") == {"status": "bfs_activate_rfq"}
    assert await handler.handle_button(make_user(), empty, "bfs_cancel") == {"status": "bfs_send_cancel_message"}
    assert await handler.handle_button(make_user(), make_session(), "unknown") == {
        "status": "unknown_bfs_button",
        "button_id": "unknown",
    }

    categorizer.categorize_item = AsyncMock(return_value={"success": False})
    payload = await handler._build_api_payload([{"description": "pump"}], "u", "s")
    assert payload == [{"category": [], "description": ["pump"]}]
    categorizer.categorize_item.return_value = {"success": True, "category": ""}
    assert (await handler._build_api_payload([{"description": "pump"}], "u", "s"))[0]["category"] == []

    await handler._send_post_bid_menu(make_user(role="seller"), make_session(), "seller")
    buttons = whatsapp.send_configurable_buttons.await_args.args[2]
    assert [button["id"] for button in buttons] == ["view_rfqs", "quote_status", "search_bfs"]


@pytest.mark.asyncio
async def test_bfs_otp_timing_validation_and_seller_submission(monkeypatch):
    whatsapp = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(),
    )
    session_manager = SimpleNamespace(save_session=AsyncMock())
    handler = bare(
        BFSSearchHandler,
        whatsapp_service=whatsapp,
        session_manager=session_manager,
        bfs_api_service=SimpleNamespace(request_bfs_item=AsyncMock(return_value={"success": True})),
    )
    user = make_user(role="seller")

    state = make_session(bfs_results=[{"id": "i", "sellPrice": 2}])
    monkeypatch.setattr(bfs_mod, "parse_bid_format", Mock(return_value={"error": "invalid"}))
    assert (await handler.handle_bid_format_input(user, state, "bad"))["status"] == "bfs_bid_format_invalid"
    state.workflow_state["bfs_bid_retry_count"] = bfs_mod.MAX_BID_RETRY_ATTEMPTS - 1
    handler._clear_bid_state = AsyncMock()
    assert (await handler.handle_bid_format_input(user, state, "bad"))["status"] == "bfs_bid_max_retries"
    handler._clear_bid_state.assert_awaited_once()

    auth = SimpleNamespace(
        retrieve=AsyncMock(return_value=SimpleNamespace(otp_validated_at=time.time(), email="seller@example.com", org_id="org", id="seller")),
        store=AsyncMock(),
    )
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    handler._submit_bids_to_api = AsyncMock(return_value={"status": "submitted"})
    recent = make_session(bfs_bid_items=[{"key": "pump", "price": 3, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}}])
    assert await handler._send_bid_otp(user, recent, recent.workflow_state["bfs_bid_items"]) == {"status": "submitted"}
    handler._submit_bids_to_api.assert_awaited_once()

    auth.retrieve.return_value = SimpleNamespace(otp_validated_at=None, email=None)
    no_email = make_session(bfs_bid_items=[{"key": "pump", "price": 3, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}}])
    handler._clear_bid_state = AsyncMock()
    assert (await handler._send_bid_otp(user, no_email, no_email.workflow_state["bfs_bid_items"]))["status"] == "bfs_bid_no_email"

    class FakeOTP:
        def __init__(self, **_kwargs):
            pass

        async def send_otp(self, *_args, **_kwargs):
            return {"status": "otp_sent"}

        async def validate_otp(self, *_args):
            return {"status": "otp_valid"}

    monkeypatch.setattr("app.services.otp_service.OTPService", FakeOTP)
    monkeypatch.setattr("app.procucev_apis.register_apis.RegisterAPIService", FakeOTP)
    monkeypatch.setattr("app.services.support_notification_service.SupportNotificationService", FakeOTP)
    auth.retrieve.return_value = SimpleNamespace(otp_validated_at=None, email="seller@example.com", org_id="org", id="seller")
    pending = make_session(bfs_bid_items=[{"key": "pump", "price": 3, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}}])
    sent = await handler._send_bid_otp(user, pending, pending.workflow_state["bfs_bid_items"])
    assert sent["status"] == "bfs_bid_otp_sent"

    otp_session = make_session(bfs_bid_items=[{"key": "pump", "price": 3, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}}], otp_email="seller@example.com")
    auth.retrieve.side_effect = [SimpleNamespace(otp_validated_at=None), None]
    handler._submit_bids_to_api = AsyncMock(return_value={"status": "submitted"})
    assert await handler.handle_bid_otp_input(user, otp_session, "1234") == {"status": "submitted"}
    auth.store.assert_not_awaited()

    auth.retrieve.side_effect = [SimpleNamespace(otp_validated_at=None)]
    FakeOTP.validate_otp = AsyncMock(return_value={"status": "max_otp_exceeded"})
    handler._clear_bid_state = AsyncMock()
    assert (await handler.handle_bid_otp_input(user, otp_session, "bad"))["status"] == "bfs_bid_max_otp_retries"

    auth.retrieve.side_effect = None
    auth.retrieve.return_value = SimpleNamespace(org_id="org", id="seller")
    handler._submit_bids_to_api = BFSSearchHandler._submit_bids_to_api.__get__(handler, BFSSearchHandler)
    handler._clear_bid_state = AsyncMock()
    seller_state = make_session(
        bfs_bid_items=[
            {"key": "missing quantity", "price": 3, "quantity": 0, "original_item": {"id": "i"}},
            {"key": "missing id", "price": 3, "quantity": 1, "original_item": {}},
            {"key": "pump", "price": 3, "quantity": 1, "original_item": {"id": "i", "sellPrice": 2}},
        ]
    )
    handler.bfs_api_service.request_bfs_item.return_value = {"success": True}
    assert (await handler._submit_bids_to_api(user, seller_state))["status"] == "bfs_bid_submitted"
    assert whatsapp.send_configurable_buttons.await_args.args[2][0]["id"] == "view_rfqs"


@pytest.mark.asyncio
async def test_format_items_error_and_seller_optional_dependencies(monkeypatch):
    handler = bare(
        FormatModificationHandler,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        session_manager=SimpleNamespace(save_session=AsyncMock()),
    )
    monkeypatch.setattr(format_mod.WorkflowManager, "is_awaiting_modification", lambda _session: (True, "items"))
    monkeypatch.setattr(format_mod.WorkflowManager, "get_retry_count", lambda _session: 0)
    monkeypatch.setattr(format_mod.WorkflowManager, "increment_retry_count", lambda *_args, **_kwargs: 1)
    result = await handler.handle_format_modification("edited items", make_session(), make_user())
    assert result["status"] == "format_error"

    mixin = bare(
        SellerAuthMixin,
        whatsapp_service=SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock()),
        authentication_service=None,
        session_manager=None,
        otp_service=None,
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock()),
        openai_service=SimpleNamespace(generate_response=AsyncMock()),
    )
    assert await mixin._parse_switch_choice("2") == "stay"
    assert await mixin._parse_switch_choice("stay") == "stay"
    assert await mixin.get_seller_info("+1", "seller") == (None, None)

    mixin._parse_switch_choice = AsyncMock(return_value="stay")
    stay_session = make_session(target_seller_id="seller")
    assert (await mixin.handle_seller_switch_response("+1", stay_session, "2"))["status"] == "switch_declined"
    assert stay_session.workflow_state == {}

    mixin._parse_switch_choice = AsyncMock(return_value="switch")
    mixin.initiate_seller_auth = AsyncMock(return_value={"status": "otp"})
    switch_session = make_session(target_seller_id="seller")
    assert (await mixin.handle_seller_switch_response("+1", switch_session, "1"))["status"] == "otp"

    mixin.get_seller_info = AsyncMock(return_value=("seller@example.com", {"id": "seller"}))
    mixin.initiate_seller_auth = SellerAuthMixin.initiate_seller_auth.__get__(mixin, type(mixin))
    assert (await mixin.initiate_seller_auth("+1", make_session(), "seller"))["status"] == "otp_instruction_sent"


@pytest.mark.asyncio
async def test_attachment_setup_session_approval_and_download_variants(monkeypatch):
    payload_logger = attachment_mod.whatsapp_payload_logger
    original_handlers = list(payload_logger.handlers)
    payload_logger.handlers.clear()
    fake_handler = MagicMock()
    try:
        monkeypatch.setattr(attachment_mod.os.path, "exists", lambda _path: False)
        make_dir = Mock()
        monkeypatch.setattr(attachment_mod.os, "makedirs", make_dir)
        monkeypatch.setattr(logging.handlers, "RotatingFileHandler", lambda *_args, **_kwargs: fake_handler)
        reloaded = importlib.reload(attachment_mod)
        assert make_dir.called
        assert fake_handler.setFormatter.called
        importlib.reload(attachment_mod)
    finally:
        payload_logger.handlers.clear()
        payload_logger.handlers.extend(original_handlers)

    session = make_session()
    added = AttachmentHelpers.add_attachment_to_session(session, {"file_name": "a.png"})
    assert added["success"] and session.workflow_state["pending_attachments"]
    assert AttachmentHelpers.get_attachment_summary(make_session())["total_count"] == 0
    assert not AttachmentHelpers.add_attachment_to_session(SimpleNamespace(workflow_state=None), {"file_name": "bad"})["success"]

    all_pending = make_session(pending_attachments=[{"file_name": "a"}, {"file_name": "b", "status": "rejected"}])
    assert AttachmentHelpers.approve_pending_attachment(all_pending)
    assert len(all_pending.workflow_state["extracted_entities"][0]["attachments"]) == 1
    not_found = make_session(pending_attachments=[{"file_name": "a"}])
    assert AttachmentHelpers.approve_pending_attachment(not_found, "missing")
    limited = make_session(
        pending_attachments=[{"file_name": "f"}],
        extracted_entities=[{"attachments": [{"file_name": str(i)} for i in range(AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ)]}],
    )
    assert AttachmentHelpers.approve_pending_attachment(limited)
    assert len(limited.workflow_state["extracted_entities"][0]["attachments"]) == AttachmentHelpers.MAX_ATTACHMENTS_PER_RFQ

    response = SimpleNamespace(
        status=200,
        headers={"Content-Type": "image/png"},
        read=AsyncMock(return_value=b"png"),
        text=AsyncMock(return_value=""),
    )
    client = SimpleNamespace(get=MagicMock(return_value=AsyncContext(response)))
    monkeypatch.setattr(attachment_mod.aiohttp, "ClientSession", lambda: AsyncContext(client))
    monkeypatch.setattr(attachment_mod, "get_settings", lambda: SimpleNamespace(WHATSAPP_FROM_NUMBER="1", WHATSAPP_MEDIA_DOWNLOAD_URL="https://media"), raising=False)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(WHATSAPP_FROM_NUMBER="1", WHATSAPP_MEDIA_DOWNLOAD_URL="https://media"))
    result = await attachment_mod.AttachmentHelpers.download_and_encode_attachment("id", "photo.png")
    assert result["attachment"]["file_type"] == "image/png"
    result = await attachment_mod.AttachmentHelpers.download_and_encode_attachment("id", "photo.bin", "application/octet-stream")
    assert result["attachment"]["file_type"] == "application/octet-stream"
    assert base64.b64encode(b"png").decode() == result["attachment"]["file_content"]


class Field:
    def __init__(self, required=True, description=None):
        self.required = required
        self.description = description

    def is_required(self):
        return self.required


class DynamicSchema:
    model_fields = {
        "sourceType": Field(True, "internal"),
        "full_name": Field(True, None),
        "zipCode": Field(True, "Postal Code"),
        "address1": Field(False, "Address"),
        "optional": Field(False, None),
    }


class BrokenSchema:
    @property
    def model_fields(self):
        raise RuntimeError("schema unavailable")


@pytest.mark.asyncio
async def test_authentication_dynamic_schema_and_validation_fallbacks(monkeypatch):
    assert "Full Name" in AuthenticationHelpers.generate_registration_message(DynamicSchema, "buyer")
    assert "full_name" not in AuthenticationHelpers.generate_confirmation_message_dynamic(DynamicSchema, {"full_name": "Ada"})
    questions = AuthenticationHelpers.generate_registration_questions_dynamic(
        DynamicSchema,
        {"unknown": "ignored"},
        ["address1", "zipCode", "full_name"],
        "bad value",
    )
    assert "pincode" in questions and "full name" in questions and "Validation Error" in questions
    assert AuthenticationHelpers.generate_registration_message(BrokenSchema(), "seller").startswith("Hello Seller")
    assert AuthenticationHelpers.generate_confirmation_message_dynamic(BrokenSchema(), {}) == "Please confirm your registration details. (Error generating message)"
    assert AuthenticationHelpers.generate_registration_questions_dynamic(BrokenSchema(), {}, ["full_name"]) == "Please provide the remaining registration details."

    payload = AuthenticationHelpers.build_registration_payload_dynamic(DynamicSchema, {"full_name": "Ada", "optional": ""}, "9199")
    assert payload["full_name"] == "Ada" and "details" not in payload
    monkeypatch.setattr("app.schemas.user.normalize_phone_number", Mock(side_effect=RuntimeError("phone")))
    fallback = AuthenticationHelpers.build_registration_payload_dynamic(DynamicSchema, {}, "9199")
    assert fallback == {"organizationPhonenumber": "9199", "sourceType": "W", "whatsApp": True}

    monkeypatch.setattr(authentication_mod, "get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune"}))
    valid, error = await AuthenticationHelpers.validate_entities(
        {"email": "a@x.com", "gstin": "27ABCDE1234F1Z5", "zipCode": "560001"},
        DynamicSchema,
    )
    assert valid["email"] == "a@x.com" and error is None


@pytest.mark.asyncio
async def test_chat_transform_context_stage_and_auth_message_fallbacks(monkeypatch):
    schema = ChatServiceHelpers.create_rfq_schema_from_entities(
        {"description": "pump", "date_validation_error": True}
    )
    assert schema.date_validation_error is True
    transformed = ChatServiceHelpers.transform_entities_to_schema({"description": "pump"})
    assert transformed["items"][0]["unit_of_measures"] == "unit(s)"
    combined = ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products(
        [
            {"entities": {"description": "pump", "brand": "Acme", "remarks": "urgent"}},
            {"entities": {"brand": "Only brand"}},
        ]
    )
    assert combined.items[0]["brand"] == "Acme" and combined.project_desc == "pump"
    assert ChatServiceHelpers.transform_entities_to_schema({"description": "pump", "deliveryDate": datetime(2025, 1, 1)})["delivery_date"].year == 2025
    monkeypatch.setattr(ChatServiceHelpers, "transform_entities_to_schema", staticmethod(lambda _entities: {}))
    generated = ChatServiceHelpers.create_combined_rfq_schema_from_multiple_products(
        [{"entities": {"description": "pump"}}]
    )
    assert generated.project_desc == "RFQ for pump"

    class FlakyEmptyDict(dict):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def __bool__(self):
            self.calls += 1
            return self.calls == 1

    assert ChatServiceHelpers.determine_conversation_stage(make_session(extracted_entities={"description": "x"})) == "processing_single"
    assert ChatServiceHelpers.determine_conversation_stage(make_session(extracted_entities=FlakyEmptyDict())) == "collecting"
    assert ChatServiceHelpers.determine_conversation_stage(make_session(extracted_entities=SimpleNamespace())) == "collecting"

    redis = SimpleNamespace(get=AsyncMock(return_value=None))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: redis)
    context = await ChatServiceHelpers.build_conversation_context(make_session(), "hello")
    assert context["user_role"] is None

    history = make_session()
    history.conversation_history = {"messages": [{"sender": "assistant", "content": "noise", "intent": "other"}]}
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(history, "current", {"original_message": "original"}) == "original"
    assert ChatServiceHelpers.find_most_relevant_message_after_auth(history, "current") == "current"


def test_excel_confirmation_and_excel_helper_remaining_fallbacks(monkeypatch):
    message = ExcelConfirmationHelpers.generate_confirmation_message({"has_missing_items": True}, "x.xlsx")
    assert "Missing quantity" in message and "Details" not in message

    existing = SimpleNamespace(workflow_state={"old": True})
    ExcelConfirmationHelpers.save_excel_confirmation_data(existing, {"rfqs": [{"products": []}], "items": []})
    assert existing.workflow_state["pending_excel_confirmation"]["ready_for_multiple_rfq_creation"]
    assert ExcelConfirmationHelpers.prepare_multiple_rfq_data({"rfqs": [{"products": [{"description": "x"}]}]})[0]["products"]

    class BrokenState:
        @property
        def workflow_state(self):
            raise RuntimeError("state")

    assert ExcelConfirmationHelpers.get_excel_confirmation_data(BrokenState()) == {}
    ExcelConfirmationHelpers.clear_excel_confirmation_data(BrokenState())

    class BrokenRFQs:
        def __len__(self):
            raise RuntimeError("rfqs")

    assert ExcelConfirmationHelpers.format_rfq_creation_summary(BrokenRFQs()) == "RFQs created successfully! You'll receive quotes soon."

    assert ExcelHelpers.convert_excel_to_entities(None) == []
    assert ExcelHelpers.calculate_excel_completeness(None) == 0
    assert "first few" not in ExcelHelpers.generate_excel_summary({"filename": "x", "total_items": 2, "items": []})

    class BadItem:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("field")

    assert ExcelHelpers.identify_missing_fields([BadItem()]) == []

    def raises(*_args, **_kwargs):
        raise RuntimeError("helper")

    monkeypatch.setattr(ExcelHelpers, "convert_excel_to_entities", staticmethod(raises))
    monkeypatch.setattr(ExcelHelpers, "calculate_excel_completeness", staticmethod(raises))
    monkeypatch.setattr(ExcelHelpers, "identify_missing_fields", staticmethod(raises))
    failed_context = ExcelHelpers.prepare_excel_context({"success": False, "filename": "x", "items": []}, "+1")
    assert failed_context["extracted_entities"] == [] and failed_context["missing_fields"] == []

    headers_context = {"excel_data": {"filename": "x", "items": [], "headers": ["Description"], "column_mapping": {}}}
    instructions = ExcelHelpers.generate_reupload_instructions([], headers_context)
    assert any("Missing required columns" in line for line in instructions)
    for field in ("Specification", "ItemDescription", "Uom", "Quantity"):
        text = ExcelHelpers.generate_reupload_instructions(
            [],
            {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {"missing_required_fields": [f"Missing '{field}' in Item 1: x"]}}},
        )[0]
        assert field in text
    assert ExcelHelpers.generate_reupload_instructions(
        [], {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {"missing_required_fields": ["other problem"]}}}
    )[0].endswith("updated file.")
    assert "looks good" in ExcelHelpers.generate_reupload_instructions(
        [], {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {"warnings": []}}}
    )[0]




@pytest.mark.asyncio
async def test_session_expiration_renewal_reset_and_activity_variants(monkeypatch):
    settings = SimpleNamespace(redis_session_storage_enabled=False, session_timeout_minutes=60)
    monkeypatch.setattr(session_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr(session_mod, "is_expired", lambda *_args: (False, None, None))
    session = make_session()
    session.created_at = datetime(2025, 1, 1)
    assert not await SessionHelpers.is_session_expired(session)
    missing_created = make_session()
    assert await SessionHelpers.is_session_expired(missing_created)
    monkeypatch.setattr(session_mod, "is_expired", lambda *_args: (True, None, None))
    assert await SessionHelpers.is_session_expired(session)
    assert await SessionHelpers.renew_session_activity(None) is None
    session.workflow_state = None
    renewed = await SessionHelpers.renew_session_activity(session)
    assert renewed.workflow_state["last_activity_at"]

    assert not await SessionHelpers.should_send_expiration_message(None)
    active = make_session(existing=True)
    await SessionHelpers.renew_session_activity(active)
    session.workflow_state = None
    session.created_at = None
    session.conversation_history = None
    monkeypatch.setattr(SessionHelpers, "is_session_expired", AsyncMock(return_value=True))
    assert not await SessionHelpers.should_send_expiration_message(session)
    session.conversation_history = {"openai_messages": [{}]}
    assert not await SessionHelpers.should_send_expiration_message(session)
    session.conversation_history = {"openai_messages": [{}, {}]}
    session.workflow_state = {"last_expiry_notification": "bad"}
    assert await SessionHelpers.should_send_expiration_message(session)
    session.workflow_state["last_expiry_notification"] = datetime.now(timezone.utc).isoformat()
    assert not await SessionHelpers.should_send_expiration_message(session)

    db = SimpleNamespace(append_session_data=Mock())
    assert await SessionHelpers.handle_session_expiry(None, db) is None
    empty_expiry = make_session()
    empty_expiry.workflow_type = WorkflowType.rfq_creation
    await SessionHelpers.handle_session_expiry(empty_expiry, db)
    auth_session = make_session(profile_selection_stage="stage", profile_options=[1], remove=True)
    auth_session.workflow_type = WorkflowType.authentication
    await SessionHelpers.handle_session_expiry(auth_session, db)
    assert auth_session.workflow_type == WorkflowType.authentication
    normal_session = make_session(old=True)
    normal_session.workflow_type = WorkflowType.rfq_creation
    await SessionHelpers.handle_session_expiry(normal_session, db)
    assert normal_session.workflow_type is None

    empty_fields = make_session()
    empty_fields.bfs_products_searched = None
    empty_fields.bfs_search_count = None
    empty_fields.bfs_price_accepted = None
    empty_fields.bfs_counter_offers = None
    SessionHelpers.update_bfs_activity(empty_fields, {})
    assert empty_fields.bfs_search_count == 0
    truthy = make_session()
    SessionHelpers.update_bfs_activity(truthy, {"searched_products": [], "accepted_prices": [], "counter_offers": []})
    populated_bfs = make_session()
    populated_bfs.bfs_products_searched = ["old"]
    populated_bfs.bfs_search_count = 1
    populated_bfs.bfs_price_accepted = [1]
    populated_bfs.bfs_counter_offers = [2]
    SessionHelpers.update_bfs_activity(populated_bfs, {"searched_products": ["new"]})
    SessionHelpers.update_bfs_activity(populated_bfs, {"accepted_prices": [3]})
    SessionHelpers.update_bfs_activity(populated_bfs, {"counter_offers": [4]})
    assert populated_bfs.bfs_products_searched == ["old", "new"]

    bidding = make_session()
    for field in ("products_bid_for", "bids_received", "bids_accepted", "counter_offers_made", "counter_offers_accepted", "rfqs_with_response"):
        setattr(bidding, field, None)
    SessionHelpers.update_bidding_activity(
        bidding,
        {"products_bid_for": ["p"], "bids_received": ["r"], "bids_accepted": ["a"], "counter_offers_made": ["m"], "counter_offers_accepted": ["c"], "rfqs_with_response": ["r1", "r1"]},
    )
    assert bidding.rfqs_with_response == ["r1"]
    populated_bidding = make_session()
    populated_bidding.products_bid_for = ["old-product"]
    populated_bidding.bids_received = ["old-received"]
    populated_bidding.bids_accepted = ["old-accepted"]
    populated_bidding.counter_offers_made = ["old-made"]
    populated_bidding.counter_offers_accepted = ["old-counter"]
    populated_bidding.rfqs_with_response = ["existing"]
    SessionHelpers.update_bidding_activity(populated_bidding, {})
    SessionHelpers.update_bidding_activity(
        populated_bidding,
        {"products_bid_for": ["new-product"], "bids_received": ["new-received"], "bids_accepted": ["new-accepted"], "counter_offers_made": ["new-made"], "counter_offers_accepted": ["new-counter"], "rfqs_with_response": ["existing", "new"]},
    )
    assert populated_bidding.rfqs_with_response == ["existing", "new"]


def test_session_averages_serialization_and_summary_paths():
    empty = make_session()
    empty.rfq_ids = []
    empty.rfq_id = None
    empty.product_items = [{"category": "x"}]
    empty.extracted_entities = []
    SessionHelpers.calculate_session_averages(empty)
    assert empty.avg_products_per_rfq == 0
    only_id = make_session()
    only_id.rfq_ids = []
    only_id.rfq_id = "r"
    only_id.product_items = "bad"
    only_id.extracted_entities = {"description": "pump"}
    SessionHelpers.calculate_session_averages(only_id)
    assert only_id.avg_products_per_rfq == 0 and only_id.avg_categories_per_rfq == 1
    mixed = make_session()
    mixed.rfq_ids = ["r"]
    mixed.product_items = [{"category": "IT"}, {"name": "none"}, "bad"]
    mixed.extracted_entities = [{"category": "IT"}, {"description": "pump"}, "bad"]
    SessionHelpers.calculate_session_averages(mixed)
    assert mixed.avg_products_per_rfq == 3 and mixed.avg_categories_per_rfq == 2

    class ValueEnum:
        value = "value"

    cleaned = SessionHelpers.clean_for_json_serialization(
        {"none": None, "enum": ValueEnum(), "date": date(2025, 1, 1), "dict": {"x": 1}, "list": [1], "tuple": (2,), "object": object()}
    )
    assert cleaned["enum"] == "value" and cleaned["date"] == "2025-01-01" and cleaned["tuple"] == [2]
    assert cleaned["object"] == "<object object>" or cleaned["object"] == str(cleaned["object"])
    assert SessionHelpers.should_use_summary_aware_extraction("refer to prior") is False


@pytest.mark.asyncio
async def test_summarization_history_preview_data_decisions_and_redis_fallbacks(monkeypatch):
    monkeypatch.setattr(SummarizationHelpers, "_get_atomic_message_index", Mock(return_value=1))
    session = make_session()
    session.conversation_history = {"openai_messages": "bad", "metadata": None, "messages": None}
    SummarizationHelpers.add_to_conversation_history(session, "system", {"image": True})
    SummarizationHelpers.add_to_conversation_history(session, "user", "x" * 101, intent="buy")
    SummarizationHelpers.add_to_conversation_history(session, "assistant", 42)
    assert len(session.conversation_history["messages"]) == 3
    assert "confidence" not in session.conversation_history["messages"][1]

    class Redis:
        def incr(self, _key):
            raise RuntimeError("redis")

    monkeypatch.setattr("redis.from_url", lambda *_args, **_kwargs: Redis())
    assert isinstance(SummarizationHelpers._get_atomic_message_index("s"), int)

    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    complete = make_session()
    complete.created_at = now
    complete.completed_at = now + timedelta(minutes=30)
    complete.workflow_type = WorkflowType.rfq_creation
    complete.outcome = ConversationOutcome.completed
    complete.rfq_ids = ["r1"]
    complete.conversation_history = {"messages": [{"sender": "user", "content": "confirm"}, {"sender": "assistant", "content": "ok"}]}
    data = SummarizationHelpers.prepare_enhanced_summary_data(
        complete,
        {"multiple_rfqs": [{"id": 1}], "rfq_details": {"id": 2}, "user_preferences": {"brand": "A"}, "budget_constraints": {"max": 2}},
    )
    assert data["session_duration_minutes"] == 30 and data["products_count"] == 1
    assert any("Preferences" in decision for decision in data["key_decisions_made"])

    class BrokenSession:
        external_user_id = "u"
        workflow_type = None
        outcome = None
        rfq_ids = []
        rfq_id = None

        @property
        def conversation_history(self):
            raise RuntimeError("history")

    assert SummarizationHelpers.prepare_enhanced_summary_data(BrokenSession(), {})["user_id"] == "u"
    assert SummarizationHelpers._extract_key_decisions([{"sender": "assistant", "content": "none"}], {}) == []
    assert SummarizationHelpers._extract_key_decisions([{"sender": "user", "content": "I need a pump"}], {"user_preferences": {"brand": "A"}, "budget_constraints": {"max": 2}})

    class BadMessage:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("message")

    assert SummarizationHelpers._extract_key_decisions([BadMessage()], {}) == []

    chat = SimpleNamespace(generate_session_summary=AsyncMock(side_effect=RuntimeError("summary")))
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    await SummarizationHelpers.handle_session_completion_async(chat, daily, {"session_id": "s", "user_id": "u"})
    daily.generate_daily_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_support_non_keyerror_formatting_is_kept_safe():
    helper = SupportHelpers.__new__(SupportHelpers)

    class ExplodingString(str):
        def format(self, **_kwargs):
            raise RuntimeError("format failure")

    helper._parse_email_list = lambda value: [value] if value else []
    helper._build_email_body = lambda data: data.get("body", "")
    result = helper.integrate_template_and_data(
        {"to": "user@example.com", "subject": ExplodingString("subject"), "body": "body", "empty": ""},
        {},
    )
    assert result["subject"] == "subject"
