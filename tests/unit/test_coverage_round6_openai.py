"""Focused deterministic coverage for reachable OpenAIService branch gaps."""

import asyncio
import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.openai_service as openai_module


class FakeFile:
    def __init__(self, value='{"tool": "definition"}'):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.value


def response(args=None, *, output_type="function_call", output_text="", usage=True, raw=False):
    output = []
    if args is not None:
        arguments = args if raw else json.dumps(args)
        output = [SimpleNamespace(type=output_type, arguments=arguments)]
    token_usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=4,
        input_tokens_details=SimpleNamespace(cached_tokens=2),
    )
    return SimpleNamespace(output=output, output_text=output_text, usage=token_usage if usage else None)


@pytest.fixture
def service(monkeypatch):
    settings = SimpleNamespace(
        openai_model_default="default-model",
        openai_model_advanced="advanced-model",
        support_email="support@example.test",
        support_contact_info="help@example.test",
        PROCUCEV_PORTAL_URL="https://portal.example.test",
        rfq_followup_note="https://portal.example.test/login",
    )
    interaction_logger = MagicMock()
    client = SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock()),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
        close=AsyncMock(),
    )

    monkeypatch.setattr(openai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_module, "get_interaction_logger", lambda: interaction_logger)
    monkeypatch.setattr(openai_module, "logger", MagicMock())
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: FakeFile())

    obj = openai_module.OpenAIService()
    obj._client = client
    monkeypatch.setattr(obj, "_load_prompt", lambda *args, **kwargs: "PROMPT")
    return obj, client, interaction_logger, settings


@pytest.mark.asyncio
async def test_lifecycle_image_messages_and_surviving_prompt_loader(monkeypatch, tmp_path, service):
    obj, client, _, settings = service

    obj._client = None
    await obj.close()

    obj._client = client
    run_arguments = []

    def no_event_loop():
        raise RuntimeError("no event loop")

    def fake_run(coroutine):
        run_arguments.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(asyncio, "get_event_loop", no_event_loop)
    monkeypatch.setattr(asyncio, "run", fake_run)
    obj.close_sync()
    assert obj._client_closed is True
    assert len(run_arguments) == 1

    assert obj._build_messages_with_history(None, '{"mime_type":"image/png"}') == [
        {"role": "user", "content": "User sent an image attachment"}
    ]
    assert obj._build_messages_with_history(
        {"user_message": {"mime_type": "image/jpeg"}}, "plain text"
    ) == [{"role": "user", "content": "User sent an image attachment"}]

    obj.prompts_dir = Path(tmp_path)
    prompt_values = {
        "_get_seller_end_of_flow_reminder_prompt.txt": "special {support_email}",
        "_get_seller_common_response_prompt.txt": "regular {support_email}",
        "plain.txt": "plain {support_email} {support_info_email} {portal_url}",
    }

    def prompt_open(path, *args, **kwargs):
        name = Path(path).name
        if name not in prompt_values:
            raise FileNotFoundError(name)
        return FakeFile(prompt_values[name])

    monkeypatch.setattr(builtins, "open", prompt_open)
    obj._load_prompt = openai_module.OpenAIService._load_prompt.__get__(obj)
    assert obj._load_prompt(
        "seller", "_get_seller_common_response_prompt", workflow_state="end_of_flow_reminder"
    ) == "special support@example.test"
    assert obj._load_prompt(
        "seller", "_get_seller_common_response_prompt", workflow_state="collecting"
    ) == "regular support@example.test"
    assert obj._load_prompt(
        "seller",
        "plain",
        support_email="custom@example.test",
        support_info_email="info@example.test",
        portal_url="https://custom.example.test",
    ) == "plain custom@example.test info@example.test https://custom.example.test"
    assert obj._load_prompt("seller", "missing").startswith("Generate an appropriate response")
    assert settings.support_email in "support@example.test"


@pytest.mark.asyncio
async def test_classify_intent_notification_failure_boundaries(monkeypatch, service):
    obj, client, interaction_logger, _ = service
    fallback = {"fallback": True}
    obj._get_fallback_classification = AsyncMock(return_value=fallback)

    class FakeRateLimitError(Exception):
        pass

    class FakeAPIError(Exception):
        pass

    monkeypatch.setattr(openai_module, "RateLimitError", FakeRateLimitError)
    monkeypatch.setattr(openai_module, "get_user_phone_context", lambda: None)
    obj._notify_openai_error = AsyncMock(side_effect=RuntimeError("notification unavailable"))
    client.responses.create.side_effect = FakeRateLimitError("rate limited")
    assert await obj.classify_intent("rate limited") == fallback

    monkeypatch.setattr(openai_module, "APIError", FakeAPIError)
    client.responses.create.side_effect = FakeAPIError("api unavailable")
    assert await obj.classify_intent("api failure") == fallback

    client.responses.create.side_effect = RuntimeError("unexpected failure")
    assert await obj.classify_intent("unexpected failure") == fallback
    assert obj._notify_openai_error.await_count == 3
    assert interaction_logger.log_error.call_count == 3

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({"intent": "context", "confidence": 70})
    await obj.classify_intent(
        "context dict",
        {"workflow_state": {"extracted_entities": {"description": "bolt"}}},
    )
    await obj.classify_intent(
        "context scalar",
        {"workflow_state": {"extracted_entities": "opaque"}},
    )
    await obj.classify_intent(
        "section context",
        {
            "workflow_state": {
                "sectioned_rfq": {
                    "active": True,
                    "current_section": "items",
                    "awaiting_delivery_modification": False,
                    "awaiting_items_modification": False,
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_summary_extraction_without_usage_and_intent_switch_api_fallback(monkeypatch, service):
    obj, client, _, _ = service

    client.responses.create.return_value = response(
        {"products": [{"description": "bolt"}], "resolved_references": [], "confidence": 85},
        usage=False,
    )
    extracted = await obj.extract_entities_with_summary_context(
        "same bolt", [{"summary": "previous", "entities": {}}]
    )
    assert extracted["success"] is True
    assert extracted["confidence"] == 85

    client.responses.create.return_value = response(None, usage=False)
    assert not (await obj.extract_entities_with_summary_context("nothing", []))["success"]

    class FakeAPIError(Exception):
        pass

    scheduled = []

    def fake_create_task(coroutine):
        scheduled.append(coroutine)
        coroutine.close()
        return object()

    monkeypatch.setattr(openai_module, "APIError", FakeAPIError)
    monkeypatch.setattr(openai_module.asyncio, "create_task", fake_create_task)
    obj._notify_openai_error = AsyncMock()
    client.responses.create.side_effect = FakeAPIError("switch unavailable")
    failed = await obj.analyze_intent_switch_response("continue", {})
    assert failed["confidence"] == 20
    assert failed["chosen_action"] == "continue_current"
    assert len(scheduled) == 1

    client.responses.create.side_effect = None
    client.responses.create.return_value = response({}, output_type="text", usage=False)
    non_function = await obj.analyze_intent_switch_response("text response", {})
    assert non_function["confidence"] == 30
    assert non_function["success"] is False
    client.responses.create.return_value = response(None, usage=False)
    no_output = await obj.analyze_intent_switch_response("empty response", {})
    assert no_output["confidence"] == 30
    assert no_output["success"] is False


@pytest.mark.asyncio
async def test_rate_limit_timeout_persists_and_cleans_every_boundary(monkeypatch, service):
    obj, _, _, _ = service
    import app.database as database_module
    import app.models as models_module
    import app.redis_db as redis_module
    import app.services.helpers.session_helpers as session_module
    import app.services.whatsapp_service as whatsapp_module

    session_data = {
        "session_id": "session-1",
        "external_user_id": "user-1",
        "workflow_type": "rfq_creation",
        "workflow_state": {"stage": "collecting"},
        "conversation_history": {"openai_messages": []},
        "extracted_entities": [{"description": "bolt"}],
        "retention_date": "2030-01-01",
        "created_at": "2029-01-01T00:00:00",
        "last_activity_at": "2029-01-01T00:01:00",
        "unexpected": "ignored",
    }
    session_store = MagicMock()
    session_store.get_session = AsyncMock(return_value=session_data)
    session_store.delete_session = AsyncMock()
    direct_redis = SimpleNamespace(
        init_client=AsyncMock(),
        client=SimpleNamespace(delete=AsyncMock(return_value=9)),
    )
    db_manager = MagicMock()
    captured_session = {}

    def make_session(**kwargs):
        captured_session.update(kwargs)
        return kwargs

    whatsapp = MagicMock(send_message=AsyncMock())
    monkeypatch.setattr(
        session_module.SessionHelpers,
        "generate_session_id",
        staticmethod(lambda phone, kind: "session-1"),
    )
    monkeypatch.setattr(redis_module, "get_session_redis_service", lambda: session_store)
    monkeypatch.setattr(redis_module, "get_redis_service", lambda: direct_redis)
    monkeypatch.setattr(database_module, "DatabaseManager", lambda: db_manager)
    monkeypatch.setattr(
        models_module,
        "ConversationOutcome",
        SimpleNamespace(abandoned=SimpleNamespace(value="abandoned")),
    )
    monkeypatch.setattr(models_module, "ConversationSession", make_session)
    monkeypatch.setattr(whatsapp_module, "WhatsAppService", lambda: whatsapp)
    monkeypatch.setattr(openai_module, "utc_now", lambda: __import__("datetime").datetime(2030, 2, 3, 4, 5, 6))

    await obj._handle_rate_limit_timeout("+1555")
    assert session_data["outcome"] == "abandoned"
    assert session_data["completed_at"] == "2030-02-03T04:05:06"
    assert captured_session["session_id"] == "session-1"
    assert "unexpected" not in captured_session
    db_manager.save_conversation_session.assert_called_once_with(captured_session)
    db_manager.close.assert_called_once()
    direct_redis.client.delete.assert_called_once_with(
        "1555:incoming",
        "1555:outgoing",
        "1555:processing",
        "1555:session",
        "1555:batch_trigger",
        "1555:response_ready",
        "1555:ack_sent",
        "1555:pending_reply",
        "1555:last_activity",
    )
    session_store.delete_session.assert_awaited_once_with("session-1")
    whatsapp.send_message.assert_awaited_once_with(
        recipient_id="+1555",
        message=(
            "Sorry, your request is taking longer than expected due to high traffic. "
            "Please try sending your message again in some time."
        ),
        skip_concatenation=True,
    )

    session_store.get_session.return_value = dict(session_data)
    monkeypatch.setattr(database_module, "DatabaseManager", lambda: (_ for _ in ()).throw(RuntimeError("db init")))
    await obj._handle_rate_limit_timeout("1555")

    monkeypatch.setattr(database_module, "DatabaseManager", lambda: db_manager)
    session_store.get_session.return_value = None
    whatsapp.send_message.side_effect = RuntimeError("whatsapp unavailable")
    await obj._handle_rate_limit_timeout("1555")
    # The DB-init failure still has a session and therefore deletes it; the
    # no-session call above must not add another delete.
    assert session_store.delete_session.await_count == 2


@pytest.mark.asyncio
async def test_clarification_entities_and_empty_categorization_fallback(service):
    obj, client, _, _ = service
    client.responses.create.return_value = response(
        {"progress_acknowledgment": "Thanks", "questions": ["Quantity?"]}
    )
    context = {
        "user_message": "Please quote the attached bolt",
        "extracted_entities": {
            "attachments": [{"file_content": "secret-base64", "name": "bolt.png"}],
            "items": [{"description": "bolt"}],
        },
    }
    clarification = await obj.generate_clarification_response(["Fallback?"], 60, context)
    assert "Quantity?" in clarification
    clarification_request = client.responses.create.await_args.kwargs["input"][0]["content"]
    assert "[base64_data]" in clarification_request
    assert "secret-base64" not in clarification_request


@pytest.mark.asyncio
async def test_rfq_confirmation_serialization_and_opt_out_error(service):
    obj, client, _, _ = service
    rfq_data = {
        "items": [
            {"brand": 123, "remarks": "\x00" + "r" * 80, "description": "\x00bolt"},
            "not-a-dict",
            {"brand": "Acme", "remarks": "short", "description": "nut"},
            {"description": 42},
            {"brand": "", "remarks": "", "description": ""},
            {"description": "hidden"},
        ],
        "delivery_date": "not-an-iso-date",
    }
    client.responses.create.return_value = response({"summary": "ready"})
    assert await obj.generate_rfq_confirmation(rfq_data, {"user_message": "buy"}) == "ready"
    confirmation_input = client.responses.create.await_args.kwargs["input"][0]["content"]
    assert '"brand": 123' in confirmation_input
    assert "not-an-iso-date" in confirmation_input
    assert '"description": "hidden"' not in confirmation_input

    client.responses.create.return_value = response({"summary": "x" * 901})
    long_result = await obj.generate_rfq_confirmation(
        {"items": [{"description": "bolt"}], "delivery_date": 123}, {}
    )
    assert len(long_result) == 900
    assert "+" not in long_result

    client.responses.create.return_value = response({})
    assert await obj.generate_rfq_confirmation({}, {}) == "No summary generated"

    nested = {
        "attachments": [{"file_content": "secret"}, "raw"],
        "nested": [{"attachments": [{"file_content": "nested-secret"}]}],
        "tuple": (1, {"value": 2}),
    }
    stripped = obj._strip_base64_from_entities(nested)
    assert stripped["attachments"][0]["file_content"] == "[base64_data]"
    assert stripped["attachments"][1] == "raw"
    assert stripped["nested"][0]["attachments"][0]["file_content"] == "[base64_data]"

    cleaned = obj._clean_for_json_serialization(
        {"whole": 2.0, "fraction": 2.5, "enum": SimpleNamespace(value="chosen"), "bad": object()}
    )
    assert cleaned["whole"] == 2
    assert cleaned["fraction"] == 2.5
    assert cleaned["enum"] == "chosen"
    assert isinstance(cleaned["bad"], str)

    client.responses.create.side_effect = RuntimeError("offline")
    assert await obj.generate_opt_out_confirmation("Acme") == (
        "Hi Acme, you're now opted out of RFQ notifications. "
        "To opt back in, reply 'opt-in'."
    )
