from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.conversation_analytics_service as analytics_module
import app.services.daily_aggregation_service as aggregation_module
import app.services.daily_summary_service as summary_module
import app.services.enhanced_auto_categorization_service as categorization_module
import app.services.enhanced_excel_report_service as report_module
import app.services.excel_processing_service as processing_module
import app.services.excel_validation_service as validation_module
import app.services.message_queue_service as queue_module
import app.services.webhook_health_monitor_service as health_module
import app.services.whatsapp_service as whatsapp_module
from app.models import SessionState, UserType


class DbContext:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *args):
        return False


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *args):
        return False


class FakeRedisLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeHttpResponse:
    def __init__(self, status=200, payload=None, error=None):
        self.status = status
        self.status_code = status
        self.payload = payload if payload is not None else {"status": "ok"}
        self.error = error
        self.text = json.dumps(self.payload)

    async def read(self):
        return b"file-bytes"

    def json(self):
        if self.error:
            raise self.error
        return self.payload


class FakeExcelWriter:
    def __init__(self):
        self.book = SimpleNamespace(sheetnames=[])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# Conversation analytics ----------------------------------------------------

def analytics_service():
    service = analytics_module.ConversationAnalyticsService.__new__(analytics_module.ConversationAnalyticsService)
    service.batch_size = 2
    service.openai_service = SimpleNamespace(
        tools_dir=Path("."), default_model="model", _load_prompt=MagicMock(return_value="system"),
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock()))
    )
    return service


def test_conversation_dataframes_extract_prompt_and_safe_values():
    service = analytics_service()
    sessions = [
        {
            "session_id": "b", "phone_number": "+911", "user_type": "buyer", "confidence_score": 90,
            "analysis_reasoning": "buyer", "buyer_identities": [{"buyer_email": "b@example.com", "buyer_metrics": {
                "successful_rfqs_ai": 2, "bfs_search_details": [{"search_keyword": "bolt", "results_found": ["A", "", "B"]}]
            }}], "registration_metrics": {"buyer_successful_registration": 1}
        },
        {"session_id": "s", "phone_number": "922", "user_type": "seller", "seller_identities": [{"seller_email": "s@example.com", "seller_metrics": {"rfq_requested_ai": 3}}]},
        {"session_id": "u", "phone_number": "933", "user_type": "unknown", "unknown_user_metrics": {"number_of_faq_or_general_queries": 4}},
    ]
    buyer, seller, unknown = service._create_dataframes_from_sessions(sessions, "2024-01-01")
    assert list(buyer["buyer_email"]) == ["b@example.com"]
    assert buyer.iloc[0]["successful_rfqs_ai"] == 2
    assert list(seller["seller_email"]) == ["s@example.com"]
    assert unknown.iloc[0]["number_of_faq_or_general_queries"] == 4
    bfs = service._create_bfs_search_dataframe(sessions, "2024-01-01")
    assert bfs.iloc[0]["searched_result"] == "A, B"
    assert service._extract_chat_sequence({"messages": [
        {"role": "user", "content": {"body": {"text": "hello"}}, "timestamp": "t"},
        {"role": "assistant", "content": {"button_reply": {"title": "Yes"}}},
        {"role": "system", "content": "ignored"},
    ]}) == "[t] User: hello\nAssistant: Yes"
    prompt = service._build_batch_prompt([{"session_id": "S1"}], 3, date(2024, 1, 1))
    assert "BATCH NUMBER: 3" in prompt and "S1" in prompt
    assert service._safe_json_value(float("nan")) is None
    assert service._safe_json_value([]) is None


@pytest.mark.asyncio
async def test_conversation_batch_prepare_ai_and_rate_limit_fallback(monkeypatch):
    service = analytics_service()
    sessions = [SimpleNamespace(session_id="S1", external_user_id="U1", created_at=None,
                                 conversation_history={"messages": [{"role": "user", "content": "x" * 600}]})]
    prepared = await service._prepare_batch_session_data(sessions)
    assert prepared[0]["conversation_history"][0]["content"] == "x" * 500
    service.openai_service.client.responses.create.return_value = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", arguments=json.dumps({"sessions": [{"session_id": "S1"}]}))]
    )
    fake_file = MagicMock()
    fake_file.__enter__.return_value = fake_file
    fake_file.__exit__.return_value = False
    fake_file.read.return_value = json.dumps({"name": "tool"})
    monkeypatch.setattr("builtins.open", MagicMock(return_value=fake_file))
    result = await service._analyze_batch_with_ai(prepared, 1, date(2024, 1, 1))
    assert result["sessions"][0]["session_id"] == "S1"
    service._analyze_batch_with_ai = AsyncMock(side_effect=[{"sessions": [{"session_id": "S1"}]}, RuntimeError("bad")])
    result = await service._process_sessions_individually(prepared * 2, 1, date(2024, 1, 1))
    assert result["total_sessions"] == 1
    service._prepare_batch_session_data = AsyncMock(side_effect=RuntimeError("prepare"))
    result = await service._process_sessions_in_batches(sessions, date(2024, 1, 1))
    assert result["total_sessions"] == 0


@pytest.mark.asyncio
async def test_conversation_daily_empty_error_and_date_range(monkeypatch):
    service = analytics_service()
    query = MagicMock()
    query.with_entities.return_value = query
    query.filter.return_value = query
    query.all.return_value = []
    db = MagicMock(query=MagicMock(return_value=query))
    monkeypatch.setattr(analytics_module, "get_db_session", lambda: DbContext(db))
    result = await service.analyze_daily_conversations(date(2024, 1, 1))
    assert result["success"] and result["total_sessions"] == 0 and result["buyer_df"].empty
    query.all.side_effect = RuntimeError("db down")
    result = await service.analyze_daily_conversations(date(2024, 1, 1))
    assert result["success"] is False and "db down" in result["error"]
    service.analyze_daily_conversations = AsyncMock(side_effect=[{"success": True}, {"success": False}])
    result = await service.analyze_date_range(date(2024, 1, 1), date(2024, 1, 2))
    assert result["processed_dates"] == 2


# Daily aggregation and summary --------------------------------------------

def session(**overrides):
    values = dict(
        user_type=UserType.buyer, external_user_id="U1", rfq_ids=["R1", None], rfq_id=None,
        product_items=[{"category": "Tools"}, {"category": "Tools"}], products_bid_for=["p1"],
        bids_accepted={"count": 1}, rfqs_with_response=["R1", "R1"], bfs_search_count=2,
        bfs_price_accepted=["p1"], counter_offers_accepted={"count": 1}, counter_offers_made=["offer"],
        seller_responses=["response"], extracted_entities={"description": "Drill"},
        parent_session_id=None, session_state=SessionState.completed,
        created_at=datetime(2024, 1, 1), completed_at=datetime(2024, 1, 1, 1), last_activity_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_daily_aggregation_metric_normal_empty_and_rolling_helpers():
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    buyer = service._calculate_buyer_summary_metrics([session(), session(user_type=UserType.seller, external_user_id="S")], date(2024, 1, 1))
    assert buyer["chats_initiated_by_buyers"] == 1 and buyer["total_rfqs_submitted"] == 1
    assert buyer["bfs_products_searched"] == 2 and buyer["bids_accepted_by_buyers"] == 1
    seller = service._calculate_seller_summary_metrics([session(user_type=UserType.seller)], date(2024, 1, 1))
    assert seller["seller_chats_initiated"] == 1 and seller["counter_offers_accepted"] == 1
    categories = service._calculate_category_summary_metrics([session()], date(2024, 1, 1))
    assert categories["Tools"]["rfqs_uploaded"] == 1
    assert set(service._extract_categories_from_session(session(extracted_entities=[{"category": "Chem"}, "bad"]))) == {"Tools", "Chem"}
    assert service._aggregate_buyer_rolling_metrics([]) == {}
    totals = service._aggregate_buyer_rolling_metrics([{"chats_initiated_by_buyers": 2, "total_rfqs_submitted": 2, "avg_products_per_rfq": 1.5, "avg_categories_per_rfq": 1}])
    assert totals["chats_initiated_by_buyers"] == 2 and totals["avg_products_per_rfq"] == 1.5
    assert service._aggregate_seller_rolling_metrics([]) == {}
    assert service._aggregate_category_rolling_metrics([{"Tools": {"rfqs_uploaded": 2}}])["Tools"]["rfqs_uploaded"] == 2


def test_daily_aggregation_store_and_run_empty_or_exception(monkeypatch):
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = []
    db = MagicMock(query=MagicMock(return_value=query))
    monkeypatch.setattr(aggregation_module, "get_db_session", lambda: DbContext(db))
    assert service.run_daily_aggregation(date(2024, 1, 1)) is True
    query.all.side_effect = RuntimeError("query")
    assert service.run_daily_aggregation(date(2024, 1, 1)) is False
    query.all.side_effect = None
    existing = SimpleNamespace(metric_data={}, is_complete=False)
    q2 = MagicMock()
    q2.filter.return_value = q2
    q2.first.return_value = existing
    db2 = MagicMock(query=MagicMock(return_value=q2))
    service._store_metrics(db2, date(2024, 1, 1), "buyer_summary", {"x": 1})
    assert existing.metric_data == {"x": 1} and existing.is_complete is True
    q2.first.return_value = None
    service._store_metrics(db2, date(2024, 1, 1), "seller_summary", {"x": 2})
    db2.add.assert_called_once()


@pytest.mark.asyncio
async def test_daily_summary_disabled_empty_create_update_get_and_duration(monkeypatch):
    service = summary_module.DailySummaryService.__new__(summary_module.DailySummaryService)
    service.settings = SimpleNamespace(enable_daily_summarization=True)
    query = MagicMock()
    query.filter.return_value = query
    query.all.side_effect = [[session()], []]
    query.first.return_value = None
    db = MagicMock(query=MagicMock(return_value=query))
    monkeypatch.setattr(summary_module, "get_db_session", lambda: DbContext(db))
    result = await service.generate_daily_summary("U1", date(2024, 1, 1))
    assert result.sessions_count == 1 and result.rfqs_created_count == 1
    assert result.avg_session_duration == 60 and "Drill" in result.primary_product_categories
    assert await service.generate_daily_summary("U1", date(2024, 1, 1)) is None
    service.settings.enable_daily_summarization = False
    assert await service.generate_daily_summary("U1", date(2024, 1, 1)) is None
    service.settings.enable_daily_summarization = True
    query.all.side_effect = None
    query.all.return_value = [session()]
    existing = SimpleNamespace(user_type=None, session_states_breakdown=None, primary_product_categories=None)
    query.first.return_value = existing
    result = await service.generate_daily_summary("U1", date(2024, 1, 1))
    assert result is existing and db.refresh.called
    summary = SimpleNamespace(date=date(2024, 1, 1), user_type=UserType.buyer, sessions_count=1,
                              rfqs_created_count=2, session_states_breakdown=None, seller_interaction_count=0,
                              avg_session_duration=1, primary_product_categories=None, product_items_count=1,
                              unique_categories_count=1, session_continuation_count=0, total_rfq_value_estimate=2.5)
    query.first.return_value = summary
    found = await service.get_user_daily_summary("U1", date(2024, 1, 1))
    assert found["user_type"] == "buyer" and found["total_rfq_value_estimate"] == 2.5
    assert service._calculate_duration(session(created_at=datetime(2024, 1, 1), completed_at=None, last_activity_at=datetime(2024, 1, 1, 0, 30))) == 30
    assert service._calculate_duration(session(created_at="bad", completed_at="bad", last_activity_at=None)) is None


# Excel validation and processing -------------------------------------------

def test_excel_validation_primitives_and_types():
    service = validation_module.ExcelValidationService()
    assert service._validate_file_extension("data.XLSX")
    assert not service._validate_file_extension("data.csv")
    assert service._validate_file_integrity(b"short")["valid"] is False
    assert service._validate_file_integrity(b"\0" * 8)["error_type"] == "corrupted_file"
    assert service._validate_excel_content(b"a,b\nc,d")["error_type"] == "csv_format_detected"
    assert service._validate_excel_content(b"PK\x03\x04data")["format"] == "xlsx"
    assert service._validate_data_types(pd.DataFrame({"Quantity": [1, "bad"]}))
    assert not service._validate_data_types(pd.DataFrame({"Unnamed: 0": [1, "bad"]}))


@pytest.mark.asyncio
async def test_excel_validation_download_retry_and_pipeline(monkeypatch):
    service = validation_module.ExcelValidationService()
    service._download_file_with_retry = AsyncMock(return_value=b"valid")
    service._validate_file_integrity = MagicMock(return_value={"valid": True})
    service._validate_excel_content = MagicMock(return_value={"valid": True, "format": "xlsx"})
    service._validate_excel_readability = AsyncMock(return_value={"valid": True})
    service._validate_excel_structure_comprehensive = AsyncMock(return_value={"valid": True})
    service._validate_data_quality = AsyncMock(return_value={"valid": True})
    result = await service.validate_excel_file_from_url("url", "a.xlsx")
    assert result["valid"] and result["validation_summary"]["structure_check"] == "passed"
    result = await service.validate_excel_file_from_url("url", "a.xlsx", skip_content_validation=True)
    assert result["validation_summary"]["data_quality_check"] == "skipped"
    assert (await service.validate_excel_file_from_url("url", "a.txt"))["error_type"] == "invalid_extension"
    service._download_file_with_retry.return_value = None
    assert (await service.validate_excel_file_from_url("url", "a.xlsx"))["error_type"] == "download_failed"
    service._download_file_with_retry.return_value = b"valid"
    service._validate_file_integrity.return_value = {"valid": False, "error_type": "bad"}
    assert (await service.validate_excel_file_from_url("url", "a.xlsx"))["error_type"] == "bad"
    service._validate_file_integrity.side_effect = RuntimeError("unexpected")
    assert (await service.validate_excel_file_from_url("url", "a.xlsx"))["error_type"] == "validation_error"


@pytest.mark.asyncio
async def test_excel_validation_readability_structure_and_quality_branches(monkeypatch):
    service = validation_module.ExcelValidationService()
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(side_effect=ValueError("password protected")))
    result = await service._validate_excel_readability(b"x", "a.xlsx")
    assert result["error_type"] == "password_protected"
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"A": ["x"], "B": ["y"]})))
    assert (await service._validate_data_quality(b"x"))["valid"] is True
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(return_value=pd.DataFrame({"Quantity": ["@bad"]})))
    result = await service._validate_data_quality(b"x")
    assert result["valid"] is False
    monkeypatch.setattr(validation_module, "load_workbook", MagicMock(side_effect=RuntimeError("invalid")))
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(side_effect=RuntimeError("pandas invalid")))
    assert (await service._validate_excel_structure_comprehensive(b"x"))["valid"] is False


@pytest.mark.asyncio
async def test_excel_processing_pipeline_helpers_and_rejections(monkeypatch):
    service = processing_module.ExcelProcessingService.__new__(processing_module.ExcelProcessingService)
    service.target_columns = ["S.No", "ItemDescription", "Specification", "Uom", "Quantity", "Remarks"]
    service.openai_service = SimpleNamespace(process_excel_to_rfqs=AsyncMock())
    assert service._create_fallback_mapping(["Quantity", "Other"]) == {"Quantity": "Quantity"}
    extracted = service._extract_items_with_mapping(
        pd.DataFrame([["bolt", "M10", 2], ["missing", "", 1]], columns=["ItemDescription", "Specification", "Quantity"]),
        ["ItemDescription", "Specification", "Quantity"], {"ItemDescription": "ItemDescription", "Specification": "Specification", "Quantity": "Quantity"})
    assert extracted["success"] and len(extracted["items"]) == 1 and extracted["removed_rows"] == 1
    assert service._normalize_quantity("1,200 kg") == 1200.0
    assert service._normalize_quantity("bad") is None
    assert service._detect_regional_formats([{"Quantity": "1,5"}])["decimal_separator"] == ","
    assert service._validate_business_rules([{"ItemDescription": "x", "Quantity": 0, "Uom": "each"}])["valid"] is False
    assert service._validate_items_comprehensive([{"ItemDescription": "x", "Specification": "s", "Uom": "Units", "Quantity": 2}])["valid"]
    cleaned = service._preprocess_excel_data(pd.DataFrame([[1, 2, 2], [1, 2, 2], [None, None, None]], columns=["A", "A.1", "Unnamed: 2"]))
    assert len(cleaned) == 1
    encoded = service.encode_for_api(b"abc", "x.xlsx")
    assert encoded["boqFileName"] == "x.xlsx"
    assert service._format_date_for_display("2024-01-02") == "02 January"
    assert service._validate_rfqs_consistency([{"city": "A"}, {"city": "A"}])["valid"]
    assert service._validate_rfqs_consistency([{"deliveryDate": "2024-01-01"}, {"deliveryDate": "2024-01-02"}])["valid"] is False

    service._is_valid_excel_file = MagicMock(return_value=True)
    service._validate_excel_structure = AsyncMock(return_value={"valid": True})
    monkeypatch.setattr(processing_module.pd, "ExcelFile", MagicMock(return_value=SimpleNamespace(sheet_names=["Sheet1"], close=MagicMock())))
    monkeypatch.setattr(processing_module.pd, "read_excel", MagicMock(return_value=pd.DataFrame([["x", "s", 2]], columns=["ItemDescription", "Specification", "Quantity"])))
    service._process_excel_with_openai = AsyncMock(return_value={"success": True, "rfqs": [{"products": [{"description": "x", "quantity": 2}]}], "processing_summary": {"total_products": 1, "total_products_extracted": 1}})
    result = await service.process_excel_file(b"bytes", "x.xlsx")
    assert result["success"] and result["items"][0]["ItemDescription"] == "x"
    service._process_excel_with_openai.return_value = {"success": True, "rfqs": [], "processing_summary": {"total_products": 1, "skipped_rows": 1}}
    result = await service.process_excel_file(b"bytes", "x.xlsx")
    assert result["success"] is False and result["should_skip_rfq_creation"] is True
    assert (await service.process_excel_file(b"x" * (3 * 1024 * 1024 + 1), "x.xlsx"))["success"] is False
    service._is_valid_excel_file.return_value = False
    assert (await service.process_excel_file(b"bytes", "x.xlsx"))["success"] is False


# WhatsApp and queue --------------------------------------------------------

def whatsapp_service():
    service = whatsapp_module.WhatsAppService.__new__(whatsapp_module.WhatsAppService)
    service.mock_mode = True
    service.username = "u"
    service.password = "p"
    service.from_number = "f"
    service.base_url = "http://wa"
    service.template_base_url = "http://templates"
    service.retry_service = SimpleNamespace(retry_with_backoff=AsyncMock())
    return service


@pytest.mark.asyncio
async def test_whatsapp_mock_send_template_tracking_and_formats(monkeypatch):
    service = whatsapp_service()
    service.retry_service.retry_with_backoff.return_value = {"success": True, "result": whatsapp_module.MessageResponse(True, "m"), "attempts": 1}
    service._clear_pending_reply_flag = AsyncMock()
    service._track_message_in_history = AsyncMock()
    result = await service.send_message("9999999999", "hello", session_id="S1")
    assert result.success and service._track_message_in_history.await_count == 1
    await service.send_template_message("9999999999", "welcome", ["A"])
    assert service._clear_pending_reply_flag.await_count == 2
    assert service.format_vendor_results([]).startswith("No vendors")
    assert "and 1 more vendors" in service.format_vendor_results([{"name": str(i)} for i in range(6)])
    assert service.format_bfs_results([]).startswith("No products")
    assert "RFQ Summary" in service.format_rfq_summary({"product_name": "bolt", "delivery_city": "Pune", "delivery_state": "MH"})
    assert service._format_phone_number("+91 (99999) 99999") == "919999999999"
    assert service._format_phone_number("12") == ""


@pytest.mark.asyncio
async def test_whatsapp_interactive_buttons_lists_http_and_response_shapes(monkeypatch):
    service = whatsapp_service()
    service.mock_mode = False
    service._clear_pending_reply_flag = AsyncMock()
    service._track_message_in_history = AsyncMock()
    fake_cache = MagicMock(get=AsyncMock(return_value=None), set=AsyncMock())
    monkeypatch.setattr(whatsapp_module, "get_redis_service", lambda: fake_cache)
    service._format_phone_number = MagicMock(return_value="919999999999")
    response = FakeHttpResponse(200, [{"mid": "M1"}])
    monkeypatch.setattr(whatsapp_module.requests, "post", MagicMock(return_value=response))
    result = await service.send_interactive_message("x", "button", {"body": {"text": "hi"}})
    assert result.success and result.message_id == "M1"
    service.send_interactive_message = AsyncMock(return_value=whatsapp_module.MessageResponse(True))
    result = await service.send_configurable_buttons("x", "body\x00", [{"id": "1", "title": "One"}, {"id": "2", "title": "Two"}], session_id="S")
    assert result.success and service._track_message_in_history.await_args.args[2] == "interactive_button"
    assert (await service.send_configurable_buttons("x", "body", [])).success is False
    assert (await service.send_configurable_buttons("x", "body", [{"id": "1"}])).success is False
    await service.send_list_message("x", "h", "b", [{"title": str(i)} for i in range(11)])
    assert service.send_interactive_message.await_args.args[1] == "list"
    assert len(service.send_interactive_message.await_args.args[2]["action"]["sections"]) == 2
    assert service._handle_api_response(FakeHttpResponse(200, {"mid": 3})).message_id == "3"
    assert service._handle_api_response(FakeHttpResponse(500, {"Error": "down"})).error == "API error: 500 - down"
    assert service._handle_api_response(FakeHttpResponse(200, error=ValueError("json"))).success is False


def queue_service():
    service = queue_module.MessageQueueService.__new__(queue_module.MessageQueueService)
    service.batch_window = 3
    service.please_wait_threshold = 15
    service.max_please_wait_count = 3
    service.monitoring_poll_interval = 1
    service.response_ready_ttl = 60
    service.monitor_lock_ttl = 180
    service.please_wait_interval_ttl = 600
    service._background_tasks = []
    service._running = True
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(success=True)))
    service.redis = MagicMock()
    service.redis.set = AsyncMock(return_value=True)
    service.redis.zadd = AsyncMock()
    service.redis.exists = AsyncMock(return_value=False)
    service.redis.setex = AsyncMock()
    service.redis.zrange = AsyncMock(return_value=[])
    service.redis.zrem = AsyncMock()
    service.redis.rpush = AsyncMock()
    service.redis.lpop = AsyncMock(return_value=None)
    service.redis.lpush = AsyncMock()
    service.redis.scan = AsyncMock(return_value=(0, []))
    service.redis.get = AsyncMock(return_value=None)
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service.redis.delete = AsyncMock()
    service.redis.close = AsyncMock()
    return service


@pytest.mark.asyncio
async def test_message_queue_enqueue_batch_wrapper_status_health_and_shutdown(monkeypatch):
    service = queue_service()
    service.redis.zadd = AsyncMock(); service.redis.exists = AsyncMock(return_value=False)
    service._create_batch = AsyncMock()
    await service.enqueue_message({"from": "+123", "timestamp": "2024-01-01 00:00:00", "text": {"body": "hello"}, "message_id": "M"})
    service.redis.zadd.assert_awaited_once()
    service._create_batch.assert_not_awaited()
    service.redis.exists.return_value = True
    service._refresh_batch_timer = AsyncMock()
    await service.enqueue_message({"from": "123", "content": "next", "timestamp": 2})
    service._refresh_batch_timer.assert_awaited_once_with("123")
    assert queue_module.Message.from_dict(queue_module.Message("m", "u", "c", "text", 1, {}).to_dict()).content == "c"
    service._create_batch = queue_module.MessageQueueService._create_batch.__get__(service)
    service.redis.lock.return_value = FakeRedisLock()
    service.redis.zrange = AsyncMock(return_value=[json.dumps(queue_module.Message("m", "1", "Hello", "text", 1, {}).to_dict()), json.dumps(queue_module.Message("m2", "1", "hello", "text", 2, {}).to_dict())])
    service.redis.zrem = AsyncMock(); service.redis.rpush = AsyncMock(); service.redis.exists.return_value = False
    service._try_start_processing = AsyncMock()
    monkeypatch.setattr(queue_module.time, "time", lambda: 1)
    await service._create_batch("1")
    batch = queue_module.Batch.from_dict(json.loads(service.redis.rpush.await_args.args[1]))
    assert batch.concatenated_content == "Hello\nhello" and batch.message_count == 2
    service.redis.zcard = AsyncMock(return_value=0); service.redis.llen = AsyncMock(return_value=0)
    service.redis.get = AsyncMock(return_value=None); service.redis.exists = AsyncMock(return_value=False)
    assert (await service.get_queue_status("1"))["incoming_queue_size"] == 0
    service.redis.scan = AsyncMock(return_value=(0, []))
    metrics = await service.get_health_metrics()
    assert metrics["active_users"] == 0
    service.redis.close = AsyncMock()
    await service.shutdown()
    assert service._running is False and service.redis.close.await_count == 1


@pytest.mark.asyncio
async def test_message_queue_wrapper_suppression_and_dataclass_error_paths():
    service = queue_service()
    user = "123"
    batch = queue_module.Batch("B", user, "hi", "text", 1, 1)
    service.redis.get = AsyncMock(return_value=queue_module.ProcessingSession("B", 1).to_json())
    service.redis.zcard = AsyncMock(return_value=0); service.redis.llen = AsyncMock(return_value=0)
    service.redis.setex = AsyncMock(); service._cleanup_and_next = AsyncMock()
    service._should_suppress_response = AsyncMock(return_value=False)
    result = await service.send_message(user, "answer")
    assert result.success and service.whatsapp_service.send_message.await_count == 1
    service._should_suppress_response.return_value = True
    service._cleanup_and_next.reset_mock()
    result = await service.send_message(user, "new answer")
    assert result.success and service._cleanup_and_next.await_count == 1
    service.redis.get.return_value = None
    service.whatsapp_service.send_message.reset_mock()
    await service.send_message(user, "direct")
    service.whatsapp_service.send_message.assert_awaited_once()
    with pytest.raises(ValueError):
        await service.send_message("")
    assert queue_module.ProcessingSession.from_json(queue_module.ProcessingSession("B", 1).to_json()).batch_id == "B"


# Webhook health monitor ----------------------------------------------------

def health_service():
    service = health_module.WebhookHealthMonitorService.__new__(health_module.WebhookHealthMonitorService)
    service.settings = SimpleNamespace(WHATSAPP_BASE_URL="http://wa", WHATSAPP_USERNAME="u", WHATSAPP_PASSWORD="p",
                                       webhook_alert_state_ttl_seconds=60, webhook_health_monitoring_enabled=True)
    service.response_threshold = 1
    service.api_timeout = 3
    service.grace_period = 0
    service.recovery_confirmations = 2
    service.warning_threshold = 2
    service.alert_recipients = []
    service.redis = MagicMock()
    service.email_service = MagicMock()
    service.worker_id = "worker"
    service.is_leader = False
    service._running = False
    service._session = None
    return service


@pytest.mark.asyncio
async def test_webhook_health_http_state_history_and_leader_paths(monkeypatch):
    service = health_service()
    response = FakeHttpResponse(200)
    session_obj = MagicMock()
    session_obj.closed = False
    session_obj.post.return_value = AsyncContext(response)
    service._session = session_obj
    status, latency, error = await service._check_api_health()
    assert status == health_module.HealthStatus.OK and error is None
    session_obj.post.return_value = AsyncContext(FakeHttpResponse(500))
    assert (await service._check_api_health())[0] == health_module.HealthStatus.CRITICAL
    session_obj.post.return_value = AsyncContext(error=asyncio.TimeoutError())
    assert (await service._check_api_health())[0] == health_module.HealthStatus.CRITICAL
    service.redis.init_client = AsyncMock(); service.redis.client = MagicMock()
    service.redis.client.set = AsyncMock(return_value=True)
    assert await service._try_acquire_leader_lock()
    service.redis.client.set.return_value = False; service.redis.get = AsyncMock(return_value="other")
    assert await service._try_acquire_leader_lock() is False
    state = await service._get_state()
    assert state["current_state"] == "HEALTHY"
    service.redis.get.return_value = json.dumps([{"old": i} for i in range(100)])
    service.redis.set = AsyncMock()
    await service._add_to_history(health_module.HealthStatus.OK, 1.2, None)
    history = json.loads(service.redis.set.await_args.args[1])
    assert len(history) == 100 and history[-1]["status"] == "OK"
    state = {"current_state": "HEALTHY", "is_alerting": False, "last_alert_time": None, "failure_start_time": None,
             "consecutive_failures": 0, "consecutive_successes": 0, "consecutive_warnings": 0, "last_severity": "OK",
             "last_check_time": None, "last_latency_ms": None, "last_error": None}
    service._save_state = AsyncMock(); service._send_critical_alert = AsyncMock()
    await service._process_check_result(state, health_module.HealthStatus.CRITICAL, 2, "down")
    assert state["current_state"] == "FAILING"
    state["failure_start_time"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    await service._process_check_result(state, health_module.HealthStatus.CRITICAL, 2, "down")
    assert state["current_state"] == "ALERTING" and service._send_critical_alert.await_count == 1
    assert health_module.WebhookHealthMonitorService._format_duration(timedelta(hours=1, minutes=2, seconds=3)) == "1h 2m 3s"


# Enhanced report and categorization ---------------------------------------

def test_report_helpers_and_metric_queries(monkeypatch):
    service = report_module.EnhancedExcelReportService.__new__(report_module.EnhancedExcelReportService)
    assert service.sanitize_for_excel("a\x00b\x7fc") == "abc"
    assert service.sanitize_for_excel(None) == ""
    assert service._get_empty_metrics()["total_rfqs"] == 0
    assert service._get_metric_value("Total RFQs Submitted", {"total_rfqs": 3}) == 3
    assert service._get_metric_value("unknown", {}) == 0
    assert service._get_seller_metric_value("RFQs Requested", {"rfqs_requested": 2}) == 2
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=pd.DataFrame([["Seller Chats Initiated", 4]], columns=["metric_name", "total_value"])))
    result = service._calculate_seller_metrics(SimpleNamespace(bind="db"), date(2024, 1, 1), date(2024, 1, 2))
    assert result["seller_chats_initiated"] == 4
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(side_effect=RuntimeError("db")))
    assert service._calculate_seller_metrics(SimpleNamespace(bind="db"), date(2024, 1, 1), date(2024, 1, 2))["rfqs_requested"] == 0
    assert service._generate_category_details_fallback(SimpleNamespace(bind="db"), date(2024, 1, 1), date(2024, 1, 2)).shape[0] == 3


def test_report_generation_uses_mocked_sheets(monkeypatch):
    service = report_module.EnhancedExcelReportService.__new__(report_module.EnhancedExcelReportService)
    writer = FakeExcelWriter()
    monkeypatch.setattr(report_module.pd, "ExcelWriter", MagicMock(return_value=writer))
    methods = ["_generate_buyer_details_sheet", "_generate_seller_details_sheet", "_generate_category_details_sheet",
               "_generate_buyer_summary_sheet", "_generate_seller_summary_sheet", "_generate_category_summary_sheet",
               "_generate_aggregate_sheet_90d", "_format_excel_sheets"]
    for name in methods:
        setattr(service, name, MagicMock())
    assert service.generate_report(date(2024, 1, 1), "report.xlsx") == "report.xlsx"
    assert all(getattr(service, name).called for name in methods)


def categorization_service():
    service = categorization_module.EnhancedAutoCategorizationService.__new__(categorization_module.EnhancedAutoCategorizationService)
    service.collection = MagicMock(); service.category_collection = MagicMock()
    service.fallback_service = MagicMock(); service.openai_service = MagicMock()
    return service


def test_categorization_helpers_search_and_health(monkeypatch):
    service = categorization_service()
    assert service._build_enhanced_description("Battery", {"level_3_category": "Battery", "level_2_category": "Power"}) == "Battery Power"
    service.category_collection.count.return_value = 0
    assert service._search_by_category_name("x")["success"] is False
    service.category_collection.count.return_value = 1
    service.category_collection.query.return_value = {"documents": [["Power"]], "metadatas": [[{"item_count": 2}]], "distances": [[0.2]]}
    assert service._search_by_category_name("x")["best_match"]["category_name"] == "Power"
    assert service._hybrid_category_selection("Power", .8, {"success": True, "matches": [{"category_name": "Power", "similarity": .8}]})["agreement"]
    service.collection.count.return_value = 1
    service.collection.query.return_value = {"documents": [["x"]], "metadatas": [[{"client_category_name": "Tools", "level_1_category": "L1", "level_2_category": "L2", "level_3_category": "L3", "category_path": "L1/L2/L3", "confidence_score": .9, "item_description": "x"}]], "distances": [[.2]]}
    assert service.get_category_suggestions("x")[0]["client_category"] == "Tools"
    service.fallback_service.get_collection_stats.return_value = {}
    service.chroma_path = "mock"
    assert service.health_check()["overall_status"] == "healthy"
    service.collection.count.side_effect = RuntimeError("chroma")
    assert service.health_check()["overall_status"] == "unhealthy"


@pytest.mark.asyncio
async def test_categorization_high_similarity_no_match_openai_and_exception(monkeypatch):
    service = categorization_service()
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._search_hierarchical_levels = MagicMock(return_value={"success": True, "similarity_score": .95,
        "best_match": {"client_category_name": "Tools"}, "all_level_matches": [{"metadata": {"item_description": "bolt", "client_category_name": "Tools"}, "matched_level": "level_3", "similarity_score": .95}]})
    service._log_categorization = MagicMock()
    result = await service.categorize_item("bolt", "U")
    assert result["method"] == "enhanced_taxonomy_high_similarity"
    service._search_hierarchical_levels.return_value = {"success": False}
    service.fallback_service._get_similar_items.return_value = []
    service._log_fallback_categorization = MagicMock()
    result = await service.categorize_item("unknown", "U")
    assert result["client_category"] == "Other" and result["requires_review"]
    service.fallback_service._get_similar_items.return_value = [{"category": "Tools", "similarity_score": .7}]
    service.openai_service.categorize_with_similar_items = AsyncMock(return_value={"success": True, "category": "Tools", "confidence": .8})
    service._update_learning_taxonomy = AsyncMock()
    result = await service.categorize_item("bolt", "U")
    assert result["method"] == "enhanced_fallback_openai" and service._update_learning_taxonomy.await_count == 1
    service._keyword_lookup_source_of_truth.side_effect = RuntimeError("bad")
    result = await service.categorize_item("bad", "U")
    assert result["success"] is False and result["method"] == "enhanced_error"
