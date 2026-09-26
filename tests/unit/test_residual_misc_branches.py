"""Deterministic residual branch coverage for task, helper, processor, and utility modules.

Every integration boundary in this module is replaced by a fake or mock.  The
cases intentionally complement the existing unit suites by exercising failure,
cleanup, fallback, and malformed-input branches.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.tasks import bfs_notification_task as bfs
from app.tasks import seller_matching_task as seller_task
from app.tasks import taxonomy_build_task as taxonomy
from app.tasks import vector_store_sync_task as vector_sync
from app.services.helpers.excel_helpers import ExcelHelpers
from app.services.helpers.session_helpers import SessionHelpers
from app.services.helpers.summarization_helpers import SummarizationHelpers
from app.services.helpers.support_helpers import SupportHelpers
from app.services.processors import excel_message_processor as excel_processor
from app.services.processors import image_message_processor as image_processor
from app.tools.user_selection_tool import UserSelectionTool
from app.utils import bfs_bid_format_parser as bid_parser
from app.utils import bfs_format_parser as bfs_parser
from app.utils import logging_utils


# Small local fakes ---------------------------------------------------------


def session(**overrides):
    data = {
        "session_id": "sid",
        "external_user_id": "user-1",
        "workflow_type": None,
        "workflow_state": {},
        "conversation_history": {"openai_messages": [], "messages": [], "metadata": []},
        "created_at": datetime(2024, 1, 1),
        "last_activity_at": datetime(2024, 1, 1),
        "product_items": [],
        "extracted_entities": {},
        "rfq_ids": [],
        "rfq_id": None,
        "outcome": None,
        "completed_at": None,
        "retention_date": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class Query:
    def __init__(self, values=(), first=None, delete_count=0):
        self.values = list(values)
        self.first_value = first
        self.delete_count = delete_count

    def all(self):
        return self.values

    def first(self):
        return self.first_value

    def filter(self, *args, **kwargs):
        return self

    def delete(self, **kwargs):
        return self.delete_count


class DB:
    def __init__(self, query=None):
        self.query_value = query or Query()
        self.added = []
        self.commit = Mock()
        self.rollback = Mock()
        self.close = Mock()

    def query(self, *args, **kwargs):
        return self.query_value

    def add(self, value):
        self.added.append(value)


# Taxonomy and vector task residual branches -------------------------------


def patch_taxonomy_loader(monkeypatch, process):
    module = SimpleNamespace(process_category_mappings=process)
    spec = SimpleNamespace(
        loader=SimpleNamespace(exec_module=lambda target: target.__dict__.update(module.__dict__))
    )
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *a: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _: SimpleNamespace())


@pytest.mark.asyncio
async def test_taxonomy_sequential_failure_continues_and_checkpoint_read_error(monkeypatch):
    monkeypatch.setattr(taxonomy, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1))
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"category": "a"}, {"category": "b"}])
    redis = SimpleNamespace(
        get=AsyncMock(side_effect=RuntimeError("redis read")),
        set=AsyncMock(),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())
    process = AsyncMock(side_effect=[RuntimeError("first"), RuntimeError("second"), RuntimeError("third")])
    patch_taxonomy_loader(monkeypatch, process)

    result = await taxonomy.build_taxonomy_async(None, batch_size=2, process_all=True, parallel=False, resume=True)

    assert result["success"] is False
    assert result["total_errors"] == 1
    assert process.await_count == 3


@pytest.mark.asyncio
async def test_taxonomy_parallel_false_result_and_checkpoint_cleanup_failure(monkeypatch):
    monkeypatch.setattr(taxonomy, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True, bulk_pool_workers=1))
    monkeypatch.setattr(taxonomy, "get_remote_item_categories", lambda: [{"category": "a"}])
    redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({"next_batch": 0})), set=AsyncMock(), delete=AsyncMock(side_effect=RuntimeError("delete")))
    monkeypatch.setattr(taxonomy.AsyncRedisConnectionManager, "get_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(taxonomy.asyncio, "sleep", AsyncMock())
    process = AsyncMock(return_value={"success": False, "error": "rejected"})
    patch_taxonomy_loader(monkeypatch, process)

    result = await taxonomy.build_taxonomy_async(None, batch_size=1, process_all=True, parallel=True, resume=True)

    assert result["success"] is False
    assert result["total_errors"] == 3
    assert result["batches_processed"] == 0
    assert process.await_count == 3


def test_vector_sync_outer_timeout_and_cleanup_close(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    monkeypatch.setattr(vector_sync, "get_settings", Mock(side_effect=SoftTimeLimitExceeded()))
    assert vector_sync.sync_vector_store.run()["status"] == "timeout"

    db = DB(Query())
    db.query_value = Query()
    monkeypatch.setattr(vector_sync, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=True))
    monkeypatch.setattr(vector_sync, "_cleanup_buyer_mappings", Mock(side_effect=RuntimeError("cleanup")))
    monkeypatch.setattr(vector_sync, "_run_seller_category_mapping", lambda: {"success": True})
    result = vector_sync.sync_vector_store.run(cleanup_buyer_mappings=True)
    assert result["status"] == "completed"
    assert result["steps_failed"][0]["step"] == "buyer_mapping_cleanup"


def test_vector_cleanup_rollback(monkeypatch):
    database = __import__("app.database", fromlist=["get_db_session"])
    models = __import__("app.models", fromlist=["SellerLearningMapping"])
    db = DB(Query())
    db.query_value = Query()
    db.commit.side_effect = RuntimeError("commit")
    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(database, "execute_remote_query", lambda *_: [{"uuid": "buyer"}])
    monkeypatch.setattr(models, "SellerLearningMapping", SimpleNamespace(seller_id=SimpleNamespace(in_=lambda _: [])))
    result = vector_sync._cleanup_buyer_mappings()
    assert result["success"] is False
    db.rollback.assert_called_once()
    db.close.assert_called_once()


# BFS/category/seller residual branches -----------------------------------

@pytest.mark.asyncio
async def test_bfs_service_exception_and_no_successful_records(monkeypatch):
    monkeypatch.setattr(bfs, "get_settings", lambda: SimpleNamespace(enable_remote_categorization=True))
    monkeypatch.setattr(bfs, "get_pending_bfs_notifications", lambda limit: [{"bfs_user_uuid": "b", "seller_phone": "9199"}])
    monkeypatch.setattr(bfs, "get_users_active_in_last_24hrs", lambda phones: set())
    monkeypatch.setattr(bfs, "normalize_phone_for_comparison", lambda phone: phone.lstrip("+"))
    service = SimpleNamespace(send_bfs_bid_notifications_batch=AsyncMock(return_value={"results": [], "sent": 0, "failed": 1, "skipped": 0}))
    monkeypatch.setattr(bfs, "get_seller_notification_service", lambda: service)
    marked = Mock()
    monkeypatch.setattr(bfs, "mark_notifications_sent_batch", marked)
    result = await bfs.process_bfs_notifications()
    assert result["failed"] == 1
    marked.assert_not_called()

    service.send_bfs_bid_notifications_batch.side_effect = RuntimeError("whatsapp")
    with pytest.raises(RuntimeError, match="whatsapp"):
        await bfs.process_bfs_notifications()




def test_seller_query_and_remote_logging_exceptions(monkeypatch):
    monkeypatch.setattr(seller_task, "execute_remote_query", Mock(side_effect=RuntimeError("query")))
    with pytest.raises(RuntimeError):
        seller_task.get_rfq_notification_progress("r")
    # The two query helpers intentionally propagate database errors to the task wrapper.
    with pytest.raises(RuntimeError):
        seller_task.get_rfq_item_categories("r")
    with pytest.raises(RuntimeError):
        seller_task.get_sellers_notified_in_last_24hrs()

    db = SimpleNamespace(execute=Mock(side_effect=RuntimeError("write")), rollback=Mock(), close=Mock())
    monkeypatch.setattr(seller_task, "get_remote_db_session", lambda: db)
    assert seller_task.log_selected_sellers_to_remote("r", "u", [{"seller_id": "s"}]) is False
    db.rollback.assert_called_once()
    db.close.assert_called_once()




# Excel and support helpers ------------------------------------------------


def test_excel_helper_malformed_items_thresholds_and_reupload_variants():
    class BadItem:
        def get(self, key, default=None):
            if key == "Quantity":
                raise RuntimeError("bad field")
            return default

    entities = ExcelHelpers.convert_excel_to_entities([BadItem(), {"ItemDescription": "x"}])
    assert len(entities) == 1 and entities[0]["description"] == "x"
    assert ExcelHelpers.calculate_excel_completeness([BadItem(), {"ItemDescription": "x", "Quantity": 1}]) == 50
    missing = ExcelHelpers.identify_missing_fields([{}, {}, {"ItemDescription": "x", "Quantity": 1}])
    assert "Item descriptions" in missing and "Quantities" in missing
    assert "empty or corrupted" in " ".join(ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [], "headers": []}}))
    assert ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"error": "❌ invalid"}}) == ["❌ invalid"]
    assert "Specification" in ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {"missing_required_fields": ["Missing 'Specification' in Item 1"]}}})[0]
    assert "empty fields" in " ".join(ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {"warnings": ["warning"]}}}))
    assert ExcelHelpers.generate_reupload_instructions([], {"excel_data": {"filename": "x", "items": [{"x": 1}], "validation_result": {}}})[0].startswith("Your Excel file")


def test_support_template_formatting_and_mocked_send(monkeypatch, tmp_path):
    helper = SupportHelpers.__new__(SupportHelpers)
    helper.templates_dir = tmp_path
    helper.email_service = SimpleNamespace(send_email=AsyncMock(return_value={"status": "Success"}))
    (tmp_path / "ok.json").write_text('{"to": "{email}", "cc": "a@x, b@x", "subject": "Hello {name}", "body": "Hi {name}"}', encoding="utf-8")
    assert helper.load_template("ok")["subject"] == "Hello {name}"
    assert helper.load_template("bad-json") is None
    helper._parse_email_list = lambda value: [x.strip() for x in value.split(",") if x.strip()]
    helper._build_email_body = lambda data: data.get("body", "")
    helper._log_support_operation = Mock()
    structure = helper.integrate_template_and_data(helper.load_template("ok"), {"email": "user@x", "name": "Ada"})
    assert structure["to"] == ["user@x"] and structure["cc"] == ["a@x", "b@x"]
    result = asyncio.run(helper.send_support_email("ok", {"email": "user@x", "name": "Ada"}))
    assert result["success"] is True
    helper.email_service.send_email.return_value = {"status": "Failed"}
    assert asyncio.run(helper.send_support_email("ok", {"email": "user@x", "name": "Ada"}))["success"] is False
    helper.email_service.send_email.side_effect = RuntimeError("mail")
    assert asyncio.run(helper.send_support_email("ok", {}))["success"] is False
    assert asyncio.run(helper.send_support_email("missing", {}))["success"] is False


# Session and summarization helpers ---------------------------------------

@pytest.mark.asyncio
async def test_session_expiration_eligibility_and_state_reset(monkeypatch):
    settings = SimpleNamespace(redis_session_storage_enabled=False, session_timeout_minutes=60)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.helpers.session_helpers.is_expired", lambda *_: (False, None, None))
    s = session(created_at=datetime(2024, 1, 1), conversation_history={"openai_messages": [{}, {}]})
    assert await SessionHelpers.should_send_expiration_message(s) is False
    monkeypatch.setattr(SessionHelpers, "is_session_expired", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.helpers.session_helpers.utc_now", lambda: datetime(2025, 1, 2, tzinfo=timezone.utc))
    s.created_at = datetime(2024, 1, 1)
    s.workflow_state = {"last_expiry_notification": (datetime(2025, 1, 2, tzinfo=timezone.utc) - timedelta(hours=2)).isoformat()}
    assert await SessionHelpers.should_send_expiration_message(s) is True
    s.conversation_history = {"openai_messages": [{}]}
    assert await SessionHelpers.should_send_expiration_message(s) is False

    monkeypatch.setattr("app.services.helpers.session_helpers.utc_now", lambda: datetime(2025, 1, 2, tzinfo=timezone.utc))
    s = session(workflow_state={"profile_selection_stage": "stage", "profile_options": [1], "remove": True})
    from app.models import WorkflowType
    s.workflow_type = WorkflowType.authentication
    db = SimpleNamespace(append_session_data=Mock())
    await SessionHelpers.handle_session_expiry(s, db)
    assert s.workflow_type == WorkflowType.authentication
    assert s.workflow_state["profile_selection_stage"] == "stage"
    assert "remove" not in s.workflow_state


def test_session_averages_and_serialization_failure_fallbacks():
    s = session(rfq_ids=["r"], product_items="not-a-list", extracted_entities=[{"category": "A"}, {"description": "B"}])
    SessionHelpers.calculate_session_averages(s)
    assert s.avg_products_per_rfq == 0 and s.avg_categories_per_rfq == 2

    class ValueEnum:
        value = "enum"

    class Bad:
        def __str__(self):
            return "bad"

    cleaned = SessionHelpers.clean_for_json_serialization({"e": ValueEnum(), "d": date(2025, 1, 1), "bad": Bad()})
    assert cleaned == {"e": "enum", "d": "2025-01-01", "bad": "bad"}


@pytest.mark.asyncio
async def test_summarization_tracking_fallback_trim_and_completion_errors(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock())
    s = session(conversation_history=None)
    original = SummarizationHelpers.add_to_conversation_history
    monkeypatch.setattr(SummarizationHelpers, "add_to_conversation_history", Mock(side_effect=RuntimeError("track")))
    await SummarizationHelpers.send_and_track_message(wa, s, "1", "hello")
    assert wa.send_message.await_count == 2
    wa.send_message.side_effect = RuntimeError("send")
    with pytest.raises(RuntimeError):
        await SummarizationHelpers.send_and_track_message(wa, s, "1", "hello")
    monkeypatch.setattr(SummarizationHelpers, "add_to_conversation_history", original)

    monkeypatch.setattr(SummarizationHelpers, "_get_atomic_message_index", Mock(return_value=1))
    s = session(conversation_history={"openai_messages": [], "messages": [], "metadata": []})
    for i in range(52):
        SummarizationHelpers.add_to_conversation_history(s, "user", {"image": i}, intent="buy", confidence=.5)
    assert len(s.conversation_history["messages"]) == 50
    assert "intent" in s.conversation_history["messages"][-1]
    async def coro():
        return "x"
    pending = coro()
    before = len(s.conversation_history["messages"])
    SummarizationHelpers.add_to_conversation_history(s, "user", pending)
    pending.close()
    assert len(s.conversation_history["messages"]) == before

    chat = SimpleNamespace(generate_session_summary=AsyncMock(side_effect=RuntimeError("summary")))
    daily = SimpleNamespace(generate_daily_summary=AsyncMock())
    await SummarizationHelpers.handle_session_completion_async(chat, daily, {"session_id": "s", "user_id": "u"})
    daily.generate_daily_summary.assert_not_awaited()


def test_summarization_stage_and_decision_fallbacks():
    assert SummarizationHelpers._determine_workflow_stage({}) == "conversation_start"
    assert SummarizationHelpers._determine_workflow_stage({"products_discussed": [1]}) == "initial_discussion"
    assert SummarizationHelpers._determine_workflow_stage({"incomplete_products": [1]}) == "information_gathering"
    assert SummarizationHelpers._determine_workflow_stage({"completed_products": [1]}) == "product_completion"
    assert SummarizationHelpers._determine_workflow_stage({"rfq_details": {}}) == "conversation_start"
    assert SummarizationHelpers._calculate_completion_level({"rfq_details": {"id": 1}}) == "fully_completed"
    assert SummarizationHelpers._calculate_completion_level({"completed_products": [1]}) == "nearly_completed"
    assert SummarizationHelpers._calculate_completion_level({"incomplete_products": [1]}) == "partially_completed"
    decisions = SummarizationHelpers._extract_key_decisions([{"sender": "user", "content": {"image": True}}, {"sender": "user", "content": 4}], {"budget_constraints": {"max": 2}})
    assert decisions == ["Budget: {'max': 2}"]
    assert SummarizationHelpers.extract_rich_entities_for_summary(SimpleNamespace(workflow_state=None, product_items=None, interaction_metrics=None, seller_responses=None, rfq_metadata=None)) == {}


# Processor branches -------------------------------------------------------

@pytest.mark.asyncio
async def test_excel_processor_validation_failure_and_structured_complete(monkeypatch):
    redis = SimpleNamespace(lock=Mock())
    monkeypatch.setattr(excel_processor, "get_settings", lambda: SimpleNamespace(redis_url="redis://test"))
    monkeypatch.setattr(excel_processor.Redis, "from_url", Mock(return_value=redis))
    wa = SimpleNamespace(send_message=AsyncMock())
    responses = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"), generate_clarification_response=AsyncMock())
    processor = excel_processor.ExcelMessageProcessor(wa, responses, object())
    lock = SimpleNamespace(acquire=AsyncMock(return_value=True), release=AsyncMock())
    redis.lock.return_value = lock
    validation = SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": False, "error": "bad file"}))
    monkeypatch.setattr(excel_processor, "ExcelValidationService", lambda: validation)
    user = SimpleNamespace(phone_number="+1", is_registered=True)
    s = session()
    result = await processor.process_excel_upload(user, s, {"document": {"link": "url", "filename": "x.xlsx"}})
    assert result["response"] == "validation_failed"
    lock.release.assert_awaited_once()

    processor.response_helpers.generate_contextual_response.reset_mock()
    structured = {"filename": "x.xlsx", "rfqs": [{"products": [{"description": "Laptop", "quantity": 2}]}], "processing_summary": {"total_products_extracted": 1}}
    result = await processor._handle_complete_excel(user, s, structured)
    assert result["status"] == "redirect_to_multiple_flow"
    assert s.workflow_state["structured_rfqs"] == structured["rfqs"]


@pytest.mark.asyncio
async def test_excel_processor_missing_quantities_and_critical_cleanup(monkeypatch):
    monkeypatch.setattr(excel_processor, "get_settings", lambda: SimpleNamespace(redis_url="redis://test"))
    lock = SimpleNamespace(acquire=AsyncMock(return_value=True), release=AsyncMock())
    redis = SimpleNamespace(lock=Mock(return_value=lock))
    monkeypatch.setattr(excel_processor.Redis, "from_url", Mock(return_value=redis))
    wa = SimpleNamespace(send_message=AsyncMock())
    responses = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"), generate_clarification_response=AsyncMock())
    monkeypatch.setattr(excel_processor, "ExcelValidationService", lambda: SimpleNamespace(validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"x"})))
    monkeypatch.setattr(excel_processor, "ExcelProcessingService", lambda *_: SimpleNamespace(process_excel_file=AsyncMock(return_value={"success": True, "items": [{"Quantity": ""}] * 4, "filename": "x"})))
    processor = excel_processor.ExcelMessageProcessor(wa, responses, object())
    user = SimpleNamespace(phone_number="1", is_registered=True)
    s = session()
    result = await processor.process_excel_upload(user, s, {"document": {"link": "url", "filename": "x.xlsx"}})
    assert result["response"] == "missing_quantities_reupload_required"
    assert "excel_file_processed" not in s.workflow_state
    lock.release.assert_awaited_once()

    lock.release.side_effect = RuntimeError("unlock")
    monkeypatch.setattr(excel_processor, "ExcelProcessingService", lambda *_: (_ for _ in ()).throw(RuntimeError("processing")))
    result = await processor.process_excel_upload(user, s, {"document": {"link": "url", "filename": "x.xlsx"}})
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_image_processor_flat_base64_irrelevant_and_download_error(monkeypatch):
    wa = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    responses = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="response"))
    processor = image_processor.ImageMessageProcessor(wa, responses)
    user = SimpleNamespace(phone_number="1", is_registered=True)
    irrelevant = session(workflow_state={}, conversation_history={"messages": [{"role": "assistant", "content": "last"}]})
    assert (await processor.process_image_message(user, irrelevant, {"data": "abc"}))["response"] == "attachment_ignored"
    assert wa.send_message.await_args.args[1] == "last"

    relevant = session(workflow_state={"pending_rfq": {"id": "r"}})
    monkeypatch.setattr(image_processor.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": True, "count": 1, "max": 4}))
    monkeypatch.setattr(image_processor.AttachmentHelpers, "approve_pending_attachment", Mock())
    monkeypatch.setattr(processor, "_determine_next_step", AsyncMock(return_value={"status": "handled", "response": "attachment_added"}))
    result = await processor.process_image_message(user, relevant, {"data": "abc", "filename": "a.png", "mime_type": "image/png", "caption": "remark"})
    assert result["response"] == "attachment_added"
    assert relevant.workflow_state["attachment_caption"] == "remark"

    monkeypatch.setattr(image_processor.AttachmentHelpers, "download_and_encode_attachment", AsyncMock(return_value={"success": False, "error": "download"}))
    result = await processor.process_image_message(user, relevant, {"id": "media", "mime_type": "image/jpeg"})
    assert result["status"] == "error" and result["response"] == "download"


# Logging, selection, and pure parsers ------------------------------------


def test_logging_context_formatter_database_and_decorators(monkeypatch):
    logging_utils.clear_user_phone_context()
    record = logging.LogRecord("module", logging.INFO, __file__, 1, "hello", (), None)
    assert "N/A | module" in logging_utils.CustomFormatter().format(record)
    record.phone_number = "record-phone"
    assert "record-phone" in logging_utils.CustomFormatter().format(record)
    with logging_utils.UserPhoneContext("ctx"):
        assert logging_utils.get_user_phone_context() == "ctx"
        assert "ctx" in logging_utils.CustomFormatter().format(logging.LogRecord("m", logging.INFO, __file__, 1, "x", (), None))
    assert logging_utils.get_user_phone_context() is None
    logging_utils._thread_local.phone_number = "thread"
    assert logging_utils.get_user_phone_context() == "thread"
    logging_utils.clear_user_phone_context()

    logger = MagicMock()
    logging_utils.log_info(logger, "info", user_id="u", phone_number="1", key="v")
    logging_utils.log_error(logger, "error", error=ValueError("bad"), user_id="u")
    logging_utils.log_debug(logger, "debug")
    db = SimpleNamespace(add=Mock(), commit=Mock(), close=Mock())
    monkeypatch.setattr("app.database.get_db_session", lambda: db)
    logging_utils.log_to_database("INFO", "message", service="svc")
    db.commit.assert_called_once()

    @logging_utils.log_service_method("svc")
    def ok(value):
        return value

    @logging_utils.log_service_method("svc")
    async def async_ok(value):
        return value

    assert ok(1) == 1
    assert asyncio.run(async_ok(2)) == 2
    with pytest.raises(ValueError):
        @logging_utils.log_service_method("svc")
        def bad():
            raise ValueError("bad")
        bad()


def test_user_selection_rule_and_ai_failures(monkeypatch, tmp_path):
    service = SimpleNamespace(default_model="model", client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))
    tool = UserSelectionTool(service)
    options = [{"number": 1, "profile": {"email": "buyer@example.com", "role": "buyer"}}, {"number": 2, "action": "Register new", "display": "Register"}]
    assert tool._rule_based_analysis("1", options)["selected_option"] == 1
    assert tool._rule_based_analysis("firstmarketingservices.in", options)["selected_option"] is None
    assert tool._rule_based_analysis("buyer@example.com", options)["selected_option"] == 1
    assert tool._rule_based_analysis("register me as seller", options)["register"]["type"] == "seller"
    (tmp_path / "profile_selection").mkdir()
    (tmp_path / "profile_selection" / "user_selection_analysis.txt").write_text("prompt", encoding="utf-8")
    (tmp_path / "user_selection_analysis.json").write_text("{}", encoding="utf-8")
    tool.prompts_dir = tmp_path
    tool.tools_dir = tmp_path
    service.client.responses.create.return_value = SimpleNamespace(output=[SimpleNamespace(type="function_call", arguments='{"selected_option": 2, "confidence": 0.9, "reasoning": "register", "alternative_matches": [], "requires_clarification": false, "register": {"type": "seller"}}')])
    assert asyncio.run(tool._ai_based_analysis("register", options))["selected_option"] == 2
    service.client.responses.create.side_effect = RuntimeError("ai")
    assert asyncio.run(tool._ai_based_analysis("unclear", options))["confidence"] == 0.1
    assert asyncio.run(tool.analyze_user_selection({"button_reply": {"title": "1"}}, options))["selected_option"] == 1


def test_bfs_parsers_invalid_missing_and_stock_edges():
    assert bfs_parser.parse_bfs_format("")["error"]
    assert bfs_parser.parse_bfs_format("header only")["error"]
    display, missing = bfs_parser.generate_bfs_display_with_missing([{}])
    assert "Please provide" in display and missing == ["Product name"]
    assert bfs_parser.is_bfs_complete([]) is False

    item = {"description": "Laptop", "specification": "16GB", "sellPrice": 100, "availableQuantity": 2}
    generated = bid_parser.generate_bid_format([item])
    assert bid_parser.parse_bid_format("", [item])["error"]
    assert bid_parser.parse_bid_format(generated.replace("a. Your Price:", "a. Your Price: 0").replace("b. Your Qty:", "b. Your Qty: 1"), [item])["error"]
    valid = generated.replace("a. Your Price:", "a. Your Price: 90").replace("b. Your Qty:", "b. Your Qty: 1")
    assert bid_parser.parse_bid_format(valid, [item])["bids"][0]["quantity"] == 1
    too_many = valid.replace("Your Qty: 1", "Your Qty: 9")
    assert "exceeds available" in bid_parser.parse_bid_format(too_many, [item])["error"]
    assert bid_parser.generate_bid_summary([]) == ""
    assert bid_parser.generate_bid_summary([{"key": "x", "price": 1, "quantity": 0}]) == ""


@pytest.mark.asyncio
async def test_seller_matching_standard_rules_path(monkeypatch):
    monkeypatch.setattr(seller_task, "get_sellers_already_notified_for_rfq", lambda _: set())
    monkeypatch.setattr(seller_task, "get_rfq_item_categories", lambda _: ["Tools"])
    monkeypatch.setattr(seller_task, "get_users_active_in_last_24hrs", lambda _: set())
    monkeypatch.setattr(seller_task, "normalize_phone_for_comparison", lambda value: value.lstrip("+"))
    standard = SimpleNamespace(select_sellers_for_rfq=AsyncMock(return_value={"subscribed_sellers": [{"seller_id": "s", "phone_number": "+1", "seller_name": "S"}], "unsubscribed_sellers": []}))
    monkeypatch.setattr(seller_task, "log_selected_sellers_to_remote", lambda *_: True)
    notification = SimpleNamespace(send_rfq_notifications=AsyncMock(return_value={"sent": 1, "failed": 0, "results": [{"seller_id": "s", "success": True}]}))
    monkeypatch.setattr(seller_task, "SellerNotificationService", lambda: notification)
    monkeypatch.setattr(seller_task, "record_rfq_seller_notifications", Mock())
    result = await seller_task.process_single_rfq_matching({"rfq_id": "r", "rfq_uuid": "u", "description": "x"}, standard, set())
    assert result["success"] is True
