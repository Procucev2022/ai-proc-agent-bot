"""Focused deterministic coverage for ChatService's remaining decision paths.

The tests construct ChatService without running its production constructor unless
that constructor is the behavior under test. Every database, cache, AI, Redis,
WhatsApp, HTTP, background, and filesystem boundary is replaced with a mock.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.chat_service as chat_mod
from app.models import ConversationOutcome, WorkflowType


# Deterministic doubles ------------------------------------------------------


def settings(**overrides):
    values = {
        "redis_url": "redis://unit/0",
        "redis_session_storage_enabled": False,
        "support_contact_info": "support@example.test",
        "support_email": "support@example.test",
        "rfq_max_allowed": 2,
        "use_sectioned_rfq": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def user(role="buyer", **overrides):
    role_value = SimpleNamespace(value=role) if isinstance(role, str) else role
    values = {
        "id": "user-1",
        "org_id": "org-1",
        "phone_number": "+919999999999",
        "email": "buyer@example.test",
        "username": "buyer@example.test",
        "name": "Test User",
        "role": role_value,
        "is_registered": True,
        "self_client": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def session(workflow_type=WorkflowType.general_inquiry, workflow_state=None, **state):
    values = {
        "session_id": "session-1",
        "external_user_id": "919999999999",
        "phone_number": "+919999999999",
        "workflow_type": workflow_type,
        "workflow_state": dict(workflow_state or state),
        "conversation_history": {"messages": [], "openai_messages": [], "metadata": []},
        "extracted_entities": [],
        "product_items": [],
        "rfq_ids": [],
        "outcome": None,
        "retention_date": date(2025, 1, 1),
        "last_activity_at": datetime(2025, 1, 1),
        "created_at": datetime(2025, 1, 1),
        "completed_at": None,
    }
    return SimpleNamespace(**values)


def cache_double(**overrides):
    cache = SimpleNamespace(
        store_meaningful_message=AsyncMock(),
        clear_meaningful_message=AsyncMock(),
        get_meaningful_message=AsyncMock(return_value=None),
        clear_user_data=AsyncMock(),
        get_user_data=AsyncMock(return_value={}),
        get_account_options_for_intent_switch=AsyncMock(return_value=None),
    )
    for name, value in overrides.items():
        setattr(cache, name, value)
    return cache


def chat_stub():
    service = object.__new__(chat_mod.ChatService)

    service.whatsapp_service = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(),
    )
    service.session_manager = SimpleNamespace(
        get_conversation_context=AsyncMock(return_value=session()),
        save_session=AsyncMock(),
        send_and_track_message=AsyncMock(),
        add_message_to_history=MagicMock(),
    )
    service.settings = settings()
    service.db_manager = SimpleNamespace(
        save_conversation_session=MagicMock(return_value={"saved": True}),
        append_session_data=MagicMock(),
        close=MagicMock(),
    )

    service._intent_service = SimpleNamespace(classify_intent=AsyncMock())
    service._entity_service = SimpleNamespace()
    service._openai_service = SimpleNamespace(
        close=AsyncMock(),
        generate_response=AsyncMock(return_value="generated"),
        generate_clarification_response=AsyncMock(return_value="clarify"),
        parse_confirmation_response=AsyncMock(return_value="yes"),
        extract_entities=AsyncMock(return_value={"rfq_id": []}),
    )
    service._response_helpers = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="context"),
        generate_completion_response=AsyncMock(return_value="complete"),
        generate_clarification_response=AsyncMock(return_value="clarify"),
        generate_seller_contextual_response=AsyncMock(return_value="seller error"),
    )
    service._authentication_service = SimpleNamespace(
        otp_service=SimpleNamespace(validate_otp=AsyncMock(return_value={"status": "otp_invalid"})),
        validate_token=AsyncMock(return_value=None),
        user_authenticate=AsyncMock(return_value={"success": True}),
        store_user_session_with_email=AsyncMock(return_value={"success": True}),
        handle_email_confirmation=AsyncMock(return_value={"status": "email_confirmed"}),
        handle_email_otp_validation=AsyncMock(return_value={"status": "otp_invalid"}),
    )
    service._registration_service = SimpleNamespace(
        initiate_registration=AsyncMock(return_value={"status": "registration_started"})
    )
    service._confirmation_service = SimpleNamespace()
    service._exit_service = SimpleNamespace(
        handle_exit_intent=AsyncMock(return_value={"status": "exited"}),
        handle_exit_confirmation=AsyncMock(return_value={"status": "exit_confirmed"}),
    )
    service._cancel_service = SimpleNamespace(
        handle_cancel_intent=AsyncMock(return_value={"status": "cancel"}),
        handle_cancel_confirmation=AsyncMock(return_value={"status": "cancelled"}),
        confirmation_service=SimpleNamespace(parse_confirmation=AsyncMock(return_value="yes")),
        _send_cancellation_message=AsyncMock(),
        _clear_workflow_state=AsyncMock(),
    )
    service._faq_service = SimpleNamespace(get_faq_answer=AsyncMock(return_value=None))
    service._confirmation_handler = SimpleNamespace(
        handle_confirmation_button=AsyncMock(return_value={"status": "confirmed"}),
        handle_pending_confirmations=AsyncMock(return_value={"status": "pending"}),
        handle_optional_fields_response=AsyncMock(return_value={"status": "optional"}),
    )
    service._intent_switch_handler = SimpleNamespace(
        should_handle_intent_switch=AsyncMock(return_value=False),
        handle_intent_switch_response=AsyncMock(return_value={"status": "clarification"}),
        handle_intent_switch_choice=AsyncMock(return_value={"status": "switch"}),
    )
    service._products_array_handler = SimpleNamespace(
        handle_products_array=AsyncMock(return_value={"status": "products"})
    )
    service._purchase_intent_handler = SimpleNamespace(
        handle_purchase_intent=AsyncMock(return_value={"status": "purchase"})
    )
    service._attachment_decision_handler = SimpleNamespace(
        handle_attachment_decision=AsyncMock(return_value={"status": "attachment"})
    )
    service._image_processor = SimpleNamespace(
        process_image_message=AsyncMock(return_value={"status": "image"})
    )
    service._format_modification_handler = SimpleNamespace(
        handle_format_modification=AsyncMock(return_value={"status": "format"})
    )
    service._seller_service = SimpleNamespace(
        handle_seller_workflow=AsyncMock(return_value={"success": True, "message": "seller"})
    )
    service._rfq_status_service = SimpleNamespace(
        handle_rfq_status_inquiry=AsyncMock(return_value={"status": "rfq_status"})
    )
    service._bfs_search_handler = SimpleNamespace(
        handle_bfs_search=AsyncMock(return_value={"status": "bfs"}),
        handle_button=AsyncMock(return_value={"status": "bfs_button"}),
        handle_bid_format_input=AsyncMock(return_value={"status": "bid_format"}),
        handle_bid_otp_input=AsyncMock(return_value={"status": "bid_otp"}),
    )
    service.chat_summary_service = SimpleNamespace(
        generate_session_summary=AsyncMock()
    )
    service.daily_summary_service = SimpleNamespace(
        generate_daily_summary=AsyncMock()
    )
    return service


class FakeLock:
    def __init__(self, acquired=True, release_error=None):
        self.acquire = AsyncMock(return_value=acquired)
        self.release = AsyncMock(side_effect=release_error)


@pytest.fixture(autouse=True)
def isolate_technical_failures(monkeypatch):
    monkeypatch.setattr(
        "app.utils.technical_failure_handler.handle_technical_failure", AsyncMock()
    )


# Constructor, lazy services, and early workflow helpers --------------------


def test_chat_targeted_constructor_queue_fallback_and_lazy_services(monkeypatch):
    constructors = [
        "ChatSummaryService",
        "DailySummaryService",
        "DatabaseManager",
        "VendorService",
        "RFQService",
        "RFQBackgroundService",
        "SessionManagementService",
    ]
    for name in constructors:
        monkeypatch.setattr(chat_mod, name, lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(chat_mod, "get_settings", lambda: settings())
    direct_whatsapp = MagicMock()
    monkeypatch.setattr(
        "app.services.whatsapp_service.WhatsAppService", lambda: direct_whatsapp
    )

    queue = MagicMock()
    queued = chat_mod.ChatService(db_session="db", message_queue_service=queue)
    direct = chat_mod.ChatService(db_session="db")
    assert queued.whatsapp_service is queue
    assert direct.whatsapp_service is direct_whatsapp
    assert direct.settings.support_email == "support@example.test"

    lazy_factories = {
        "IntentService": "intent",
        "EntityService": "entity",
        "OpenAIService": "openai",
        "ResponseHelpers": "response",
    }
    for name, value in lazy_factories.items():
        monkeypatch.setattr(chat_mod, name, lambda value=value, **kwargs: value)

    internal_paths = [
        "app.tools.confirmation_tool.ConfirmationTool",
        "app.services.confirmation_service.ConfirmationService",
        "app.services.authentication_service.AuthenticationService",
        "app.services.registration_service.RegistrationService",
        "app.services.exit_service.ExitService",
        "app.services.cancel_service.CancelService",
        "app.services.faq_service.FAQService",
        "app.services.handlers.confirmation_handler.ConfirmationHandler",
        "app.services.handlers.intent_switch_handler.IntentSwitchHandler",
        "app.services.handlers.products_array_handler.ProductsArrayHandler",
        "app.services.handlers.format_modification_handler.FormatModificationHandler",
        "app.services.handlers.purchase_intent_handler.PurchaseIntentHandler",
        "app.services.handlers.attachment_decision_handler.AttachmentDecisionHandler",
        "app.services.processors.image_message_processor.ImageMessageProcessor",
        "app.services.seller_service.SellerService",
        "app.services.rfq_status_service.RFQStatusService",
        "app.services.handlers.bfs_search_handler.BFSSearchHandler",
    ]
    for path in internal_paths:
        monkeypatch.setattr(path, lambda *args, **kwargs: MagicMock())

    properties = [
        direct.intent_service,
        direct.entity_service,
        direct.openai_service,
        direct.response_helpers,
        direct.confirmation_service,
        direct.authentication_service,
        direct.registration_service,
        direct.exit_service,
        direct.cancel_service,
        direct.faq_service,
        direct.confirmation_handler,
        direct.intent_switch_handler,
        direct.products_array_handler,
        direct.format_modification_handler,
        direct.purchase_intent_handler,
        direct.attachment_decision_handler,
        direct.image_processor,
        direct.seller_service,
        direct.rfq_status_service,
        direct.bfs_search_handler,
    ]
    assert all(value is not None for value in properties)
    assert direct.intent_service is direct.intent_service
    assert direct.purchase_intent_handler.confirmation_handler is direct.confirmation_handler
    assert direct.attachment_decision_handler.purchase_intent_handler is direct.purchase_intent_handler


@pytest.mark.asyncio
async def test_chat_section_activation_transition_cleanup_and_summary_response_helpers(monkeypatch):
    service = chat_stub()
    target = session(workflow_type=None)
    monkeypatch.setattr(chat_mod.WorkflowManager, "initialize_sectioned_rfq", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
    assert await service._activate_sectioned_rfq(user(), target, "start") == {
        "status": "sectioned_rfq_activated"
    }
    service.session_manager.save_session.assert_awaited_once()
    service.whatsapp_service.send_message.assert_awaited_once()

    assert await service._validate_seller_workflow_transition(
        target, "list_rfq_to_seller", "seller_respond_to_rfq_list"
    )
    assert not await service._validate_seller_workflow_transition(target, "unknown", "completed")

    service._openai_service.close.side_effect = RuntimeError("close")
    await service.cleanup()
    service._response_helpers.generate_contextual_response.return_value = "contextual"
    assert await service._generate_contextual_response({}, [], "stage") == "contextual"
    assert await service._generate_completion_response({}, {}) == "complete"
    assert await service._generate_clarification_response([], 50, {}) == "clarify"
    await service._send_contextual_response("+1", {}, [], "stage")


# process_message authentication, attachment, and fallback paths ------------


@pytest.mark.asyncio
async def test_chat_process_message_auth_status_matrix(monkeypatch):
    cache = cache_double()
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)

    async def invoke(auth_result, workflow_state=None):
        service = chat_stub()
        current = session(workflow_state=workflow_state or {})
        service.session_manager.get_conversation_context.return_value = current
        # Keep the pre-existing meaningful message intact; a greeting would be
        # tracked by process_message before the auth result is handled.
        service._intent_service.classify_intent.return_value = {
            "intent": "other", "confidence": 0, "relevant_message": "hello"
        }
        service.authentication_orchestrator_flow = AsyncMock(return_value=auth_result)
        service.handle_irrelevant_message_flow = AsyncMock()
        return await service.process_message("+1", "hello", "text"), service, current

    result, service, current = await invoke({"status": "buyer_options_presented"})
    assert result["status"] == "buyer_options_presented"
    assert service.session_manager.save_session.await_count == 1

    preserved = {
        "last_meaningful_message": "buy pumps",
        "last_meaningful_intent_result": {"intent": "buy_something", "confidence": 90},
    }
    result, service, current = await invoke({"status": "seller_options_presented"}, preserved)
    assert result["status"] == "seller_options_presented"
    assert current.workflow_type is None
    assert current.workflow_state["last_meaningful_message"] == "buy pumps"
    assert service.session_manager.save_session.await_args.args[1] is None

    service_exit_result = {"status": "redirected_to_support"}
    result, service, _ = await invoke(service_exit_result)
    service.exit_service.handle_exit_intent.assert_awaited_once()
    assert result["status"] == "exited"

    result, service, current = await invoke({
        "status": "registration_completed",
        "user_type": "buyer",
        "registration_flow_complete": True,
    })
    assert result == {
        "status": "registration_completed",
        "message": "Registration successful",
        "flow_terminated": True,
    }
    assert current.workflow_type is None and current.workflow_state == {}

    result, service, _ = await invoke({
        "status": "verification_required",
        "redirect_info": {"message": "verify email"},
    })
    assert result["status"] == "verification_required"
    result, service, _ = await invoke({
        "status": "verification_failed",
        "redirect_info": {"message": "pending review"},
    })
    assert result["status"] == "verification_failed"

    result, _, _ = await invoke({"status": "new_user_registration_sent"})
    assert result["status"] == "new_user_registration_sent"
    result, _, _ = await invoke({"status": "unexpected"})
    assert result == {"status": "error", "error": "Authentication failed"}


@pytest.mark.asyncio
async def test_chat_process_message_authenticated_documents_locks_and_critical_fallback(monkeypatch):
    class AuthenticatedUser(SimpleNamespace):
        pass

    monkeypatch.setattr(chat_mod, "User", AuthenticatedUser)
    authenticated = AuthenticatedUser(**vars(user()))
    redis = MagicMock()
    lock = FakeLock(acquired=True, release_error=RuntimeError("release"))
    redis.lock.return_value = lock
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_url="redis://unit"))

    service = chat_stub()
    current = session()
    service.session_manager.get_conversation_context.return_value = current
    service._intent_service.classify_intent.return_value = {"intent": "greeting", "confidence": 0}
    service.authentication_orchestrator_flow = AsyncMock(return_value=authenticated)
    service.handle_irrelevant_message_flow = AsyncMock()

    result = await service.process_message("+1", {"id": "media"}, "document")
    assert result == {"status": "image"}
    lock.acquire.assert_awaited_once_with(blocking=False)
    lock.release.assert_awaited_once()
    assert service.session_manager.save_session.await_count >= 1

    busy = FakeLock(acquired=False)
    redis.lock.return_value = busy
    result = await service.process_message("+1", {"id": "media"}, "image")
    assert result == {"status": "handled", "response": "upload_in_progress"}

    result = await service.process_message("+1", "voice", "voice")
    assert result["status"] == "error" and "Unknown message type" in result["error"]

    failing = chat_stub()
    retry_session = session()
    failing.session_manager.get_conversation_context = AsyncMock(
        side_effect=[RuntimeError("context"), retry_session]
    )
    exit_service = MagicMock()
    exit_service.handle_exit_intent = AsyncMock(return_value={"status": "exited"})
    monkeypatch.setattr("app.services.exit_service.ExitService", lambda *args, **kwargs: exit_service)
    result = await failing.process_message("+1", "hello", "text")
    assert result["status"] == "technical_failure"
    exit_service.handle_exit_intent.assert_awaited_once()


# Text state transitions and authentication/intent routing ------------------


@pytest.mark.asyncio
async def test_chat_text_dict_users_role_switches_and_pending_seller_workflow(monkeypatch):
    service = chat_stub()
    registered = user()

    assert await service._process_text_message(
        {"verification_required": True, "verification_info": {"reason": "email"}},
        session(), "hello", {"intent": "greeting"}
    ) == {"status": "verification_required", "verification_info": {"reason": "email"}}

    class BrokenSchema:
        @classmethod
        def from_mixed_data(cls, value):
            raise ValueError("bad user")

    monkeypatch.setattr("app.schemas.user.User", BrokenSchema)
    assert await service._process_text_message(
        {"email": "broken"}, session(), "hello", {"intent": "greeting"}
    ) == {"status": "error", "error": "Invalid user data"}

    class Switcher:
        def __init__(self, *_args):
            pass

        async def handle_role_switch_response(self, current, sess, message, auth):
            sess.workflow_state.pop("pending_role_switch", None)
            return {"status": "authentication_completed", "original_message": "buy pumps", "original_intent_result": {"intent": "greeting", "confidence": 90}}

        async def handle_account_switch_response(self, current, sess, message, auth):
            return {"status": "account_switch_handled"}

    monkeypatch.setattr(
        "app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch",
        Switcher,
    )
    service.authentication_service.validate_token.return_value = registered
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting"})
    role_switch_session = session(pending_role_switch=True)
    result = await service._process_text_message(registered, role_switch_session, "yes", {})
    assert result == {"status": "greeting"}
    account_session = session(pending_account_switch=True)
    result = await service._process_text_message(registered, account_session, "2", {})
    assert result["status"] == "account_switch_handled"

    service._handle_seller_rfq_intimation_flow = AsyncMock(return_value={"status": "seller_flow"})
    seller_session = session(workflow_type=WorkflowType.seller_rfq_intimation)
    assert await service._process_text_message(registered, seller_session, "interest", {}) == {"status": "seller_flow"}


@pytest.mark.asyncio
async def test_chat_text_intent_switch_outcomes_pending_states_and_bfs_results(monkeypatch):
    service = chat_stub()
    buyer = user()
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting"})
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service._handle_fallback = AsyncMock(return_value={"status": "fallback"})
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status"})
    service.handle_irrelevant_message_flow = AsyncMock()
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())

    outcomes = [
        {"status": "corrupted_intent_switch_data"},
        {"status": "continue_current_workflow"},
        {
            "status": "switch_to_new_intent",
            "new_intent": "buy_something",
            "new_message": "pumps",
            "intent_result": {"intent": "buy_something", "confidence": 90},
        },
        {
            "status": "switch_to_new_intent",
            "new_intent": "rfq_status_check",
            "new_message": "status",
            "intent_result": {"intent": "rfq_status_check", "confidence": 90},
        },
        {
            "status": "switch_to_new_intent",
            "new_intent": "sell_something",
            "new_message": "sell",
            "intent_result": {"intent": "sell_something", "confidence": 90},
        },
        {
            "status": "switch_to_new_intent",
            "new_intent": "general_inquiry",
            "new_message": "help",
            "intent_result": {"intent": "general_inquiry", "confidence": 90},
        },
        {
            "status": "switch_to_new_intent",
            "new_intent": "greeting",
            "new_message": "hi",
            "intent_result": {"intent": "greeting", "confidence": 90},
        },
        {
            "status": "switch_to_new_intent",
            "new_intent": "other",
            "new_message": "other",
            "intent_result": {"intent": "other", "confidence": 90},
        },
        {"status": "error", "error": "switch failed"},
        {"status": "clarification_requested"},
    ]
    for outcome in outcomes:
        service._intent_switch_handler.handle_intent_switch_response.return_value = outcome
        result = await service._process_text_message(
            buyer,
            session(pending_intent_switch=True),
            "choice",
            {"intent": "greeting", "confidence": 80},
        )
        assert result is None or result.get("status") in {
            "greeting", "purchase", "status", "seller", "fallback", "error", "clarification_requested"
        }
    service._response_helpers.generate_contextual_response.assert_awaited()
    service.session_manager.send_and_track_message.assert_awaited()

    optional = session(pending_optional_rfq={"description": "pump"})
    service._confirmation_handler.handle_optional_fields_response.return_value = {"status": "optional"}
    assert (await service._process_text_message(buyer, optional, "skip", {"intent": "other", "confidence": 90}))["status"] == "optional"

    service._confirmation_handler.handle_optional_fields_response.return_value = {"status": "continue_with_purchase_intent"}
    optional = session(pending_optional_combined_rfq={"description": "valve"})
    assert (await service._process_text_message(buyer, optional, "details", {"intent": "other", "confidence": 90}))["status"] == "purchase"

    bfs = session(bfs_results=[{
        "description": "pump", "specification": "steel", "availableQuantity": "3", "ageOfAsset": 2, "sellPrice": 1250
    }])
    assert (await service._process_text_message(buyer, bfs, "again", {"intent": "other", "confidence": 90}))["status"] == "bfs_awaiting_button_click"
    empty_bfs = session(bfs_results={"unexpected": True}, bfs_searched_products=["pump"])
    assert (await service._process_text_message(buyer, empty_bfs, "again", {"intent": "other", "confidence": 90}))["status"] == "bfs_awaiting_button_click"
    assert "bfs_results" not in empty_bfs.workflow_state


@pytest.mark.asyncio
async def test_chat_text_registration_role_switch_and_tracked_message_sanitization(monkeypatch):
    service = chat_stub()
    buyer = user(role="buyer")
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)

    class Switcher:
        def __init__(self, *_args):
            pass

        async def handle_role_switch_confirmation(self, *_args):
            return {"status": "role_switch"}

        async def handle_account_switch_confirmation(self, *_args):
            return {"status": "account_switch"}

    monkeypatch.setattr(
        "app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch",
        Switcher,
    )

    result = await service._process_text_message(
        buyer, session(), "register seller", {
            "intent": "register_account", "confidence": 90,
            "context_analysis": {"registration_details": {"registration_type": "seller"}},
        }
    )
    assert result["status"] == "role_switch"
    result = await service._process_text_message(
        buyer, session(), "register buyer", {
            "intent": "register_account", "confidence": 90,
            "context_analysis": {"registration_details": {"registration_type": "buyer"}},
        }
    )
    assert result["status"] == "account_switch"
    result = await service._process_text_message(
        buyer, session(), "register", {
            "intent": "register_account", "confidence": 90,
            "context_analysis": {"registration_details": {}},
        }
    )
    assert result["status"] == "registration_clarification_requested"

    unregistered = user(is_registered=False)
    service._handle_registration_workflow = AsyncMock(return_value={"status": "registration_started"})
    result = await service._process_text_message(
        unregistered, session(), "new buyer", {
            "intent": "register_account", "confidence": 90,
            "context_analysis": {"registration_details": {"registration_type": "buyer"}},
        }
    )
    assert result["status"] == "registration_started"

    cache = cache_double(
        get_account_options_for_intent_switch=AsyncMock(return_value={"success": True, "has_target_accounts": True})
    )
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)
    result = await service._process_text_message(
        buyer, session(), "sell", {"intent": "sell_something", "confidence": 90}
    )
    assert result["status"] == "role_switch"
    seller = user(role="seller")
    result = await service._process_text_message(
        seller, session(), "buy", {"intent": "buy_something", "confidence": 90}
    )
    assert result["status"] == "seller_flow_processed"

    recent = session(recently_completed_registration=True, registration_completion_time="now")
    result = await service._process_text_message(
        buyer, recent, "maybe", {"intent": "unknown", "confidence": 0.2}
    )
    assert result["status"] == "purchase"
    assert "recently_completed_registration" not in recent.workflow_state

    tracked = session(
        workflow_type=WorkflowType.general_inquiry,
        last_meaningful_message="old pumps",
        last_meaningful_intent_result={
            "intent": "buy_something", "confidence": "90", "all_intent_scores": {"buy": 1, "bad": object()},
            "context_analysis": {"stage": "rfq", "nested": {"ok": True, "bad": object()}},
            "reasoning": 123, "suggested_clarification": None, "success": 1,
        },
    )
    result = await service._process_text_message(
        buyer, tracked, "current", {"intent": "buy_something", "confidence": 90}
    )
    assert result["status"] == "purchase"
    purchase_args = service._purchase_intent_handler.handle_purchase_intent.await_args.args
    assert purchase_args[2] == "old pumps"
    assert tracked.workflow_state["meaningful_message_used"] is True


# Excel/document processing and confirmation paths --------------------------


@pytest.mark.asyncio
async def test_chat_excel_upload_complete_incomplete_direct_document_and_lock_paths(monkeypatch):
    service = chat_stub()
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    validator = SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"xlsx"})
    )
    processor = SimpleNamespace(
        process_excel_file=AsyncMock(return_value={"success": True, "items": [{"ItemDescription": "pump"}], "filename": "x.xlsx"})
    )
    monkeypatch.setattr(chat_mod, "ExcelValidationService", lambda: validator)
    monkeypatch.setattr(chat_mod, "ExcelProcessingService", lambda *_args: processor)
    monkeypatch.setattr(
        chat_mod.ExcelHelpers, "prepare_excel_context", lambda result, phone: {"completeness": 100, "excel_data": result}
    )
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "initialize_workflow_state", lambda s: s.workflow_state.setdefault("base", True))
    service._handle_complete_excel = AsyncMock(return_value={"status": "complete"})
    redis = MagicMock()
    lock = FakeLock(acquired=True)
    redis.lock.return_value = lock
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *args, **kwargs: redis)
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_url="redis://unit"))

    result = await service._process_excel_upload(
        user(), session(workflow_state={}), {"id": "media-1", "filename": "x.xlsx"}
    )
    assert result == {"status": "complete"}
    assert processor.process_excel_file.await_args.kwargs["filename"] == "x.xlsx"
    lock.release.assert_awaited_once()

    processor.process_excel_file.return_value = {"success": False, "items": [], "filename": "x.xlsx"}
    service._handle_complete_excel = chat_mod.ChatService._handle_complete_excel.__get__(service, chat_mod.ChatService)
    service._handle_incomplete_excel = AsyncMock(return_value={"status": "incomplete"})
    lock = FakeLock(acquired=True)
    redis.lock.return_value = lock
    result = await service._process_excel_upload(
        user(), session(workflow_state={}), {"document": {"link": "url", "filename": "x.xlsx"}}
    )
    assert result["status"] == "incomplete"
    lock.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_excel_confirmation_ai_keyword_sectioned_nonsectioned_cancel_and_error(monkeypatch):
    service = chat_stub()
    buyer = user(name="Ada Buyer", role="buyer")
    saved = {"products": [{"description": "pump"}], "processing_result": {"items": []}, "filename": "x.xlsx"}

    service._products_array_handler.handle_products_array.return_value = {"status": "products"}
    monkeypatch.setattr("app.config.get_settings", lambda: settings(use_sectioned_rfq=False))
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True, stale="remove")
    assert (await service._handle_excel_confirmation_response(buyer, current, "yes"))["status"] == "products"
    assert "excel_confirmation_data" not in current.workflow_state

    service._openai_service.parse_confirmation_response.side_effect = RuntimeError("AI unavailable")
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True)
    assert (await service._handle_excel_confirmation_response(buyer, current, "proceed now"))["status"] == "products"

    service._openai_service.parse_confirmation_response.side_effect = None
    service._openai_service.parse_confirmation_response.return_value = "no"
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True)
    assert (await service._handle_excel_confirmation_response(buyer, current, "no"))["status"] == "excel_cancelled_redirected_to_greeting"
    assert current.workflow_state == {} and current.workflow_type is None

    seller = user(role="seller", name="Bob Seller")
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True)
    assert (await service._handle_excel_confirmation_response(seller, current, "cancel"))["status"] == "excel_cancelled_redirected_to_greeting"

    service._openai_service.parse_confirmation_response.return_value = None
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True)
    assert (await service._handle_excel_confirmation_response(buyer, current, "hmm"))["status"] == "excel_clarification_requested"

    current = session(excel_confirmation_data={"products": []}, awaiting_excel_confirmation=True)
    service._openai_service.parse_confirmation_response.return_value = "yes"
    assert (await service._handle_excel_confirmation_response(buyer, current, "yes"))["status"] == "excel_data_missing"

    monkeypatch.setattr("app.config.get_settings", lambda: settings(use_sectioned_rfq=True))
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True)
    service._handle_excel_sectioned_rfq = AsyncMock(return_value={"status": "validation_failed"})
    assert (await service._handle_excel_confirmation_response(buyer, current, "yes"))["status"] == "validation_failed"
    service._handle_excel_sectioned_rfq.return_value = {"status": "excel_sectioned_rfq_initialized"}
    assert (await service._handle_excel_confirmation_response(buyer, current, "yes"))["status"] == "purchase"
    service._handle_excel_sectioned_rfq.return_value = {"status": "other"}
    assert (await service._handle_excel_confirmation_response(buyer, current, "yes"))["status"] == "other"

    service._openai_service.parse_confirmation_response.return_value = "yes"
    service._products_array_handler.handle_products_array.side_effect = RuntimeError("products")
    monkeypatch.setattr("app.config.get_settings", lambda: settings(use_sectioned_rfq=False))
    current = session(excel_confirmation_data=saved, awaiting_excel_confirmation=True)
    result = await service._handle_excel_confirmation_response(buyer, current, "yes")
    assert result["status"] == "error"
    service.whatsapp_service.send_configurable_buttons.assert_awaited()


@pytest.mark.asyncio
async def test_chat_excel_transform_conversion_and_sectioned_validation_edges(monkeypatch):
    service = chat_stub()
    date_handler = SimpleNamespace(
        _validate_delivery_date=AsyncMock(return_value={"is_valid": True, "normalized_date": "2025-04-01"})
    )
    monkeypatch.setattr(
        "app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler",
        lambda **kwargs: date_handler,
    )
    monkeypatch.setattr(
        "app.utils.pincode_lookup.get_location_from_pincode_async",
        AsyncMock(return_value={"city": "Pune", "state": "MH"}),
    )
    transformed, details = await service.transform_rfq_to_section_rfq_format(
        session(delivery_date="tomorrow", pincode="560001"),
        [{"description": "pump", "quantity": "2.5", "uom": "pcs"}, {"description": "valve", "quantity": "bad"}],
    )
    assert details == {"deliveryDate": "2025-04-01", "pincode": "560001", "city": "Pune", "state": "MH"}
    assert transformed[0]["quantity"] == 2.5 and transformed[1]["quantity"] == 0

    with pytest.raises(ValueError, match="6-digit"):
        await service.transform_rfq_to_section_rfq_format(session(pincode="123"), [])
    monkeypatch.setattr("app.utils.pincode_lookup.get_location_from_pincode_async", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="not found"):
        await service.transform_rfq_to_section_rfq_format(session(pincode="560001"), [])

    confirmed = session(sectioned_rfq={"sections": {"date_location": {"confirmed": True, "data": {"city": "Delhi", "pincode": "110001"}}}})
    products, details = await service.transform_rfq_to_section_rfq_format(confirmed, [{"description": "x", "quantity": 1}])
    assert details["city"] == "Delhi" and products[0]["pincode"] == "110001"

    converted = service._convert_excel_items_to_products_array([
        {"ItemDescription": " pump ", "Specification": "steel", "Quantity": "2.0", "Uom": "pcs", "Remarks": ""},
        {"ItemDescription": None, "Quantity": "not-number"},
    ])
    assert converted[0]["description"] == "pump" and converted[0]["quantity"] == "2.0"
    assert converted[1]["quantity"] is None

    class BrokenItem:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("field")

    with pytest.raises(RuntimeError, match="field"):
        service._convert_excel_items_to_products_array([BrokenItem()])

    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    monkeypatch.setattr(chat_mod.WorkflowManager, "initialize_sectioned_rfq", lambda s: None)
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_sectioned_rfq_section", lambda *args: None)
    service.transform_rfq_to_section_rfq_format = AsyncMock(side_effect=ValueError("bad file"))
    result = await service._handle_excel_sectioned_rfq(user(), session(), [])
    assert result["status"] == "validation_failed"
    service.transform_rfq_to_section_rfq_format.side_effect = RuntimeError("unexpected")
    result = await service._handle_excel_sectioned_rfq(user(), session(), [])
    assert result["status"] == "error"


# Button/list routing and seller/menu fallback paths ------------------------


@pytest.mark.asyncio
async def test_chat_button_sectioned_bfs_confirmation_and_completion_paths(monkeypatch):
    service = chat_stub()
    buyer = user()
    current = session()
    section_handler = SimpleNamespace(handle_section_button_click=AsyncMock(return_value={"status": "section_button"}))
    service._purchase_intent_handler = SimpleNamespace(
        handle_purchase_intent=AsyncMock(return_value={"status": "purchase"})
    )
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: True)
    monkeypatch.setattr(
        "app.services.handlers.sectioned_rfq_creation_handler.SectionedRFQCreationHandler",
        lambda **kwargs: section_handler,
    )
    monkeypatch.setattr(
        "app.services.cancel_service.CancelService",
        lambda *args, **kwargs: service._cancel_service,
    )
    assert (await service._handle_button_response(buyer, current, "confirm_items"))["status"] == "section_button"
    section_handler.handle_section_button_click.assert_awaited_once()

    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    assert (await service._handle_button_response(buyer, current, "confirm_items"))["status"] == "button_handled"

    current.workflow_state["bfs_search_pending"] = True
    assert (await service._handle_button_response(buyer, current, "check_availability_rfq|pump"))["status"] == "bfs"
    assert "bfs_search_pending" not in current.workflow_state
    assert (await service._handle_button_response(buyer, current, "check_availability_rfq|"))["status"] == "bfs_rfq_no_item"

    service._bfs_search_handler.handle_button.return_value = {"status": "bfs_activate_rfq"}
    service._activate_sectioned_rfq = AsyncMock(return_value={"status": "activated"})
    assert (await service._handle_button_response(buyer, current, "search_bfs"))["status"] == "activated"
    service._bfs_search_handler.handle_button.return_value = {"status": "bfs_send_cancel_message"}
    assert (await service._handle_button_response(buyer, current, "bfs_cancel"))["status"] == "bfs_cancelled"

    service._confirmation_handler.handle_confirmation_button.return_value = {"status": "confirmed"}
    assert (await service._handle_button_response(buyer, current, "continue_rfq"))["status"] == "button_handled"
    assert (await service._handle_button_response(buyer, current, "confirm_no_changes"))["status"] == "confirmed"

    database = MagicMock()
    monkeypatch.setattr("app.database.DatabaseManager", lambda: database)
    redis_session = SimpleNamespace(delete_session=AsyncMock())
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_session_storage_enabled=True))
    service._confirmation_handler.handle_confirmation_button.return_value = {"status": "multiple_rfqs_created"}
    current.outcome = ConversationOutcome.completed
    assert (await service._handle_button_response(buyer, current, "confirm_rfq"))["status"] == "multiple_rfqs_created"
    database.append_session_data.assert_called_once()
    database.close.assert_called_once()
    redis_session.delete_session.assert_awaited_once_with(current.session_id)

    service._confirmation_handler.handle_confirmation_button.return_value = {"status": "failed"}
    assert (await service._handle_button_response(buyer, current, "confirm_rfq"))["status"] == "failed"
    assert (await service._handle_button_response(buyer, current, "random")) == {"status": "button_handled", "button_id": "random"}


@pytest.mark.asyncio
async def test_chat_button_menu_routes_auth_email_cancel_exit_and_new_rfq_state(monkeypatch):
    service = chat_stub()
    buyer = user()
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status"})
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller"})
    service._handle_support_request = AsyncMock(return_value={"status": "support"})
    service._handle_excel_confirmation_button = AsyncMock(return_value={"status": "excel"})
    service._handle_cancel_confirmation_button = AsyncMock(return_value={"status": "cancel"})
    service._handle_exit_confirmation_button = AsyncMock(return_value={"status": "exit"})
    service._handle_authentication_email_button = AsyncMock(return_value={"status": "email"})
    service._process_text_message = AsyncMock(return_value={"status": "modified"})
    service._activate_sectioned_rfq = AsyncMock(return_value={"status": "activated"})

    for button_id, expected in [
        ("rfq_status", "status"), ("check_rfqs", "status"), ("view_rfqs", "seller"),
        ("check_submissions", "seller"), ("get_support", "support"), ("exit", "exited"),
        ("confirm_excel", "excel"), ("confirm_cancel", "cancel"), ("confirm_exit", "exit"),
        ("no_rfq", "modified"), ("confirm_email", "email"),
    ]:
        result = await service._handle_button_response(buyer, session(), button_id)
        assert result["status"] == expected

    tracked = session(last_meaningful_message="pumps", last_meaningful_intent_result={"intent": "buy_something"})
    assert (await service._handle_button_response(buyer, tracked, "create_rfq"))["status"] == "purchase"
    assert tracked.workflow_state["meaningful_message_used"] is True
    stale = session(pending_rfq={"x": 1}, last_meaningful_message="stale", last_meaningful_intent_result={"intent": "buy_something"})
    assert (await service._handle_button_response(buyer, stale, "create_rfq"))["status"] == "activated"

    service._authentication_service.handle_email_confirmation.side_effect = RuntimeError("email")
    assert (await chat_mod.ChatService._handle_authentication_email_button(service, buyer, session(), "confirm_email"))["status"] == "error"
    service._handle_excel_confirmation_response = AsyncMock(side_effect=RuntimeError("excel"))
    assert (await chat_mod.ChatService._handle_excel_confirmation_button(service, buyer, session(), "confirm_excel"))["status"] == "error"
    service._cancel_service.handle_cancel_confirmation.side_effect = RuntimeError("cancel")
    assert (await chat_mod.ChatService._handle_cancel_confirmation_button(service, buyer, session(), "confirm_cancel"))["status"] == "error"
    service._exit_service.handle_exit_confirmation.side_effect = RuntimeError("exit")
    assert (await chat_mod.ChatService._handle_exit_confirmation_button(service, buyer, session(), "confirm_exit"))["status"] == "error"


@pytest.mark.asyncio
async def test_chat_menu_fallback_faq_support_clarification_and_seller_flow_errors(monkeypatch):
    service = chat_stub()
    buyer = user(role="buyer")
    seller = user(role="seller")
    unknown = user(role="other", name=None)

    for handler in (service._handle_greeting_inquiry if False else None,):
        assert handler is None
    for target in (buyer, seller, unknown):
        assert (await chat_mod.ChatService._handle_greeting_inquiry(service, target, "hi", session()))["status"] == "greeting_handled"
        assert (await chat_mod.ChatService._handle_support_request(service, target, "help", session()))["status"] == "support_handled"
        assert (await chat_mod.ChatService._handle_clarification_request(service, target, "?", session()))["status"] == "clarification_sent"
        assert (await chat_mod.ChatService._handle_fallback(service, target, "?", session()))["status"] == "fallback_handled"

    service._faq_service.get_faq_answer.return_value = "answer"
    assert (await service._handle_faq_request(buyer, "question"))["answer_provided"]
    service._faq_service.get_faq_answer.return_value = None
    assert not (await service._handle_faq_request(buyer, "question"))["answer_provided"]
    service._faq_service.get_faq_answer.side_effect = RuntimeError("faq")
    assert (await service._handle_faq_request(buyer, "question"))["status"] == "error"

    service._seller_service.handle_seller_workflow.return_value = {
        "success": True, "workflow_step": "show_subscription_plans", "message": "plans"
    }
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
    result = await service._handle_seller_flow(seller, session(), "plans")
    assert result["status"] == "seller_flow_processed"
    service._seller_service.handle_seller_workflow.side_effect = RuntimeError("seller")
    assert (await service._handle_seller_flow(seller, session(), "broken"))["status"] == "error"
    service._response_helpers.generate_seller_contextual_response.side_effect = RuntimeError("response")
    assert (await service._handle_seller_flow(seller, session(), "broken"))["status"] == "error"


# Contextual/state helpers and summaries ------------------------------------


@pytest.mark.asyncio
async def test_chat_contextual_interactions_safe_blocked_and_error_paths(monkeypatch):
    service = chat_stub()
    buyer = user()
    monkeypatch.setattr(chat_mod.WorkflowManager, "get_workflow_type", lambda _: WorkflowType.rfq_creation)
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_stage", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "clear_pending", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "transition_workflow", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "clear_all_rfq_pending", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "safe_reset_workflow_state", MagicMock())
    service._generate_session_summary = AsyncMock(return_value="summary")

    clean = session(extracted_entities=[{"description": "pump"}])
    result = await service._handle_contextual_interaction(
        buyer, clean, "show", {
            "contextual_response": "base",
            "contextual_actions": [
                {"type": "show_session_summary"}, {"type": "suggest_alternatives"},
                {"type": "update_entities"}, {"type": "clear_session_data"}, {"type": "unknown"},
            ],
            "context_understanding": {"user_intent": "summary", "confidence": 88},
        }
    )
    assert result["status"] == "contextual_interaction_handled"
    assert result["destructive_blocked"] is True
    assert result["actions_performed"] == 5

    empty = session()
    result = await service._handle_contextual_interaction(
        buyer, empty, "reset", {
            "contextual_response": "base",
            "contextual_actions": [
                {"type": "change_workflow_state"}, {"type": "change_workflow_type"},
                {"type": "rollback_to_previous"}, {"type": "clear_session_data"},
                {"type": "restart_workflow"},
            ],
            "context_understanding": {},
        }
    )
    assert result["destructive_blocked"] is False
    assert service.session_manager.save_session.await_count >= 1

    service._generate_session_summary.side_effect = RuntimeError("summary")
    result = await service._handle_contextual_interaction(
        buyer, empty, "bad", {"contextual_actions": [{"type": "show_session_summary"}]}
    )
    assert result["status"] == "contextual_interaction_error"


@pytest.mark.asyncio
async def test_chat_entity_state_tracking_and_auth_meaningful_helpers():
    service = chat_stub()
    current = session(extracted_entities=[{"product_name": "Pump", "quantity": "1"}])
    await service._process_entity_updates(current, [
        {"action": "add", "product_name": "Valve", "quantity": 2},
        {"action": "update", "product_name": "pump", "preferred_brand": "Acme"},
        {"action": "remove", "product_name": "valve"},
    ])
    assert current.workflow_state["extracted_entities"] == [{"product_name": "Pump", "quantity": "1", "preferred_brand": "Acme"}]

    await service._rollback_to_stage(current, "collecting")
    assert current.workflow_state["stage"] == "collecting"
    current.workflow_state.update({"a": 1, "b": 2})
    await service._clear_session_fields(current, ["a", "missing"])
    assert "a" not in current.workflow_state and current.workflow_state["b"] == 2
    await service._rollback_to_stage(current, "entity_collection")
    assert current.workflow_state["extracted_entities"] == []

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
        await service._restart_workflow(current)
    finally:
        monkeypatch.undo()
    assert current.workflow_state["stage"] == "collecting"

    current.conversation_history = {"messages": [{"sender": "assistant"}, {"sender": "user", "content": "pumps"}]}
    service._update_last_user_message_with_intent(current, "buy_something", 90)
    assert current.conversation_history["messages"][1]["intent"] == "buy_something"
    service._update_last_user_message_with_intent(SimpleNamespace(conversation_history=None), "x", 0)

    service._track_meaningful_message_during_auth_flow(current, "buy pumps", {"intent": "buy_something", "confidence": 90})
    assert current.workflow_state["last_meaningful_message"] == "buy pumps"
    current.workflow_state["pending_role_switch"] = True
    service._track_meaningful_message_during_auth_flow(current, "buy pumps", {"intent": "buy_something", "confidence": 90})
    current.workflow_state.pop("pending_role_switch")
    current.workflow_state["profile_selection_stage"] = True
    service._track_meaningful_message_during_auth_flow(current, "buy pumps", {"intent": "buy_something", "confidence": 90})
    current.workflow_state.pop("profile_selection_stage")
    current.workflow_state["meaningful_message_used"] = True
    service._track_meaningful_message_during_auth_flow(current, "buy pumps", {"intent": "buy_something", "confidence": 90})
    assert "meaningful_message_used" not in current.workflow_state

    assert service._is_auth_flow_response(123, "x") is False
    assert service._is_auth_flow_response("1234", "x") is True
    assert service._is_auth_flow_response("yes", "x", session(pending_rfq={"x": 1})) is False
    assert service._is_auth_flow_response("email@example.test", "x") is True
    assert service._is_auth_flow_response("retry", "x") is True
    assert service._is_auth_flow_response("12345", "register_account") is True
    assert service._is_auth_flow_response("ordinary", "x") is False

    tracked = session(last_meaningful_message="pumps", last_meaningful_intent_result={"intent": "buy_something"})
    message, intent = service._get_meaningful_message_after_auth(tracked, "yes", {"intent": "greeting"})
    assert message == "pumps" and intent["intent"] == "buy_something"
    message, intent = service._get_meaningful_message_after_auth(session(), "yes", {"intent": "x"})
    assert intent["intent"] == "greeting"
    message, intent = service._get_meaningful_message_after_auth(session(), "buy pumps", {"intent": "buy_something"})
    assert message == "buy pumps"


@pytest.mark.asyncio
async def test_chat_summary_storage_seller_selection_and_completion_fallbacks(monkeypatch):
    service = chat_stub()
    complex_state = {
        "extracted_entities": [
            {"description": "pump", "quantity": 2, "brand": "Acme", "delivery_date": "tomorrow"},
            {"entities": {"description": "gasket", "quantity": 1, "brand": "X", "deliveryDate": "today"}, "missing_fields": ["pincode"]},
        ],
        "incomplete_products": {"entities": {"description": "valve", "quantity": 1}, "missing_fields": ["pincode"]},
        "complete_products": {"entities": [{"description": "washer", "quantity": 3}]},
        "pending_rfq": {"entities": {"description": "pump", "quantity": 2}},
        "pending_combined_rfq": {"products": [{"entities": {"description": "bolt", "quantity": 4}}]},
    }
    summary = await service._generate_session_summary(session(workflow_state=complex_state))
    assert "Collected Information" in summary and "Pump" in summary and "Still Required" in summary
    assert "Bolt" in summary
    assert "No products" in await service._generate_session_summary(session())
    broken = session(workflow_state={"pending_combined_rfq": {"products": [None]}})
    assert "don't have" in await service._generate_session_summary(broken)

    current = session(seller_candidate_rfqs=[{"rfq_id": "3343"}, {"rfq_id": "3351"}])
    service._openai_service.extract_entities.return_value = {"rfq_id": ["3351", None, "9999"]}
    monkeypatch.setattr(chat_mod, "get_settings", lambda: settings(rfq_max_allowed=1))
    result = await service._handle_seller_rfq_selection(user(role="seller"), current, "3351")
    assert result == {"status": "seller_rfq_ids_captured", "rfq_ids": ["3351"]}
    service._openai_service.extract_entities.return_value = {"rfq_id": []}
    result = await service._handle_seller_rfq_selection(user(role="seller"), current, "RFQ 3343")
    assert result["status"] == "seller_rfq_ids_captured"
    result = await service._handle_seller_rfq_selection(user(role="seller"), session(seller_candidate_rfqs=[{"rfq_id": "1"}]), "none")
    assert result["status"] == "awaiting_valid_rfq_ids"
    service._openai_service.extract_entities.side_effect = RuntimeError("extract")
    assert (await service._handle_seller_rfq_selection(user(role="seller"), current, "x"))["status"] == "error"

    service._chat_summary_service = SimpleNamespace()
    monkeypatch.setattr(chat_mod.SummarizationHelpers, "extract_rich_entities_for_summary", MagicMock(return_value=[{"description": "pump"}]))
    monkeypatch.setattr(chat_mod.SummarizationHelpers, "prepare_enhanced_summary_data", MagicMock(return_value={"count": 1}))
    background = AsyncMock()
    monkeypatch.setattr(chat_mod.SummarizationHelpers, "handle_session_completion_async", background)
    created = []
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created.append(coro))
    await service._handle_session_completion_enhanced(session())
    assert service.chat_summary_service is not None
    assert created
    for coro in created:
        coro.close()

    service._handle_session_completion_fallback = AsyncMock()
    monkeypatch.setattr(chat_mod.SummarizationHelpers, "extract_rich_entities_for_summary", MagicMock(side_effect=RuntimeError("summary")))
    await service._handle_session_completion_enhanced(session())
    service.chat_summary_service.generate_session_summary.side_effect = RuntimeError("summary")
    await service._handle_session_completion_fallback(session())


@pytest.mark.asyncio
async def test_chat_excel_button_and_small_placeholder_helpers():
    service = chat_stub()
    await service._show_auth_placeholder("+1")
    await service._show_seller_flow_placeholder("+1")
    service.whatsapp_service.send_message.side_effect = RuntimeError("send")
    await service._show_auth_placeholder("+1")
    await service._show_seller_flow_placeholder("+1")

    service._bfs_search_handler.handle_bfs_search.return_value = {"status": "bfs"}
    await service._check_bfs_availability(
        user(), session(), [{"success": True, "rfq_data": {"items": [{"description": "pump"}, {"product_name": "valve"}]}}]
    )
    await service._check_bfs_availability(user(), session(), [{"success": False}])
    service._bfs_search_handler.handle_bfs_search.side_effect = RuntimeError("bfs")
    await service._check_bfs_availability(user(), session(), [{"success": True, "rfq_data": {"items": [{"description": "pump"}]}}])

    assert service._should_use_summary_aware_extraction("old request") is False
    service.db_manager.save_conversation_session.side_effect = RuntimeError("db")
    result = await service._save_session(session(outcome=ConversationOutcome.completed), WorkflowType.general_inquiry)
    assert result.session_id == "session-1"
    assert service._clean_for_json_serialization({"date": date(2025, 1, 1), "enum": WorkflowType.rfq_creation, "x": object()})["enum"] == "rfq_creation"


@pytest.mark.asyncio
async def test_chat_seller_rfq_intimation_flow_otp_switch_unknown_and_error(monkeypatch):
    service = chat_stub()
    handler = SimpleNamespace(
        handle_switch_response=AsyncMock(return_value={"status": "switch"}),
        handle_otp_validated=AsyncMock(return_value={"status": "portal"}),
    )
    monkeypatch.setattr(
        "app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler",
        lambda **kwargs: handler,
    )
    current = session(auth_stage="switch_prompt")
    assert (await service._handle_seller_rfq_intimation_flow(user(), current, "yes"))["status"] == "switch"
    service._authentication_service.handle_email_otp_validation.return_value = {"status": "otp_valid"}
    current = session(auth_stage="otp")
    assert (await service._handle_seller_rfq_intimation_flow(user(), current, "1234"))["status"] == "portal"
    service._authentication_service.handle_email_otp_validation.return_value = {"status": "otp_invalid"}
    assert (await service._handle_seller_rfq_intimation_flow(user(), session(auth_stage="otp"), "bad"))["status"] == "otp_invalid"
    assert await service._handle_seller_rfq_intimation_flow(user(), session(auth_stage="unknown"), "x") is None
    handler.handle_switch_response.side_effect = RuntimeError("handler")
    broken = session(auth_stage="switch_prompt")
    assert (await service._handle_seller_rfq_intimation_flow(user(), broken, "yes"))["status"] == "error"
    assert broken.workflow_state == {} and broken.workflow_type is None


# Follow-up matrix for pre-classification workflows and residual state seams ---


@pytest.mark.asyncio
async def test_chat_process_message_notification_buttons_and_seller_workflows(monkeypatch):
    service = chat_stub()
    interest_handler = SimpleNamespace(
        handle_rfq_interest_click=AsyncMock(return_value={"status": "interested"}),
        handle_check_details_click=AsyncMock(return_value={"status": "details"}),
        handle_request_rfq_click=AsyncMock(return_value={"status": "requested"}),
        handle_switch_response=AsyncMock(return_value={"status": "switched"}),
        handle_otp_validated=AsyncMock(return_value={"status": "portal"}),
    )
    monkeypatch.setattr(
        "app.services.handlers.seller_rfq_interest_handler.SellerRFQInterestHandler",
        lambda **_kwargs: interest_handler,
    )

    notification_buttons = [
        ("rfq_interested_RFQ_TEST_001_seller-1", "interested"),
        ("rfq_check_details_RFQ_TEST_001_seller-1", "details"),
        ("rfq_request_RFQ_TEST_001_seller-1", "requested"),
    ]
    for button_id, status in notification_buttons:
        current = session()
        service.session_manager.get_conversation_context.return_value = current
        result = await service.process_message(
            "+1", {"button_reply": {"id": button_id}}, "interactive"
        )
        assert result == {"status": status}

    # A malformed notification ID still reaches the handler with seller_id=None.
    current = session()
    service.session_manager.get_conversation_context.return_value = current
    await service.process_message(
        "+1", {"button_reply": {"id": "rfq_interested_ONLY"}}, "interactive"
    )
    assert interest_handler.handle_rfq_interest_click.await_args.args[2] is None

    bid_handler = SimpleNamespace(
        handle_accept_bid_click=AsyncMock(return_value={"status": "accepted"}),
        handle_reject_bid_click=AsyncMock(return_value={"status": "rejected"}),
    )
    monkeypatch.setattr(
        "app.services.handlers.bfs_seller_bid_handler.BFSSellerBidHandler",
        lambda **_kwargs: bid_handler,
    )
    for button_id, expected in [
        ("bfs_seller_accept_BFS_UUID_seller-1", "accepted"),
        ("bfs_seller_reject_BFS_UUID_seller-1", "rejected"),
    ]:
        service.session_manager.get_conversation_context.return_value = session()
        result = await service.process_message(
            "+1", {"button_reply": {"id": button_id}}, "interactive"
        )
        assert result["status"] == expected

    service.exit_service.handle_exit_intent.return_value = {"status": "exit_button"}
    service.session_manager.get_conversation_context.return_value = session()
    assert (await service.process_message(
        "+1", {"button_reply": {"id": "confirm_exit"}}, "interactive"
    ))["status"] == "exit_button"

    service.cancel_service.handle_cancel_intent.return_value = {"status": "cancel_button"}
    service.session_manager.get_conversation_context.return_value = session()
    assert (await service.process_message(
        "+1", {"button_reply": {"id": "confirm_cancel"}}, "interactive"
    ))["status"] == "cancel_button"

    # The seller-intimation entry point handles exit, switch, OTP success,
    # retry, max-attempt, and missing-OTP-service states before classification.
    exit_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "switch_prompt"},
    )
    service.session_manager.get_conversation_context.return_value = exit_session
    assert (await service.process_message("+1", "exit", "text"))["status"] == "exit_button"

    switch_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "switch_prompt"},
    )
    service.session_manager.get_conversation_context.return_value = switch_session
    assert (await service.process_message("+1", "yes", "text"))["status"] == "switched"

    target_seller = user(role="seller", email="seller@example.test")
    otp_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={
            "auth_stage": "otp",
            "target_seller_email": target_seller.email,
            "target_seller_id": "seller-1",
            "target_seller_user": target_seller,
        },
    )
    service.authentication_service.otp_service.validate_otp.return_value = {"status": "otp_valid"}
    service.session_manager.get_conversation_context.return_value = otp_session
    assert (await service.process_message("+1", "123456", "text"))["status"] == "portal"
    service.authentication_service.store_user_session_with_email.assert_awaited()

    max_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "otp"},
    )
    service.authentication_service.otp_service.validate_otp.return_value = {"status": "max_otp_exceeded"}
    service.session_manager.get_conversation_context.return_value = max_session
    assert (await service.process_message("+1", "123456", "text"))["status"] == "max_otp_exceeded"
    assert max_session.workflow_type is None and max_session.workflow_state == {}

    retry_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "otp"},
    )
    service.authentication_service.otp_service.validate_otp.return_value = {"status": "otp_invalid"}
    service.session_manager.get_conversation_context.return_value = retry_session
    assert await service.process_message("+1", "bad", "text") == {
        "status": "otp_invalid", "retry": True
    }

    missing_otp_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "otp"},
    )
    service.authentication_service.otp_service = None
    service.session_manager.get_conversation_context.return_value = missing_otp_session
    missing = await service.process_message("+1", "123456", "text")
    assert missing == {"status": "error", "message": "OTP service not available"}

    # BFS bid routing uses the same early workflow contract with a separate handler.
    bfs_handler = SimpleNamespace(
        handle_switch_response=AsyncMock(return_value={"status": "bfs_switched"}),
        handle_otp_validated=AsyncMock(return_value={"status": "bfs_portal"}),
    )
    monkeypatch.setattr(
        "app.services.handlers.bfs_seller_bid_handler.BFSSellerBidHandler",
        lambda **_kwargs: bfs_handler,
    )
    service.authentication_service.otp_service = SimpleNamespace(
        validate_otp=AsyncMock(return_value={"status": "otp_valid"})
    )
    bfs_switch = session(
        workflow_type=WorkflowType.bfs_seller_bid,
        workflow_state={"auth_stage": "switch_prompt"},
    )
    service.session_manager.get_conversation_context.return_value = bfs_switch
    assert (await service.process_message("+1", "yes", "text"))["status"] == "bfs_switched"
    bfs_otp = session(
        workflow_type=WorkflowType.bfs_seller_bid,
        workflow_state={"auth_stage": "otp", "target_seller_user": target_seller,
                        "target_seller_email": target_seller.email},
    )
    service.session_manager.get_conversation_context.return_value = bfs_otp
    assert (await service.process_message("+1", "123456", "text"))["status"] == "bfs_portal"


@pytest.mark.asyncio
async def test_chat_process_message_classification_and_authentication_fallbacks(monkeypatch):
    cache = cache_double()
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)

    async def invoke(intent_result, auth_result, configure=None):
        service = chat_stub()
        current = session()
        service.session_manager.get_conversation_context.return_value = current
        if isinstance(intent_result, BaseException):
            service.intent_service.classify_intent.side_effect = intent_result
        else:
            service.intent_service.classify_intent.return_value = intent_result
        service.authentication_orchestrator_flow = AsyncMock(return_value=auth_result)
        service.handle_irrelevant_message_flow = AsyncMock()
        if configure:
            configure(service, current)
        return await service.process_message("+1", "hello", "text"), service, current

    timeout, service, _ = await invoke({"timeout_handled": True}, {"status": "unused"})
    assert timeout == {"status": "rate_limit_timeout", "message": "Rate limit timeout handled"}
    service.authentication_orchestrator_flow.assert_not_awaited()

    classified_error, _, _ = await invoke(RuntimeError("classifier"), {"status": "new_user_registration_sent"})
    assert classified_error["status"] == "new_user_registration_sent"

    already_exited, service, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "redirected_to_support", "exit_completed": True},
    )
    assert already_exited["exit_completed"] is True
    service.exit_service.handle_exit_intent.assert_not_awaited()

    progress, service, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "otp_sent", "workflow_type": "not-a-workflow"},
    )
    assert progress["status"] == "otp_sent"
    assert service.session_manager.save_session.await_args.args[1] == WorkflowType.authentication

    verification, _, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "verification_required", "redirect_info": {}},
    )
    assert verification["status"] == "verification_required"

    unexpected, _, _ = await invoke({"intent": "other", "confidence": 0}, "unexpected")
    assert unexpected == {"status": "error", "error": "Authentication failed"}

    # Legacy registration completion covers cache refresh and both buyer outcomes.
    def configure_seller(service, _current):
        service.authentication_service.validate_token.return_value = user(role="seller")
        service.authentication_service.user_authenticate.return_value = {"success": True}

    seller_registered, _, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "registration_completed", "user_type": "buyer", "registration_flow_complete": False},
        configure_seller,
    )
    assert seller_registered == {
        "status": "registration_completed",
        "message": "Buyer registration successful - awaiting approval",
    }

    cache.get_meaningful_message.return_value = {
        "message": "buy pumps",
        "intent_result": {"intent": "buy_something", "confidence": 90},
    }

    def configure_buyer_main(service, _current):
        service.authentication_service.validate_token.return_value = user()
        service.authentication_service.user_authenticate.return_value = {"success": True}
        service._process_text_message = AsyncMock(return_value={"status": "main_flow"})

    buyer_main, service, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "registration_completed", "user_type": "buyer", "registration_flow_complete": False,
         "redirect_to_main_flow": True, "approved": True},
        configure_buyer_main,
    )
    assert buyer_main == {"status": "main_flow"}
    service._process_text_message.assert_awaited_once()
    cache.get_meaningful_message.return_value = None

    buyer_pending, _, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "registration_completed", "user_type": "buyer", "registration_flow_complete": False,
         "redirect_to_main_flow": False, "approved": False},
        configure_buyer_main,
    )
    assert buyer_pending["message"].startswith("Buyer registration successful")

    def configure_authenticated_buyer(service, _current):
        service.authentication_service.validate_token.return_value = user()
        service._process_text_message = AsyncMock(return_value={"status": "authenticated_main"})

    authenticated_buyer, service, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "authentication_completed", "user_type": "buyer", "original_message": "pumps"},
        configure_authenticated_buyer,
    )
    assert authenticated_buyer == {"status": "authenticated_main"}
    service._process_text_message.assert_awaited_once()

    def configure_authenticated_seller(service, _current):
        service.authentication_service.validate_token.return_value = user(role="seller")
        service._process_text_message = AsyncMock(return_value={"status": "seller_main"})

    authenticated_seller, _, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "authentication_completed", "user_type": "seller", "original_message": "sell"},
        configure_authenticated_seller,
    )
    assert authenticated_seller == {"status": "seller_main"}

    def configure_missing_user(service, _current):
        service.authentication_service.validate_token.return_value = None

    missing_buyer, _, _ = await invoke(
        {"intent": "other", "confidence": 0},
        {"status": "authentication_completed", "user_type": "buyer"},
        configure_missing_user,
    )
    assert missing_buyer["error"] == "Session not found after authentication"

    class AuthenticatedUser(SimpleNamespace):
        pass

    monkeypatch.setattr(chat_mod, "User", AuthenticatedUser)
    registered = AuthenticatedUser(**vars(user()))
    unregistered = AuthenticatedUser(**vars(user(is_registered=False)))

    def configure_user_result(service, _current):
        service._process_text_message = AsyncMock(return_value={"status": "user_flow"})

    user_result, service, _ = await invoke(
        {"intent": "other", "confidence": 0}, registered, configure_user_result
    )
    assert user_result == {"status": "user_flow"}
    unregistered_result, _, _ = await invoke(
        {"intent": "other", "confidence": 0}, unregistered, configure_user_result
    )
    assert unregistered_result == {"status": "user_flow"}


@pytest.mark.asyncio
async def test_chat_text_pending_states_and_route_matrix(monkeypatch):
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _session: False)

    service = chat_stub()
    service._get_workflow_or_default = MagicMock(return_value=WorkflowType.general_inquiry)

    class IncompleteSwitcher:
        def __init__(self, *_args):
            pass

        async def handle_role_switch_response(self, *_args):
            return {"status": "authentication_completed", "original_message": "pumps"}

    monkeypatch.setattr(
        "app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch",
        IncompleteSwitcher,
    )
    service.authentication_service.validate_token.return_value = None
    pending_role = session(pending_role_switch=True)
    result = await service._process_text_message(
        user(), pending_role, "yes", {"intent": "greeting", "confidence": 80}
    )
    assert result["status"] == "authentication_completed"

    service._intent_service.classify_intent.return_value = {"intent": "greeting", "confidence": 90}
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "fallback_greeting"})
    assert (await service._process_text_message(user(), session(), "hello", None))["status"] == "fallback_greeting"

    service._cancel_service.confirmation_service.parse_confirmation.return_value = "no"
    service._cancel_service.handle_cancel_confirmation.return_value = {"status": "cancelled_aborted"}
    service._bfs_search_handler.handle_bfs_search.return_value = {"status": "bfs_after_cancel"}
    cancel_pending = session(cancel_pending=True)
    result = await service._process_text_message(
        user(), cancel_pending, "no", {"intent": "bfs_search", "confidence": 90}
    )
    assert result == {"status": "bfs_after_cancel"}

    service._cancel_service.handle_cancel_confirmation.return_value = {"status": "other"}
    other_cancel = session(cancel_pending=True)
    assert await service._process_text_message(
        user(), other_cancel, "maybe", {"intent": "other", "confidence": 90}
    ) == {"status": "other"}

    service.exit_service.handle_exit_confirmation.return_value = {"status": "exit_pending_handled"}
    exit_pending = session(exit_pending=True)
    assert await service._process_text_message(
        user(), exit_pending, "yes", {"intent": "other", "confidence": 90}
    ) == {"status": "exit_pending_handled"}

    attachment = session(awaiting_attachment_decision=True)
    assert await service._process_text_message(
        user(), attachment, "file", {"intent": "other", "confidence": 90}
    ) == {"status": "attachment"}

    bfs_pending = session(bfs_search_pending=True)
    assert await service._process_text_message(
        user(), bfs_pending, "pump", {"intent": "other", "confidence": 90}
    ) == {"status": "bfs_after_cancel"}
    assert "bfs_search_pending" not in bfs_pending.workflow_state

    bid_format = session(bfs_bid_stage="format_input")
    assert (await service._process_text_message(
        user(), bid_format, "100", {"intent": "other", "confidence": 90}
    ))["status"] == "bid_format"
    bid_otp = session(bfs_bid_stage="otp_pending")
    assert (await service._process_text_message(
        user(), bid_otp, "1234", {"intent": "other", "confidence": 90}
    ))["status"] == "bid_otp"

    database = MagicMock()
    monkeypatch.setattr("app.database.DatabaseManager", lambda: database)
    redis_session = SimpleNamespace(delete_session=AsyncMock())
    monkeypatch.setattr("app.redis_db.get_session_redis_service", lambda: redis_session)
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_session_storage_enabled=True))
    service._confirmation_handler.handle_pending_confirmations.return_value = {
        "status": "multiple_rfqs_created"
    }
    completed = session(pending_rfq={"items": []})
    completed.outcome = ConversationOutcome.completed
    assert (await service._process_text_message(
        user(), completed, "confirm", {"intent": "other", "confidence": 90}
    ))["status"] == "multiple_rfqs_created"
    database.append_session_data.assert_called_once()
    redis_session.delete_session.assert_awaited_once_with(completed.session_id)

    service._confirmation_handler.handle_pending_confirmations.return_value = {
        "continue_with_purchase_intent": True
    }
    service._purchase_intent_handler.handle_purchase_intent.return_value = {"status": "continued"}
    continued = session(pending_rfq={"items": []})
    assert await service._process_text_message(
        user(), continued, "modify", {"intent": "other", "confidence": 90}
    ) == {"status": "continued"}

    service._purchase_intent_handler.handle_purchase_intent.return_value = {"status": "sectioned"}
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _session: True)
    sectioned_buyer = session(sectioned_rfq={"active": True})
    assert await service._process_text_message(
        user(), sectioned_buyer, "next", {"intent": "other", "confidence": 90}
    ) == {"status": "sectioned"}
    service._handle_seller_flow = AsyncMock(return_value={"status": "seller_section_clear"})
    sectioned_seller = session(sectioned_rfq={"active": True}, extracted_entities=[{"x": 1}])
    assert await service._process_text_message(
        user(role="seller"), sectioned_seller, "next", {"intent": "other", "confidence": 90}
    ) == {"status": "seller_section_clear"}
    assert "sectioned_rfq" not in sectioned_seller.workflow_state

    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _session: False)
    service._intent_switch_handler.should_handle_intent_switch.return_value = True
    service._intent_switch_handler.handle_intent_switch_choice.return_value = {"status": "active_switch"}
    active = session(extracted_entities=[{"description": "pump"}])
    assert await service._process_text_message(
        user(), active, "change", {"intent": "other", "confidence": 90}
    ) == {"status": "active_switch"}

    def route_service(intent, confidence=90, role="buyer", context=None):
        routed = chat_stub()
        routed._get_workflow_or_default = MagicMock(return_value=WorkflowType.general_inquiry)
        routed._handle_format_modification = AsyncMock(return_value={"status": "format_route"})
        routed._handle_support_request = AsyncMock(return_value={"status": "support_route"})
        routed._handle_contextual_interaction = AsyncMock(return_value={"status": "context_route"})
        routed._handle_seller_flow = AsyncMock(return_value={"status": "seller_route"})
        routed._handle_account_switch_intent = AsyncMock(return_value={"status": "account_route"})
        routed._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status_route"})
        routed._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting_route"})
        routed._handle_clarification_request = AsyncMock(return_value={"status": "clarification_route"})
        routed._handle_fallback = AsyncMock(return_value={"status": "fallback_route"})
        routed.handle_irrelevant_message_flow = AsyncMock()
        routed._purchase_intent_handler.handle_purchase_intent.return_value = {"status": "purchase_route"}
        routed._bfs_search_handler.handle_bfs_search.return_value = {"status": "bfs_route"}
        current = session()
        intent_result = {"intent": intent, "confidence": confidence}
        if context:
            intent_result.update(context)
        return routed, role, current, intent_result

    routes = [
        ("format_modification", 90, "buyer", "format_route"),
        ("support", 90, "buyer", "support_route"),
        ("contextual_reference", 90, "buyer", "context_route"),
        ("modification_request", 90, "buyer", "purchase_route"),
        ("confirmation_response", 90, "buyer", "purchase_route"),
        ("reference_request", 90, "buyer", "purchase_route"),
        ("bfs_search", 90, "buyer", "bfs_route"),
        ("sell_something", 90, "other", "seller_route"),
        ("rfq_status_check", 90, "buyer", "status_route"),
        ("account_switch", 90, "buyer", "account_route"),
        ("greeting", 90, "buyer", "greeting_route"),
        ("unknown", 0.4, "buyer", "clarification_route"),
        ("unknown", 0.6, "buyer", "fallback_route"),
    ]
    for intent, confidence, role, expected in routes:
        context = {"should_handle_directly": True} if intent == "contextual_reference" else None
        routed, role_name, current, intent_result = route_service(intent, confidence, role, context)
        result = await routed._process_text_message(
            user(role=role_name), current, "request", intent_result
        )
        assert result["status"] == expected

    general, role_name, current, intent_result = route_service("general_inquiry")
    assert await general._process_text_message(user(role=role_name), current, "help", intent_result) is None


@pytest.mark.asyncio
async def test_chat_service_excel_and_helper_exception_fallbacks(monkeypatch):
    service = chat_stub()
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _session: False)
    validator = SimpleNamespace(
        validate_excel_file_from_url=AsyncMock(return_value={"valid": True, "content": b"xlsx"})
    )
    processor = SimpleNamespace(process_excel_file=AsyncMock(side_effect=RuntimeError("processor")))
    monkeypatch.setattr(chat_mod, "ExcelValidationService", lambda: validator)
    monkeypatch.setattr(chat_mod, "ExcelProcessingService", lambda *_args: processor)
    redis = MagicMock()
    lock = FakeLock(acquired=True)
    redis.lock.return_value = lock
    monkeypatch.setattr("redis.asyncio.Redis.from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr("app.config.get_settings", lambda: settings(redis_url="redis://unit"))
    failed_session = session()
    failed = await service._process_excel_upload(
        user(), failed_session, {"id": "media", "filename": "broken.xlsx"}
    )
    assert failed["status"] == "error"
    assert "excel_file_processed" not in failed_session.workflow_state
    lock.release.assert_awaited_once()

    lock_error = FakeLock(acquired=True, release_error=RuntimeError("release"))
    redis.lock.return_value = lock_error
    second = session()
    assert (await service._process_excel_upload(
        user(), second, {"document": {"link": "url", "filename": "broken.xlsx"}}
    ))["status"] == "error"
    lock_error.release.assert_awaited_once()

    service._openai_service.parse_confirmation_response.return_value = None
    cancelled = session(excel_confirmation_data={"products": [{"description": "pump"}]}, awaiting_excel_confirmation=True)
    assert (await service._handle_excel_confirmation_response(
        user(), cancelled, "restart"
    ))["status"] == "excel_cancelled_redirected_to_greeting"

    service.cancel_service.handle_cancel_confirmation.return_value = {"status": "other"}
    assert await service._handle_cancel_confirmation_button(
        user(), session(), "confirm_cancel"
    ) is None

    class BadString:
        def __str__(self):
            raise RuntimeError("string conversion")

    with pytest.raises(RuntimeError, match="string conversion"):
        service._convert_excel_items_to_products_array([{"ItemDescription": BadString()}])

    llm_context = {"conversation_history": {"messages": [{"role": "system", "content": "hidden"}]}}
    assert await service._generate_llm_response("+1", "hello", llm_context) == "generated"

    service._generate_session_summary = AsyncMock(return_value="")
    no_update = session()
    result = await service._handle_contextual_interaction(
        user(), no_update, "summary", {"contextual_actions": [{"type": "show_session_summary"}]}
    )
    assert result["status"] == "contextual_interaction_handled"

    entities = session(extracted_entities=[{"product_name": "pump"}])
    await service._process_entity_updates(entities, [{"action": "update", "product_name": "missing", "quantity": 2}])
    assert entities.workflow_state["extracted_entities"] == [{"product_name": "pump"}]

    class BrokenUpdate:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("update")

    await service._process_entity_updates(entities, [BrokenUpdate()])

    monkeypatch.setattr(chat_mod, "utc_now", lambda: (_ for _ in ()).throw(RuntimeError("clock")))
    await service._restart_workflow(session())

    class BrokenMessage:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("message")

    service._update_last_user_message_with_intent(
        SimpleNamespace(conversation_history={"messages": [BrokenMessage()]}), "x", 1
    )
    service._track_meaningful_message_during_auth_flow(session(), "hello", None)

    class BrokenText(str):
        def lower(self):
            raise RuntimeError("lower")

    assert service._is_auth_flow_response(BrokenText("hello"), "other") is False

    class BrokenIntent:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("intent")

    message, intent = service._get_meaningful_message_after_auth(session(), "current", BrokenIntent())
    assert message == "current" and intent is not None


# Final reachable branch matrix ---------------------------------------------


@pytest.mark.asyncio
async def test_chat_process_message_auth_matrix_and_authenticated_dispatch(monkeypatch):
    monkeypatch.setattr(
        chat_mod.ChatServiceHelpers,
        "build_conversation_context",
        AsyncMock(return_value={}),
    )
    cache = cache_double()
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache)

    class AuthenticatedUser(SimpleNamespace):
        pass

    monkeypatch.setattr(chat_mod, "User", AuthenticatedUser)
    registered = AuthenticatedUser(**vars(user()))

    async def invoke(auth_result, current=None, message="hello", message_type="text", configure=None):
        service = chat_stub()
        current = current or session()
        service.session_manager.get_conversation_context.return_value = current
        service.intent_service.classify_intent.return_value = {
            "intent": "other", "confidence": 0, "relevant_message": ""
        }
        service.authentication_orchestrator_flow = AsyncMock(return_value=auth_result)
        service.handle_irrelevant_message_flow = AsyncMock()
        if configure:
            configure(service, current)
        result = await service.process_message("+1", message, message_type)
        return result, service, current

    result, service, current = await invoke({"status": "buyer_options_presented"})
    assert result["status"] == "buyer_options_presented"
    assert service.session_manager.save_session.await_args.args[1] == WorkflowType.authentication
    assert current.workflow_state == {}

    result, service, _ = await invoke(
        {"status": "otp_sent"}, session(workflow_type=WorkflowType.registration)
    )
    assert result["status"] == "otp_sent"
    assert service.session_manager.save_session.await_args.args[1] == WorkflowType.registration

    def configure_missing_registration_user(service, _current):
        service.authentication_service.validate_token.return_value = None

    result, _, _ = await invoke(
        {
            "status": "registration_completed",
            "user_type": "buyer",
            "registration_flow_complete": False,
        },
        configure=configure_missing_registration_user,
    )
    assert result == {"status": "error", "error": "Session not found after registration"}

    cache.get_meaningful_message.return_value = {
        "message": "sell pumps",
        "intent_result": {"intent": "sell_something", "confidence": 90},
    }

    def configure_seller_auth(service, _current):
        service.authentication_service.validate_token.return_value = user(role="seller")
        service._process_text_message = AsyncMock(return_value={"status": "seller_main"})

    result, service, current = await invoke(
        {
            "status": "authentication_completed",
            "user_type": "seller",
            "original_message": "",
            "original_intent": {"intent": "sell_something", "confidence": 90},
        },
        configure=configure_seller_auth,
    )
    assert result == {"status": "seller_main"}
    service._process_text_message.assert_awaited_once()
    cache.clear_meaningful_message.assert_awaited()
    assert service._process_text_message.await_args.args[2] == "sell pumps"

    def configure_missing_seller(service, _current):
        service.authentication_service.validate_token.return_value = None

    result, _, _ = await invoke(
        {"status": "authentication_completed", "user_type": "seller"},
        configure=configure_missing_seller,
    )
    assert result == {
        "status": "error",
        "error": "Session not found after seller authentication",
    }

    cache.get_meaningful_message.return_value = None

    def configure_buyer_cache_population(service, _current):
        service.authentication_service.validate_token.return_value = registered
        service.authentication_service.user_authenticate.return_value = {"success": False}
        service._process_text_message = AsyncMock(return_value={"status": "buyer_main"})

    result, service, _ = await invoke(
        {
            "status": "authentication_completed",
            "user_type": "buyer",
            "original_message": "pumps",
            "original_intent": "buy_something",
        },
        configure=configure_buyer_cache_population,
    )
    assert result == {"status": "buyer_main"}
    service.authentication_service.user_authenticate.assert_awaited_once()

    cache.get_user_data.side_effect = RuntimeError("cache read")
    result, service, _ = await invoke(
        {"status": "authentication_completed", "user_type": "buyer"},
        configure=configure_buyer_cache_population,
    )
    assert result == {"status": "buyer_main"}
    assert service._process_text_message.await_count >= 1
    cache.get_user_data.side_effect = None
    cache.get_user_data.return_value = {}

    for status in ("new_user_registration_retry_sent", "new_user_registration"):
        result, _, _ = await invoke({"status": status})
        assert result["status"] == status

    def configure_interactive(service, _current):
        service._process_interactive_message = AsyncMock(return_value={"status": "interactive_dispatch"})

    result, service, _ = await invoke(
        registered,
        message={"type": "button_reply", "button_reply": {"id": "unhandled"}},
        message_type="interactive",
        configure=configure_interactive,
    )
    assert result == {"status": "interactive_dispatch"}
    service._process_interactive_message.assert_awaited_once()

    def configure_excel(service, _current):
        service._process_excel_upload = AsyncMock(return_value={"status": "excel_dispatch"})

    excel = {"document": {"filename": "quote.xlsx", "data": "base64-data"}}
    result, service, _ = await invoke(
        registered, message=excel, message_type="excel_upload", configure=configure_excel
    )
    assert result == {"status": "excel_dispatch"}
    assert service.intent_service.classify_intent.await_args.args[0] == "Excel file upload: quote.xlsx"
    service._process_excel_upload.assert_awaited_once_with(registered, service.session_manager.get_conversation_context.return_value, excel)
    service.session_manager.save_session.assert_awaited_once_with(
        service.session_manager.get_conversation_context.return_value, WorkflowType.rfq_creation
    )

    def configure_text(service, _current):
        service._process_text_message = AsyncMock(return_value={"status": "text_dispatch"})

    result, service, _ = await invoke(
        registered,
        message="tracked",
        configure=configure_text,
    )
    assert result == {"status": "text_dispatch"}
    service._process_text_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_process_message_preclassification_remaining_states(monkeypatch):
    monkeypatch.setattr(
        chat_mod.ChatServiceHelpers,
        "build_conversation_context",
        AsyncMock(return_value={}),
    )
    service = chat_stub()
    service.exit_service.handle_exit_intent.return_value = {"status": "declined_exit"}
    service.cancel_service.handle_cancel_intent.return_value = {"status": "declined_cancel"}

    for button_id, expected in (
        ("decline_exit", "declined_exit"),
        ("'decline_cancel", "declined_cancel"),
        ("cancel_no_credits", "declined_cancel"),
    ):
        service.session_manager.get_conversation_context.return_value = session()
        result = await service.process_message(
            "+1", {"button_reply": {"id": button_id}}, "interactive"
        )
        assert result["status"] == expected

    service.authentication_orchestrator_flow = AsyncMock(return_value={"status": "unused"})
    service.intent_service.classify_intent.return_value = {
        "intent": "exit_system", "confidence": 90
    }
    auth_session = session(workflow_type=WorkflowType.authentication)
    service.session_manager.get_conversation_context.return_value = auth_session
    service.exit_service.handle_exit_intent.return_value = {"status": "auth_exit"}
    assert (await service.process_message("+1", "exit", "text"))["status"] == "auth_exit"
    service.authentication_orchestrator_flow.assert_not_awaited()

    service.intent_service.classify_intent.return_value = {
        "intent": "cancel_workflow", "confidence": 90
    }
    service.cancel_service.handle_cancel_intent.return_value = {"status": "auth_cancel"}
    service.session_manager.get_conversation_context.return_value = session(
        workflow_type=WorkflowType.authentication
    )
    assert (await service.process_message("+1", "cancel", "text"))["status"] == "auth_cancel"
    service.session_manager.save_session.assert_awaited()

    service.authentication_orchestrator_flow = AsyncMock(
        return_value={"status": "new_user_registration_sent"}
    )
    service.intent_service.classify_intent.return_value = {
        "intent": "other", "confidence": 0
    }
    unknown_stage = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "unknown"},
    )
    service.session_manager.get_conversation_context.return_value = unknown_stage
    result = await service.process_message("+1", "continue", "text")
    assert result["status"] == "new_user_registration_sent"

    bid_handler = SimpleNamespace(
        handle_switch_response=AsyncMock(return_value={"status": "switched"}),
        handle_otp_validated=AsyncMock(return_value={"status": "portal"}),
    )
    monkeypatch.setattr(
        "app.services.handlers.bfs_seller_bid_handler.BFSSellerBidHandler",
        lambda **_kwargs: bid_handler,
    )
    target = user(role="seller", email="target@example.test")
    for otp_result, expected in (
        ({"status": "max_otp_exceeded"}, "max_otp_exceeded"),
        ({"status": "otp_invalid"}, "otp_invalid"),
    ):
        service.authentication_service.otp_service.validate_otp.return_value = otp_result
        current = session(
            workflow_type=WorkflowType.bfs_seller_bid,
            workflow_state={"auth_stage": "otp", "target_seller_user": target},
        )
        service.session_manager.get_conversation_context.return_value = current
        result = await service.process_message("+1", "123456", "text")
        assert result["status"] == expected
        if expected == "max_otp_exceeded":
            assert current.workflow_type is None and current.workflow_state == {}

    current = session(
        workflow_type=WorkflowType.bfs_seller_bid,
        workflow_state={"auth_stage": "otp"},
    )
    service.authentication_service.otp_service = None
    service.session_manager.get_conversation_context.return_value = current
    assert await service.process_message("+1", "123456", "text") == {
        "status": "error", "message": "OTP service not available"
    }


@pytest.mark.asyncio
async def test_chat_text_residual_priority_bfs_seller_and_tracked_paths(monkeypatch):
    monkeypatch.setattr(chat_mod.WorkflowManager, "is_sectioned_rfq_active", lambda _: False)
    service = chat_stub()
    service._get_workflow_or_default = MagicMock(return_value=WorkflowType.general_inquiry)
    service._handle_greeting_inquiry = AsyncMock(return_value={"status": "greeting"})
    service._handle_seller_rfq_intimation_flow = AsyncMock(return_value=None)
    service._intent_service.classify_intent.return_value = {"intent": "greeting", "confidence": 90}

    seller_flow_session = session(
        workflow_type=WorkflowType.seller_rfq_intimation,
        workflow_state={"auth_stage": "unknown"},
    )
    assert await service._process_text_message(
        user(), seller_flow_session, "hello", None
    ) == {"status": "greeting"}
    service._handle_seller_rfq_intimation_flow.assert_awaited_once()

    class PendingSwitcher:
        def __init__(self, *_args):
            pass

        async def handle_role_switch_response(self, *_args):
            return {"status": "role_switch_pending"}

    monkeypatch.setattr(
        "app.services.handlers.auth_registration_intent_switch.AuthRegistrationIntentSwitch",
        PendingSwitcher,
    )
    pending_role = session(pending_role_switch=True)
    assert await service._process_text_message(
        user(), pending_role, "later", {"intent": "greeting", "confidence": 90}
    ) == {"status": "role_switch_pending"}

    service._handle_excel_confirmation_response = AsyncMock(return_value={"status": "excel_cancel"})
    excel_pending = session(awaiting_excel_confirmation=True)
    assert await service._process_text_message(
        user(), excel_pending, "cancel", {"intent": "cancel_workflow", "confidence": 90}
    ) == {"status": "excel_cancel"}
    assert await service._process_text_message(
        user(), excel_pending, "exit", {"intent": "exit_system", "confidence": 90}
    ) == {"status": "excel_cancel"}

    service.confirmation_handler.handle_optional_fields_response.return_value = {
        "status": "optional_exit"
    }
    optional = session(pending_optional_rfq={"description": "pump"})
    assert await service._process_text_message(
        user(), optional, "exit", {"intent": "exit_system", "confidence": 90}
    ) == {"status": "optional_exit"}

    service.cancel_service.confirmation_service.parse_confirmation.return_value = "yes"
    service.cancel_service.handle_cancel_confirmation.return_value = {"status": "cancelled"}
    cancelled = session(cancel_pending=True)
    assert await service._process_text_message(
        user(), cancelled, "yes", {"intent": "other", "confidence": 90}
    ) == {"status": "cancelled"}

    bfs_display = session(
        bfs_results=[
            {"description": "", "specification": None, "availableQuantity": None,
             "ageOfAsset": None, "sellPrice": None},
            "not-a-dict",
        ]
    )
    assert await service._process_text_message(
        user(), bfs_display, "again", {"intent": "other", "confidence": 90}
    ) == {"status": "bfs_awaiting_button_click"}
    service.whatsapp_service.send_configurable_buttons.assert_awaited()

    service._handle_seller_flow = AsyncMock(return_value={"status": "seller_route"})
    service._handle_rfq_status_inquiry = AsyncMock(return_value={"status": "status_route"})
    seller = user(role="seller")
    assert await service._process_text_message(
        seller, session(), "status", {"intent": "rfq_status_check", "confidence": 90}
    ) == {"status": "status_route"}
    assert await service._process_text_message(
        seller, session(), "continue", {"intent": "other", "confidence": 0}
    ) == {"status": "seller_route"}
    assert await service._process_text_message(
        seller, session(workflow_type=WorkflowType.seller_rfq_view), "continue",
        {"intent": "other", "confidence": 90},
    ) == {"status": "seller_route"}

    tracked_object = session(
        last_meaningful_message="old request",
        last_meaningful_intent_result=object(),
    )
    service._purchase_intent_handler.handle_purchase_intent.return_value = {"status": "purchase"}
    assert await service._process_text_message(
        user(), tracked_object, "new request", {"intent": "buy_something", "confidence": 90}
    ) == {"status": "purchase"}

    tracked_minimal = session(
        last_meaningful_message="old request",
        last_meaningful_intent_result={
            "intent": None, "confidence": 0, "all_intent_scores": [],
            "context_analysis": [], "reasoning": None,
            "suggested_clarification": None, "success": False,
        },
    )
    assert await service._process_text_message(
        user(), tracked_minimal, "new request", {"intent": "buy_something", "confidence": 90}
    ) == {"status": "purchase"}

    service._purchase_intent_handler.handle_purchase_intent.side_effect = RecursionError("loop")
    with pytest.raises(RecursionError, match="loop"):
        await service._process_text_message(
            user(), session(), "buy", {"intent": "buy_something", "confidence": 90}
        )


@pytest.mark.asyncio
async def test_chat_activation_greeting_and_seller_result_variants(monkeypatch):
    service = chat_stub()

    def initialize_with_state(current):
        current.workflow_state["sectioned_rfq"] = {}

    monkeypatch.setattr(chat_mod.WorkflowManager, "initialize_sectioned_rfq", initialize_with_state)
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_sectioned_rfq_section", MagicMock())
    monkeypatch.setattr(chat_mod.WorkflowManager, "set_workflow_type", MagicMock())
    activated = session()
    assert await service._activate_sectioned_rfq(user(), activated) == {
        "status": "sectioned_rfq_activated"
    }
    assert activated.workflow_state["sectioned_rfq"]["active"] is True

    assert service._get_workflow_or_default(session(), "not-a-workflow") == WorkflowType.general_inquiry

    no_role = user(role=None, name=None)
    assert await service._handle_greeting_inquiry(no_role, "hi", session()) == {
        "status": "greeting_handled"
    }

    service._seller_service.handle_seller_workflow.return_value = {
        "success": False, "workflow_step": "display_rfqs_to_seller", "message": None
    }
    assert (await service._handle_seller_flow(user(role="seller"), session(), "show"))["status"] == "seller_flow_processed"

    service._seller_service.handle_seller_workflow.return_value = {
        "success": False, "workflow_step": "payment_link_generated", "message": None
    }
    assert (await service._handle_seller_flow(user(role="seller"), session(), "pay"))["status"] == "seller_flow_processed"

    service._seller_service.handle_seller_workflow.return_value = {
        "success": False, "workflow_step": "other", "message": "already sent",
        "message_already_sent": True,
    }
    before = service.whatsapp_service.send_message.await_count
    assert (await service._handle_seller_flow(user(role="seller"), session(), "other"))["status"] == "seller_flow_processed"
    assert service.whatsapp_service.send_message.await_count == before

    service._seller_service.handle_seller_workflow.return_value = {
        "success": False, "workflow_step": "other", "message": "send this",
        "message_already_sent": False,
    }
    assert (await service._handle_seller_flow(user(role="seller"), session(), "other"))["status"] == "seller_flow_processed"
    assert service.whatsapp_service.send_message.await_count == before + 1

    service._seller_service.handle_seller_workflow.return_value = {
        "success": True, "workflow_step": "show_subscription_plans", "message": "plans"
    }
    existing = session(workflow_type=WorkflowType.seller_rfq_view)
    assert (await service._handle_seller_flow(user(role="seller"), existing, "plans"))["status"] == "seller_flow_processed"

    service._seller_service.handle_seller_workflow.return_value = {
        "status": "rfq_status_found", "message": "status"
    }
    assert (await service._handle_seller_flow(user(role="seller"), session(), "status"))["status"] == "rfq_status_found"


@pytest.mark.asyncio
async def test_chat_cleanup_without_openai_service():
    service = chat_stub()
    service._openai_service = None
    await service.cleanup()
