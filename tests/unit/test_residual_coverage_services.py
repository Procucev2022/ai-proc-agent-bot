"""Deterministic tests for residual service branches and fallback paths."""
from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.chat_service as chat_mod
import app.services.enhanced_excel_report_service as report_mod
import app.services.excel_validation_service as validation_mod
import app.services.inactivity_timeout_service as timeout_mod
import app.services.message_queue_service as queue_mod


class AsyncCM:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *_args):
        return False


class DownloadSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, _url):
        return AsyncCM(self.response, self.error)


class DownloadResponse:
    def __init__(self, status, body=b"bytes"):
        self.status = status
        self.body = body

    async def read(self):
        return self.body


@pytest.mark.asyncio
async def test_excel_download_retry_status_exception_and_content_edges(monkeypatch):
    service = validation_mod.ExcelValidationService()
    responses = iter([DownloadResponse(500), DownloadResponse(200, b"ok")])
    monkeypatch.setattr(
        validation_mod.aiohttp,
        "ClientSession",
        lambda **_kwargs: DownloadSession(next(responses)),
    )
    assert await service._download_file_with_retry("url") == b"ok"

    monkeypatch.setattr(
        validation_mod.aiohttp,
        "ClientSession",
        lambda **_kwargs: DownloadSession(DownloadResponse(404)),
    )
    assert await service._download_file_with_retry("url") is None

    attempts = {"count": 0}

    def failing_session(**_kwargs):
        attempts["count"] += 1
        raise RuntimeError("network")

    monkeypatch.setattr(validation_mod.aiohttp, "ClientSession", failing_session)
    assert await service._download_file_with_retry("url", max_retries=2) is None
    assert attempts["count"] == 2

    assert service._validate_file_extension("") is False
    assert service._validate_file_extension("no_extension") is False
    assert service._validate_excel_content(b"x")['error_type'] == "corrupted_file"
    assert service._validate_excel_content(b"not excel")['error_type'] == "invalid_format"
    assert service._validate_excel_content(b"a,b\n1,2")['error_type'] == "csv_format_detected"


@pytest.mark.asyncio
async def test_excel_readability_all_reader_paths(monkeypatch):
    service = validation_mod.ExcelValidationService()
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame()))
    assert (await service._validate_excel_readability(b"x", "x.xlsx"))["valid"]

    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(side_effect=ValueError("bad format")))
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(return_value=SimpleNamespace(close=MagicMock())))
    assert (await service._validate_excel_readability(b"x", "x.xlsx"))["valid"]

    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(side_effect=validation_mod.InvalidFileException("invalid")))
    monkeypatch.setattr(validation_mod.xlrd, "open_workbook", MagicMock(return_value=object()))
    assert (await service._validate_excel_readability(b"x", "x.xls"))["valid"]

    monkeypatch.setattr(validation_mod.xlrd, "open_workbook", MagicMock(side_effect=RuntimeError("corrupt")))
    result = await service._validate_excel_readability(b"x", "x.xls")
    assert result["error_type"] == "unreadable_file"

    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(side_effect=ValueError("encrypted")))
    result = await service._validate_excel_readability(b"x", "x.xlsx")
    assert result["error_type"] == "password_protected"

    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(side_effect=RuntimeError("outer")))
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(side_effect=RuntimeError("outer")))
    result = await service._validate_excel_readability(b"x", "x.xlsx")
    assert result["error_type"] == "readability_error"


@pytest.mark.asyncio
async def test_excel_structure_and_quality_fallbacks(monkeypatch):
    service = validation_mod.ExcelValidationService()

    worksheet = SimpleNamespace(
        iter_rows=lambda: [[SimpleNamespace(value="a")]],
        merged_cells=SimpleNamespace(ranges=[]),
        row_dimensions={}, column_dimensions={}, _pivots=[],
    )
    workbook = SimpleNamespace(worksheets=[worksheet], active=worksheet, close=MagicMock())
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(return_value=workbook))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["valid"]

    multi = SimpleNamespace(worksheets=[worksheet, worksheet], active=worksheet, close=MagicMock())
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(return_value=multi))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "multiple_worksheets"

    hidden = SimpleNamespace(
        iter_rows=lambda: [[SimpleNamespace(value="a")]],
        merged_cells=SimpleNamespace(ranges=["A1:B1"]),
        row_dimensions={}, column_dimensions={}, _pivots=[],
    )
    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(return_value=SimpleNamespace(worksheets=[hidden], active=hidden, close=MagicMock())))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "merged_cells_found"

    monkeypatch.setattr(validation_mod, "load_workbook", MagicMock(side_effect=RuntimeError("openpyxl")))
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A": [1]})))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["valid"]
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(side_effect=RuntimeError("pandas")))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["error_type"] == "structure_validation_error"

    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame()))
    assert (await service._validate_data_quality(b"x"))["error_type"] == "no_data_found"
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A": [1], "B": [2]})))
    assert (await service._validate_data_quality(b"x"))["error_type"] == "no_headers_detected"
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A!": ["x"], "B": ["y"]})))
    assert (await service._validate_data_quality(b"x"))["error_type"] == "invalid_header_characters"
    monkeypatch.setattr(service, "SPECIAL_CHARS_PATTERN", r"$^")
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"数量": ["x"], "項目": ["y"]})))
    assert (await service._validate_data_quality(b"x"))["error_type"] == "non_english_headers"

    quantity = pd.DataFrame({"Quantity": [1.2, "@bad"], "Description": ["x", "y"]})
    monkeypatch.setattr(validation_mod.pd, "read_excel", MagicMock(return_value=quantity))
    assert (await service._validate_data_quality(b"x"))["error_type"] == "invalid_quantity_values"


class FakeDBContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_args):
        return False


class FakeWriter:
    def __init__(self):
        self.book = SimpleNamespace(sheetnames=[])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_report_constructor_generate_defaults_and_detail_fallbacks(monkeypatch):
    monkeypatch.setattr(report_mod, "get_settings", lambda: SimpleNamespace(report_email_recipients=[]))
    service = report_mod.EnhancedExcelReportService()
    assert service.settings.report_email_recipients == []
    db = SimpleNamespace(bind="bind")
    monkeypatch.setattr(report_mod, "get_db_session_context", lambda: FakeDBContext(db))
    monkeypatch.setattr(pd.DataFrame, "to_excel", MagicMock())
    frame = pd.DataFrame({"A": [1]})
    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(return_value=frame))
    writer = FakeWriter()
    service._generate_buyer_details_sheet(writer, date(2024, 1, 2))
    service._generate_seller_details_sheet(writer, date(2024, 1, 2))
    service._generate_category_details_sheet(writer, date(2024, 1, 2))
    assert service._generate_buyer_details_fallback(db, date(2024, 1, 1), date(2024, 1, 2)).equals(frame)
    assert service._generate_seller_details_fallback(db, date(2024, 1, 1), date(2024, 1, 2)).equals(frame)

    monkeypatch.setattr(report_mod.pd, "read_sql", MagicMock(side_effect=RuntimeError("missing")))
    with pytest.raises(UnboundLocalError):
        service._generate_buyer_details_sheet(writer, date(2024, 1, 2))
    with pytest.raises(UnboundLocalError):
        service._generate_seller_details_sheet(writer, date(2024, 1, 2))
    with pytest.raises(UnboundLocalError):
        service._generate_category_details_sheet(writer, date(2024, 1, 2))
    assert service._generate_category_details_fallback(db, date(2024, 1, 1), date(2024, 1, 2)).shape[0] == 3


def test_report_generate_email_path_closes_awaitable(monkeypatch):
    service = report_mod.EnhancedExcelReportService.__new__(report_mod.EnhancedExcelReportService)
    writer = FakeWriter()
    monkeypatch.setattr(report_mod.pd, "ExcelWriter", lambda *_args, **_kwargs: writer)
    for name in ("_generate_buyer_details_sheet", "_generate_seller_details_sheet", "_generate_category_details_sheet",
                 "_generate_buyer_summary_sheet", "_generate_seller_summary_sheet", "_generate_category_summary_sheet",
                 "_generate_aggregate_sheet_90d", "_format_excel_sheets"):
        setattr(service, name, MagicMock())
    seen = []

    def fake_run(awaitable):
        seen.append(awaitable)
        awaitable.close()

    monkeypatch.setattr(report_mod.asyncio, "run", fake_run)
    assert service.generate_report(send_email=True, output_file="out.xlsx") == "out.xlsx"
    assert seen


class FakeRedis:
    def __init__(self):
        self.get = AsyncMock(return_value=None)
        self.get_session = AsyncMock(return_value=None)
        self.delete = AsyncMock(return_value=1)
        self.store_session = AsyncMock()
        self.save_session = AsyncMock()


@pytest.mark.asyncio
async def test_inactivity_timeout_full_workflow_race_and_failures(monkeypatch):
    service = timeout_mod.InactivityTimeoutService.__new__(timeout_mod.InactivityTimeoutService)
    service.redis = FakeRedis()
    service.redis_session = FakeRedis()
    service.timeout_seconds = 10
    service.activity_key_ttl = 30
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock())

    service.redis_session.get_session.return_value = {"workflow_type": "rfq_creation", "workflow_state": "bad", "conversation_history": "bad", "extracted_entities": {}}
    service.redis.get.return_value = "95"
    monkeypatch.setattr(timeout_mod.time, "time", lambda: 100)
    await service._handle_timeout("+1", "S1", "1:last_activity")
    service.redis.delete.assert_not_awaited()

    service.redis.get.return_value = "0"
    auth = SimpleNamespace(retrieve=AsyncMock(return_value=SimpleNamespace(self_client=True)))
    monkeypatch.setattr("app.redis_db.get_auth_redis_service", lambda: auth)
    monkeypatch.setattr("app.database.DatabaseManager", lambda: SimpleNamespace(append_session_data=MagicMock(), close=MagicMock()))
    monkeypatch.setattr("app.services.helpers.session_helpers.SessionHelpers.clean_for_json_serialization", lambda value: value)
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: SimpleNamespace(clear_meaningful_message=AsyncMock(return_value=True)))
    await service._handle_timeout("+1", "S1", "1:last_activity")
    assert service.redis_session.store_session.await_count == 1
    assert service.whatsapp_service.send_message.await_count == 1

    service.redis_session.get_session.side_effect = RuntimeError("redis")
    service.redis.get.side_effect = RuntimeError("activity")
    service.redis.delete.side_effect = RuntimeError("delete")
    service.whatsapp_service.send_message.side_effect = RuntimeError("wa")
    await service._handle_timeout("1", "S2", "bad")


@pytest.mark.asyncio
async def test_message_queue_edge_keys_enqueue_and_background_cleanup(monkeypatch):
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.batch_window = 3
    service._background_tasks = []
    service._running = True
    service.redis = SimpleNamespace(
        zadd=AsyncMock(), exists=AsyncMock(return_value=False), setex=AsyncMock(),
    )
    service._create_batch = AsyncMock()
    assert service._key_lock_ack("1") == "1:lock:ack"
    assert service._key_ack_sent("1") == "1:ack_sent"
    await service.enqueue_message({"from": "+1", "timestamp": 1, "type": "image", "content": "", "message_id": "m"})
    service._create_batch.assert_awaited_once()
    await service.enqueue_message({"from": "1", "timestamp": 2, "type": "text", "content": "next"})
    service.redis.exists.return_value = True
    service._refresh_batch_timer = AsyncMock()
    await service.enqueue_message({"from": "1", "timestamp": 3, "type": "text", "text": {"body": "body"}})
    service._refresh_batch_timer.assert_awaited_once_with("1")

    class Task:
        def __init__(self, name, done):
            self.name = name
            self._done = done
        def done(self):
            return self._done
        def get_name(self):
            return self.name

    service._background_tasks = [Task("batch_poller", True), Task("monitoring_loop", False)]
    created = []

    def fake_create_task(coro, name=None):
        coro.close()
        task = Task(name, False)
        created.append(task)
        return task

    monkeypatch.setattr(queue_mod.asyncio, "create_task", fake_create_task)
    service._ensure_background_tasks()
    assert len(service._background_tasks) == 2 and created


def chat_service_stub():
    service = chat_mod.ChatService.__new__(chat_mod.ChatService)
    service.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock(), send_message=AsyncMock())
    service.session_manager = SimpleNamespace(save_session=AsyncMock(), send_and_track_message=AsyncMock())
    service._response_helpers = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="context"))
    service._openai_service = SimpleNamespace(parse_confirmation_response=AsyncMock(return_value="yes"), generate_clarification_response=AsyncMock(return_value="clarify"))
    service._faq_service = SimpleNamespace(get_faq_answer=AsyncMock(return_value="answer"))
    service._products_array_handler = SimpleNamespace(handle_products_array=AsyncMock(return_value={"status": "products"}))
    service._purchase_intent_handler = SimpleNamespace(handle_purchase_intent=AsyncMock(return_value={"status": "purchase"}))
    service._bfs_search_handler = SimpleNamespace(handle_button=AsyncMock(return_value={"status": "bfs"}), handle_bfs_search=AsyncMock(return_value={"status": "bfs_search"}), handle_bid_format_input=AsyncMock(return_value={"status": "bid"}), handle_bid_otp_input=AsyncMock(return_value={"status": "otp"}))
    service._cancel_service = SimpleNamespace(_send_cancellation_message=AsyncMock(), handle_cancel_intent=AsyncMock(return_value={"status": "cancel"}), handle_cancel_confirmation=AsyncMock(return_value={"status": "cancelled"}))
    service._exit_service = SimpleNamespace(handle_exit_intent=AsyncMock(return_value={"status": "exited"}))
    service._confirmation_handler = SimpleNamespace(handle_confirmation_button=AsyncMock(return_value={"status": "confirmed"}), handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}), handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}))
    service._authentication_service = SimpleNamespace(handle_email_confirmation=AsyncMock(return_value={"status": "email"}))
    service._intent_switch_handler = SimpleNamespace(should_handle_intent_switch=AsyncMock(return_value=False), handle_intent_switch_choice=AsyncMock(return_value={"status": "switch"}), handle_intent_switch_response=AsyncMock(return_value={"status": "switch"}))
    service._entity_service = SimpleNamespace()
    service._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    service._handle_excel_sectioned_rfq = AsyncMock(return_value={"status": "excel_sectioned_rfq_initialized"})
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "rfq_status"})
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel"})
    service._handle_cancel_confirmation_button = AsyncMock(return_value={"status": "cancel"})
    service._handle_exit_confirmation_button = AsyncMock(return_value={"status": "exit_confirm"})
    service._activate_sectioned_rfq = AsyncMock(return_value={"status": "activated"})
    return service


def chat_user(role="buyer"):
    return SimpleNamespace(phone_number="+1", role=role, name="alex user", email="a@x", id="u", org_id="o")


def chat_session(**state):
    return SimpleNamespace(
        session_id="S", external_user_id="1", workflow_state=dict(state), workflow_type=None,
        conversation_history={"messages": [], "metadata": [], "openai_messages": []},
        outcome=None, product_items=[], extracted_entities={}, rfq_ids=[],
    )


@pytest.mark.asyncio
async def test_chat_excel_conversion_confirmation_and_incomplete_paths(monkeypatch):
    service = chat_service_stub()
    products = service._convert_excel_items_to_products_array([
        {"ItemDescription": " Laptop ", "Specification": "HP", "Quantity": "2", "Uom": "pcs", "Remarks": ""},
        {"ItemDescription": "Chair", "Quantity": "bad", "State": None},
    ])
    assert products[0]["description"] == "Laptop" and products[1]["quantity"] is None

    user = chat_user(); session = chat_session()
    result = await service._handle_complete_excel(user, session, {
        "items": [{"ItemDescription": "Laptop", "Quantity": 2}], "total_items": 1,
        "filename": "x.xlsx", "processing_summary": {"skipped_rows": 0},
    })
    assert result["status"] == "excel_confirmation_sent" and session.workflow_state["awaiting_excel_confirmation"]
    service._convert_excel_items_to_products_array = MagicMock(return_value=[])
    assert (await service._handle_complete_excel(user, session, {"items": [], "total_items": 0}))["status"] == "incomplete"
    service._convert_excel_items_to_products_array.side_effect = RuntimeError("convert")
    service.response_helpers.generate_contextual_response = AsyncMock(return_value="error")
    assert (await service._handle_complete_excel(user, session, {"items": []}))["status"] == "incomplete"

    service = chat_service_stub()
    service._handle_excel_confirmation_response = chat_mod.ChatService._handle_excel_confirmation_response.__get__(service)
    session = chat_session(excel_confirmation_data={"products": [{"description": "x"}], "processing_result": {}, "filename": "x"}, awaiting_excel_confirmation=True)
    monkeypatch.setattr(chat_mod, "get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(use_sectioned_rfq=False))
    assert (await service._handle_excel_confirmation_response(user, session, "yes"))["status"] == "products"
    service.openai_service.parse_confirmation_response.return_value = "no"
    session = chat_session()
    session.workflow_state["excel_confirmation_data"] = {"products": [{"description": "x"}]}
    assert (await service._handle_excel_confirmation_response(user, session, "no"))["status"] == "excel_cancelled_redirected_to_greeting"
    service = chat_service_stub()
    service._handle_excel_confirmation_response = chat_mod.ChatService._handle_excel_confirmation_response.__get__(service)
    service.openai_service.parse_confirmation_response.return_value = None
    assert (await service._handle_excel_confirmation_response(user, chat_session(), "hmm"))["status"] == "excel_clarification_requested"
    service.openai_service.parse_confirmation_response.side_effect = RuntimeError("AI")
    assert (await service._handle_excel_confirmation_response(user, chat_session(), "confirm"))["status"] == "excel_data_missing"

    service.openai_service.generate_clarification_response.return_value = "provide rows"
    service._handle_incomplete_excel = chat_mod.ChatService._handle_incomplete_excel.__get__(service)
    result = await service._handle_incomplete_excel(user, chat_session(), {"excel_data": {"total_items": 2}, "missing_fields": ["quantity"], "completeness": 50})
    assert result["status"] == "excel_reupload_required"
    result = await service._handle_incomplete_excel(user, chat_session(), {"excel_data": {"should_skip_rfq_creation": True, "processing_summary": {"skipped_items_summary": "bad"}, "error": "reject"}, "missing_fields": []})
    assert result["status"] == "excel_rejected"


@pytest.mark.asyncio
async def test_chat_menu_role_fallback_and_button_matrix(monkeypatch):
    service = chat_service_stub()
    for role in ("buyer", "seller", "other"):
        result = await service._handle_greeting_inquiry(chat_user(role), "hi", chat_session())
        assert result["status"] == "greeting_handled"
    service.faq_service.get_faq_answer.return_value = None
    assert (await service._handle_faq_request(chat_user(), "faq"))["answer_provided"] is False
    service.faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert (await service._handle_faq_request(chat_user(), "faq"))["status"] == "error"
    service._handle_support_request = chat_mod.ChatService._handle_support_request.__get__(service)
    service._handle_clarification_request = chat_mod.ChatService._handle_clarification_request.__get__(service)
    service._handle_fallback = chat_mod.ChatService._handle_fallback.__get__(service)
    monkeypatch.setattr(chat_mod, "get_settings", lambda: SimpleNamespace(support_contact_info="help@x"))
    for role in ("buyer", "seller", "other"):
        assert (await service._handle_support_request(chat_user(role), "help", chat_session()))["status"] == "support_handled"
        assert (await service._handle_clarification_request(chat_user(role), "?", chat_session()))["status"] == "clarification_sent"
        assert (await service._handle_fallback(chat_user(role), "?", chat_session()))["status"] == "fallback_handled"

    user = chat_user(); session = chat_session()
    for button, expected in (("create_rfq", "activated"), ("search_bfs", "bfs"), ("rfq_status", "rfq_status"), ("view_rfqs", "seller"), ("get_support", "support_handled"), ("exit", "exited"), ("confirm_excel", "excel"), ("confirm_cancel", "cancel"), ("confirm_exit", "exit_confirm")):
        if button == "confirm_cancel":
            service._handle_cancel_confirmation_button = AsyncMock(return_value={"status": "cancel"})
        result = await service._handle_button_response(user, session, button)
        assert result["status"] == expected
    assert (await service._handle_button_response(user, session, "unknown"))["status"] == "button_handled"
    service.confirmation_handler.handle_confirmation_button.return_value = {"status": "continued"}
    assert (await service._handle_button_response(user, session, "continue_rfq"))["status"] == "button_handled"
    service.authentication_service.handle_email_confirmation.return_value = {"status": "email"}
    assert (await service._handle_button_response(user, session, "confirm_email"))["status"] == "email"


@pytest.mark.asyncio
async def test_chat_transform_delivery_and_pure_state_helpers(monkeypatch):
    service = chat_service_stub()
    service._entity_service = SimpleNamespace()
    session = chat_session(delivery_date="tomorrow", pincode="560001", city="", state="")
    monkeypatch.setattr("app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler", lambda **_: SimpleNamespace(_validate_delivery_date=AsyncMock(return_value={"is_valid": True, "normalized_date": "2 January 2026"})))
    monkeypatch.setattr("app.utils.pincode_lookup.get_location_from_pincode_async", AsyncMock(return_value={"city": "Pune", "state": "MH"}))
    products, delivery = await service.transform_rfq_to_section_rfq_format(session, [{"description": "x", "quantity": "2.5", "uom": "kg"}, {"description": "y", "quantity": "bad"}])
    assert products[0]["quantity"] == 2.5 and products[1]["quantity"] == 0 and delivery["city"] == "Pune"
    with pytest.raises(ValueError):
        await service.transform_rfq_to_section_rfq_format(chat_session(pincode="bad"), [{"description": "x"}])
    session.workflow_state["sectioned_rfq"] = {"sections": {"date_location": {"confirmed": True, "data": {"pincode": "1"}}}}
    products, delivery = await service.transform_rfq_to_section_rfq_format(session, [{"description": "x", "quantity": 1}])
    assert delivery["pincode"] == "1"
    assert not service._should_use_summary_aware_extraction("that one")
    assert not service._should_use_summary_aware_extraction("new laptop")
    assert service._get_workflow_or_default(chat_session(), "bad") == chat_mod.WorkflowType.general_inquiry


@pytest.mark.asyncio
async def test_chat_text_router_priority_and_intent_matrix(monkeypatch):
    service = chat_service_stub()
    service._update_last_user_message_with_intent = MagicMock()
    service._handle_contextual_interaction = AsyncMock(return_value={"status": "context"})
    service._handle_format_modification = AsyncMock(return_value={"status": "format"})
    service._handle_account_switch_intent = AsyncMock(return_value={"status": "switch"})
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting"})
    service._handle_fallback = AsyncMock(return_value={"status": "fallback"})
    service._handle_clarification_request = AsyncMock(return_value={"status": "clarify"})
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service.handle_irrelevant_message_flow = AsyncMock()
    service._handle_seller_rfq_intimation_flow = AsyncMock(return_value=None)
    service._attachment_decision_handler = SimpleNamespace(handle_attachment_decision=AsyncMock(return_value={"status": "attachment"}))
    service._intent_switch_handler.should_handle_intent_switch = AsyncMock(return_value=False)
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _s: False)
    monkeypatch.setattr(
        "app.services.user_cache_service.get_user_cache_service",
        lambda: SimpleNamespace(get_account_options_for_intent_switch=AsyncMock(return_value=None)),
    )
    user = chat_user(); user.is_registered = True

    async def route(intent, confidence=90, state=None, role="buyer"):
        current = chat_session(**(state or {}))
        current.workflow_type = None
        actor = chat_user(role); actor.is_registered = True
        return await chat_mod.ChatService._process_text_message(service, actor, current, intent, {"intent": intent, "confidence": confidence})

    service._cancel_service.handle_cancel_intent.return_value = {"status": "cancel"}
    assert (await route("cancel_workflow"))["status"] == "cancel"
    assert (await route("exit_system"))["status"] == "exited"
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    assert (await route("support"))["status"] == "support"
    service._attachment_decision_handler.handle_attachment_decision.return_value = {"status": "attachment"}
    assert (await route("other", state={"awaiting_attachment_decision": True}))["status"] == "attachment"
    service._confirmation_handler.handle_optional_fields_response.return_value = {"status": "optional"}
    assert (await route("other", state={"pending_optional_rfq": {"field": "value"}}))["status"] == "optional"
    service._bfs_search_handler.handle_bfs_search.return_value = {"status": "bfs"}
    assert (await route("other", state={"bfs_search_pending": True}))["status"] == "bfs"
    service._bfs_search_handler.handle_bid_format_input.return_value = {"status": "bid"}
    assert (await route("other", state={"bfs_bid_stage": "format_input"}))["status"] == "bid"
    service._bfs_search_handler.handle_bid_otp_input.return_value = {"status": "otp"}
    assert (await route("other", state={"bfs_bid_stage": "otp_pending"}))["status"] == "otp"
    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel"})
    assert (await route("other", state={"awaiting_excel_confirmation": True}))["status"] == "excel"
    service._confirmation_handler.handle_pending_confirmations.return_value = {"status": "pending"}
    assert (await route("other", state={"pending_rfq": {"item": "laptop"}}))["status"] == "pending"
    assert (await route("buy_something"))["status"] == "purchase"
    assert (await route("format_modification"))["status"] == "format"
    assert (await route("confirmation_response"))["status"] == "purchase"
    assert (await route("reference_request"))["status"] == "purchase"
    assert (await route("bfs_search"))["status"] == "bfs"
    assert (await route("sell_something"))["status"] == "role_switch_confirmation_requested"
    assert (await route("rfq_status_check"))["status"] == "rfq_status"
    assert (await route("account_switch"))["status"] == "switch"
    await route("general_inquiry")
    assert (await route("greeting"))["status"] == "greeting"
    assert (await route("unknown", 0.1))["status"] == "clarify"
    assert (await route("unknown", 0.9))["status"] == "fallback"

    seller = chat_user("seller"); seller.is_registered = True
    seller_session = chat_session()
    seller_session.workflow_type = SimpleNamespace(value="seller_rfq_view")
    service._handle_seller_flow.return_value = {"status": "seller"}
    assert (await chat_mod.ChatService._process_text_message(service, seller, seller_session, "view", {"intent": "other", "confidence": .5}))["status"] == "seller"
    seller_session.workflow_type = None
    assert (await chat_mod.ChatService._process_text_message(service, seller, seller_session, "status", {"intent": "rfq_status_check", "confidence": .9}))["status"] == "rfq_status"

    recent = chat_session(recently_completed_registration=True)
    assert (await chat_mod.ChatService._process_text_message(service, user, recent, "buy", {"intent": "unknown", "confidence": .2}))["status"] == "purchase"
    unclear_reg = {"intent": "register_account", "confidence": .9, "context_analysis": {"registration_details": {}}}
    assert (await chat_mod.ChatService._process_text_message(service, user, chat_session(), "register", unclear_reg))["status"] == "registration_clarification_requested"
