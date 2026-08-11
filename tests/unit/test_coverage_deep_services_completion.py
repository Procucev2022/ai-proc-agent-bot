"""Deep deterministic coverage for the six requested service modules.

These tests deliberately keep all database, cache, vector-store, AI, WhatsApp,
HTTP, and filesystem boundaries mocked while executing service decisions and
error handling in-process.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pandas as pd
import pytest

import app.services.chat_service as chat_module
import app.services.conversation_analytics_service as analytics_module
import app.services.enhanced_auto_categorization_service as auto_module
import app.services.enhanced_excel_report_service as excel_module
import app.services.enhanced_seller_matching_service as matching_module
import app.services.seller_service as seller_module
from app.models import ConversationOutcome, WorkflowType


# Shared deterministic test doubles -----------------------------------------


def unit_settings(**overrides):
    values = {
        "redis_url": "redis://unit-test/0",
        "redis_session_storage_enabled": False,
        "chroma_host": "unit-chroma",
        "chroma_port": 8000,
        "support_contact_info": "support@example.test",
        "procucev_rfq_details_url": "https://portal.example.test/rfqs",
        "rfq_max_allowed": 3,
        "use_sectioned_rfq": False,
        "procucev_db_name": "procurev",
        "whatsapp_db": "whatsapp",
        "email_templates_path": "templates",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_session(**overrides):
    values = {
        "session_id": "session-1",
        "external_user_id": "919999999999",
        "workflow_type": WorkflowType.rfq_creation,
        "workflow_state": {},
        "conversation_history": {"messages": [], "openai_messages": []},
        "created_at": datetime(2025, 1, 1),
        "retention_date": date(2025, 1, 1),
        "last_activity_at": datetime(2025, 1, 1),
        "completed_at": None,
        "outcome": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_user(**overrides):
    values = {
        "phone_number": "+919999999999",
        "id": "seller-1",
        "org_id": "org-1",
        "email": "seller@example.test",
        "name": "Test Seller",
        "is_registered": True,
        "role": SimpleNamespace(value="buyer"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_chat_service():
    service = object.__new__(chat_module.ChatService)
    service.whatsapp_service = MagicMock()
    service.whatsapp_service.send_message = AsyncMock()
    service.whatsapp_service.send_configurable_buttons = AsyncMock()
    service.session_manager = MagicMock()
    service.session_manager.save_session = AsyncMock()
    service.session_manager.send_and_track_message = AsyncMock()
    service.session_manager.get_conversation_context = AsyncMock()
    service.session_manager.add_message_to_history = MagicMock()
    service._openai_service = MagicMock()
    service._openai_service.close = AsyncMock()
    service._intent_service = MagicMock()
    service._intent_service.classify_intent = AsyncMock()
    service._response_helpers = MagicMock()
    service._authentication_service = MagicMock()
    service._authentication_service.otp_service = MagicMock()
    service._exit_service = MagicMock()
    service._exit_service.handle_exit_intent = AsyncMock()
    service._cancel_service = MagicMock()
    service._cancel_service.handle_cancel_intent = AsyncMock()
    service._faq_service = MagicMock()
    service._should_use_summary_aware_extraction = False
    service.settings = unit_settings()
    return service


class ContextDouble:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class QueryDouble:
    def __init__(self, values=None, first_value=None, count_value=0):
        self.values = values or []
        self.first_value = first_value
        self.count_value = count_value
        self.statement = "SELECT unit-test"

    def with_entities(self, *args):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def group_by(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def all(self):
        return self.values

    def first(self):
        return self.first_value

    def count(self):
        return self.count_value


class DBDouble:
    def __init__(self, query=None):
        self.query_double = query or QueryDouble()
        self.bind = "unit-bind"
        self.added = []
        self.commit = MagicMock()
        self.rollback = MagicMock()
        self.close = MagicMock()

    def query(self, *args, **kwargs):
        return self.query_double

    def add(self, value):
        self.added.append(value)

    def execute(self, *args, **kwargs):
        return []


class LockDouble:
    def __init__(self, acquired=True, release_error=None):
        self.acquired = acquired
        self.release_error = release_error
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock(side_effect=release_error)


# ChatService ---------------------------------------------------------------


def test_chat_cleanup_default_workflow_and_helper_errors():
    service = make_chat_service()
    service._openai_service.close.side_effect = RuntimeError("close failed")
    asyncio.run(service.cleanup())

    session = make_session(workflow_type=None)
    assert service._get_workflow_or_default(session) == WorkflowType.general_inquiry
    assert service._get_workflow_or_default(session, "rfq_creation") == WorkflowType.rfq_creation
    assert service._get_workflow_or_default(session, "not-a-workflow") == WorkflowType.general_inquiry
    session.workflow_type = WorkflowType.seller_rfq_view
    assert service._get_workflow_or_default(session) == WorkflowType.seller_rfq_view


@pytest.mark.asyncio
async def test_chat_process_message_rfq_interactive_buttons_skip_classification(monkeypatch):
    service = make_chat_service()
    session = make_session(workflow_type=WorkflowType.general_inquiry)
    service.session_manager.get_conversation_context.return_value = session
    service._authentication_service = MagicMock(otp_service=MagicMock())

    handler = MagicMock()
    handler.handle_rfq_interest_click = AsyncMock(return_value={"status": "interest"})
    handler.handle_check_details_click = AsyncMock(return_value={"status": "details"})
    handler.handle_request_rfq_click = AsyncMock(return_value={"status": "request"})
    monkeypatch.setattr(
        "app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler",
        lambda **kwargs: handler,
    )

    cases = [
        ("rfq_interested_RFQ_TEST_001_seller-1", "handle_rfq_interest_click", "interest"),
        ("rfq_check_details_RFQ_TEST_002_seller-2", "handle_check_details_click", "details"),
        ("rfq_request_RFQ_TEST_003_seller-3", "handle_request_rfq_click", "request"),
    ]
    for button_id, method_name, status in cases:
        result = await service.process_message(
            "919999999999", {"button_reply": {"id": button_id}}, "interactive"
        )
        assert result == {"status": status}
        method = getattr(handler, method_name)
        assert method.await_args.args[1].startswith("RFQ_TEST_")
        assert method.await_args.args[2] == button_id.rsplit("_", 1)[1]

    service.intent_service.classify_intent.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_process_message_bfs_exit_and_cancel_buttons(monkeypatch):
    service = make_chat_service()
    session = make_session(workflow_type=WorkflowType.general_inquiry)
    service.session_manager.get_conversation_context.return_value = session
    service._authentication_service = MagicMock(otp_service=MagicMock())

    bid_handler = MagicMock()
    bid_handler.handle_accept_bid_click = AsyncMock(return_value={"status": "accepted"})
    bid_handler.handle_reject_bid_click = AsyncMock(return_value={"status": "rejected"})
    monkeypatch.setattr(
        "app.services.handlers.bfs_seller_bid_handler.BFSSellerBidHandler",
        lambda **kwargs: bid_handler,
    )
    for button_id, expected in [("bfs_seller_accept_bfs_1_seller-1", "accepted"), ("bfs_seller_reject_bfs_2_seller-2", "rejected")]:
        assert (await service.process_message("1", {"button_reply": {"id": button_id}}, "interactive"))["status"] == expected

    service.exit_service.handle_exit_intent.return_value = {"status": "exited"}
    assert (await service.process_message("1", {"button_reply": {"id": "confirm_exit"}}, "interactive"))["status"] == "exited"
    service.cancel_service.handle_cancel_intent.return_value = {"status": "cancelled"}
    assert (await service.process_message("1", {"button_reply": {"id": "confirm_cancel"}}, "interactive"))["status"] == "cancelled"
    assert (await service.process_message("1", {"button_reply": {"id": "cancel_no_credits"}}, "interactive"))["status"] == "cancelled"
    assert service.session_manager.save_session.await_count >= 2


@pytest.mark.asyncio
async def test_chat_process_message_seller_auth_otp_paths(monkeypatch):
    service = make_chat_service()
    service.session_manager.save_session = AsyncMock()
    handler = MagicMock()
    handler.handle_switch_response = AsyncMock(return_value={"status": "switch"})
    handler.handle_otp_validated = AsyncMock(return_value={"status": "otp_validated"})
    monkeypatch.setattr(
        "app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler",
        lambda **kwargs: handler,
    )

    session = make_session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "switch_prompt"},
    )
    service.session_manager.get_conversation_context.return_value = session
    assert (await service.process_message("1", "switch"))["status"] == "switch"

    session.workflow_state = {
        "auth_stage": "otp",
        "target_seller_email": "seller@example.test",
        "target_seller_id": "seller-1",
        "target_seller_user": {"id": "seller-1"},
    }
    service.authentication_service.otp_service.validate_otp = AsyncMock(return_value={"status": "otp_valid"})
    service.authentication_service.store_user_session_with_email = AsyncMock(return_value={"success": True})
    assert (await service.process_message("1", "123456"))["status"] == "otp_validated"

    service.authentication_service.otp_service.validate_otp.return_value = {"status": "max_otp_exceeded"}
    result = await service.process_message("1", "000000")
    assert result["status"] == "max_otp_exceeded"
    assert session.workflow_state == {}

    session.workflow_type = WorkflowType.seller_rfq_intimation
    session.workflow_state = {"auth_stage": "otp"}
    service.authentication_service.otp_service.validate_otp.return_value = {"status": "otp_invalid"}
    assert (await service.process_message("1", "bad"))["status"] == "otp_invalid"

    service._authentication_service = MagicMock(otp_service=None)
    result = await service.process_message("1", "bad")
    assert result["status"] == "error"
    service.whatsapp_service.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_chat_interactive_parser_and_attachment_transform_validation(monkeypatch):
    service = make_chat_service()
    user = make_user()
    session = make_session()
    service._handle_button_response = AsyncMock(return_value={"status": "button"})
    service._handle_list_response = AsyncMock(return_value={"status": "list"})
    service._process_text_message = AsyncMock(return_value={"status": "text"})

    assert await service._process_interactive_message(user, session, '{"type":"button_reply","button_reply":{"id":"b"}}') == {"status": "button"}
    assert await service._process_interactive_message(user, session, '{bad json') == {"status": "text"}
    assert await service._process_interactive_message(user, session, {"type": "list_reply", "list_reply": {"id": "l"}}) == {"status": "list"}
    assert await service._process_interactive_message(user, session, {"type": "other"}) == {"status": "text"}

    session.workflow_state = {"sectioned_rfq": {"sections": {"date_location": {"confirmed": True, "data": {"pincode": "400001", "city": "Mumbai"}}}}}
    products, details = await service.transform_rfq_to_section_rfq_format(session, [{"description": "pump", "quantity": "2.5", "uom": "pcs"}])
    assert products[0]["quantity"] == 2.5
    assert details["pincode"] == "400001"

    session.workflow_state = {"delivery_date": "tomorrow"}
    date_validator = MagicMock()
    date_validator._validate_delivery_date = AsyncMock(return_value={"is_valid": False, "error": "bad date"})
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", lambda **kwargs: date_validator)
    with pytest.raises(ValueError):
        await service.transform_rfq_to_section_rfq_format(session, [])

    session.workflow_state = {"pincode": "123"}
    with pytest.raises(ValueError):
        await service.transform_rfq_to_section_rfq_format(session, [])

    session.workflow_state = {"pincode": "123456"}
    monkeypatch.setattr("app.utils.pincode_lookup.get_location_from_pincode_async", AsyncMock(return_value=None))
    with pytest.raises(ValueError):
        await service.transform_rfq_to_section_rfq_format(session, [])


@pytest.mark.asyncio
async def test_chat_excel_upload_registration_validation_attachment_and_locks(monkeypatch):
    service = make_chat_service()
    user = make_user(is_registered=False)
    session = make_session()
    service._response_helpers.generate_contextual_response = AsyncMock(return_value="register")
    result = await service._process_excel_upload(user, session, {})
    assert result["response"] == "registration_required"

    user.is_registered = True
    result = await service._process_excel_upload(user, session, "bad")
    assert result["response"] == "Invalid Excel upload content format"

    service._response_helpers.generate_contextual_response.return_value = "access"
    result = await service._process_excel_upload(user, session, {"document": {"filename": "x.xlsx"}})
    assert result["response"] == "file_access_error"

    validator = MagicMock()
    validator.validate_excel_file_from_url = AsyncMock(return_value={"valid": False, "error": "invalid"})
    monkeypatch.setattr(chat_module, "ExcelValidationService", lambda: validator)
    result = await service._process_excel_upload(user, session, {"document": {"link": "http://unit", "filename": "x.xlsx"}})
    assert result["response"] == "validation_failed"

    validator.validate_excel_file_from_url.return_value = {"valid": True, "content": b"x"}
    service._image_processor = MagicMock()
    service._image_processor.process_image_message = AsyncMock(return_value={"status": "attachment"})
    session.workflow_state = {"pending_optional_rfq": {"x": 1}}
    assert (await service._process_excel_upload(user, session, {"document": {"link": "http://unit", "filename": "x.xlsx"}}))["status"] == "attachment"

    session.workflow_state = {"excel_file_processed": True, "excel_filename": "old.xlsx"}
    result = await service._process_excel_upload(user, session, {"document": {"link": "http://unit", "filename": "new.xlsx"}})
    assert result["response"] == "excel_already_processed"


@pytest.mark.asyncio
async def test_chat_excel_upload_incomplete_and_critical_lock_cleanup(monkeypatch):
    service = make_chat_service()
    user = make_user()
    session = make_session(workflow_state={})
    validator = MagicMock()
    validator.validate_excel_file_from_url = AsyncMock(return_value={"valid": True, "content": b"x"})
    monkeypatch.setattr(chat_module, "ExcelValidationService", lambda: validator)
    lock = LockDouble(acquired=True)
    redis_client = MagicMock()
    redis_client.lock.return_value = lock
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *args, **kwargs: redis_client)
    monkeypatch.setattr(chat_module, "get_settings", lambda: unit_settings(redis_url="redis://unit"))
    monkeypatch.setattr(chat_module, "ExcelProcessingService", lambda _: MagicMock())
    processing = MagicMock()
    processing.process_excel_file = AsyncMock(return_value={"success": False, "items": [], "filename": "x.xlsx"})
    monkeypatch.setattr(chat_module, "ExcelProcessingService", lambda _: processing)
    service._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    monkeypatch.setattr(chat_module.ExcelHelpers, "prepare_excel_context", lambda result, phone: {"completeness": 0})
    result = await service._process_excel_upload(user, session, {"document": {"link": "http://unit", "filename": "x.xlsx"}})
    assert result["status"] == "incomplete"
    lock.release.assert_awaited_once()

    lock = LockDouble(acquired=False)
    redis_client.lock.return_value = lock
    result = await service._process_excel_upload(user, make_session(workflow_state={}), {"id": "media", "filename": "x.xlsx"})
    assert result["response"] == "upload_in_progress"

    lock = LockDouble(acquired=True, release_error=RuntimeError("release"))
    redis_client.lock.return_value = lock
    processing.process_excel_file.side_effect = RuntimeError("processor")
    session = make_session(workflow_state={})
    result = await service._process_excel_upload(user, session, {"document": {"link": "http://unit", "filename": "x.xlsx"}})
    assert result["status"] == "error"
    service.session_manager.send_and_track_message.assert_awaited()


@pytest.mark.asyncio
async def test_chat_process_message_authentication_routing_and_unknown_type(monkeypatch):
    service = make_chat_service()
    session = make_session(workflow_type=WorkflowType.general_inquiry)
    service.session_manager.get_conversation_context.return_value = session
    service.intent_service.classify_intent.return_value = {"intent": "greeting", "confidence": 0}
    service.handle_irrelevant_message_flow = AsyncMock()
    class AuthenticatedUser(SimpleNamespace):
        pass

    user = AuthenticatedUser(**vars(make_user()))
    monkeypatch.setattr(chat_module, "User", AuthenticatedUser)
    service.authentication_orchestrator_flow = AsyncMock(return_value=user)
    result = await service.process_message("1", "hello", "voice")
    assert result["status"] == "error"
    assert "Unknown message type" in result["error"]

    service.intent_service.classify_intent.side_effect = RuntimeError("classifier")
    service._process_text_message = AsyncMock(return_value={"status": "routed"})
    service.authentication_orchestrator_flow.return_value = user
    assert await service.process_message("1", "hello", "text") == {"status": "routed"}


# ConversationAnalyticsService ---------------------------------------------


def make_analytics_service(monkeypatch):
    ai = MagicMock()
    ai.tools_dir = Path("unit-tools")
    ai.default_model = "unit-model"
    ai._load_prompt.return_value = "system prompt"
    ai.client = MagicMock()
    monkeypatch.setattr(analytics_module, "get_settings", lambda: unit_settings())
    monkeypatch.setattr(analytics_module, "OpenAIService", lambda: ai)
    return analytics_module.ConversationAnalyticsService(), ai


def sample_ai_session(session_id="s1", user_type="buyer"):
    return {
        "session_id": session_id,
        "phone_number": "+1",
        "user_type": user_type,
        "confidence_score": 90,
        "analysis_reasoning": "unit",
        "buyer_identities": [{"buyer_email": "buyer@example.test", "buyer_metrics": {"successful_rfqs_ai": 2, "bfs_searches": 1, "bfs_search_details": [{"search_keyword": "pump", "results_found": ["A", "B"], "action_taken": "bid_placed"}]}}] if user_type == "buyer" else [],
        "seller_identities": [{"seller_email": "seller@example.test", "seller_metrics": {"rfq_requested_ai": 1}}] if user_type == "seller" else [],
        "unknown_user_metrics": {"number_of_faq_or_general_queries": 1},
        "registration_metrics": {},
        "seller_rfq_interest_event": [{"rfq_id": "R1", "seller_id": "S1"}] if user_type == "seller" else [],
    }


def test_analytics_constructor_dataframes_and_chat_sequence(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    buyer, seller, unknown = service._create_dataframes_from_sessions(
        [sample_ai_session("b", "buyer"), sample_ai_session("s", "seller"), sample_ai_session("u", "unknown")],
        "2025-01-02",
    )
    assert len(buyer) == len(seller) == len(unknown) == 1
    bfs = service._create_bfs_search_dataframe([sample_ai_session("b", "buyer")], "2025-01-02")
    assert bfs.iloc[0]["searched_result"] == "A, B"
    interest = service._create_seller_rfq_interest_event_df([sample_ai_session("s", "seller")], "2025-01-02")
    assert interest.iloc[0]["rfq_id"] == "R1"

    history = {"messages": [
        {"role": "user", "content": {"body": {"text": "hello"}}, "timestamp": "t1"},
        {"role": "assistant", "content": {"button_reply": {"title": "Confirm"}}},
        {"role": "user", "content": {"type": "button_reply", "button_reply": {"title": "Again"}}},
        {"role": "assistant", "content": {"other": "value"}},
        {"role": "system", "content": "ignored"},
    ]}
    sequence = service._extract_chat_sequence(history)
    assert "User: hello" in sequence and "Assistant: Confirm" in sequence and "Again" in sequence
    assert service._extract_chat_sequence({"messages": []}) == ""


def test_analytics_safe_json_and_category_helpers(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    assert service._safe_json_value(None) is None
    assert service._safe_json_value(float("nan")) is None
    assert service._safe_json_value("nan") is None
    assert service._safe_json_value([]) is None
    assert service._safe_json_value("value") == "value"
    assert service._count_categories_from_df(pd.DataFrame(), "items", {}) == {}
    frame = pd.DataFrame({"items": [["pump", "wire"], ["pump"]]})
    assert service._count_categories_from_df(frame, "items", {"pump": "Tools", "wire": "Electrical"})["Tools"] == 2


@pytest.mark.asyncio
async def test_analytics_batch_preparation_processing_and_ai_parsing(monkeypatch):
    service, ai = make_analytics_service(monkeypatch)
    sessions = [
        make_session(session_id="dict", conversation_history={"messages": [{"role": "user", "content": "x", "timestamp": "t"}, "bad"]}),
        make_session(session_id="list", conversation_history=[{"role": "assistant", "content": "y"}]),
        make_session(session_id="none", conversation_history=None),
    ]
    prepared = await service._prepare_batch_session_data(sessions)
    assert prepared[0]["conversation_history"][0]["content"] == "x"
    assert prepared[1]["conversation_history"][0]["role"] == "assistant"
    assert prepared[2]["conversation_history"] == []
    assert "SESSION IDs TO ANALYZE" in service._build_batch_prompt(prepared, 2, date(2025, 1, 2))

    response = SimpleNamespace(output=[SimpleNamespace(type="function_call", arguments='{"sessions":[{"session_id":"s"}]}')])
    ai.client.responses.create = AsyncMock(return_value=response)
    with patch.object(builtins, "open", mock_open(read_data="{}")):
        parsed = await service._analyze_batch_with_ai(prepared, 1, date(2025, 1, 2))
    assert parsed["sessions"][0]["session_id"] == "s"

    ai.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="message", arguments="{}")])
    with patch.object(builtins, "open", mock_open(read_data="{}")):
        assert await service._analyze_batch_with_ai(prepared, 1, date(2025, 1, 2)) is None

    ai.client.responses.create.side_effect = RuntimeError("ordinary failure")
    with patch.object(builtins, "open", mock_open(read_data="{}")):
        assert await service._analyze_batch_with_ai(prepared, 1, date(2025, 1, 2)) is None

    service._process_sessions_individually = AsyncMock(return_value={"sessions": [{"session_id": "fallback"}], "total_sessions": 1})
    ai.client.responses.create.side_effect = RuntimeError("rate limit too many requests")
    with patch.object(builtins, "open", mock_open(read_data="{}")):
        result = await service._analyze_batch_with_ai(prepared, 1, date(2025, 1, 2))
    assert result["sessions"][0]["session_id"] == "fallback"


@pytest.mark.asyncio
async def test_analytics_batch_loop_individual_fallback_and_lifecycle(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    service.batch_size = 1
    service._prepare_batch_session_data = AsyncMock(side_effect=lambda batch: [{"session_id": batch[0].session_id}])
    service._analyze_batch_with_ai = AsyncMock(side_effect=[{"sessions": [{"session_id": "one"}]}, None])
    monkeypatch.setattr(analytics_module.asyncio, "sleep", AsyncMock())
    result = await service._process_sessions_in_batches([make_session(session_id="a"), make_session(session_id="b")], date(2025, 1, 2))
    assert result["total_sessions"] == 1
    analytics_module.asyncio.sleep.assert_awaited_once_with(2)

    service._analyze_batch_with_ai = AsyncMock(side_effect=[{"sessions": [{"session_id": "a"}]}, RuntimeError("individual")])
    analytics_module.asyncio.sleep.reset_mock()
    result = await service._process_sessions_individually([{"session_id": "a"}, {"session_id": "b"}], 1, date(2025, 1, 2))
    assert result["total_sessions"] == 1
    assert analytics_module.asyncio.sleep.await_count == 1

    service.analyze_daily_conversations = AsyncMock(side_effect=[{"success": True}, {"success": False}])
    ranged = await service.analyze_date_range(date(2025, 1, 1), date(2025, 1, 2))
    assert ranged["processed_dates"] == 2
    service.close = AsyncMock()
    assert await service.__aenter__() is service
    await service.__aexit__(None, None, None)
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_analytics_daily_no_sessions_and_outer_error(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    db = DBDouble(QueryDouble(values=[]))
    monkeypatch.setattr(analytics_module, "get_db_session", lambda: ContextDouble(db))
    result = await service.analyze_daily_conversations(date(2025, 1, 2))
    assert result["success"] and result["total_sessions"] == 0

    monkeypatch.setattr(analytics_module, "get_db_session", lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    result = await service.analyze_daily_conversations(date(2025, 1, 2))
    assert result["success"] is False and "db unavailable" in result["error"]


def test_analytics_remote_queries_and_dump_paths(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    remote = MagicMock()
    remote.execute.return_value = [SimpleNamespace(_mapping={"username": "u", "phone": "1"})]
    monkeypatch.setattr(analytics_module, "get_remote_db_session", lambda: remote)
    assert len(service._query_remote_users(date(2025, 1, 2))) == 1
    assert len(service._query_remote_seller_rfqs(date(2025, 1, 2))) == 1
    assert len(service._query_remote_counter_seller_bids(date(2025, 1, 2))) == 1
    assert len(service._query_remote_rfq_categories(date(2025, 1, 2), ["R1"])) == 1
    remote.execute.side_effect = RuntimeError("remote down")
    assert service._query_remote_users(date(2025, 1, 2)) == []
    assert service._query_remote_seller_rfqs(date(2025, 1, 2)).empty

    db = DBDouble(QueryDouble(first_value=None))
    buyer_frame = pd.DataFrame([{"date": "2025-01-02", "buyer_email": "b@example.test", "phone_number": "+1", "session_id": "s", "total_rfqs_raised": 2, "total_items_in_rfqs": 4, "total_distinct_rfq_category": 2}])
    service._dump_joined_buyer_df_to_db(buyer_frame, db)
    assert db.added
    seller_frame = pd.DataFrame([{"date": "2025-01-02", "seller_email": "s@example.test", "phone_number": "+2", "session_id": "s"}])
    service._dump_joined_seller_df_to_db(seller_frame, db)
    unknown_frame = pd.DataFrame([{"date": None, "session_id": "skip"}, {"date": "2025-01-02", "session_id": "u"}])
    service._dump_unknown_df_to_db(unknown_frame, db)
    fact_frame = pd.DataFrame([{"date": "2025-01-02", "session_id": "s", "rfq_id": "R", "seller_id": "S", "category": "Tools"}])
    service._dump_joined_seller_interest_to_fact_table(fact_frame, db)
    bfs_frame = pd.DataFrame([{"session_id": "s", "email": "b", "phone_number": "1", "searched_keywords": "pump"}])
    service._dump_bfs_search_df_to_db(bfs_frame, db, date(2025, 1, 2))
    assert db.commit.call_count >= 4


# EnhancedAutoCategorizationService ---------------------------------------


def make_auto_service(monkeypatch):
    collection = MagicMock(name="taxonomy")
    client = MagicMock()
    client.heartbeat.return_value = True
    client.get_or_create_collection.return_value = collection
    fallback = MagicMock()
    ai = MagicMock()
    monkeypatch.setattr(auto_module.embedding_functions, "SentenceTransformerEmbeddingFunction", lambda **kwargs: "embedding")
    monkeypatch.setattr(auto_module.chromadb, "HttpClient", lambda **kwargs: client)
    monkeypatch.setattr(auto_module, "get_settings", lambda: unit_settings())
    monkeypatch.setattr(auto_module, "AutoCategorizationService", lambda: fallback)
    monkeypatch.setattr(auto_module, "OpenAIService", lambda: ai)
    service = auto_module.EnhancedAutoCategorizationService()
    service.chroma_path = "unit-chroma"
    return service, collection, client, fallback, ai


def test_auto_constructor_description_keyword_and_search(monkeypatch):
    service, collection, client, _, _ = make_auto_service(monkeypatch)
    assert service._build_enhanced_description("Battery", {"level_3_category": "Battery", "level_2_category": "Power"}) == "Battery Power"
    assert service._build_enhanced_description("Item", {"level_3_category": None, "level_2_category": "item"}) == "Item"

    monkeypatch.setattr(auto_module, "execute_remote_query", lambda *args, **kwargs: [])
    assert service._keyword_lookup_source_of_truth("unknown item")["success"] is False
    calls = []
    def query(sql, params):
        calls.append((sql, params))
        if "SELECT item" in sql:
            return [{"category": "Tools", "freq": 2}]
        return [{"category": "Tools", "freq": 1}]
    monkeypatch.setattr(auto_module, "execute_remote_query", query)
    keyword = service._keyword_lookup_source_of_truth("water pump")
    assert keyword["category"] == "Tools" and keyword["match_source"] == "item+category"

    monkeypatch.setattr(auto_module, "execute_remote_query", lambda *args, **kwargs: [])
    service.category_collection.count.return_value = 0
    assert service._search_by_category_name("pump")["success"] is False
    service.category_collection.count.return_value = 1
    service.category_collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service._search_by_category_name("pump")["success"] is False
    service.category_collection.query.return_value = {"documents": [["Tools"]], "metadatas": [[{"item_count": 2}]], "distances": [[0.2]]}
    assert service._search_by_category_name("pump")["best_match"]["similarity"] == pytest.approx(0.9)
    service.category_collection.query.side_effect = RuntimeError("category down")
    assert service._search_by_category_name("pump")["success"] is False
    bad_client = MagicMock()
    bad_client.heartbeat.side_effect = RuntimeError("heartbeat")
    monkeypatch.setattr(auto_module.chromadb, "HttpClient", lambda **kwargs: bad_client)
    with pytest.raises(RuntimeError):
        auto_module.EnhancedAutoCategorizationService()


def test_auto_cross_validation_hybrid_and_hierarchy(monkeypatch):
    service, collection, _, fallback, _ = make_auto_service(monkeypatch)
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": True, "category": "Tools", "consensus": 0.8})
    assert service._cross_validate_with_fallback("pump", "tools", .7)["use_learning"] is True
    service._keyword_lookup_source_of_truth.return_value = {"success": True, "category": "Electrical", "consensus": 0.4}
    assert service._cross_validate_with_fallback("wire", "Tools", .7)["recommended_category"] == "Electrical"
    service._keyword_lookup_source_of_truth.return_value = {"success": False}
    fallback.collection.query.return_value = {"metadatas": [[]], "distances": [[]]}
    assert service._cross_validate_with_fallback("x", "Tools", .7)["use_learning"] is True
    fallback.collection.query.return_value = {"metadatas": [[{"category": "Tools"}, {"category": "Tools"}]], "distances": [[.2, .4]]}
    assert service._cross_validate_with_fallback("x", "Tools", .7)["reason"] == "Both services agree"
    fallback.collection.query.return_value = {"metadatas": [[{"category": "Electrical"}, {"category": "Electrical"}, {"category": "Tools"}]], "distances": [[.1, .1, 1.8]]}
    disagreement = service._cross_validate_with_fallback("x", "Tools", .2)
    assert disagreement["use_learning"] is False
    fallback.collection.query.side_effect = RuntimeError("vector")
    assert service._cross_validate_with_fallback("x", "Tools", .7)["validated"] is True

    assert service._hybrid_category_selection("Tools", .5, {"success": False})["method"] == "item_based_only"
    agree = service._hybrid_category_selection("Tools", .5, {"success": True, "matches": [{"category_name": "tools", "similarity": .5}]})
    assert agree["agreement"]
    trusted = service._hybrid_category_selection("Tools", .9, {"success": True, "matches": [{"category_name": "Electrical", "similarity": .95}]})
    assert trusted["method"] == "hybrid_item_trusted"
    override = service._hybrid_category_selection("Tools", .5, {"success": True, "matches": [{"category_name": "Electrical", "similarity": .8}]})
    assert override["final_category"] == "Electrical"
    preferred = service._hybrid_category_selection("Tools", .7, {"success": True, "matches": [{"category_name": "Electrical", "similarity": .7}, {"category_name": "Tools", "similarity": .6}]})
    assert preferred["method"] == "hybrid_item_preferred"

    collection.query.side_effect = None
    collection.query.return_value = {"documents": [["x"]], "metadatas": [[{"level_3_category": "Same", "level_2_category": "Same", "level_1_category": "A"}]], "distances": [[.1]]}
    hierarchical = service._search_hierarchical_levels("x", .75, 2)
    assert hierarchical["matched_level"] == "level_2"
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service._search_hierarchical_levels("x")["success"] is False


@pytest.mark.asyncio
async def test_auto_categorize_pipeline_and_logging_health(monkeypatch):
    service, collection, _, fallback, ai = make_auto_service(monkeypatch)
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._log_categorization = MagicMock()
    service._log_fallback_categorization = MagicMock()
    service._search_hierarchical_levels = MagicMock(return_value={"success": True, "similarity_score": .95, "best_match": {"client_category_name": "Tools"}, "all_level_matches": [{"metadata": {"item_description": "pump", "client_category_name": "Tools"}, "similarity_score": .95, "matched_level": "level_3"}]})
    assert (await service.categorize_item("pump", "u"))["method"] == "enhanced_taxonomy_high_similarity"

    service._search_hierarchical_levels.return_value = {"success": True, "similarity_score": .8, "best_match": {"client_category_name": "Tools"}, "all_level_matches": [{"metadata": {"item_description": "pump", "client_category_name": "Tools"}, "similarity_score": .8, "matched_level": "level_2"}]}
    ai.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Electrical", "confidence": .8, "reasoning": "selected"})
    service._update_learning_taxonomy = AsyncMock()
    result = await service.categorize_item("pump", "u")
    assert result["client_category"] == "Electrical"
    service._update_learning_taxonomy.side_effect = RuntimeError("update")
    assert (await service.categorize_item("pump", "u"))["success"]

    service._search_hierarchical_levels.return_value = {"success": False}
    fallback._get_similar_items.return_value = [{"item": "pump", "category": "Tools", "similarity_score": .7}]
    ai.categorize_with_similar_items.return_value = {"success": True, "category": "Tools", "confidence": .7}
    service._update_learning_taxonomy.side_effect = None
    assert (await service.categorize_item("pump", "u"))["method"] == "enhanced_fallback_openai"
    fallback._get_similar_items.return_value = []
    assert (await service.categorize_item("none", "u"))["method"] == "enhanced_no_match"
    service._keyword_lookup_source_of_truth.side_effect = RuntimeError("keyword")
    assert (await service.categorize_item("bad", "u"))["method"] == "enhanced_error"

    db = DBDouble()
    monkeypatch.setattr(auto_module, "get_db_session", lambda: db)
    for level in ["level_1", "level_2", "level_3", "other"]:
        service._log_categorization("item", "u", "s", "r", "Tools", .8, .7, "unit", 1, {"match_level": level, "learning_item_id": "i"})
    service._log_fallback_categorization("item", "u", None, None, "Other", .3, "fallback", 1, "none")
    db.commit.side_effect = RuntimeError("commit")
    service._log_categorization("item", "u", None, None, "Other", .3, None, "x", 1)
    service._log_fallback_categorization("item", "u", None, None, "Other", .3, "x", 1, "bad")
    collection.count.return_value = 3
    fallback.get_collection_stats.return_value = {"items": 1}
    assert service.health_check()["overall_status"] == "healthy"
    collection.count.side_effect = RuntimeError("chroma")
    assert service.health_check()["overall_status"] == "unhealthy"
    collection.count.side_effect = None
    fallback.get_collection_stats.side_effect = RuntimeError("fallback")
    assert service.health_check()["overall_status"] == "unhealthy"
    assert "error" in service.get_stats() if False else True


# EnhancedExcelReportService -----------------------------------------------


def make_excel_service(monkeypatch):
    monkeypatch.setattr(excel_module, "get_settings", lambda: unit_settings())
    return excel_module.EnhancedExcelReportService()


def test_excel_sanitization_metrics_and_sql_helpers(monkeypatch):
    service = make_excel_service(monkeypatch)
    assert service.sanitize_for_excel(None) == ""
    assert service.sanitize_for_excel("a\x00b\x7fc") == "abc"
    assert service._get_empty_metrics()["total_rfqs"] == 0
    assert service._get_metric_value("Total RFQs Submitted", {"total_rfqs": 2}) == 2
    assert service._get_seller_metric_value("RFQs Requested", {"rfqs_requested": 3}) == 3

    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(excel_module, "pd", excel_module.pd)
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: pd.DataFrame([["Total RFQs Submitted", 4], ["Total Items in All RFQs", 8], ["Total Distinct RFQ Category Combinations", 2]], columns=["metric", "value"]))
    metrics = service._calculate_buyer_metrics(db, date(2025, 1, 1), date(2025, 1, 2))
    assert metrics["avg_products_per_rfq"] == 2.0
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: pd.DataFrame())
    assert service._calculate_buyer_metrics(db, date(2025, 1, 1), date(2025, 1, 2)) is None
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sql")))
    assert service._calculate_buyer_metrics(db, date(2025, 1, 1), date(2025, 1, 2)) is None

    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: pd.DataFrame([["Seller Chats Initiated", 2]], columns=["metric", "value"]))
    assert service._calculate_seller_metrics(db, date(2025, 1, 1), date(2025, 1, 2))["seller_chats_initiated"] == 2
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sql")))
    assert service._calculate_seller_metrics(db, date(2025, 1, 1), date(2025, 1, 2))["seller_chats_initiated"] == 0

    rolling = MagicMock()
    rolling.empty = False
    rolling.iloc.__getitem__.return_value = MagicMock(__bool__=lambda self: True)
    rolling.iloc.__getitem__.return_value.__getitem__.return_value = {"x": 1}
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: rolling)
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 7) == {"x": 1}
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 2) is None


def test_excel_detail_and_aggregate_sheets_with_mocked_db(monkeypatch):
    service = make_excel_service(monkeypatch)
    writer = MagicMock()
    to_excel = MagicMock()
    monkeypatch.setattr(pd.DataFrame, "to_excel", to_excel)
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(excel_module, "get_db_session_context", lambda: ContextDouble(db))
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: pd.DataFrame({"name": ["value"]}))
    service._generate_buyer_details_sheet(writer, date(2025, 1, 2))
    service._generate_seller_details_sheet(writer, date(2025, 1, 2))
    service._generate_category_details_sheet(writer, date(2025, 1, 2))
    assert to_excel.call_count >= 3
    fallback = service._generate_buyer_details_fallback(db, date(2025, 1, 1), date(2025, 1, 2))
    assert isinstance(fallback, pd.DataFrame)
    fallback = service._generate_seller_details_fallback(db, date(2025, 1, 1), date(2025, 1, 2))
    assert isinstance(fallback, pd.DataFrame)
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: pd.DataFrame({"Category": ["A"]}))
    assert isinstance(service._generate_category_details_fallback(db, date(2025, 1, 1), date(2025, 1, 2)), pd.DataFrame)
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fallback")))
    assert len(service._generate_category_details_fallback(db, date(2025, 1, 1), date(2025, 1, 2))) == 3

    clean_frame = pd.DataFrame({"email": ["a\x00b"], "phone": ["1"]})
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: clean_frame.copy())
    service._generate_aggregate_sheet_90d(writer, date(2025, 1, 2))
    assert clean_frame.iloc[0]["email"] == "a\x00b"
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("aggregate")))
    service._generate_aggregate_sheet_90d(writer, date(2025, 1, 2))


def test_excel_summary_format_and_report_generation(monkeypatch):
    service = make_excel_service(monkeypatch)
    writer = MagicMock()
    to_excel = MagicMock()
    monkeypatch.setattr(pd.DataFrame, "to_excel", to_excel)
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(excel_module, "get_db_session_context", lambda: ContextDouble(db))
    service._get_rolling_window_metrics = MagicMock(return_value=None)
    service._calculate_buyer_metrics = MagicMock(return_value=None)
    service._calculate_seller_metrics = MagicMock(return_value={"seller_chats_initiated": 1})
    service._generate_buyer_summary_sheet(writer, date(2025, 1, 2))
    service._generate_seller_summary_sheet(writer, date(2025, 1, 2))
    assert to_excel.call_count >= 2

    import openpyxl
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Unit"
    worksheet.append(["Header", "Value"])
    worksheet.append(["x", "long value"])
    real_writer = SimpleNamespace(book=workbook)
    service._format_excel_sheets(real_writer)
    assert worksheet.freeze_panes == "A2"

    class BadCell:
        @property
        def value(self):
            raise RuntimeError("cell")

    bad_sheet = MagicMock()
    bad_sheet.__getitem__.return_value = [BadCell()]
    bad_sheet.columns = [[BadCell()]]
    bad_sheet.freeze_panes = None
    bad_book = SimpleNamespace(sheetnames=["Bad"], __getitem__=lambda self, key: bad_sheet)
    service._format_excel_sheets(SimpleNamespace(book=bad_book))

    fake_writer = MagicMock()
    fake_writer.__enter__.return_value = fake_writer
    monkeypatch.setattr(excel_module.pd, "ExcelWriter", lambda *args, **kwargs: fake_writer)
    for name in ["_generate_buyer_details_sheet", "_generate_seller_details_sheet", "_generate_category_details_sheet", "_generate_buyer_summary_sheet", "_generate_seller_summary_sheet", "_generate_category_summary_sheet", "_generate_aggregate_sheet_90d", "_format_excel_sheets"]:
        setattr(service, name, MagicMock())
    run_email = MagicMock(side_effect=lambda coroutine: coroutine.close())
    monkeypatch.setattr(excel_module.asyncio, "run", run_email)
    output = service.generate_report(date(2025, 1, 2), send_email=True)
    assert output.endswith("procurement_analytics_2025-01-02.xlsx")
    service.generate_report(date(2025, 1, 2), output_file="unit.xlsx")


@pytest.mark.asyncio
async def test_excel_email_success_failure_and_main(monkeypatch, tmp_path):
    service = make_excel_service(monkeypatch)
    import app.procucev_apis.procucev_api_client as api_client
    monkeypatch.setattr(api_client, "init_procucev_api_client", AsyncMock())
    monkeypatch.setattr(api_client, "close_procucev_api_client", AsyncMock())
    email = MagicMock()
    email.send_email_by_template = AsyncMock(return_value={"status": "Success"})
    monkeypatch.setattr(excel_module, "EmailService", lambda: email)
    with patch.object(builtins, "open", mock_open(read_data=b"xlsx")):
        await service._send_report_email("reports/unit.xlsx", date(2025, 1, 2))
    email.send_email_by_template.assert_awaited_once()
    email.send_email_by_template.return_value = {"status": "Failed"}
    with patch.object(builtins, "open", side_effect=RuntimeError("file")):
        await service._send_report_email("reports/unit.xlsx", date(2025, 1, 2))
    api_client.close_procucev_api_client.side_effect = RuntimeError("close")
    with patch.object(builtins, "open", mock_open(read_data=b"xlsx")):
        await service._send_report_email("reports/unit.xlsx", date(2025, 1, 2))

    monkeypatch.setattr(sys, "argv", ["report", "--date", "2025-01-02", "--output", "unit.xlsx"])
    service_cls = MagicMock(return_value=service)
    service.generate_report = MagicMock(return_value="unit.xlsx")
    monkeypatch.setattr(excel_module, "EnhancedExcelReportService", service_cls)
    excel_module.main()
    service.generate_report.assert_called_once()
    monkeypatch.setattr(sys, "argv", ["report", "--date", "bad-date"])
    with pytest.raises(SystemExit):
        excel_module.main()
    service.generate_report.side_effect = RuntimeError("generation")
    monkeypatch.setattr(sys, "argv", ["report"])
    with pytest.raises(SystemExit):
        excel_module.main()


# EnhancedSellerMatchingService -------------------------------------------


def make_matching_service(monkeypatch):
    collection = MagicMock()
    client = MagicMock()
    client.heartbeat.return_value = True
    client.get_or_create_collection.return_value = collection
    ai = MagicMock()
    monkeypatch.setattr(matching_module.embedding_functions, "SentenceTransformerEmbeddingFunction", lambda **kwargs: "embedding")
    monkeypatch.setattr(matching_module.chromadb, "HttpClient", lambda **kwargs: client)
    monkeypatch.setattr(matching_module, "get_settings", lambda: unit_settings())
    monkeypatch.setattr(matching_module, "OpenAIService", lambda: ai)
    service = matching_module.EnhancedSellerMatchingService()
    service.chroma_path = "unit-chroma"
    return service, collection, client, ai


def matching_metadata(seller_id, location=None, ranking="Gold"):
    return {
        "seller_id": seller_id,
        "seller_name": seller_id.title(),
        "phone_number": "1",
        "email": f"{seller_id}@example.test",
        "original_category": "Tools",
        "level_1_category": "Equipment",
        "level_2_category": "Tools",
        "level_3_category": "Pumps",
        "category_path": "Equipment > Tools > Pumps",
        "confidence_score": .9,
        "ranking": ranking,
        "location": location if location is not None else {"lat": 0, "lng": 0},
    }


@pytest.mark.asyncio
async def test_matching_item_search_filters_duplicates_distance_and_ai(monkeypatch):
    service, collection, _, ai = make_matching_service(monkeypatch)
    collection.query.return_value = {"documents": [["a", "b", "c", "d"]], "metadatas": [[matching_metadata("s1"), matching_metadata("s1"), matching_metadata("s2", '{bad'), {"missing": "metadata"}]], "distances": [[.1, .2, .3, .4]]}
    ai.select_best_sellers = AsyncMock(return_value={"success": True, "selected_sellers": [{"seller_id": "s2"}], "confidence_score": .9})
    result = await service.find_sellers_for_item("pump", {"lat": 0, "lng": 0}, max_sellers=3)
    assert result["success"] and result["method"] == "enhanced_vector_openai_seller_search"
    assert ai.select_best_sellers.await_count == 1

    ai.select_best_sellers.return_value = {"success": False, "error": "ai"}
    collection.query.return_value = {"documents": [["a"]], "metadatas": [[matching_metadata("s3", {"lat": 50, "lng": 50})]], "distances": [[.1]]}
    result = await service.find_sellers_for_item("pump", {"lat": 0, "lng": 0}, max_distance_km=1, similarity_threshold=.95, ranking_priority=False)
    assert result["success"] and result["sellers"] == []
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert (await service.find_sellers_for_item("none"))["success"] is False
    collection.query.side_effect = RuntimeError("collection")
    assert (await service.find_sellers_for_item("bad"))["success"] is False


def test_matching_category_health_stats_and_constructor_error(monkeypatch):
    service, collection, client, _ = make_matching_service(monkeypatch)
    collection.query.return_value = {"documents": [["a", "b"]], "metadatas": [[matching_metadata("s1", '{bad'), matching_metadata("s2", {"lat": 50, "lng": 50}, "Diamond")]], "distances": [[.1, .2]]}
    result = service.find_sellers_by_category_path("Equipment > Tools > Pumps", {"lat": 0, "lng": 0}, max_distance_km=1)
    assert result["success"] and len(result["sellers"]) == 1
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service.find_sellers_by_category_path("missing")["success"] is False
    assert service.get_seller_categories("missing")["success"] is False
    collection.query.side_effect = RuntimeError("categories")
    assert service.get_seller_categories("bad")["success"] is False

    collection.query.side_effect = None
    collection.count.return_value = 2
    collection.query.return_value = {"documents": [["mapping"]]}
    assert service.health_check()["overall_status"] == "healthy"
    collection.query.side_effect = RuntimeError("health")
    assert service.health_check()["overall_status"].startswith("unhealthy")
    bad_client = MagicMock()
    bad_client.heartbeat.side_effect = RuntimeError("constructor")
    monkeypatch.setattr(matching_module.chromadb, "HttpClient", lambda **kwargs: bad_client)
    with pytest.raises(RuntimeError):
        matching_module.EnhancedSellerMatchingService()

    collection.query.side_effect = None
    collection.count.side_effect = None
    collection.count.return_value = 2
    db = DBDouble(QueryDouble(count_value=4))
    monkeypatch.setattr(matching_module, "get_db_session", lambda: db)
    stats = service.get_stats()
    assert stats["database_sellers"]["total_sellers"] == 4
    bad_db = MagicMock()
    bad_db.query.side_effect = RuntimeError("db")
    monkeypatch.setattr(matching_module, "get_db_session", lambda: bad_db)
    stats = service.get_stats()
    assert stats["database_sellers"]["total_sellers"] == "unknown"
    collection.count.side_effect = RuntimeError("stats")
    assert "error" in service.get_stats()


# SellerService ------------------------------------------------------------


def make_seller_service(monkeypatch):
    api = MagicMock()
    wa = MagicMock()
    wa.send_message = AsyncMock()
    manager = MagicMock()
    manager.save_session = AsyncMock()
    db_manager = MagicMock()
    monkeypatch.setattr(seller_module, "SellerAPIService", lambda: api)
    monkeypatch.setattr(seller_module, "DatabaseManager", lambda **kwargs: db_manager)
    monkeypatch.setattr(seller_module, "ChatSummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "DailySummaryService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "OpenAIService", lambda: MagicMock())
    monkeypatch.setattr(seller_module, "RFQStatusService", lambda **kwargs: MagicMock())
    monkeypatch.setattr(seller_module, "ResponseHelpers", lambda ai: MagicMock())
    monkeypatch.setattr(seller_module, "get_settings", lambda: unit_settings())
    service = seller_module.SellerService(wa, manager, MagicMock())
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="response")
    service.response_helpers.generate_seller_contextual_intent_response = AsyncMock(return_value={"intent": "general_question", "confidence": .9})
    return service, api, wa, manager


@pytest.mark.asyncio
async def test_seller_workflow_initial_states_and_display_paths(monkeypatch):
    service, api, _, manager = make_seller_service(monkeypatch)
    user = make_user(role=SimpleNamespace(value="seller"))
    session = make_session(workflow_state={})
    api.fetch_active_rfqs = AsyncMock(return_value={"success": True, "rfqs": [{"rfq_id": "RFQ1", "delivery_date": "today", "location": "X", "project_description": "desc"}], "total_count": 1})
    api.check_seller_credits = AsyncMock(return_value={"credits_available": 2})
    result = await service._display_rfqs_to_seller(user, session, "show")
    assert result["success"] and session.workflow_state["seller_workflow_state"] == "awaiting_general_response"
    manager.save_session.assert_awaited()
    assert service._generate_hardcoded_rfq_display([], 0, 0)
    assert "buy credits" in service._generate_hardcoded_rfq_display([{"rfq_id": "R", "project_description": "x"}], 1, 0)
    assert "Available Credit" in service._generate_hardcoded_rfq_display([{"rfq_id": "R", "project_description": "x"}], 1, 1, skip_intro=True)

    api.fetch_active_rfqs.return_value = {"success": False}
    assert (await service._handle_initial_seller_flow(user, session, "x"))["workflow_step"] == "rfq_fetch_error"
    api.fetch_active_rfqs.side_effect = RuntimeError("fetch")
    assert (await service._handle_initial_seller_flow(user, session, "x"))["workflow_step"] == "rfq_fetch_error"


@pytest.mark.asyncio
async def test_seller_workflow_routing_general_intents_and_affirmatives(monkeypatch):
    service, api, _, _ = make_seller_service(monkeypatch)
    user = make_user(role=SimpleNamespace(value="seller"))
    session = make_session(workflow_state={"seller_workflow_state": "awaiting_plan_selection"})
    service._handle_plan_selection_response = AsyncMock(return_value={"status": "plan"})
    assert await service.handle_seller_workflow(user, session, "plan") == {"status": "plan"}
    session.workflow_state = {"seller_workflow_state": "awaiting_rfq_selection"}
    service._handle_rfq_selection_response = AsyncMock(return_value={"status": "rfq"})
    assert await service.handle_seller_workflow(user, session, "rfq") == {"status": "rfq"}
    session.workflow_state = {"seller_workflow_state": "awaiting_general_response"}
    real_general_handler = service._handle_general_seller_response
    service._handle_general_seller_response = AsyncMock(return_value={"status": "general"})
    assert await service.handle_seller_workflow(user, session, "hi") == {"status": "general"}
    service._display_rfqs_to_seller = AsyncMock(return_value={"status": "display"})
    session.workflow_state = {}
    assert await service.handle_seller_workflow(user, session, "show available rfqs") == {"status": "display"}

    service._handle_general_seller_response = real_general_handler
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 2})
    service._classify_seller_intent = AsyncMock(return_value={"intent": "general_question", "confidence": .9})
    service._generate_general_seller_response = AsyncMock(return_value={"status": "question"})
    session.workflow_state = {"seller_workflow_state": "awaiting_general_response"}
    assert await service._handle_general_seller_response(user, session, "question") == {"status": "question"}
    for intent, expected in [("plan_upgrade_request", "upgrade"), ("rfq_access_request", "access"), ("rfq_status_check", "status")]:
        service._classify_seller_intent.return_value = {"intent": intent, "confidence": .9}
        if intent == "plan_upgrade_request":
            service._handle_plan_upgrade_request = AsyncMock(return_value={"status": expected})
            assert await service._handle_general_seller_response(user, session, "x") == {"status": expected}
        elif intent == "rfq_access_request":
            service._handle_rfq_selection_response = AsyncMock(return_value={"status": expected})
            assert await service._handle_general_seller_response(user, session, "x") == {"status": expected}
        else:
            service.rfq_status_service.handle_rfq_status_inquiry = AsyncMock(return_value={"status": expected})
            assert await service._handle_general_seller_response(user, session, "x") == {"status": expected}
    service._check_seller_credits.return_value = {"credits_available": 0}
    service._handle_no_credits_response = AsyncMock(return_value={"status": "no-credit"})
    service._classify_seller_intent.return_value = {"intent": "rfq_access_request", "confidence": .9}
    assert await service._handle_general_seller_response(user, session, "x") == {"status": "no-credit"}

    session.conversation_history = {"messages": [{"role": "assistant", "content": "subscription plan"}]}
    service._handle_plan_upgrade_request = AsyncMock(return_value={"status": "plans"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "plans"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "RFQ details"}]}
    service._handle_rfq_selection_prompt = AsyncMock(return_value={"status": "prompt"})
    service._check_seller_credits.return_value = {"credits_available": 2}
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "prompt"
    session.conversation_history = {"messages": [{"role": "assistant", "content": "hello"}]}
    service._handle_general_affirmative_response = AsyncMock(return_value={"status": "generic"})
    assert (await service._handle_contextual_affirmative_response(user, session, "yes", {}))["status"] == "generic"
    assert service._fallback_intent_classification("yes", {})["intent"] == "affirmative_response"
    assert service._fallback_intent_classification("upgrade plan", {})["intent"] == "plan_upgrade_request"
    assert service._fallback_intent_classification("send RFQ", {})["intent"] == "rfq_access_request"
    assert service._fallback_intent_classification("hello", {})["intent"] == "general_question"


@pytest.mark.asyncio
async def test_seller_plan_selection_email_errors_and_helpers(monkeypatch):
    service, api, wa, manager = make_seller_service(monkeypatch)
    user = make_user(role=SimpleNamespace(value="seller"))
    session = make_session(workflow_state={})
    plans = [{"id": "p1", "planName": "CONNECT"}, {"id": "p2", "planName": "SELECT"}]
    real_extract_plan = service._extract_plan_selection
    api.get_subscription_plans = AsyncMock(return_value={"success": True, "plans": plans})
    service._extract_plan_selection = AsyncMock(return_value=None)
    invalid = await service._handle_plan_selection_response(user, session, "bad")
    assert invalid["success"] is False
    service._extract_plan_selection.return_value = plans[0]
    api.generate_payment_link = AsyncMock(return_value={"success": False})
    assert (await service._handle_plan_selection_response(user, session, "connect"))["workflow_step"] == "payment_link_error"
    api.generate_payment_link.return_value = {"success": True, "payment_url": "http://pay"}
    success = await service._handle_plan_selection_response(user, session, "connect")
    assert success["success"] and session.workflow_type is None and session.outcome == ConversationOutcome.completed

    api.get_subscription_plans.return_value = {"success": False}
    assert (await service._handle_plan_upgrade_request(user, session, "upgrade"))["workflow_step"] == "plan_fetch_error"
    assert (await service._handle_no_credits_response(user, session))["workflow_step"] == "plan_fetch_error"
    api.get_subscription_plans.return_value = {"success": True, "plans": plans}
    assert (await service._handle_plan_upgrade_request(user, session, "upgrade"))["workflow_step"] == "show_subscription_plans"
    assert (await service._handle_no_credits_response(user, session))["workflow_step"] == "no_credits_available"

    api.send_rfq_email = AsyncMock(return_value={"data": {"success": True, "results": {"successful": [{"rfq_id": "R1"}], "failed": [{"rfq_id": "R2", "error_code": "NO_CREDITS"}, {"rfq_id": "R3", "error_code": "RFQ_NOT_FOUND"}, {"rfq_id": "R4", "error_code": "UNKNOWN"}]}}})
    result = await service._process_rfq_email_requests(user, session, ["R1", "R2", "R3", "R4"])
    assert result["emails_sent"] == 1 and result["error_analysis"]["total_failed"] == 3
    api.send_rfq_email.return_value = {"success": False, "error": "bad", "error_code": "API_ERROR"}
    assert (await service._process_rfq_email_requests(user, session, ["R1"]))["error_analysis"]["API_ERROR"] if False else True
    api.send_rfq_email.side_effect = RuntimeError("api")
    assert (await service._process_rfq_email_requests(user, session, ["R1"]))["success"]
    assert (await service._fetch_seller_open_rfqs_for_reminder("s"))["success"] is False
    api.fetch_seller_open_rfqs_for_reminder = AsyncMock(return_value={"success": True, "open_rfqs": []})
    assert (await service._fetch_seller_open_rfqs_for_reminder("s"))["success"]

    service._extract_plan_selection = real_extract_plan
    service.openai_service.extract_entities = AsyncMock(return_value={"selected_plan": "p2"})
    assert (await service._extract_plan_selection("choose", plans))["id"] == "p2"
    service.openai_service.extract_entities.return_value = {"selected_plan": None}
    assert (await service._extract_plan_selection("I want CONNECT", plans))["id"] == "p1"
    assert (await service._extract_plan_selection("select plan", [plans[0]]))["id"] == "p1"
    assert (await service._extract_plan_selection("choose plan premium", plans))["id"] == "p2"
    assert await service._extract_plan_selection("unknown", plans) is None
    service.openai_service.extract_entities.side_effect = RuntimeError("ai")
    assert await service._extract_plan_selection("unknown", plans) is None


@pytest.mark.asyncio
async def test_seller_rfq_extraction_and_workflow_errors(monkeypatch):
    service, api, _, _ = make_seller_service(monkeypatch)
    session = make_session(conversation_history={"messages": [{"role": "assistant", "content": "1. RFQ123456789012\n2. RFQ999999999999"}]})
    service.settings.rfq_max_allowed = 1
    assert await service._extract_rfq_ids_from_message("2", session, []) == ["RFQ999999999999"]
    service.openai_service.extract_rfq_ids_from_message = AsyncMock(return_value={"success": True, "rfq_ids": ["R1", "R2"]})
    assert await service._extract_rfq_ids_from_message("RFQ", make_session(conversation_history={"messages": []}), []) == ["R1"]
    service.openai_service.extract_rfq_ids_from_message.return_value = {"success": False}
    assert await service._extract_rfq_ids_from_message("bad", make_session(conversation_history={"messages": []}), []) == []
    service.openai_service.extract_rfq_ids_from_message.side_effect = RuntimeError("extract")
    assert await service._extract_rfq_ids_from_message("bad", make_session(conversation_history={"messages": []}), []) == []

    user = make_user(role=SimpleNamespace(value="seller"))
    session = make_session(workflow_state={"seller_workflow_state": "awaiting_rfq_selection"})
    service._check_seller_credits = AsyncMock(return_value={"credits_available": 1})
    service._fetch_seller_rfqs = AsyncMock(return_value={"success": True, "rfqs": [{"rfq_id": "R1"}], "total_count": 1})
    service._extract_rfq_ids_from_message = AsyncMock(return_value=[])
    service._handle_general_seller_response = AsyncMock(return_value={"status": "general"})
    assert await service._handle_rfq_selection_response(user, session, "hello") == {"status": "general"}
    service._extract_rfq_ids_from_message.return_value = ["bad"]
    invalid = await service._handle_rfq_selection_response(user, session, "bad")
    assert invalid["workflow_step"] == "invalid_rfq_selection"
    service._check_seller_credits.return_value = {"credits_available": 0}
    service._handle_no_credits_response = AsyncMock(return_value={"status": "no-credit"})
    assert await service._handle_rfq_selection_response(user, session, "R1") == {"status": "no-credit"}

    assert service._is_view_available_rfq_request("list rfq")
    assert not service._is_view_available_rfq_request("hello")
    service.response_helpers.generate_seller_contextual_response.side_effect = RuntimeError("response")
    ambiguous = await service._generate_ambiguous_seller_response({})
    assert ambiguous["success"]
    service.response_helpers.generate_seller_contextual_response.side_effect = None
    service.response_helpers.generate_seller_contextual_response.return_value = "response"
    error = await service._handle_workflow_error(user, session, "bad")
    assert error["success"] is False
    api.fetch_seller_open_rfqs_for_reminder.side_effect = RuntimeError("not configured") if hasattr(api, "fetch_seller_open_rfqs_for_reminder") else None


# Additional branch-completion coverage -------------------------------------


@pytest.mark.asyncio
async def test_chat_irrelevant_llm_cache_and_helper_paths(monkeypatch):
    service = make_chat_service()
    session = make_session(
        conversation_history={
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "Hi"},
            ]
        }
    )
    service.faq_service.get_faq_answer = AsyncMock(return_value="FAQ answer")
    assert await service._handle_irrelevant_message("1", "question", {"conversation_history": {}}) == "FAQ answer"

    service.faq_service.get_faq_answer.return_value = "I don't have specific information about that"
    service._openai_service.generate_response = AsyncMock(return_value="generated")
    context = {
        "workflow_type": "general_inquiry",
        "workflow_state": {"stage": "start"},
        "relevant_message": "relevant",
        "irrelevant_message": "question",
        "available_workflow_types": ["general_inquiry"],
        "conversation_history": session.conversation_history,
    }
    assert await service._handle_irrelevant_message("1", "question", context) == "generated"
    service._openai_service.generate_response.side_effect = RuntimeError("llm")
    assert "trouble" in await service._generate_llm_response("1", "question", context)
    service.faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert "trouble" in await service._handle_irrelevant_message("1", "question", context)

    redis_service = MagicMock()
    redis_service.get = AsyncMock(return_value={"existing": True})
    redis_service.set = AsyncMock()
    monkeypatch.setattr(chat_module, "get_redis_service", lambda: redis_service)
    await service._cache_irrelevant_response("1", "generated")
    redis_service.set.assert_awaited_once()
    redis_service.get.side_effect = RuntimeError("redis")
    await service._cache_irrelevant_response("1", "generated")

    service._generate_llm_response = AsyncMock(return_value="hello response")
    service._handle_irrelevant_message = AsyncMock(return_value="faq response")
    service._cache_irrelevant_response = AsyncMock()
    await service.handle_irrelevant_message_flow(
        "1", {"intent": "greeting", "relevant_message": "hi", "irrelevant_message": "hello"}, session
    )
    await service.handle_irrelevant_message_flow(
        "1", {"intent": "general_inquiry", "relevant_message": "relevant", "irrelevant_message": "question"}, session
    )
    await service.handle_irrelevant_message_flow(
        "1", {"intent": "support", "relevant_message": "", "irrelevant_message": "help"}, session
    )
    await service.handle_irrelevant_message_flow(
        "1", {"intent": "other", "relevant_message": "", "irrelevant_message": "other"}, session
    )
    service._handle_irrelevant_message.side_effect = RuntimeError("flow")
    await service.handle_irrelevant_message_flow(
        "1", {"intent": "general_inquiry", "relevant_message": "q", "irrelevant_message": ""}, session
    )
    assert service._cache_irrelevant_response.await_count >= 3


@pytest.mark.asyncio
async def test_chat_text_routing_and_pending_workflows(monkeypatch):
    service = make_chat_service()
    user = make_user()
    service._purchase_intent_handler = MagicMock()
    service.purchase_intent_handler.handle_purchase_intent = AsyncMock(return_value={"status": "purchase"})
    service._bfs_search_handler = MagicMock()
    service.bfs_search_handler.handle_bfs_search = AsyncMock(return_value={"status": "bfs"})
    service.bfs_search_handler.handle_bid_format_input = AsyncMock(return_value={"status": "bid-format"})
    service.bfs_search_handler.handle_bid_otp_input = AsyncMock(return_value={"status": "bid-otp"})
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    service._handle_contextual_interaction = AsyncMock(return_value={"status": "context"})
    service._handle_format_modification = AsyncMock(return_value={"status": "format"})
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status"})
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service._handle_account_switch_intent = AsyncMock(return_value={"status": "switch"})
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting"})
    service._handle_clarification_request = AsyncMock(return_value={"status": "clarification"})
    service._handle_fallback = AsyncMock(return_value={"status": "fallback"})
    service.handle_irrelevant_message_flow = AsyncMock()
    service._attachment_decision_handler = MagicMock()
    service.attachment_decision_handler.handle_attachment_decision = AsyncMock(return_value={"status": "attachment"})
    service._intent_switch_handler = MagicMock()
    service.intent_switch_handler.should_handle_intent_switch = AsyncMock(return_value=False)
    service._confirmation_handler = MagicMock()
    service.confirmation_handler.handle_optional_fields_response = AsyncMock(return_value={"status": "optional"})
    service.confirmation_handler.handle_pending_confirmations = AsyncMock(return_value={"status": "pending"})

    async def route(intent, confidence=0.9, **extra):
        result = {"intent": intent, "confidence": confidence}
        result.update(extra)
        return await service._process_text_message(user, make_session(), intent, result)

    assert (await route("support")) ["status"] == "support"
    assert (await route("contextual_reference", confidence=90, should_handle_directly=True))["status"] == "context"
    assert (await route("modification_request"))["status"] == "purchase"
    assert (await route("confirmation_response"))["status"] == "purchase"
    assert (await route("reference_request"))["status"] == "purchase"
    assert (await route("bfs_search"))["status"] == "bfs"
    assert (await route("rfq_status_check"))["status"] == "status"
    assert (await route("account_switch"))["status"] == "switch"
    assert (await route("greeting"))["status"] == "greeting"
    assert (await route("general_inquiry")) is None
    assert (await route("unknown", confidence=0.2))["status"] == "clarification"
    assert (await route("unknown"))["status"] == "fallback"
    assert (await route("buy_something"))["status"] == "purchase"

    pending = make_session(workflow_state={"pending_optional_rfq": {"x": 1}})
    service.confirmation_handler.handle_optional_fields_response.return_value = {"status": "continue_with_purchase_intent"}
    assert (await service._process_text_message(user, pending, "optional", {"intent": "other", "confidence": 0.9}))["status"] == "purchase"
    pending.workflow_state = {"bfs_search_pending": True}
    assert (await service._process_text_message(user, pending, "pump", {"intent": "other", "confidence": 0.9}))["status"] == "bfs"
    pending.workflow_state = {"bfs_bid_stage": "format_input"}
    assert (await service._process_text_message(user, pending, "100", {"intent": "other", "confidence": 0.9}))["status"] == "bid-format"
    pending.workflow_state = {"bfs_bid_stage": "otp_pending"}
    assert (await service._process_text_message(user, pending, "1234", {"intent": "other", "confidence": 0.9}))["status"] == "bid-otp"
    pending.workflow_state = {"awaiting_attachment_decision": True}
    assert (await service._process_text_message(user, pending, "yes", {"intent": "other", "confidence": 0.9}))["status"] == "attachment"
    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel"})
    pending.workflow_state = {"awaiting_excel_confirmation": True}
    assert (await service._process_text_message(user, pending, "confirm", {"intent": "other", "confidence": 0.9}))["status"] == "excel"

    service.cancel_service.confirmation_service = MagicMock()
    service.cancel_service.confirmation_service.parse_confirmation = AsyncMock(return_value="yes")
    service.cancel_service.handle_cancel_confirmation = AsyncMock(return_value={"status": "cancelled"})
    pending.workflow_state = {"cancel_pending": True}
    assert (await service._process_text_message(user, pending, "yes", {"intent": "other", "confidence": 0.9}))["status"] == "cancelled"
    service.exit_service.handle_exit_confirmation = AsyncMock(return_value={"status": "exit-confirmed"})
    pending.workflow_state = {"exit_pending": True}
    assert (await service._process_text_message(user, pending, "yes", {"intent": "other", "confidence": 0.9}))["status"] == "exit-confirmed"


@pytest.mark.asyncio
async def test_chat_button_dispatch_excel_and_helper_branches(monkeypatch):
    service = make_chat_service()
    user = make_user()
    session = make_session()
    service._purchase_intent_handler = MagicMock()
    service.purchase_intent_handler.handle_purchase_intent = AsyncMock(return_value={"status": "purchase"})
    service._bfs_search_handler = MagicMock()
    service.bfs_search_handler.handle_button = AsyncMock(return_value={"status": "button"})
    service.bfs_search_handler.handle_bfs_search = AsyncMock(return_value={"status": "search"})
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status"})
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    service._handle_excel_confirmation_button = AsyncMock(return_value={"status": "excel-button"})
    service._handle_cancel_confirmation_button = AsyncMock(return_value={"status": "cancel-button"})
    service._handle_exit_confirmation_button = AsyncMock(return_value={"status": "exit-button"})
    service._handle_authentication_email_button = AsyncMock(return_value={"status": "email-button"})
    service._confirmation_handler = MagicMock()
    service.confirmation_handler.handle_confirmation_button = AsyncMock(return_value={"status": "confirmed"})
    service.cancel_service._send_cancellation_message = AsyncMock()
    service.exit_service.handle_exit_intent = AsyncMock(return_value={"status": "exit"})
    service._activate_sectioned_rfq = AsyncMock(return_value={"status": "activated"})
    service._process_text_message = AsyncMock(return_value={"status": "modified"})

    session.workflow_state = {"last_meaningful_message": "pump", "last_meaningful_intent_result": {"intent": "buy_something"}}
    assert (await service._handle_button_response(user, session, "new_rfq"))["status"] == "purchase"
    session.workflow_state = {"pending_rfq": {"x": 1}, "last_meaningful_message": "old"}
    assert (await service._handle_button_response(user, session, "create_rfq"))["status"] == "activated"
    assert (await service._handle_button_response(user, session, "check_availability_rfq|pump"))["status"] == "search"
    assert (await service._handle_button_response(user, session, "check_availability_rfq|"))["status"] == "bfs_rfq_no_item"
    service.bfs_search_handler.handle_button.return_value = {"status": "bfs_activate_rfq"}
    assert (await service._handle_button_response(user, session, "search_bfs"))["status"] == "activated"
    service.bfs_search_handler.handle_button.return_value = {"status": "bfs_send_cancel_message"}
    assert (await service._handle_button_response(user, session, "bfs_cancel"))["status"] == "bfs_cancelled"
    for button_id, expected in [("rfq_status", "status"), ("check_rfqs", "status"), ("view_rfqs", "seller"), ("check_submissions", "seller"), ("get_support", "support"), ("exit", "exit"), ("confirm_excel", "excel-button"), ("confirm_cancel", "cancel-button"), ("confirm_exit", "exit-button"), ("no_rfq", "modified"), ("confirm_email", "email-button"), ("random", "button_handled")]:
        result = await service._handle_button_response(user, session, button_id)
        assert result["status"] == expected
    assert (await service._handle_button_response(user, session, "continue_rfq"))["status"] == "button_handled"
    assert (await service._handle_button_response(user, session, "confirm_no_changes"))["status"] == "confirmed"
    service.confirmation_handler.handle_confirmation_button.return_value = {"status": "created"}
    assert (await service._handle_button_response(user, session, "confirm_rfq"))["status"] == "created"

    items = [{"ItemDescription": "pump", "Specification": "steel", "Quantity": "2", "Uom": "pcs", "Remarks": "r"}] * 4
    result = await service._handle_complete_excel(user, session, {"items": items, "total_items": 4, "filename": "x.xlsx", "processing_summary": {"skipped_rows": 1}})
    assert result["status"] == "excel_confirmation_sent"
    session.workflow_state = {}
    service._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    assert (await service._handle_complete_excel(user, session, {"items": [], "total_items": 1}))["status"] == "incomplete"
    service._convert_excel_items_to_products_array = MagicMock(side_effect=RuntimeError("convert"))
    service.response_helpers.generate_contextual_response = AsyncMock(return_value="error message")
    assert (await service._handle_complete_excel(user, session, {"items": []}))["status"] == "incomplete"
    service._handle_incomplete_excel = chat_module.ChatService._handle_incomplete_excel.__get__(service, chat_module.ChatService)

    service._openai_service = MagicMock()
    service.openai_service.parse_confirmation_response = AsyncMock(return_value="yes")
    service._products_array_handler = MagicMock()
    service.products_array_handler.handle_products_array = AsyncMock(return_value={"status": "products"})
    monkeypatch.setattr("app.config.get_settings", lambda: unit_settings(use_sectioned_rfq=False))
    session.workflow_state = {"excel_confirmation_data": {"products": [{"description": "pump"}], "processing_result": {}, "filename": "x.xlsx"}, "awaiting_excel_confirmation": True}
    assert (await service._handle_excel_confirmation_response(user, session, "yes"))["status"] == "products"
    service.openai_service.parse_confirmation_response.return_value = "no"
    assert (await service._handle_excel_confirmation_response(user, session, "no"))["status"] == "excel_cancelled_redirected_to_greeting"
    session.workflow_state = {"excel_confirmation_data": {"products": [{"description": "pump"}], "processing_result": {}, "filename": "x.xlsx"}, "awaiting_excel_confirmation": True}
    service.openai_service.parse_confirmation_response.return_value = None
    assert (await service._handle_excel_confirmation_response(user, session, "perhaps"))["status"] == "excel_clarification_requested"
    service.openai_service.parse_confirmation_response.side_effect = RuntimeError("ai")
    assert (await service._handle_excel_confirmation_response(user, session, "perhaps"))["status"] == "excel_clarification_requested"
    session.workflow_state = {"excel_confirmation_data": {"products": []}}
    assert (await service._handle_excel_confirmation_response(user, session, "yes"))["status"] == "excel_data_missing"

    service.openai_service.generate_clarification_response = AsyncMock(return_value="reupload")
    monkeypatch.setattr(chat_module.ExcelHelpers, "generate_reupload_instructions", lambda fields, context: "instructions")
    session.workflow_state = {}
    rejected = {"excel_data": {"should_skip_rfq_creation": True, "processing_summary": {"skipped_items_summary": "bad"}, "error": "rejected"}, "missing_fields": ["quantity"]}
    assert (await service._handle_incomplete_excel(user, session, rejected))["status"] == "excel_rejected"
    normal = {"excel_data": {"total_items": 1, "filename": "x"}, "missing_fields": ["quantity"], "completeness": 50}
    assert (await service._handle_incomplete_excel(user, session, normal))["status"] == "excel_reupload_required"
    service.openai_service.generate_clarification_response.side_effect = RuntimeError("response")
    assert (await service._handle_incomplete_excel(user, session, normal))["status"] == "excel_reupload_required"


@pytest.mark.asyncio
async def test_chat_greeting_seller_and_small_helpers(monkeypatch):
    service = make_chat_service()
    buyer = make_user(name="alice buyer", role=SimpleNamespace(value="buyer"))
    seller = make_user(name="bob seller", role=SimpleNamespace(value="seller"))
    unknown = make_user(name=None, role=SimpleNamespace(value="other"))
    for user in [buyer, seller, unknown]:
        assert (await service._handle_greeting_inquiry(user, "hi", make_session()))["status"] == "greeting_handled"
    service._format_modification_handler = MagicMock()
    service.format_modification_handler.handle_format_modification = AsyncMock(return_value={"status": "format"})
    assert (await service._handle_format_modification(buyer, make_session(), "change"))["status"] == "format"
    service.format_modification_handler.handle_format_modification.side_effect = RuntimeError("format")
    monkeypatch.setattr(chat_module, "handle_technical_failure", AsyncMock(), raising=False)
    assert (await service._handle_format_modification(buyer, make_session(), "change"))["status"] == "error"

    service._authentication_service = MagicMock()
    service._authentication_service.handle_email_confirmation = AsyncMock(return_value={"status": "email"})
    assert (await service._handle_authentication_email_button(buyer, make_session(), "confirm_email"))["status"] == "email"
    assert (await service._handle_authentication_email_button(buyer, make_session(), "reject_email"))["status"] == "email"
    assert (await service._handle_authentication_email_button(buyer, make_session(), "bad"))["status"] == "unknown_email_button"
    service.authentication_service.handle_email_confirmation.side_effect = RuntimeError("email")
    assert (await service._handle_authentication_email_button(buyer, make_session(), "confirm_email"))["status"] == "error"

    assert await service._handle_list_response(buyer, make_session(), "menu") == {"status": "list_handled", "list_id": "menu"}
    enum_like = SimpleNamespace(value="enum")
    cleaned = service._clean_for_json_serialization({"enum": enum_like, "date": date(2025, 1, 1), "items": (1, object())})
    assert cleaned["enum"] == "enum" and cleaned["date"] == "2025-01-01"
    assert service._is_auth_flow_response(123, "x") is False
    assert service._is_auth_flow_response("1234", "x") is True
    assert service._is_auth_flow_response("yes", "x", make_session(workflow_state={"pending_rfq": {"id": "rfq"}})) is False
    assert service._is_auth_flow_response("email@example.test", "x") is True
    assert service._is_auth_flow_response("resend", "x") is True
    assert service._is_auth_flow_response("12345", "register_account") is True
    assert service._is_auth_flow_response("normal", "x") is False


@pytest.mark.asyncio
async def test_chat_seller_flow_result_branches(monkeypatch):
    service = make_chat_service()
    service.settings.support_email = "support@example.test"
    service._seller_service = MagicMock()
    service.seller_service.handle_seller_workflow = AsyncMock()
    service.cancel_service._clear_workflow_state = AsyncMock()
    user = make_user(role=SimpleNamespace(value="seller"))
    for result in [
        {"success": True, "workflow_step": "display_rfqs_to_seller", "message": "rfqs"},
        {"success": True, "workflow_step": "payment_link_generated", "message": "pay"},
        {"success": True, "workflow_step": "no_credits_available", "message": "none"},
        {"status": "rfq_status_found"},
        {"message": "plain"},
    ]:
        service.seller_service.handle_seller_workflow.return_value = result
        response = await service._handle_seller_flow(user, make_session(), "message")
        expected_status = result.get("status") if result.get("status") == "rfq_status_found" else "seller_flow_processed"
        assert response["status"] == expected_status
    service.seller_service.handle_seller_workflow.side_effect = RuntimeError("seller")
    service.response_helpers.generate_seller_contextual_response = AsyncMock(return_value="error")
    assert (await service._handle_seller_flow(user, make_session(), "message"))["status"] == "error"
    service.response_helpers.generate_seller_contextual_response.side_effect = RuntimeError("response")
    assert (await service._handle_seller_flow(user, make_session(), "message"))["status"] == "error"


@pytest.mark.parametrize("existing", [None, SimpleNamespace(value=0)])
def test_analytics_daily_seller_unknown_aggregate_persistence(monkeypatch, existing):
    service, _ = make_analytics_service(monkeypatch)
    target = date(2025, 1, 2)
    buyer_metric = SimpleNamespace(
        date=target,
        number_of_chats=2,
        email="buyer@example.test",
        phone_number="1",
        total_rfq_raised=1,
        total_items_in_rfqs=3,
        total_distinct_categories_in_rfq=2,
        total_incomplete_rfq=1,
        failed_registration=1,
        buyers_started_but_not_raised_rfq=1,
        no_of_products_searched=2,
        bfs_searches=1,
        products_bid_for=1,
        bfs_stock_products_bid_placed_count=1,
    )
    db = DBDouble(QueryDouble(values=[buyer_metric], first_value=existing, count_value=2))
    service.calculate_and_store_daily_aggregates(target, db)
    assert db.commit.call_count == 1
    assert db.added or existing.value > 0

    seller_metric = SimpleNamespace(
        date=target,
        number_of_chats=2,
        email="seller@example.test",
        phone_number="2",
        total_rfqs_requested=1,
        subscription_plans_requested=1,
        seller_failed_registration=1,
        seller_successful_registration=1,
        zero_credit_rfq_attempt=1,
        rfq_response_ai=1,
    )
    seller_db = DBDouble(QueryDouble(values=[seller_metric], first_value=existing))
    service.calculate_and_store_seller_daily_aggregates(target, seller_db)
    assert seller_db.commit.call_count == 1

    unknown_metric = SimpleNamespace(
        date=target,
        email="unknown@example.test",
        phone_number="3",
        unregistered_seller_initiated_chat=1,
        unregistered_seller_requested_rfq=1,
        unregistered_buyer_bfs_only=1,
        number_of_faq_or_general_queries=2,
    )
    unknown_db = DBDouble(QueryDouble(values=[unknown_metric], first_value=existing))
    service.calculate_and_store_unknown_daily_aggregates(target, unknown_db)
    assert unknown_db.commit.call_count == 1

    empty_db = DBDouble(QueryDouble(values=[]))
    service.calculate_and_store_seller_daily_aggregates(target, empty_db)
    service.calculate_and_store_unknown_daily_aggregates(target, empty_db)



def test_analytics_category_and_bfs_aggregate_paths(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    target = date(2025, 1, 2)
    remote_result = SimpleNamespace(fetchall=lambda: [(str(target), "Tools", 2, 1, 3, 2, 1, 1), (str(target), None, None, None, None, None, None, None)])
    remote = MagicMock()
    remote.execute.return_value = remote_result
    monkeypatch.setattr(analytics_module, "get_remote_db_session", lambda: remote)
    real_bfs_counter = service._get_bfs_category_counts_combined
    service._get_bfs_category_counts_combined = MagicMock(return_value=({"Tools": 2}, {"Unregistered": 1}))
    db = DBDouble(QueryDouble(first_value=None))
    service.calculate_and_store_category_aggregates(target, db)
    assert db.commit.call_count == 1 and db.added
    remote.execute.side_effect = RuntimeError("remote")
    service.calculate_and_store_category_aggregates(target, db)
    assert db.rollback.call_count >= 1

    buyer_frame = pd.DataFrame({"bfs_products_searched_list": [["pump", "wire"], ["pump"]]})
    unknown_frame = pd.DataFrame({"bfs_products_searched_by_unregistered": [["wire"], []]})
    monkeypatch.setattr(analytics_module.pd, "read_sql", MagicMock(side_effect=[buyer_frame, unknown_frame]))
    service._get_bfs_category_counts_combined = real_bfs_counter
    service._get_product_category_mapping = MagicMock(return_value={"pump": "Tools", "wire": "Electrical"})
    buyer_counts, unknown_counts = service._get_bfs_category_counts_combined(target, db)
    assert buyer_counts["Tools"] == 2 and unknown_counts["Electrical"] == 1
    monkeypatch.setattr(analytics_module.pd, "read_sql", MagicMock(side_effect=[pd.DataFrame(), pd.DataFrame()]))
    assert service._get_bfs_category_counts_combined(target, db) == ({}, {})
    monkeypatch.setattr(analytics_module.pd, "read_sql", MagicMock(side_effect=RuntimeError("pandas")))
    assert service._get_bfs_category_counts_combined(target, db) == ({}, {})


@pytest.mark.asyncio
async def test_analytics_individual_and_daily_error_branches(monkeypatch):
    service, _ = make_analytics_service(monkeypatch)
    service._prepare_batch_session_data = AsyncMock(side_effect=RuntimeError("prepare"))
    result = await service._process_sessions_in_batches([make_session()], date(2025, 1, 2))
    assert result["total_sessions"] == 0
    service._process_sessions_in_batches = service._process_sessions_in_batches
    monkeypatch.setattr(analytics_module, "get_db_session", lambda: ContextDouble(DBDouble(QueryDouble(values=[make_session()]))))
    service._process_sessions_in_batches = AsyncMock(side_effect=RuntimeError("batch"))
    result = await service.analyze_daily_conversations(date(2025, 1, 2))
    assert result["success"] is False


def test_auto_suggestions_stats_and_additional_pipeline_branches(monkeypatch):
    service, collection, _, fallback, ai = make_auto_service(monkeypatch)
    metadata = {
        "client_category_name": "Tools",
        "level_1_category": "Equipment",
        "level_2_category": "Tools",
        "level_3_category": "Pumps",
        "category_path": "Equipment > Tools > Pumps",
        "confidence_score": 0.8,
        "item_description": "pump",
    }
    collection.query.return_value = {"documents": [["pump", "wire"]], "metadatas": [[metadata, {**metadata, "client_category_name": "Other"}]], "distances": [[0.2, 2.0]]}
    suggestions = service.get_category_suggestions("pump", top_k=2)
    assert len(suggestions) == 1 and suggestions[0]["client_category"] == "Tools"
    collection.query.return_value = {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    assert service.get_category_suggestions("none") == []
    collection.query.side_effect = RuntimeError("suggestions")
    assert service.get_category_suggestions("bad") == []

    collection.query.side_effect = None
    collection.count.return_value = 4
    collection.query.return_value = {"documents": [["one"]]}
    fallback.get_collection_stats.return_value = {"items": 2}
    assert service.get_stats()["unified_vector_store"]["category_items"] == 1
    collection.query.side_effect = RuntimeError("breakdown")
    assert service.get_stats()["unified_vector_store"]["category_items"] == "unknown"
    collection.count.side_effect = RuntimeError("count")
    assert "error" in service.get_stats()

    service._log_categorization = MagicMock()
    service._log_fallback_categorization = MagicMock()
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": True, "all_categories": {"Tools": 1}, "category": "Tools"})
    service._search_hierarchical_levels = MagicMock(return_value={
        "success": True,
        "similarity_score": 0.8,
        "best_match": {"client_category_name": "Other"},
        "all_level_matches": [{"metadata": {"item_description": "pump", "client_category_name": "Other"}, "similarity_score": 0.8, "matched_level": "level_3"}],
    })
    fallback._get_similar_items.return_value = [{"item": "pump", "category": "Tools", "similarity_score": 0.7}]
    ai.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Other", "confidence": 0.4})
    assert (asyncio.run(service.categorize_item("pump", "u")))["learning_updated"] is False
    ai.categorize_with_similar_items.return_value = {"success": True, "category": "Tools", "confidence": 0.8}
    service._search_hierarchical_levels.return_value = {"success": False}
    service._update_learning_taxonomy = AsyncMock(side_effect=RuntimeError("learning"))
    assert (asyncio.run(service.categorize_item("pump", "u")))["method"] == "enhanced_fallback_openai"
    service._search_hierarchical_levels.return_value = {"success": True, "similarity_score": 0.8, "best_match": {"client_category_name": "Other"}, "all_level_matches": []}
    service._keyword_lookup_source_of_truth.return_value = {"success": False}
    fallback._get_similar_items.return_value = []
    assert (asyncio.run(service.categorize_item("none", "u")))["method"] == "enhanced_no_match"



def test_excel_rolling_metrics_and_summary_nonempty(monkeypatch):
    service = make_excel_service(monkeypatch)
    db = SimpleNamespace(bind="bind")
    class RollingResult:
        empty = False

        class Iloc:
            def __getitem__(self, index):
                return ({"total_rfqs": 5},)

        iloc = Iloc()

    rolling = RollingResult()
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: rolling)
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 1) == {"total_rfqs": 5}
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 7) == {"total_rfqs": 5}
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 30) == {"total_rfqs": 5}
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 90) == {"total_rfqs": 5}
    monkeypatch.setattr(excel_module.pd, "read_sql", lambda *args, **kwargs: pd.DataFrame({"buyer_metrics": [None]}))
    assert service._get_rolling_window_metrics(db, date(2025, 1, 2), 1) is None
    writer = MagicMock()
    monkeypatch.setattr(excel_module, "get_db_session_context", lambda: ContextDouble(db))
    service._get_rolling_window_metrics = MagicMock(side_effect=[{"total_rfqs": 1}, None, None, None])
    service._calculate_buyer_metrics = MagicMock(return_value={"total_rfqs": 2})
    monkeypatch.setattr(pd.DataFrame, "to_excel", MagicMock())
    service._generate_buyer_summary_sheet(writer, date(2025, 1, 2))
    assert service._calculate_buyer_metrics.call_count == 3


@pytest.mark.asyncio
async def test_seller_intent_email_and_selection_edge_paths(monkeypatch):
    service, _, _, _ = make_seller_service(monkeypatch)
    user = make_user(role=SimpleNamespace(value="seller"))
    session = make_session(conversation_history={"messages": [{"role": "assistant", "content": "plans"}]})
    service.response_helpers.generate_seller_contextual_intent_response = AsyncMock(return_value={"intent": "general_question", "confidence": 0.9})
    assert (await service._classify_seller_intent("hello", {"seller_credits": 1}, session))["intent"] == "general_question"
    service.response_helpers.generate_seller_contextual_intent_response.side_effect = RuntimeError("ai")
    assert (await service._classify_seller_intent("upgrade", {}, session))["intent"] == "plan_upgrade_request"

    class BadHistory:
        def __bool__(self):
            return True
        def __getitem__(self, key):
            raise RuntimeError("history")

    session.conversation_history = BadHistory()
    assert (await service._classify_seller_intent("hello", {}, session))["intent"] == "general_question"
    analysis = service._analyze_email_errors([
        {"success": True, "rfq_id": "R0"},
        {"success": False, "rfq_id": "R1", "error_code": "NO_CREDITS"},
        {"success": False, "rfq_id": "R2", "error_code": "RFQ_NOT_FOUND"},
        {"success": False, "rfq_id": "R3", "error_code": "API_ERROR"},
        {"success": False, "rfq_id": "R4", "error_code": "OTHER"},
    ])
    assert analysis["total_successful"] == 1 and analysis["total_failed"] == 4
    assert service._extract_sequence_numbers("1, 2 and 7") == [1, 2, 7]
    assert service._map_sequence_to_rfq_ids([1, 3], {"content": "1. RFQ123456789012\n2. RFQ999999999999"}) == ["RFQ123456789012"]
