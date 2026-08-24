"""Additional deterministic coverage for residual core application paths.

The tests in this module use only in-memory fakes and mocks.  No database,
Redis, HTTP, WhatsApp, OpenAI, Chroma, or Celery service is contacted.
"""

from __future__ import annotations

import asyncio
import json
import runpy
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pydantic import ValidationError

import app.config as config_module
import app.main as main_module
import app.models as models_module
import app.api.webhook as webhook_module
import app.procucev_apis.seller_apis as seller_api_module
import app.services.processors.interactive_message_processor as interactive_module
import app.services.processors.text_message_processor as text_module
from app.models import ConversationOutcome, SessionState, UserType, WorkflowType
from app.schemas.rfq import (
    ExcelValidationSchema,
    RFQCreateRequestSchema,
    RFQDeliveryLocationSchema,
    RFQItemSchema,
    RFQValidationSchema,
    RFQVendorSchema,
)


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _RequestWithoutJson:
    method = "GET"
    url = SimpleNamespace(path="/missing-json")


class _Session:
    session_id = "session-1"


def _db_context(value=None):
    @contextmanager
    def context():
        yield value if value is not None else object()

    return context


async def _noop():
    return None


def _lifespan_settings(**overrides):
    values = {
        "app_name": "unit-test",
        "environment": "test",
        "database_mode": "local",
        "redis_url": "redis://unit",
        "chroma_host": "localhost",
        "chroma_port": 8000,
        "azure_openai_base_url": "https://unit",
        "webhook_health_monitoring_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _processor_user(registered=True):
    return SimpleNamespace(is_registered=registered, phone_number="919999999999")


def _processor_session(**state):
    return SimpleNamespace(workflow_state=dict(state))


# Configuration and model residuals ---------------------------------------


def test_settings_validation_remaining_database_webhook_branches():
    settings = config_module.Settings()

    settings.database_mode = "client"
    settings.client_database_url = None
    with pytest.raises(ValueError, match="CLIENT_DATABASE_URL"):
        settings.validate_config()

    settings.database_mode = "local"
    settings.local_database_url = "sqlite:///:memory:"
    settings.support_team_numbers = []
    settings.webhook_health_monitoring_enabled = False
    settings.validate_config()

    settings.webhook_health_monitoring_enabled = True
    settings.webhook_health_check_interval_seconds = 20
    settings.webhook_api_timeout_seconds = 10
    settings.webhook_failure_grace_period_seconds = 30
    settings.webhook_api_response_threshold_seconds = 5
    settings.webhook_recovery_confirmations = 1
    settings.webhook_warning_consecutive_threshold = 0
    with pytest.raises(ValueError, match="WEBHOOK_WARNING_CONSECUTIVE_THRESHOLD"):
        settings.validate_config()


def test_conversation_session_none_validator_values():
    assert models_module.ConversationSession.validate_workflow_type(None, "workflow_type", None) is None
    assert models_module.ConversationSession.validate_outcome(None, "outcome", None) is None
    assert models_module.ConversationSession.validate_user_type(None, "user_type", None) is None
    assert models_module.ConversationSession.validate_session_state(None, "session_state", None) is None


# RFQ schema matrices ------------------------------------------------------


def test_rfq_item_location_and_vendor_validation_matrix():
    item = RFQItemSchema(description="steel plate", quantity="2.5", unit_of_measures="custom-unit")
    assert item.quantity == 2.5 and item.unit_of_measures == "custom-unit"

    for quantity in ("not-a-number", 0, -1, 10_000_001):
        with pytest.raises(ValidationError):
            RFQItemSchema(description="item", quantity=quantity, unit_of_measures="pcs")

    assert RFQDeliveryLocationSchema(
        state="Maharashtra", city="Pune", pincode="411005"
    ).pincode == "411005"
    assert RFQDeliveryLocationSchema(
        state="A state accepted for compatibility", city="City", pincode="123456"
    ).state == "A state accepted for compatibility"
    for pincode in ("12345", "1234567", "12A456"):
        with pytest.raises(ValidationError):
            RFQDeliveryLocationSchema(state="Maharashtra", city="Pune", pincode=pincode)

    assert RFQVendorSchema(id="v1", email="vendor@example.com").email == "vendor@example.com"
    with pytest.raises(ValidationError):
        RFQVendorSchema(id="v1", email="invalid-email")


def test_rfq_create_and_excel_validation_paths():
    base = {
        "createdBy": "user-1",
        "deliveryDate": datetime.now() + timedelta(days=2),
        "user": "user-1",
        "org": {"id": "org-1"},
        "rfqItem": [{"description": "pump", "quantity": "2", "unit_of_measures": "pcs"}],
        "clientdeliverylocationrfq": [{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}],
    }
    request = RFQCreateRequestSchema(**base)
    assert request.rfq_item[0].quantity == 2

    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**base, "deliveryDate": datetime.now() - timedelta(days=1)})
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**base, "rfqItem": []})
    with pytest.raises(ValidationError):
        RFQCreateRequestSchema(**{**base, "clientdeliverylocationrfq": []})

    empty = ExcelValidationSchema()
    assert empty.get_missing_required_excel_fields() == [
        "item_description", "specification", "uom", "quantity"
    ]
    assert empty.get_missing_optional_excel_fields() == ["serial_no", "remarks"]
    assert empty.get_excel_completion_percentage() == 0
    assert not empty.is_excel_complete()
    assert len(empty.get_excel_questions()) == 4
    assert empty.get_next_excel_questions() == empty.get_excel_questions()

    required = ExcelValidationSchema(
        item_description="pump", specification="steel", uom="pcs", quantity="2"
    )
    assert required.is_excel_complete()
    assert required.get_excel_completion_percentage() == pytest.approx(200 / 3)
    optional_questions = required.get_excel_questions()
    assert len(optional_questions) == 2
    assert required.get_missing_optional_excel_fields() == ["serial_no", "remarks"]

    complete = ExcelValidationSchema(
        serial_no="1", item_description="pump", specification="steel",
        uom="pcs", quantity="2", remarks="urgent"
    )
    assert complete.get_missing_required_excel_fields() == []
    assert complete.get_missing_optional_excel_fields() == []
    assert complete.get_excel_completion_percentage() == 100
    assert complete.get_excel_questions() == []


def test_rfq_validation_missing_fields_question_grouping_and_completion():
    schema = RFQValidationSchema(
        items=[
            {"description": "kg", "quantity": "0"},
            {"description": "pump", "quantity": "not numeric"},
        ],
        delivery_locations=[{"state": "", "city": "Pune", "pincode": ""}],
    )
    assert schema._is_invalid_description("")
    assert schema._is_invalid_description(" 123 ")
    assert schema._is_invalid_description("kg")
    assert schema._is_invalid_description("something")
    assert not schema._is_invalid_description("stainless pump")

    missing = schema.get_missing_mandatory_fields()
    assert "delivery_date" in missing
    assert {"item_0_description", "item_0_quantity", "item_1_quantity"}.issubset(missing)
    assert {"delivery_location_0_state", "delivery_location_0_pincode"}.issubset(missing)
    questions = schema.get_mandatory_questions()
    assert any("item details" in question.lower() for question in questions)
    assert any("complete address" in question.lower() for question in questions)
    assert schema.get_missing_optional_fields() == ["preferred_brand", "remarks", "attachments"]
    assert schema.get_completeness_percentage() == pytest.approx(22.2222222)
    assert schema.get_combined_questions(include_optional=False)["optional"] == []

    one_missing = RFQValidationSchema(
        delivery_date=datetime.now() + timedelta(days=1),
        items=[{"description": "pump", "quantity": 1}],
        delivery_locations=[{"state": "", "city": "Pune", "pincode": "411005"}],
    )
    one_questions = one_missing.get_mandatory_questions()
    assert any("delivery state" in question.lower() for question in one_questions)
    one_missing.set_date_validation_error()
    assert one_missing.has_date_validation_error()
    assert "delivery_date" in one_missing.get_missing_mandatory_fields()
    one_missing.set_date_validation_error(False)
    assert not one_missing.has_date_validation_error()

    mandatory_complete = RFQValidationSchema(
        delivery_date=datetime.now() + timedelta(days=1),
        division="Operations", user_id="u1", organization_id="o1",
        items=[{"description": "pump", "quantity": 1}],
        delivery_locations=[{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}],
    )
    assert mandatory_complete.is_complete()
    assert mandatory_complete.get_next_questions()
    combined = mandatory_complete.get_combined_questions(include_optional=True)
    assert combined["mandatory"] == [] and combined["has_optional"] is True

    full = RFQValidationSchema(
        delivery_date=datetime.now() + timedelta(days=1),
        division="Operations", user_id="u1", organization_id="o1",
        items=[{"description": "pump", "quantity": 1}],
        delivery_locations=[{"state": "Maharashtra", "city": "Pune", "pincode": "411005"}],
        remarks="urgent", vendors=[{"id": "v1"}], attachments=[{"name": "spec.pdf"}],
    )
    assert full.get_completeness_percentage() == 100
    assert full.get_mandatory_questions() == []
    assert full.get_next_questions() == []
    assert full.get_combined_questions() == {
        "mandatory": [], "optional": [], "has_mandatory": False, "has_optional": False
    }


# Main lifecycle, callbacks, and import-time branches ---------------------


@pytest.mark.asyncio
async def test_main_lifespan_without_background_task_registry(monkeypatch):
    monkeypatch.setattr(main_module, "settings", _lifespan_settings())
    monkeypatch.setattr(main_module, "init_database", MagicMock())

    import app.procucev_apis.procucev_api_client as api_client_module

    api_init = AsyncMock()
    api_close = AsyncMock()
    monkeypatch.setattr(api_client_module, "init_procucev_api_client", api_init)
    monkeypatch.setattr(api_client_module, "close_procucev_api_client", api_close)

    queue = SimpleNamespace(
        run_batch_poller=lambda: _noop(),
        run_monitoring_loop=lambda: _noop(),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(webhook_module, "message_queue_service", queue)

    timeout = SimpleNamespace(
        try_start_monitoring_if_available=AsyncMock(return_value=False),
        stop_monitoring=AsyncMock(),
    )
    import app.services.inactivity_timeout_service as timeout_module

    monkeypatch.setattr(timeout_module, "get_timeout_service", lambda: timeout)
    monkeypatch.setattr(main_module.gc, "get_objects", lambda: [])
    created = []

    def create_task(coro, **kwargs):
        coro.close()
        task = MagicMock(name=kwargs.get("name", "task"))
        created.append(task)
        return task

    monkeypatch.setattr(main_module.asyncio, "create_task", create_task)

    async with main_module.lifespan(main_module.app):
        pass

    # Batch poller, queue monitor, and the event-loop stall monitor.
    assert len(created) == 3
    api_init.assert_awaited_once()
    api_close.assert_awaited_once()
    queue.shutdown.assert_awaited_once()
    timeout.stop_monitoring.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_lifespan_health_monitor_startup_and_shutdown_error(monkeypatch):
    monkeypatch.setattr(main_module, "settings", _lifespan_settings(webhook_health_monitoring_enabled=True))
    monkeypatch.setattr(main_module, "init_database", MagicMock())

    import app.procucev_apis.procucev_api_client as api_client_module
    import app.services.inactivity_timeout_service as timeout_module
    import app.services.webhook_health_monitor_service as monitor_module

    monkeypatch.setattr(api_client_module, "init_procucev_api_client", AsyncMock())
    monkeypatch.setattr(api_client_module, "close_procucev_api_client", AsyncMock())
    queue = SimpleNamespace(
        _background_tasks=[],
        run_batch_poller=lambda: _noop(),
        run_monitoring_loop=lambda: _noop(),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(webhook_module, "message_queue_service", queue)

    monitor = SimpleNamespace(
        start_monitoring=lambda: _noop(),
        stop_monitoring=AsyncMock(side_effect=RuntimeError("monitor stop")),
        _close_session=AsyncMock(),
    )
    monkeypatch.setattr(monitor_module, "WebhookHealthMonitorService", lambda: monitor)
    timeout = SimpleNamespace(
        try_start_monitoring_if_available=AsyncMock(return_value=True),
        stop_monitoring=AsyncMock(),
    )
    monkeypatch.setattr(timeout_module, "get_timeout_service", lambda: timeout)
    monkeypatch.setattr(main_module.gc, "get_objects", lambda: [])
    monkeypatch.setattr(
        main_module.asyncio,
        "create_task",
        lambda coro, **_kwargs: (coro.close(), MagicMock())[1],
    )

    async with main_module.lifespan(main_module.app):
        pass

    monitor.stop_monitoring.assert_awaited_once()
    monitor._close_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_lifespan_health_monitor_constructor_error(monkeypatch):
    monkeypatch.setattr(main_module, "settings", _lifespan_settings(webhook_health_monitoring_enabled=True))
    monkeypatch.setattr(main_module, "init_database", MagicMock())

    import app.procucev_apis.procucev_api_client as api_client_module
    import app.services.inactivity_timeout_service as timeout_module
    import app.services.webhook_health_monitor_service as monitor_module

    monkeypatch.setattr(api_client_module, "init_procucev_api_client", AsyncMock())
    monkeypatch.setattr(api_client_module, "close_procucev_api_client", AsyncMock())
    queue = SimpleNamespace(
        run_batch_poller=lambda: _noop(),
        run_monitoring_loop=lambda: _noop(),
        shutdown=AsyncMock(),
    )
    monkeypatch.setattr(webhook_module, "message_queue_service", queue)
    monkeypatch.setattr(
        monitor_module,
        "WebhookHealthMonitorService",
        Mock(side_effect=RuntimeError("monitor unavailable")),
    )
    timeout = SimpleNamespace(
        try_start_monitoring_if_available=AsyncMock(return_value=False),
        stop_monitoring=AsyncMock(),
    )
    monkeypatch.setattr(timeout_module, "get_timeout_service", lambda: timeout)
    monkeypatch.setattr(main_module.gc, "get_objects", lambda: [])
    monkeypatch.setattr(
        main_module.asyncio,
        "create_task",
        lambda coro, **_kwargs: (coro.close(), MagicMock())[1],
    )

    async with main_module.lifespan(main_module.app):
        pass


@pytest.mark.asyncio
async def test_main_global_exception_without_json_attribute(monkeypatch):
    notify = AsyncMock()
    monkeypatch.setattr(main_module, "handle_server_error", notify)
    response = await main_module.global_exception_handler(
        _RequestWithoutJson(), RuntimeError("unexpected")
    )
    assert response.status_code == 500
    assert json.loads(response.body)["detail"].startswith("Internal server error")
    assert notify.await_args.kwargs["user_phone"] is None


@pytest.mark.asyncio
async def test_main_chat_callback_paths_without_sessions(monkeypatch):
    fake_chat = MagicMock()
    fake_chat.whatsapp_service = MagicMock()
    fake_chat.whatsapp_service.send_message = AsyncMock()
    fake_chat.whatsapp_service.send_configurable_buttons = AsyncMock()
    fake_chat.cleanup = AsyncMock()

    async def process(phone, content, message_type):
        await fake_chat.whatsapp_service.send_message(phone, "plain reply")
        await fake_chat.whatsapp_service.send_configurable_buttons(
            phone, "choose", [{"id": "ok", "title": "OK"}]
        )
        return {"status": "image_processed", "message_type": message_type}

    fake_chat.process_message.side_effect = process
    monkeypatch.setattr(main_module, "get_db_session_context", _db_context())
    monkeypatch.setattr(main_module, "ChatService", lambda **_kwargs: fake_chat)
    monkeypatch.setattr(
        "app.utils.logging_utils.UserPhoneContext", lambda _phone: _AsyncContext()
    )

    result = await main_module.process_chat_message.__wrapped__(
        SimpleNamespace(),
        main_module.ChatMessage(message={"image": {"data": "x"}}, phone="9199"),
    )
    assert result["success"] is True
    assert result["responses"] == ["plain reply", "choose"]
    assert result["status"] == "image_processed"
    fake_chat.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_upload_callback_paths_without_sessions(monkeypatch):
    fake_chat = MagicMock()
    fake_chat.whatsapp_service = MagicMock()
    fake_chat.whatsapp_service.send_message = AsyncMock()
    fake_chat.whatsapp_service.send_configurable_buttons = AsyncMock()

    async def process(phone, content, message_type):
        await fake_chat.whatsapp_service.send_message(phone, "uploaded")
        await fake_chat.whatsapp_service.send_configurable_buttons(
            phone, "next", [{"id": "next", "title": "Next"}]
        )
        return {"status": "uploaded"}

    fake_chat.process_message.side_effect = process
    monkeypatch.setattr(main_module, "get_db_session_context", _db_context())
    monkeypatch.setattr("app.database.get_db_session_context", _db_context())
    monkeypatch.setattr(main_module, "ChatService", lambda **_kwargs: fake_chat)

    class Upload:
        filename = "quote.xlsx"

        async def read(self):
            return b"xlsx-data"

    result = await main_module.upload_excel_file.__wrapped__(
        SimpleNamespace(), "9199", Upload()
    )
    assert result["success"] is True
    assert result["responses"] == ["uploaded", "next"]
    assert result["status"] == "uploaded"


def test_main_license_and_entrypoint_branches(monkeypatch):
    import app.config as config_module_again
    import app.license as license_module
    import uvicorn

    license_settings = SimpleNamespace(**vars(main_module.settings))
    license_settings.license_enabled = True
    monkeypatch.setattr(config_module_again, "get_settings", lambda: license_settings)
    validate_license = Mock(side_effect=[(True, "valid"), (False, "invalid")])
    monkeypatch.setattr(license_module, "validate_license", validate_license)
    uvicorn_run = Mock()
    monkeypatch.setattr(uvicorn, "run", uvicorn_run)

    runpy.run_module("app.main", run_name="__main__")
    runpy.run_module("app.main", run_name="__main__")

    assert validate_license.call_count == 2
    assert uvicorn_run.call_count == 2


# Processor and API residuals ---------------------------------------------


@pytest.mark.asyncio
async def test_interactive_processor_exception_path():
    processor = interactive_module.InteractiveMessageProcessor(AsyncMock())
    with pytest.raises(AttributeError):
        await processor.process_interactive_message(
            _processor_user(), _processor_session(), object()
        )


@pytest.mark.asyncio
async def test_text_processor_process_and_clarification_error_paths(monkeypatch):
    monkeypatch.setattr(
        text_module.ChatServiceHelpers,
        "build_conversation_context",
        lambda *_args: {},
    )
    monkeypatch.setattr(
        text_module.ChatServiceHelpers,
        "build_context",
        lambda *_args: {},
    )
    intent = MagicMock(classify_intent=Mock(side_effect=RuntimeError("classifier")))
    response_helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="response"),
        generate_clarification_response=AsyncMock(side_effect=RuntimeError("clarifier")),
    )
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    processor = text_module.TextMessageProcessor(
        intent, MagicMock(), whatsapp, response_helpers,
        MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )
    with pytest.raises(RuntimeError, match="classifier"):
        await processor.process_text_message(
            _processor_user(), _processor_session(), "hello"
        )

    clarification_processor = text_module.TextMessageProcessor(
        MagicMock(), MagicMock(), whatsapp, response_helpers,
        MagicMock(), MagicMock(), MagicMock(), MagicMock()
    )
    result = await clarification_processor._handle_clarification_request(
        _processor_user(), "unclear", _processor_session()
    )
    assert result == {"status": "error", "error": "clarifier"}


@pytest.mark.asyncio
async def test_seller_api_remaining_response_shapes(monkeypatch):
    api_client = AsyncMock()
    monkeypatch.setattr(seller_api_module, "get_procucev_api_client", lambda: api_client)
    monkeypatch.setattr(
        seller_api_module,
        "get_settings",
        lambda: SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal.example"),
    )
    service = seller_api_module.SellerAPIService()

    api_client.post.return_value = {"success": False}
    credits = await service.check_seller_credits("org-1")
    assert credits == {"success": False, "error": "Failed to check seller credits"}

    api_client.post.return_value = {"success": True, "data": {"status": "ok"}}
    status = await service.check_seller_rfq_status("seller-1", [None])
    assert status == {"success": True, "data": {"status": "ok"}}
    assert "rfqIds" not in api_client.post.await_args.kwargs["json_data"]

    api_client.post.side_effect = RuntimeError("status unavailable")
    status_error = await service.check_seller_rfq_status("seller-1", ["1"])
    assert status_error == {"success": False, "error": "status unavailable"}

    api_client.post.side_effect = None
    api_client.post.return_value = {"success": False}
    reminder = await service.fetch_seller_open_rfqs_for_reminder("seller-1")
    assert reminder == {"success": False, "error": "Failed to fetch seller open RFQs"}
