"""
WhatsApp RFQ-creation policy: buyers create RFQs only on the Procucev
Enterprise website or by emailing rfq@procucev.com.

With ENABLE_WHATSAPP_RFQ_CREATION off, no WhatsApp path may start an RFQ,
show an RFQ-creation button, or reach the RFQ-create API. These tests drive
each entry point through the real wiring rather than a hand-set attribute.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.config as app_config
import app.services.processors.text_message_processor as text_processor
import app.services.whatsapp_service as wa_mod
from app.models import WorkflowType
from app.services.chat_service import ChatService
from app.services.handlers.bfs_search_handler import BFSSearchHandler
from app.services.handlers.confirmation_handler import ConfirmationHandler
from app.utils import rfq_policy

PORTAL = "https://portal.example/login"
DISABLED = SimpleNamespace(enable_whatsapp_rfq_creation=False, PROCUCEV_PORTAL_URL=PORTAL)
ENABLED = SimpleNamespace(enable_whatsapp_rfq_creation=True, PROCUCEV_PORTAL_URL=PORTAL)


def make_user():
    return SimpleNamespace(
        phone_number="+919876543210", id="user-1", org_id="org-1",
        email="buyer@example.com", name="Ada Buyer", role="buyer", is_registered=True,
    )


def rfq_session(**state):
    workflow_state = {
        "pending_rfq": {"entities": {"description": "steel pipes"}},
        "sectioned_rfq": {"active": True},
        "extracted_entities": [{"description": "steel pipes"}],
    }
    workflow_state.update(state)
    return SimpleNamespace(
        session_id="session-1",
        external_user_id="919876543210",
        workflow_type=WorkflowType.rfq_creation,
        workflow_state=workflow_state,
        conversation_history={"messages": [], "openai_messages": [], "metadata": []},
    )


def sent_button_ids(whatsapp, call_index=-1):
    return [b["id"] for b in whatsapp.send_configurable_buttons.call_args_list[call_index].args[2]]


def assert_redirected(result, whatsapp, session):
    assert result["status"] == rfq_policy.RFQ_CREATION_DISABLED_STATUS
    assert "Procucev Enterprise website" in result["message"]
    assert rfq_policy.RFQ_EMAIL in result["message"]
    assert PORTAL in result["message"]
    assert sent_button_ids(whatsapp) == ["rfq_status", "search_bfs"]
    # The in-progress RFQ is dropped so later messages stop routing into RFQ creation
    assert "pending_rfq" not in session.workflow_state
    assert "sectioned_rfq" not in session.workflow_state
    assert session.workflow_type is None


# ---------------------------------------------------------------------------
# Policy module
# ---------------------------------------------------------------------------


def test_flag_is_explicit_and_fails_closed(monkeypatch, whatsapp_rfq_creation_disabled):
    assert rfq_policy.is_rfq_creation_enabled(ENABLED) is True
    assert rfq_policy.is_rfq_creation_enabled(DISABLED) is False

    # Settings without the flag fall back to the env var, which defaults to off
    monkeypatch.delenv("ENABLE_WHATSAPP_RFQ_CREATION", raising=False)
    assert rfq_policy.is_rfq_creation_enabled(SimpleNamespace()) is False
    monkeypatch.setenv("ENABLE_WHATSAPP_RFQ_CREATION", " TRUE ")
    assert rfq_policy.is_rfq_creation_enabled(SimpleNamespace()) is True

    # No settings passed: the global settings decide (production default is off)
    assert rfq_policy.is_rfq_creation_enabled() is False

    # Settings that fail to load never enable creation
    def broken():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(app_config, "get_settings", broken)
    monkeypatch.setenv("ENABLE_WHATSAPP_RFQ_CREATION", "false")
    assert rfq_policy.is_rfq_creation_enabled() is False
    assert rfq_policy.get_portal_url() == rfq_policy.DEFAULT_PORTAL_URL


def test_messages_name_portal_and_rfq_email():
    assert rfq_policy.get_portal_url(DISABLED) == PORTAL
    assert rfq_policy.get_portal_url(SimpleNamespace(PROCUCEV_PORTAL_URL="")) == rfq_policy.DEFAULT_PORTAL_URL

    notice = rfq_policy.rfq_policy_notice(DISABLED)
    assert "Important RFQ Policy" in notice and PORTAL in notice and "rfq@procucev.com" in notice

    message = rfq_policy.rfq_creation_disabled_message(DISABLED)
    assert "messages, buttons, or Excel uploads" in message and PORTAL in message

    assert "WhatsApp AI Agent" in rfq_policy.faq_rfq_channels_text(ENABLED)
    faq = rfq_policy.faq_rfq_channels_text(DISABLED)
    assert "cannot be created on WhatsApp" in faq and PORTAL in faq and "rfq@procucev.com" in faq


def test_filter_hides_every_rfq_creation_button():
    menu = [
        {"id": "create_rfq", "title": "Create new RFQ"},
        {"id": "rfq_status", "title": "Check RFQ Status"},
        {"id": "search_bfs", "title": "Search Ready Stocks"},
    ]
    assert rfq_policy.filter_rfq_creation_buttons(menu, ENABLED) is menu
    assert rfq_policy.filter_rfq_creation_buttons(None, DISABLED) is None
    assert rfq_policy.filter_rfq_creation_buttons([], DISABLED) == []

    no_rfq = [{"id": "rfq_status", "title": "Check RFQ Status"}]
    assert rfq_policy.filter_rfq_creation_buttons(no_rfq, DISABLED) is no_rfq

    assert [b["id"] for b in rfq_policy.filter_rfq_creation_buttons(menu, DISABLED)] == ["rfq_status", "search_bfs"]

    for button_id in rfq_policy.RFQ_CREATION_BUTTON_IDS:
        only_rfq = [{"id": button_id, "title": "RFQ"}]
        assert rfq_policy.filter_rfq_creation_buttons(only_rfq, DISABLED) == rfq_policy.FALLBACK_BUTTONS


def test_clear_rfq_workflow_resets_only_rfq_sessions():
    rfq_policy.clear_rfq_workflow(None)

    session = rfq_session()
    rfq_policy.clear_rfq_workflow(session)
    assert session.workflow_type is None
    assert "pending_rfq" not in session.workflow_state
    assert session.workflow_state["extracted_entities"] == [{"description": "steel pipes"}]

    other = SimpleNamespace(session_id="s", workflow_type=WorkflowType.rfq_status_check, workflow_state={})
    rfq_policy.clear_rfq_workflow(other)
    assert other.workflow_type == WorkflowType.rfq_status_check
    assert other.workflow_state == {}


@pytest.mark.asyncio
async def test_send_rfq_creation_disabled_uses_default_or_custom_message():
    whatsapp = SimpleNamespace(send_configurable_buttons=AsyncMock())
    session = rfq_session()
    result = await rfq_policy.send_rfq_creation_disabled(whatsapp, "+91", session, settings=DISABLED)
    assert_redirected(result, whatsapp, session)
    assert whatsapp.send_configurable_buttons.call_args.kwargs["session_id"] is session

    custom = await rfq_policy.send_rfq_creation_disabled(whatsapp, "+91", None, settings=DISABLED, message="custom")
    assert custom == {"status": rfq_policy.RFQ_CREATION_DISABLED_STATUS, "message": "custom"}


# ---------------------------------------------------------------------------
# ChatService entry points (typed message, buttons, BFS, sectioned, Excel)
# ---------------------------------------------------------------------------


def chat_service(settings=DISABLED):
    service = ChatService.__new__(ChatService)
    service.settings = settings
    service.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock(), send_message=AsyncMock())
    service.session_manager = SimpleNamespace(save_session=AsyncMock())
    service.chat_summary_service = MagicMock()
    service._response_helpers = MagicMock()
    service._entity_service = SimpleNamespace(extract_entities=AsyncMock())
    service._products_array_handler = MagicMock()
    service._confirmation_handler = MagicMock()
    service._attachment_decision_handler = MagicMock()
    service._purchase_intent_handler = None
    service._bfs_search_handler = SimpleNamespace(handle_button=AsyncMock(return_value={"status": "bfs_activate_rfq"}))
    return service


@pytest.mark.asyncio
async def test_typed_purchase_message_is_blocked_through_real_handler_wiring():
    service = chat_service()
    handler = service.purchase_intent_handler
    assert handler.settings is service.settings

    session = rfq_session()
    result = await handler.handle_purchase_intent(make_user(), session, "I need 100 steel pipes", {"intent": "buy_something"})

    assert_redirected(result, service.whatsapp_service, session)
    service._entity_service.extract_entities.assert_not_awaited()
    service.session_manager.save_session.assert_awaited()


@pytest.mark.asyncio
async def test_handler_without_settings_falls_back_to_production_default(whatsapp_rfq_creation_disabled):
    service = chat_service()
    del service.settings
    handler = service.purchase_intent_handler
    assert handler.settings is None

    session = rfq_session()
    result = await handler.handle_purchase_intent(make_user(), session, {"text": {"body": "buy pumps"}}, None)
    assert result["status"] == rfq_policy.RFQ_CREATION_DISABLED_STATUS


@pytest.mark.asyncio
@pytest.mark.parametrize("button_id", ["create_rfq", "new_rfq", "raise_rfq", "bfs_raise_rfq", "confirm_items"])
async def test_rfq_buttons_are_blocked(button_id):
    service = chat_service()
    session = rfq_session()

    result = await service._handle_button_response(make_user(), session, button_id)

    assert_redirected(result, service.whatsapp_service, session)
    service.whatsapp_service.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sectioned_activation_is_blocked_and_still_works_when_enabled():
    service = chat_service()
    session = rfq_session()
    result = await service._activate_sectioned_rfq(make_user(), session)
    assert_redirected(result, service.whatsapp_service, session)

    enabled = chat_service(ENABLED)
    fresh = SimpleNamespace(session_id="s", workflow_type=None, workflow_state={})
    assert (await enabled._activate_sectioned_rfq(make_user(), fresh))["status"] == "sectioned_rfq_activated"
    assert fresh.workflow_state["sectioned_rfq"]["active"] is True


@pytest.mark.asyncio
async def test_excel_upload_is_refused_with_portal_and_email():
    service = chat_service()
    session = rfq_session()

    result = await service._block_excel_upload(make_user(), session)

    assert result["status"] == rfq_policy.RFQ_CREATION_DISABLED_STATUS
    assert result["response"] == "excel_upload_disabled"
    assert "Excel upload is no longer supported" in result["message"]
    assert PORTAL in result["message"] and "rfq@procucev.com" in result["message"]
    assert session.workflow_type is None
    service.session_manager.save_session.assert_awaited_once_with(session)


# ---------------------------------------------------------------------------
# Confirmation handler: the only caller of the RFQ-create API
# ---------------------------------------------------------------------------


def confirmation_handler(settings=DISABLED):
    handler = ConfirmationHandler.__new__(ConfirmationHandler)
    handler.settings = settings
    handler.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock())
    handler.session_manager = SimpleNamespace(save_session=AsyncMock())
    handler.rfq_service = SimpleNamespace(create_rfq=AsyncMock(return_value={"success": True}))
    handler.confirmation_service = SimpleNamespace(parse_confirmation=AsyncMock(return_value="yes"))
    handler._handle_restart_workflow = AsyncMock(return_value={"status": "restarted"})
    return handler


@pytest.mark.asyncio
async def test_confirmation_paths_never_reach_rfq_api():
    for button_id in ("confirm_rfq", "continue_rfq", "confirm_no_changes", "no_rfq"):
        handler = confirmation_handler()
        session = rfq_session()
        result = await handler.handle_confirmation_button(make_user(), session, button_id)
        assert_redirected(result, handler.whatsapp_service, session)
        handler.rfq_service.create_rfq.assert_not_awaited()

    handler = confirmation_handler()
    session = rfq_session()
    assert_redirected(await handler.handle_pending_confirmations(make_user(), session, "yes", {}),
                      handler.whatsapp_service, session)
    handler.confirmation_service.parse_confirmation.assert_not_awaited()

    handler = confirmation_handler()
    session = rfq_session(pending_optional_rfq={"entities": {}})
    assert_redirected(await handler.handle_optional_fields_response(make_user(), session, "skip"),
                      handler.whatsapp_service, session)

    handler = confirmation_handler()
    handler.session_manager = None
    result = await handler.handle_confirmation_button(make_user(), rfq_session(), "confirm_rfq")
    assert result["status"] == rfq_policy.RFQ_CREATION_DISABLED_STATUS

    # Restart only clears state, so it stays available
    handler = confirmation_handler()
    assert await handler.handle_confirmation_button(make_user(), rfq_session(), "restart_rfq") == {"status": "restarted"}


@pytest.mark.asyncio
async def test_submit_refuses_rfq_api_call_when_disabled():
    handler = confirmation_handler()
    schema = SimpleNamespace(model_dump=lambda: {"items": [{"description": "pipes", "quantity": 1}]})

    result = await handler._submit_rfq_to_backend(schema, make_user())

    assert result == {
        "success": False,
        "status": rfq_policy.RFQ_CREATION_DISABLED_STATUS,
        "error": "RFQ creation via WhatsApp is disabled",
    }
    handler.rfq_service.create_rfq.assert_not_awaited()


# ---------------------------------------------------------------------------
# Outgoing UI: no RFQ-creation button or invitation reaches the buyer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatsapp_sender_strips_rfq_creation_buttons(whatsapp_rfq_creation_disabled):
    service = wa_mod.WhatsAppService.__new__(wa_mod.WhatsAppService)
    service._format_phone_number = lambda recipient: "919876543210"
    service.send_interactive_message = AsyncMock(return_value=wa_mod.MessageResponse(True, "m"))
    service._track_message_in_history = AsyncMock()

    menu = [
        {"id": "create_rfq", "title": "Create new RFQ"},
        {"id": "rfq_status", "title": "Check RFQ Status"},
        {"id": "search_bfs", "title": "Search Ready Stocks"},
    ]
    assert (await service.send_configurable_buttons("+919876543210", "menu", menu, skip_concatenation=True)).success
    content = service.send_interactive_message.call_args.args[2]
    assert [b["reply"]["id"] for b in content["action"]["buttons"]] == ["rfq_status", "search_bfs"]

    await service.send_configurable_buttons(
        "+919876543210", "no stock", [{"id": "bfs_raise_rfq", "title": "Create new RFQ"}], skip_concatenation=True
    )
    content = service.send_interactive_message.call_args.args[2]
    assert [b["reply"]["id"] for b in content["action"]["buttons"]] == ["rfq_status", "search_bfs"]


@pytest.mark.asyncio
async def test_bfs_no_results_text_follows_policy(monkeypatch, whatsapp_rfq_creation_disabled):
    handler = BFSSearchHandler.__new__(BFSSearchHandler)
    handler.whatsapp_service = SimpleNamespace(send_configurable_buttons=AsyncMock(), send_message=AsyncMock())

    await handler._send_bfs_results(make_user(), rfq_session(), [])
    body = handler.whatsapp_service.send_configurable_buttons.call_args.args[1]
    assert "Important RFQ Policy" in body and "rfq@procucev.com" in body
    assert "button below" not in body

    monkeypatch.setattr(app_config.get_settings(), "enable_whatsapp_rfq_creation", True)
    await handler._send_bfs_results(make_user(), rfq_session(), [])
    assert "button below" in handler.whatsapp_service.send_configurable_buttons.call_args.args[1]


@pytest.mark.asyncio
async def test_clarification_question_does_not_offer_rfq_creation(monkeypatch, whatsapp_rfq_creation_disabled):
    response = SimpleNamespace(generate_clarification_response=AsyncMock(return_value="clarify"))
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    processor = text_processor.TextMessageProcessor(MagicMock(), MagicMock(), whatsapp, response,
                                                    MagicMock(), MagicMock(), MagicMock(), MagicMock())
    session = SimpleNamespace(session_id="s", workflow_state={}, conversation_history={})

    assert (await processor._handle_clarification_request(make_user(), "hmm", session))["status"] == "clarification_sent"
    questions = response.generate_clarification_response.call_args.args[0]
    assert "Are you looking to check your RFQ status or ready stock availability?" in questions

    monkeypatch.setattr(app_config.get_settings(), "enable_whatsapp_rfq_creation", True)
    await processor._handle_clarification_request(make_user(), "hmm", SimpleNamespace(session_id="s", workflow_state={}))
    assert "Are you looking to create an RFQ or check product availability?" in \
        response.generate_clarification_response.call_args.args[0]


# ---------------------------------------------------------------------------
# Buyer services off: buyers register/verify, then only get the portal template
# ---------------------------------------------------------------------------

BUYER_OFF = SimpleNamespace(enable_whatsapp_rfq_creation=False, enable_whatsapp_buyer_services=False,
                            PROCUCEV_PORTAL_URL=PORTAL, procucev_rfq_details_url=PORTAL)
BUYER_ON = SimpleNamespace(enable_whatsapp_rfq_creation=False, enable_whatsapp_buyer_services=True,
                           PROCUCEV_PORTAL_URL=PORTAL)


def assert_portal_template(message):
    assert message.endswith(
        "Your buyer profile has just been upgraded with AI to simplify your experience. "
        "To raise an RFQ, please email us at rfq@procucev.com or log in at https://qua.procucev.com."
    )
    # Nothing else on WhatsApp is offered to buyers any more
    assert "ready stocks" not in message.lower() and "check rfq status" not in message.lower()


def test_buyer_portal_login_address_is_configurable(monkeypatch):
    monkeypatch.delenv("BUYER_PORTAL_DISPLAY_URL", raising=False)
    assert rfq_policy.get_buyer_portal_display(SimpleNamespace()) == "https://qua.procucev.com"
    monkeypatch.setenv("BUYER_PORTAL_DISPLAY_URL", " equa.procucev.com ")
    assert rfq_policy.get_buyer_portal_display(SimpleNamespace()) == "equa.procucev.com"
    assert rfq_policy.get_buyer_portal_display(SimpleNamespace(buyer_portal_display_url="portal.example")) == "portal.example"
    assert rfq_policy.buyer_portal_message(SimpleNamespace(buyer_portal_display_url="portal.example")).endswith("log in at portal.example.")


def test_buyer_services_flag_is_explicit_and_fails_closed(monkeypatch, whatsapp_buyer_services_disabled):
    assert rfq_policy.is_buyer_whatsapp_enabled(BUYER_ON) is True
    assert rfq_policy.is_buyer_whatsapp_enabled(BUYER_OFF) is False
    assert rfq_policy.is_buyer_whatsapp_enabled() is False

    monkeypatch.delenv("ENABLE_WHATSAPP_BUYER_SERVICES", raising=False)
    assert rfq_policy.is_buyer_whatsapp_enabled(SimpleNamespace()) is False
    monkeypatch.setenv("ENABLE_WHATSAPP_BUYER_SERVICES", "true")
    assert rfq_policy.is_buyer_whatsapp_enabled(SimpleNamespace()) is True

    assert_portal_template(rfq_policy.buyer_portal_message(BUYER_OFF))


@pytest.mark.asyncio
async def test_redirect_buyer_sends_template_then_ends_session():
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    exit_service = SimpleNamespace(handle_exit_intent=AsyncMock())
    session = rfq_session()

    result = await rfq_policy.redirect_buyer_to_portal(whatsapp, exit_service, "+91", session, settings=BUYER_OFF)
    assert result["status"] == "user_exited" and result["exit_completed"] is True
    assert_portal_template(result["message"])
    whatsapp.send_message.assert_awaited_once_with("+91", result["message"], session_id=session)
    exit_service.handle_exit_intent.assert_awaited_once_with("+91", session, show_message=False)

    with_intro = await rfq_policy.redirect_buyer_to_portal(whatsapp, exit_service, "+91", session,
                                                           settings=BUYER_OFF, intro="Hello")
    assert with_intro["message"].startswith("Hello\n\nYour buyer profile")


def test_chat_service_recognises_disabled_buyers():
    from app.schemas.user import UserRole

    service = chat_service(BUYER_OFF)
    assert service._is_disabled_buyer(SimpleNamespace(role="buyer")) is True
    assert service._is_disabled_buyer(SimpleNamespace(role=SimpleNamespace(value="buyer"))) is True
    assert service._is_disabled_buyer(SimpleNamespace(role=UserRole.SELLER)) is False
    assert service._is_disabled_buyer(SimpleNamespace(role=None)) is False
    assert chat_service(BUYER_ON)._is_disabled_buyer(SimpleNamespace(role="buyer")) is False


@pytest.mark.asyncio
async def test_logged_in_buyer_message_gets_only_the_template(monkeypatch):
    import app.services.chat_service as chat_mod
    from tests.unit.test_chat_service_targeted_uncovered_paths import cache_double, chat_stub, session, user

    monkeypatch.setattr(chat_mod.ChatServiceHelpers, "build_conversation_context", AsyncMock(return_value={}))
    monkeypatch.setattr("app.services.user_cache_service.get_user_cache_service", lambda: cache_double())

    class AuthenticatedUser(SimpleNamespace):
        pass

    monkeypatch.setattr(chat_mod, "User", AuthenticatedUser)

    for message_type, content in (("text", "I need 100 pipes"), ("interactive", {"button_reply": {"id": "search_bfs"}})):
        service = chat_stub()
        service.settings = BUYER_OFF
        current = session()
        service.session_manager.get_conversation_context.return_value = current
        service.intent_service.classify_intent.return_value = {"intent": "other", "confidence": 0, "relevant_message": ""}
        service.authentication_orchestrator_flow = AsyncMock(return_value=AuthenticatedUser(**vars(user())))
        service._process_text_message = AsyncMock()
        service._process_interactive_message = AsyncMock()

        result = await service.process_message("+919999999999", content, message_type)

        assert result["status"] == "user_exited"
        assert_portal_template(service.whatsapp_service.send_message.await_args.args[1])
        service._exit_service.handle_exit_intent.assert_awaited_once_with("+919999999999", current, show_message=False)
        service._process_text_message.assert_not_awaited()
        service._process_interactive_message.assert_not_awaited()
        service.whatsapp_service.send_configurable_buttons.assert_not_awaited()


class FakeExitService:
    instances = []

    def __init__(self, *args):
        self.args = args
        self.handle_exit_intent = AsyncMock(return_value={"status": "exit_completed"})
        FakeExitService.instances.append(self)


@pytest.mark.asyncio
async def test_selecting_a_buyer_profile_sends_template_without_logging_in(monkeypatch):
    from app.services.profile_selection_service import ProfileSelectionService

    FakeExitService.instances.clear()
    monkeypatch.setattr("app.services.exit_service.ExitService", FakeExitService)
    service = ProfileSelectionService.__new__(ProfileSelectionService)
    service.settings = BUYER_OFF
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    service.authentication_service = SimpleNamespace(store_user_session=AsyncMock())
    session = rfq_session()
    profile = {"role": "buyer", "email": "buyer@example.com", "user_data": {"id": "u1"}}

    result = await service._set_active_profile_and_proceed("+919876543210", profile, session, "hi", "greeting")

    assert result["status"] == "user_exited"
    message = service.whatsapp_service.send_message.await_args.args[1]
    assert message == rfq_policy.buyer_portal_message(BUYER_OFF, email="buyer@example.com", phone="+919876543210")
    assert_portal_template(message)
    service.authentication_service.store_user_session.assert_not_awaited()
    service.whatsapp_service.send_configurable_buttons.assert_not_awaited()
    exit_service = FakeExitService.instances[0]
    assert exit_service.args[1] is service.authentication_service
    exit_service.handle_exit_intent.assert_awaited_once_with("+919876543210", session, show_message=False)

    # The same holds when the buyer arrives through the role menu
    result = await service._show_role_based_menu("+919876543210", profile, rfq_session())
    assert result["status"] == "user_exited"


@pytest.mark.asyncio
async def test_registration_otp_success_sends_login_details_and_template(monkeypatch):
    from app.services.registration_service import RegistrationService

    FakeExitService.instances.clear()
    monkeypatch.setattr("app.services.exit_service.ExitService", FakeExitService)

    class Approved:
        def __init__(self, *args, **kwargs):
            pass

        async def _check_domain_approval(self, *args, **kwargs):
            return {"approved": True}

    monkeypatch.setattr("app.services.verification_check_service.VerificationCheckService", Approved)

    service = RegistrationService.__new__(RegistrationService)
    service.settings = BUYER_OFF
    service.whatsapp_service = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    service.session_manager = SimpleNamespace(save_session=AsyncMock())
    service._check_exit_command = AsyncMock(return_value=False)
    service.otp_service = SimpleNamespace(handle_user_message=AsyncMock(return_value={"status": "otp_valid"}))
    service.auth_api_service = SimpleNamespace(authenticate_user=AsyncMock(return_value={
        "success": True,
        "data": [{"id": "u1", "username": "buyer@example.com", "role": "buyer", "selfClient": True}],
    }))
    service.authentication_service = SimpleNamespace(store_user_session=AsyncMock(return_value=True))
    session = rfq_session()
    session.workflow_state = {
        "pending_registration_data": {"email": "buyer@example.com", "name": "Alex Buyer", "user_id": "u1"},
        "user_type": "buyer",
    }

    result = await service.handle_registration_otp_validation("+919876543210", "123456", session)

    assert result["status"] == "user_exited" and result["user_type"] == "buyer"
    message = service.whatsapp_service.send_message.await_args.args[1]
    assert message == rfq_policy.buyer_registration_message("buyer@example.com", "+919876543210", BUYER_OFF)
    assert "Username / Email: buyer@example.com" in message
    assert "Registered Mobile: +91 9876543210" in message
    assert "For your login credentials, please check your registered email." in message
    service.whatsapp_service.send_configurable_buttons.assert_not_awaited()
    FakeExitService.instances[0].handle_exit_intent.assert_awaited_once()


def test_buyer_registration_message_matches_the_agreed_text(monkeypatch):
    monkeypatch.delenv("BUYER_PORTAL_DISPLAY_URL", raising=False)
    assert rfq_policy.buyer_registration_message("rohit@example.com", "919511876403", SimpleNamespace()) == (
        "Thank you! Your registration has been received.\n\n"
        "Registration Details:\n\n"
        "Username / Email: rohit@example.com\n"
        "Registered Mobile: +91 9511876403\n\n"
        "Your buyer profile has just been upgraded with AI to simplify your experience. "
        "To raise an RFQ, please email us at rfq@procucev.com or log in at https://qua.procucev.com.\n\n"
        "For your login credentials, please check your registered email."
    )


def test_registered_mobile_is_shown_as_plus_91():
    assert rfq_policy.format_registered_mobile("919511876403") == "+91 9511876403"
    assert rfq_policy.format_registered_mobile("+91 95118 76403") == "+91 9511876403"
    assert rfq_policy.format_registered_mobile("9511876403") == "+91 9511876403"
    assert rfq_policy.format_registered_mobile("14155550100") == "+14155550100"
    assert rfq_policy.format_registered_mobile(None) == ""


@pytest.mark.asyncio
async def test_redirect_buyer_can_send_a_given_message():
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    exit_service = SimpleNamespace(handle_exit_intent=AsyncMock())
    result = await rfq_policy.redirect_buyer_to_portal(whatsapp, exit_service, "+91", rfq_session(), settings=BUYER_OFF, message="Custom")
    assert result["message"] == "Custom"


def test_buyer_portal_message_with_details_matches_the_agreed_text(monkeypatch):
    monkeypatch.delenv("BUYER_PORTAL_DISPLAY_URL", raising=False)
    assert rfq_policy.buyer_portal_message(SimpleNamespace(), email="rohit@example.com", phone="919511876403") == (
        "Registration Details:\n\n"
        "Username / Email: rohit@example.com\n"
        "Registered Mobile: +91 9511876403\n\n"
        "Your buyer profile has just been upgraded with AI to simplify your experience. "
        "To raise an RFQ, please email us at rfq@procucev.com or log in at https://qua.procucev.com."
    )
