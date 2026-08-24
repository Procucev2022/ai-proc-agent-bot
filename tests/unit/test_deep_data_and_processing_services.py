from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import app.services.auto_categorization_service as auto_module
import app.services.conversation_analytics_service as analytics_module
import app.services.daily_aggregation_service as aggregation_module
import app.services.daily_summary_service as summary_module
import app.services.enhanced_auto_categorization_service as enhanced_category_module
import app.services.enhanced_excel_report_service as report_module
import app.services.enhanced_seller_matching_service as matching_module
import app.services.excel_processing_service as processing_module
import app.services.excel_validation_service as validation_module
import app.services.learning_categorization_service as learning_module
import app.services.message_queue_service as queue_module
import app.services.rfq_background_service as background_module
import app.services.rfq_intimation_service as intimation_module
import app.services.seller_notification_service as notification_module
import app.services.webhook_health_monitor_service as health_module
import app.services.whatsapp_service as whatsapp_module
from app.models import RFQStatus, UserType


class Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_):
        return False


class AsyncContext:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.value

    async def __aexit__(self, *_):
        return False


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.status_code = status
        self.payload = payload if payload is not None else {"mid": "m1"}

    def json(self):
        return self.payload


# Conversation analytics, daily aggregation, and daily summaries ---------

@pytest.mark.asyncio
async def test_conversation_analytics_empty_event_remote_fallback_and_batch_gap(monkeypatch):
    service = analytics_module.ConversationAnalyticsService.__new__(analytics_module.ConversationAnalyticsService)
    service.batch_size = 1
    service.settings = SimpleNamespace(procucev_db_name="remote")
    assert service._create_seller_rfq_interest_event_df([], "2024-01-01").empty
    events = service._create_seller_rfq_interest_event_df([
        {"session_id": "s", "phone_number": "1", "seller_rfq_interest_event": [{"rfq_id": "r", "seller_id": "v"}]}
    ], "2024-01-01")
    assert events.iloc[0]["response_date"] == "2024-01-01"
    monkeypatch.setattr(analytics_module, "get_remote_db_session", MagicMock(side_effect=RuntimeError("remote down")))
    assert service._query_remote_users(date(2024, 1, 1)) == []
    assert service._query_remote_seller_rfqs(date(2024, 1, 1)).empty
    service._prepare_batch_session_data = AsyncMock(return_value=[{"session_id": "s"}])
    service._analyze_batch_with_ai = AsyncMock(return_value=None)
    result = await service._process_sessions_in_batches([SimpleNamespace()], date(2024, 1, 1))
    assert result["total_sessions"] == 0


def test_daily_aggregation_rolling_window_existing_and_empty(monkeypatch):
    service = aggregation_module.DailyAggregationService.__new__(aggregation_module.DailyAggregationService)
    existing = SimpleNamespace(buyer_metrics={}, seller_metrics={}, category_metrics={}, last_updated=None)
    query = MagicMock()
    query.filter.return_value = query
    query.all.return_value = []
    query.first.return_value = existing
    db = MagicMock(query=MagicMock(return_value=query))
    db.commit = MagicMock()
    monkeypatch.setattr(aggregation_module, "get_db_session", lambda: Context(db))
    service._calculate_rolling_window(date(2024, 1, 1), "7day", 7)
    assert existing.buyer_metrics == {} and db.commit.called
    service._update_rolling_windows = MagicMock(side_effect=RuntimeError("rolling"))
    with pytest.raises(RuntimeError, match="rolling"):
        service._update_rolling_windows(date(2024, 1, 1))


@pytest.mark.asyncio
async def test_daily_summary_handles_db_error_and_duration_fallback(monkeypatch):
    service = summary_module.DailySummaryService.__new__(summary_module.DailySummaryService)
    service.settings = SimpleNamespace(enable_daily_summarization=True)
    db = MagicMock()
    query = MagicMock()
    query.filter.return_value = query
    query.all.side_effect = RuntimeError("summary db")
    db.query.return_value = query
    monkeypatch.setattr(summary_module, "get_db_session", lambda: Context(db))
    assert await service.generate_daily_summary("u", date(2024, 1, 1)) is None
    bad = SimpleNamespace(created_at=datetime(2024, 1, 1), completed_at="bad", last_activity_at=None)
    assert service._calculate_duration(bad) is None


# Excel reporting, processing, and validation -----------------------------

def test_enhanced_excel_report_metric_fallbacks_and_rolling_lookup(monkeypatch):
    service = report_module.EnhancedExcelReportService.__new__(report_module.EnhancedExcelReportService)
    assert service._get_rolling_window_metrics(SimpleNamespace(bind="db"), date(2024, 1, 1), 2) is None
    monkeypatch.setattr(report_module.pd, "read_sql", MagicMock(return_value=pd.DataFrame()))
    assert service._get_rolling_window_metrics(SimpleNamespace(bind="db"), date(2024, 1, 1), 7) is None
    assert service._get_empty_metrics()["total_rfqs"] == 0
    assert service._get_metric_value("missing", {}) == 0


@pytest.mark.asyncio
async def test_enhanced_categorization_keyword_and_ai_error_fallback(monkeypatch):
    service = enhanced_category_module.EnhancedAutoCategorizationService.__new__(enhanced_category_module.EnhancedAutoCategorizationService)
    service.collection = MagicMock()
    service.category_collection = MagicMock()
    service.fallback_service = MagicMock()
    service.openai_service = MagicMock()
    monkeypatch.setattr(enhanced_category_module, "execute_remote_query", MagicMock(side_effect=RuntimeError("sql")))
    assert service._keyword_lookup_source_of_truth("bolt")["success"] is False
    service._keyword_lookup_source_of_truth = MagicMock(return_value={"success": False})
    service._search_hierarchical_levels = MagicMock(return_value={"success": False})
    service.fallback_service._get_similar_items.return_value = [{"category": "Tools", "similarity_score": .7}]
    service.openai_service.categorize_with_similar_items = AsyncMock(side_effect=RuntimeError("ai"))
    result = await service.categorize_item("bolt", "u")
    assert result["success"] is False and result["method"] == "enhanced_error"


@pytest.mark.asyncio
async def test_excel_processing_openai_failure_structure_and_consistency_branches(monkeypatch):
    service = processing_module.ExcelProcessingService.__new__(processing_module.ExcelProcessingService)
    service.target_columns = ["S.No", "ItemDescription", "Specification", "Uom", "Quantity", "Remarks"]
    service.openai_service = SimpleNamespace(process_excel_to_rfqs=AsyncMock(return_value={"success": False, "error": "bad rows"}))
    result = await service._process_excel_with_openai(pd.DataFrame([["x"]]), "x.xlsx")
    assert result == {"success": False, "error": "bad rows"}
    service.openai_service.process_excel_to_rfqs.side_effect = RuntimeError("openai")
    result = await service._process_excel_with_openai(pd.DataFrame([["x"]]), "x.xlsx")
    assert "openai" in result["error"]
    assert service._validate_rfqs_consistency([])["valid"] is True
    assert service._validate_rfqs_consistency([{"city": "A"}, {"city": "B"}])["valid"] is False
    monkeypatch.setattr(service, "_is_valid_excel_file", MagicMock(return_value=True))
    monkeypatch.setattr(service, "_validate_excel_structure", AsyncMock(return_value={"valid": False, "error": "merged"}))
    result = await service.process_excel_file(b"bytes", "x.xlsx")
    assert result == {"success": False, "error": "merged"}


@pytest.mark.asyncio
async def test_excel_validation_retry_statuses_and_readability_error(monkeypatch):
    service = validation_module.ExcelValidationService()

    class Session:
        def __init__(self, response):
            self.response = response

        def get(self, _):
            return AsyncContext(self.response)

    response = SimpleNamespace(status=500, read=AsyncMock(return_value=b"x"))
    monkeypatch.setattr(validation_module.aiohttp, "ClientSession", lambda **_: Session(response))
    assert await service._download_file_with_retry("url", max_retries=2) is None
    monkeypatch.setattr(validation_module.pd, "read_excel", MagicMock(side_effect=RuntimeError("bad read")))
    monkeypatch.setattr(validation_module, "load_workbook", MagicMock(side_effect=RuntimeError("bad workbook")))
    monkeypatch.setattr(validation_module.xlrd, "open_workbook", MagicMock(side_effect=RuntimeError("bad xlrd")))
    result = await service._validate_excel_readability(b"bytes", "x.xlsx")
    assert result["valid"] is False
    assert result["error_type"] in {"unreadable_file", "readability_error"}


# Queue and WhatsApp -------------------------------------------------------

def test_message_queue_key_and_dataclass_malformed_paths():
    service = queue_module.MessageQueueService.__new__(queue_module.MessageQueueService)
    assert service._key_incoming("+911") == "+911:incoming"
    assert service._key_response_ready("+911") == "+911:response_ready"
    with pytest.raises((TypeError, ValueError, KeyError, json.JSONDecodeError)):
        queue_module.ProcessingSession.from_json("not-json")
    assert queue_module.Batch.from_dict(queue_module.Batch("b", "u", "c", "text", 1, 1).to_dict()).batch_id == "b"


@pytest.mark.asyncio
async def test_whatsapp_http_exception_and_invalid_button_configuration(monkeypatch):
    service = whatsapp_module.WhatsAppService.__new__(whatsapp_module.WhatsAppService)
    service.mock_mode = False
    service.username = "u"
    service.password = "p"
    service.from_number = "f"
    service.base_url = "http://wa"
    service.template_base_url = "http://templates"
    service.retry_service = SimpleNamespace(retry_with_backoff=AsyncMock(side_effect=RuntimeError("retry")))
    service._clear_pending_reply_flag = AsyncMock()
    service._track_message_in_history = AsyncMock()
    monkeypatch.setattr(whatsapp_module, "post_to_gateway", AsyncMock(side_effect=RuntimeError("http")))
    result = await service.send_interactive_message("1", "button", {"body": {"text": "x"}})
    assert result.success is False
    assert (await service.send_configurable_buttons("1", "body", [])).success is False
    assert service._handle_api_response(FakeResponse(500, {"error": "down"})).success is False


# Webhook health monitor ---------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_health_state_persistence_and_warning_paths():
    service = health_module.WebhookHealthMonitorService.__new__(health_module.WebhookHealthMonitorService)
    service.redis = MagicMock()
    service.redis.init_client = AsyncMock()
    service.redis.client = MagicMock()
    service.redis.client.get = AsyncMock(return_value="not-json")
    service.redis.client.set = AsyncMock()
    service.redis.get = AsyncMock(return_value="not-json")
    service.redis.set = AsyncMock()
    service.settings = SimpleNamespace(webhook_alert_state_ttl_seconds=60)
    state = await service._get_state()
    assert state["current_state"] == "HEALTHY"
    service.redis.set.side_effect = RuntimeError("redis")
    await service._save_state({"current_state": "HEALTHY"})
    service._send_warning_alert = AsyncMock()
    service._save_state = AsyncMock()
    state = {"current_state": "HEALTHY", "consecutive_warnings": 0, "consecutive_failures": 0, "consecutive_successes": 0, "is_alerting": False}
    await service._process_check_result(state, health_module.HealthStatus.WARNING, 12, "slow")
    assert state["current_state"] == "FAILING"
    assert health_module.WebhookHealthMonitorService._format_duration(__import__("datetime").timedelta(seconds=65)) == "1m 5s"


# RFQ background and intimation -------------------------------------------

@pytest.mark.asyncio
async def test_rfq_background_batch_mixed_results_and_fetch_failure(monkeypatch):
    # RFQBackgroundService is a singleton; isolate this test from constructor-based tests.
    monkeypatch.setattr(background_module.RFQBackgroundService, "_instance", None)
    monkeypatch.setattr(background_module.RFQBackgroundService, "_initialized", False)
    service = background_module.RFQBackgroundService.__new__(background_module.RFQBackgroundService)
    service.max_concurrent_notifications = 2
    service.intimation_service = MagicMock()
    service.intimation_service.send_rfq_notification = AsyncMock(side_effect=[
        {"success": True, "message_id": "m"}, RuntimeError("down")
    ])
    sellers = [{"seller_id": "s1", "seller_name": "One"}, {"seller_id": "s2", "seller_name": "Two"}]
    result = await service._send_batch_notifications({"rfq_id": "r"}, sellers, "job")
    assert result["successful"] == 1 and result["failed"] == 1
    service.db_session = MagicMock()
    query = MagicMock()
    query.filter.return_value = query
    query.first.return_value = None
    service.db_session.query.return_value = query
    assert await service._fetch_rfq_data("missing") is None


@pytest.mark.asyncio
async def test_rfq_intimation_payment_email_and_timeout_failures():
    service = intimation_module.RFQIntimationService.__new__(intimation_module.RFQIntimationService)
    service.settings = SimpleNamespace(support_contact_info="support", contact_email="support")
    service.subscription_plans = {"basic": {"price": 10, "credits": 1}}
    service.conversation_timeout_minutes = 5
    service.whatsapp_service = MagicMock()
    service.whatsapp_service.send_message = AsyncMock(return_value=SimpleNamespace(success=False, error="wa"))
    seller = SimpleNamespace(phone_number="1", email="e", subscription_credits=1)
    service._get_seller_details = AsyncMock(return_value=seller)
    service._get_mock_procurev = MagicMock(return_value=SimpleNamespace(generate_payment_link=AsyncMock(return_value={"success": True, "payment_link": "url"})))
    assert (await service.handle_subscription_selection("s", "r", "basic"))["error"] == "Failed to send payment message"
    service._validate_rfq_id = AsyncMock(return_value=False)
    service._get_mock_procurev = MagicMock()
    assert (await service.handle_rfq_id_request("s", "r", "bad"))["error"] == "Invalid RFQ ID"
    service._get_mock_procurev = MagicMock(return_value=SimpleNamespace(get_seller_pending_bids=AsyncMock(return_value={"success": False})))
    result = await service.handle_conversation_timeout("s", "r")
    assert result["success"] is False or result["message_sent"] is False


# Seller notifications and matching --------------------------------------

def test_seller_notification_workflow_missing_active_and_db_error(monkeypatch):
    service = notification_module.SellerNotificationService.__new__(notification_module.SellerNotificationService)
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = None
    db.close = MagicMock()
    monkeypatch.setattr(notification_module, "get_db_session", lambda: db)
    assert service.check_seller_workflow_status("+1") == (False, None, None)
    db.execute.return_value.fetchone.return_value = ("rfq", None, 2)
    assert service.check_seller_workflow_status("1")[0] is True
    db.execute.side_effect = RuntimeError("db")
    assert service.check_seller_workflow_status("1") == (False, None, None)


@pytest.mark.asyncio
async def test_seller_notification_template_exception_and_bfs_batch_counts():
    service = notification_module.SellerNotificationService.__new__(notification_module.SellerNotificationService)
    service.whatsapp_service = MagicMock()
    service.check_seller_workflow_status = MagicMock(return_value=(True, "rfq", 1))
    result = await service.send_rfq_notifications({"rfq_id": "r"}, [{"seller_id": "s", "phone_number": "1"}])
    assert result["skipped"] == 1
    service.send_bfs_bid_notification = AsyncMock(side_effect=[
        {"success": True}, {"success": False, "skipped": True}, {"success": False}
    ])
    notifications = [{"seller_phone": "1", "bid_data": {}, "bfs_user_uuid": "b", "seller_id": "s"}] * 3
    result = await service.send_bfs_bid_notifications_batch(notifications)
    assert (result["sent"], result["skipped"], result["failed"]) == (1, 1, 1)


@pytest.mark.asyncio
async def test_enhanced_seller_matching_malformed_metadata_and_query_exception():
    service = matching_module.EnhancedSellerMatchingService.__new__(matching_module.EnhancedSellerMatchingService)
    service.collection = MagicMock()
    service.collection.query.return_value = {
        "documents": [["x", "y"]], "metadatas": [[{"seller_id": "s"}, {"seller_id": "s2"}], ""],
        "distances": [[.2, .2]]
    }
    result = await service.find_sellers_for_item("bolt")
    assert result["success"] is False or result["sellers"] == []
    service.collection.query.side_effect = RuntimeError("chroma")
    result = await service.find_sellers_for_item("bolt")
    assert result["success"] is False and "chroma" in result["error"]


# Learning and classic categorization -------------------------------------

def test_learning_suggestions_populated_and_exception(monkeypatch):
    service = learning_module.LearningCategorizationService.__new__(learning_module.LearningCategorizationService)
    item = SimpleNamespace(normalized_keywords={"keywords": ["steel"]}, learning_category_id="c")
    category = SimpleNamespace(id="c", level_1_category="Tools", level_2_category="Hand", level_3_category="Drill", usage_frequency=2, confidence_score=.8)
    query = MagicMock()
    query.all.return_value = [item]
    query.filter.return_value = query
    query.first.return_value = category
    db = MagicMock()
    db.query.return_value = query
    db.close = MagicMock()
    monkeypatch.setattr(learning_module, "get_db_session", lambda: db)
    assert service.get_learning_category_suggestions("steel")[0]["learning_category_id"] == "c"
    db.query.side_effect = RuntimeError("db")
    assert service.get_learning_category_suggestions("steel") == []


def test_auto_categorization_query_and_population_failures(monkeypatch):
    service = auto_module.AutoCategorizationService.__new__(auto_module.AutoCategorizationService)
    service.collection = MagicMock()
    service.collection.query.side_effect = RuntimeError("chroma")
    with pytest.raises(RuntimeError, match="chroma"):
        service.find_similar_items("bolt")
    service.collection.count.side_effect = RuntimeError("count")
    assert "error" in service.get_collection_stats()
    service.settings = SimpleNamespace(enable_remote_categorization=False)
    monkeypatch.setattr(auto_module, "get_settings", lambda: service.settings)
    monkeypatch.setattr(service, "_get_local_category_data", MagicMock(return_value=[]))
    assert service.populate_embeddings_from_db() == 0
