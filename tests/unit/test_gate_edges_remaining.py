from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

import app.config as config_module
import app.main as main_module
import app.models as models_module
import app.procucev_apis.bfs_apis as bfs_module
import app.procucev_apis.category_apis as category_module
import app.procucev_apis.procucev_api_client as client_module
import app.procucev_apis.seller_apis as seller_module
import app.services.chat_summary_service as summary_module
import app.services.faq_service as faq_module
import app.services.global_error_handler as error_module
import app.services.welcome_message_service as welcome_module
import app.tools.interaction_logger as interaction_module
import app.utils.bfs_bid_format_parser as bid_module
import app.utils.datetime_utils as datetime_module
import app.utils.logging_utils as logging_module
import app.utils.pincode_distance as distance_module
import app.utils.pincode_lookup as lookup_module
import app.utils.sectioned_rfq_format_parser as sectioned_module
from app.procucev_apis.procucev_api_client import ProcucevAPIClient
from app.models import ConversationOutcome, SessionState, UserType, WorkflowType
from app.schemas.rfq import RFQCreateRequestSchema, RFQItemSchema, RFQValidationSchema


class Response:
    def __init__(self, status=200, data=None, text="body", content_type="application/json"):
        self.status = status
        self._data = {} if data is None else data
        self._text = text
        self.headers = {"Content-Type": content_type}

    async def text(self):
        return self._text

    async def json(self):
        return self._data


class ResponseContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def __aexit__(self, *_args):
        return False


class SessionFake:
    closed = False

    def __init__(self, responses=(), post_responses=()):
        self.responses = list(responses)
        self.post_responses = list(post_responses or responses)
        self.calls = []
        self.post_calls = []
        self.close = AsyncMock()

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.responses.pop(0)
        return ResponseContext(value)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        value = self.post_responses.pop(0)
        return ResponseContext(value)


class RedisFake:
    def __init__(self, value=None):
        self.get = AsyncMock(return_value=value)
        self.set = AsyncMock(return_value=True)


@pytest.fixture
def api_settings():
    return SimpleNamespace(
        gmt_base_url="https://gmt.example",
        gmt_username="user",
        gmt_password="password",
        gmt_phone="+919999999999",
        gmt_client_id="client",
        gmt_client_secret="secret",
        gmt_retry_delay=0,
    )


def test_config_fallback_validation_and_model_enum_edges(monkeypatch):
    settings = config_module.Settings()
    settings.database_mode = "local"
    settings.local_database_url = "mysql+pymysql://local"
    assert settings.get_database_url() == "mysql+pymysql://local"

    settings.local_database_url = None
    with pytest.raises(ValueError, match="LOCAL_DATABASE_URL"):
        settings.get_database_url()
    settings.local_database_url = "mysql+pymysql://local"
    settings.openai_api_key = "key"
    settings.gmt_username = "u"
    settings.gmt_phone = "p"
    settings.webhook_health_monitoring_enabled = True
    settings.webhook_health_check_interval_seconds = 5
    settings.webhook_api_timeout_seconds = 5
    with pytest.raises(ValueError, match="greater than WEBHOOK_API_TIMEOUT_SECONDS"):
        settings.validate_config()

    monkeypatch.setattr(config_module, "_settings", None)
    settings.webhook_health_monitoring_enabled = False
    settings.validate_config()

    assert models_module.ConversationSession.validate_workflow_type(None, "workflow_type", "bad") is None
    assert models_module.ConversationSession.validate_outcome(None, "outcome", "bad") is None
    assert models_module.ConversationSession.validate_user_type(None, "user_type", "bad") is UserType.unknown
    assert models_module.ConversationSession.validate_session_state(None, "session_state", "bad") is SessionState.active
    assert models_module.ConversationSession.validate_workflow_type(None, "workflow_type", WorkflowType.rfq_creation) is WorkflowType.rfq_creation
    assert models_module.ConversationSession.validate_outcome(None, "outcome", ConversationOutcome.completed) is ConversationOutcome.completed


def test_rfq_quantity_request_and_dynamic_completeness_edges():
    with pytest.raises(ValidationError, match="valid number"):
        RFQItemSchema(description="x", quantity="not numeric", unit_of_measures="pcs")
    with pytest.raises(ValidationError, match="greater than 0"):
        RFQItemSchema(description="x", quantity=0, unit_of_measures="pcs")
    with pytest.raises(ValidationError, match="limit"):
        RFQItemSchema(description="x", quantity=10_000_001, unit_of_measures="pcs")

    base = dict(
        createdBy="user", deliveryDate=datetime.now() + __import__("datetime").timedelta(days=2),
        user="user", org={"id": "org"},
        rfqItem=[{"description": "bolt", "quantity": 1, "unit_of_measures": "pcs"}],
        clientdeliverylocationrfq=[{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}],
    )
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**base, "rfqItem": []})
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**base, "clientdeliverylocationrfq": []})

    empty = RFQValidationSchema()
    assert empty.get_completeness_percentage() == 0
    assert empty.get_mandatory_questions()
    complete = RFQValidationSchema(
        delivery_date=datetime.now(), division="IT", user_id="u", organization_id="o",
        items=[{"description": "bolt", "quantity": 1}],
        delivery_locations=[{"state": "MH", "city": "Pune", "pincode": "411005"}],
        remarks="r", vendors=[{"id": "v"}], attachments=[{"name": "a"}],
    )
    assert complete.get_completeness_percentage() == 100
    assert complete.get_mandatory_questions() == []
    assert complete.get_next_questions() == []


@pytest.mark.asyncio
async def test_bfs_category_and_seller_success_default_paths(monkeypatch, api_settings):
    client = AsyncMock()
    monkeypatch.setattr(bfs_module, "get_procucev_api_client", lambda: client)
    bfs = bfs_module.BFSAPIService()
    client.post.return_value = {"success": True, "data": {"accepted": True}}
    accepted = await bfs.accept_bid_by_seller("bid-1")
    assert accepted["success"] and accepted["data"] == {"accepted": True}

    monkeypatch.setattr(category_module, "get_procucev_api_client", lambda: client)
    category = category_module.CategoryAPIService()
    client.get.return_value = {"success": False}
    assert (await category.get_categories())["error"] == "Failed to get categories"

    monkeypatch.setattr(seller_module, "get_procucev_api_client", lambda: client)
    monkeypatch.setattr(seller_module, "get_settings", lambda: SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal"))
    seller = seller_module.SellerAPIService()
    client.post.return_value = {"success": True, "data": {}}
    assert (await seller.check_seller_credits("org"))["credits_available"] == 0
    client.post.return_value = {"success": True, "data": {"status": "ok"}}
    status = await seller.check_seller_rfq_status("seller", [None, "RFQ2"])
    assert status["data"] == {"status": "ok"}
    assert client.post.call_args.kwargs["json_data"]["rfqIds"] == ["RFQ2"]
    client.post.return_value = {"success": True}
    assert (await seller.fetch_seller_open_rfqs_for_reminder("seller"))["open_rfqs"] == []
    client.post.return_value = {"success": False}
    assert (await seller.generate_payment_link("plan"))["payment_url"] == "https://portal"


@pytest.mark.asyncio
async def test_procucev_auth_dynamic_credentials_and_http_edges(monkeypatch, api_settings):
    redis = RedisFake({"expires_at": 123})
    monkeypatch.setattr(client_module, "get_settings", lambda: api_settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: redis)
    monkeypatch.setattr(client_module, "manual_log_api_call", MagicMock())
    client = ProcucevAPIClient()

    session = SessionFake(post_responses=[Response(status=201, data={"token": "dynamic"})])
    client.session = session
    assert await client.authenticate_dynamic("alice", "9876543210") == "dynamic"
    assert session.post_calls[0][1]["headers"] == {}

    client.session = SessionFake([Response(data={"ok": True})])
    client.authenticate_dynamic = AsyncMock(return_value="token")
    result = await client.send_request(
        "GET", "/items", dynamic_token={"username": "alice", "phone": "1"}
    )
    assert result["ok"] is True
    assert client.session.calls[0][1]["headers"]["Authorization"] == "Bearer token"

    client.session = SessionFake([Response(status=400, data={}, text="bad")])
    result = await client.send_request("GET", "/bad")
    assert result["status_code"] == 400

    client.session = SessionFake([asyncio.TimeoutError()])
    client.max_retries = 1
    client._handle_timeout_error = AsyncMock()
    result = await client.send_request("GET", "/timeout")
    assert result["success"] is False and result["status_code"] == 500
    client._handle_timeout_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_summary_fallback_rfq_and_redis_delete(monkeypatch):
    settings = SimpleNamespace(enable_session_summarization=True, redis_session_storage_enabled=True)
    service = summary_module.ChatSummaryService.__new__(summary_module.ChatSummaryService)
    service.settings = settings
    service.openai_service = SimpleNamespace(generate_session_summary=AsyncMock(return_value="summary"))
    db = MagicMock()
    summary = SimpleNamespace()
    monkeypatch.setattr(summary_module, "get_db_session", lambda: _DbContext(db))
    monkeypatch.setattr(summary_module, "ChatSummary", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(summary_module.SummarizationHelpers, "extract_rich_entities_for_summary", lambda _: {})
    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)
    manager = MagicMock()
    monkeypatch.setattr("app.database.DatabaseManager", lambda session=None: manager)
    redis = SimpleNamespace(delete_session=AsyncMock())
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis)
    created = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created.append(coro) or MagicMock())
    session = SimpleNamespace(
        session_id="s", external_user_id="u", rfq_ids=[], rfq_id="legacy", extracted_entities=None,
        product_items=None, conversation_history=None, user_type=None, session_state=None,
        workflow_type="workflow", outcome="done", rfq_metadata=None, seller_responses=None,
        interaction_metrics=None, parent_session_id=None, workflow_state={}, retention_date=None,
        last_activity_at=None, completed_at=None, created_at=None,
    )
    result = await service.generate_session_summary(session)
    assert result.ai_generated_summary == "summary"
    assert result.rfq_ids == ["legacy"]
    assert manager.append_session_data.called
    assert len(created) == 1
    created[0].close()


@pytest.mark.asyncio
async def test_faq_and_global_error_handler_remaining_branches(monkeypatch):
    faq = faq_module.FAQService.__new__(faq_module.FAQService)
    faq.settings = SimpleNamespace(support_contact_info="support@example.com")
    faq.openai_service = SimpleNamespace(get_completion=AsyncMock(return_value="*Answer*\n- now"))
    answer = await faq.get_faq_answer("What?", {"messages": [{"role": "system", "content": "ignored"}]})
    assert answer == "Answer now"
    faq.openai_service.get_completion.side_effect = RuntimeError("offline")
    assert "trouble accessing" in await faq.get_faq_answer("What?")

    handler = error_module.GlobalErrorHandler.__new__(error_module.GlobalErrorHandler)
    handler.notification_enabled = False
    handler.whatsapp_service = SimpleNamespace(send_message=AsyncMock())
    context = error_module.ErrorContext("Database Error", "down")
    assert await handler.handle_error(context)
    assert context.timestamp is not None
    handler.notification_enabled = True
    handler.user_error_message = "technical issue"
    handler._notify_support_team = AsyncMock(side_effect=RuntimeError("notify"))
    assert await handler.handle_error(error_module.ErrorContext("API", "bad", user_phone="1")) is False
    handler._send_user_response = AsyncMock(side_effect=RuntimeError("send"))
    assert await handler.handle_error(error_module.ErrorContext("API", "bad", user_phone="1")) is False


@pytest.mark.asyncio
async def test_welcome_constructor_singleton_and_logging_context(monkeypatch, tmp_path):
    redis = SimpleNamespace(exists=AsyncMock(return_value=False), set=AsyncMock(), expireat=AsyncMock(return_value=True), delete=AsyncMock(return_value=True))
    monkeypatch.setattr(welcome_module, "get_redis_service", lambda: redis)
    welcome = welcome_module.WelcomeMessageService()
    assert welcome.redis_service is redis
    welcome_module._welcome_service = None
    first = welcome_module.get_welcome_service()
    assert welcome_module.get_welcome_service() is first

    record = logging_module.logging.LogRecord("source", logging_module.logging.INFO, __file__, 1, "hello", (), None)
    assert "N/A" in logging_module.CustomFormatter().format(record)
    with logging_module.UserPhoneContext("123"):
        assert logging_module.get_user_phone_context() == "123"
        contextual_record = logging_module.logging.LogRecord("source", logging_module.logging.INFO, __file__, 1, "hello", (), None)
        assert "123" in logging_module.CustomFormatter().format(contextual_record)
    assert logging_module.get_user_phone_context() is None

    logger = interaction_module.InteractionLogger(str(tmp_path))
    class BrokenModel:
        def model_dump(self):
            raise RuntimeError("bad model")
        def dict(self):
            raise RuntimeError("bad dict")
    assert logger._json_serializer(BrokenModel()).startswith("<")
    logger.log_intent_classification("hello", "greet", 1, "ok", "model", openai_input={"when": datetime.now(UTC)})
    assert logger.log_file.exists()


def test_pincode_lookup_retry_and_distance_nonfinite(monkeypatch):
    class HttpResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return [{"Status": "Success", "PostOffice": [{"District": "Pune", "State": "Maharashtra"}]}]

    monkeypatch.setattr(lookup_module.requests, "get", MagicMock(return_value=HttpResponse()))
    assert lookup_module.get_pincode_details("411005")
    assert asyncio.run(lookup_module.get_location_from_pincode_async("411005"))["city"] == "Pune"
    assert asyncio.run(lookup_module.get_location_from_pincode_async("bad")) is None

    monkeypatch.setattr(distance_module, "get_coordinates_from_pincode", lambda value: (1.0, 2.0))
    monkeypatch.setattr(distance_module, "geodesic", lambda *_args: SimpleNamespace(kilometers=float("inf")))
    assert distance_module.calculate_distance_between_pincodes("411005", "411006") is None
    assert distance_module.calculate_distance_from_pincode_to_coords("411005", 3, 4) == float("inf")


def test_logging_decorators_and_sectioned_display_edges(monkeypatch):
    events = []
    logger = MagicMock()

    @logging_module.log_service_method("unit")
    async def async_ok(value=None):
        events.append(value)
        return "ok"

    @logging_module.log_service_method("unit")
    def sync_bad():
        raise ValueError("bad")

    assert asyncio.run(async_ok(value="x")) == "ok"
    with pytest.raises(ValueError):
        sync_bad()
    logging_module.log_info(logger, "hello", user_id="u", phone_number="1", flow="x")
    logging_module.log_error(logger, "bad", ValueError("x"), user_id="u")
    logging_module.log_debug(logger, "debug")
    assert logger.info.called and logger.error.called and logger.debug.called

    display, missing = sectioned_module.generate_delivery_display_with_missing({})
    assert "Please provide" in display and len(missing) == 2
    assert sectioned_module.validate_delivery_completeness({"deliveryDate": "d", "pincode": "p", "city": "c", "state": "s"})
    assert not sectioned_module.validate_items_completeness([{"description": "", "quantity": 1}])
    assert sectioned_module.validate_items_completeness([{"description": "bolt", "quantity": 1}])
    assert sectioned_module._format_quantity(2.0) == "2"
    assert sectioned_module._format_quantity("x") == "x"
    assert sectioned_module._truncate_text("a" * 10, 5) == "aa..."
    assert sectioned_module.generate_items_display([]) == "No items"
    assert "more items" in sectioned_module.generate_items_display([{"description": str(i), "quantity": i} for i in range(7)])
    assert sectioned_module.generate_delivery_display_with_invalid_pincode({"deliveryDate": "tomorrow"}).endswith("6-digit pin-code]")


def test_bid_parser_and_datetime_missing_shapes():
    assert bid_module._normalize_bid_text_newlines("x Seller Price: 1 a. Your Price: 2 b. Your Qty: 1") != "x Seller Price: 1 a. Your Price: 2 b. Your Qty: 1"
    assert bid_module._extract_bid_format_section("Place your bids:\nDell: 1\nPlease confirm") == ("Dell: 1", "Please confirm")
    assert bid_module.parse_bid_format("", []) ["error"] == "No bid items provided. Please copy the format and modify prices/quantities."
    assert datetime_module.format_utc_display(datetime(2025, 1, 1, tzinfo=UTC)).endswith("UTC")
    assert datetime_module.format_date_for_validation_error("") == "N/A"
    assert datetime_module.add_business_days(datetime(2025, 1, 1), -1).day == 1


class _DbContext:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False
