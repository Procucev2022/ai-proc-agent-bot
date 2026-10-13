"""
Branch-coverage tests for ProfileSelectionService buyer-flow logic.

These tests exercise the conditional branches added for the WhatsApp buyer flow
that hides RFQ creation behind the ENABLE_WHATSAPP_RFQ_CREATION flag, plus the
deterministic selection/parsing and no-account/intent-mismatch response paths.

All external side effects (WhatsApp, OpenAI, Redis, auth, exit) are mocked.
`user_selection_tool` is set to None on purpose: it is the service's only OpenAI
dependency, and None forces the deterministic fast-path/fallback branches without
any network stub. Tests are deterministic and sub-millisecond.
"""

import pytest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from app.models import UserType
from app.services.profile_selection_service import ProfileSelectionService


PHONE = "+919876543210"
PORTAL = "https://p2pdevuiindia.azurewebsites.net/login"


def fake_session():
    return SimpleNamespace(
        session_id="session-123",
        external_user_id="919876543210",
        workflow_state={},
        user_type=None,
    )


def fake_profile(role="buyer", email="buyer@procucev.com", full_name="Jane Buyer"):
    return {
        "email": email,
        "role": role,
        "name": full_name,
        "company": "Enterprise Inc",
        "user_data": {
            "id": "user-123",
            "username": email,
            "fullName": full_name,
            "selfClient": role == "buyer",
        },
    }


def build_service(rfq_enabled=False):
    service = object.__new__(ProfileSelectionService)
    service.whatsapp_service = SimpleNamespace(
        send_message=AsyncMock(),
        send_configurable_buttons=AsyncMock(),
    )
    service.settings = SimpleNamespace(
        enable_whatsapp_rfq_creation=rfq_enabled,
        PROCUCEV_PORTAL_URL=PORTAL,
    )
    service.user_selection_tool = None
    return service


# ---------------------------------------------------------------------------
# 1. _handle_neutral_greeting flag label + role-preservation branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_neutral_greeting_rfq_disabled_label():
    service = build_service(rfq_enabled=False)
    session = fake_session()

    result = await service._handle_neutral_greeting(PHONE, [fake_profile()], session)

    assert result["status"] == "profile_selection_sent"
    assert result["selection_type"] == "neutral_greeting"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Buyer – Check Your Account Status" in sent
    assert session.user_type == UserType.unknown


@pytest.mark.asyncio
async def test_neutral_greeting_rfq_enabled_label():
    service = build_service(rfq_enabled=True)
    session = fake_session()

    result = await service._handle_neutral_greeting(PHONE, [fake_profile()], session)

    assert result["status"] == "profile_selection_sent"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Buy — Create or check my RFQs" in sent


@pytest.mark.asyncio
async def test_neutral_greeting_preserves_existing_role():
    service = build_service(rfq_enabled=False)
    session = fake_session()
    session.user_type = UserType.seller

    await service._handle_neutral_greeting(PHONE, [fake_profile()], session)

    # Guard is skipped because user_type is already seller (arc 279->281).
    assert session.user_type == UserType.seller


# ---------------------------------------------------------------------------
# 2. _handle_buyer_intent flag branches (no-accounts and accounts-exist)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buyer_intent_no_accounts_rfq_disabled():
    service = build_service(rfq_enabled=False)
    session = fake_session()

    result = await service._handle_buyer_intent(
        PHONE, [fake_profile(role="seller")], "buy", session, {"intent": "buy_something"}
    )

    assert result["status"] == "buyer_no_accounts_message_sent"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Got it! Let's check your Buyer account." in sent


@pytest.mark.asyncio
async def test_buyer_intent_no_accounts_rfq_enabled():
    service = build_service(rfq_enabled=True)
    session = fake_session()

    result = await service._handle_buyer_intent(
        PHONE, [fake_profile(role="seller")], "buy", session, {"intent": "buy_something"}
    )

    assert result["status"] == "buyer_no_accounts_message_sent"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Got it! You'd like to buy or raise an RFQ." in sent


@pytest.mark.asyncio
async def test_buyer_intent_with_accounts_rfq_disabled():
    service = build_service(rfq_enabled=False)
    session = fake_session()

    result = await service._handle_buyer_intent(
        PHONE, [fake_profile(role="buyer")], "buy", session, {"intent": "buy_something"}
    )

    assert result["status"] == "buyer_profile_selection_presented"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Got it! Let's check your Buyer account." in sent


@pytest.mark.asyncio
async def test_buyer_intent_with_accounts_rfq_enabled():
    service = build_service(rfq_enabled=True)
    session = fake_session()

    result = await service._handle_buyer_intent(
        PHONE, [fake_profile(role="buyer")], "buy", session, {"intent": "buy_something"}
    )

    assert result["status"] == "buyer_profile_selection_presented"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Got it! You'd like to buy or raise an RFQ." in sent


# ---------------------------------------------------------------------------
# 3. _process_selected_profile buyer/seller button-set branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_selected_buyer_profile_rfq_enabled_buttons():
    service = build_service(rfq_enabled=True)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    session = fake_session()
    session.workflow_state = {"profile_selection_stage": "buyer_intent"}
    selected = {"profile": fake_profile(role="buyer")}

    result = await service._process_selected_profile(PHONE, selected, session)

    assert result["status"] == "buyer_options_presented"
    args = service.whatsapp_service.send_configurable_buttons.call_args_list[0][0]
    button_ids = [b["id"] for b in args[2]]
    assert "create_rfq" in button_ids
    assert "rfq_status" in button_ids
    assert "search_bfs" in button_ids


@pytest.mark.asyncio
async def test_process_selected_buyer_profile_rfq_disabled_buttons():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    session = fake_session()
    session.workflow_state = {"profile_selection_stage": "buyer_intent"}
    selected = {"profile": fake_profile(role="buyer")}

    result = await service._process_selected_profile(PHONE, selected, session)

    assert result["status"] == "buyer_options_presented"
    args = service.whatsapp_service.send_configurable_buttons.call_args_list[0][0]
    body_text = args[1]
    button_ids = [b["id"] for b in args[2]]
    assert "create_rfq" not in button_ids
    assert "rfq_status" in button_ids
    assert "search_bfs" in button_ids
    assert "Important RFQ Policy" in body_text
    assert "rfq@procucev.com" in body_text


@pytest.mark.asyncio
async def test_process_selected_seller_profile_options():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    session = fake_session()
    session.workflow_state = {"profile_selection_stage": "seller_intent"}
    selected = {"profile": fake_profile(role="seller", email="seller@procucev.com")}

    result = await service._process_selected_profile(PHONE, selected, session)

    assert result["status"] == "seller_options_presented"
    assert result["user_type"] == "seller"
    assert result["email"] == "seller@procucev.com"


@pytest.mark.asyncio
async def test_process_selected_profile_auth_not_completed_returns_result():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "verification_required"}
    )
    session = fake_session()
    session.workflow_state = {"profile_selection_stage": "buyer_intent"}
    selected = {"profile": fake_profile(role="buyer")}

    result = await service._process_selected_profile(PHONE, selected, session)

    assert result["status"] == "verification_required"
    assert not service.whatsapp_service.send_configurable_buttons.called


@pytest.mark.asyncio
async def test_process_selected_rfq_status_stage():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    session = fake_session()
    session.workflow_state = {"profile_selection_stage": "rfq_status_check"}
    selected = {"profile": fake_profile(role="buyer")}

    result = await service._process_selected_profile(PHONE, selected, session)

    assert result["status"] == "profile_selected_and_authenticated"


@pytest.mark.asyncio
async def test_process_selected_action_register_buyer():
    service = build_service(rfq_enabled=False)
    service._redirect_to_buyer_registration = AsyncMock(
        return_value={"status": "redirected_to_buyer_registration"}
    )
    session = fake_session()

    result = await service._process_selected_profile(
        PHONE, {"action": "register_buyer"}, session
    )

    assert result["status"] == "redirected_to_buyer_registration"


@pytest.mark.asyncio
async def test_process_selected_action_exit():
    service = build_service(rfq_enabled=False)
    service._handle_exit_action = AsyncMock(return_value={"status": "exit_completed"})
    session = fake_session()

    result = await service._process_selected_profile(
        PHONE, {"action": "exit"}, session
    )

    assert result["status"] == "exit_completed"


@pytest.mark.asyncio
async def test_process_selected_registration_type_seller():
    service = build_service(rfq_enabled=False)
    service._redirect_to_seller_registration = AsyncMock(
        return_value={"status": "redirected_to_seller_registration"}
    )
    session = fake_session()

    result = await service._process_selected_profile(
        PHONE, {"registration_type": "seller"}, session
    )

    assert result["status"] == "redirected_to_seller_registration"


# ---------------------------------------------------------------------------
# 4. _show_role_based_menu buyer flag branches + name present/absent + seller
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_role_menu_buyer_rfq_disabled_no_name():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    profile = fake_profile(role="buyer")
    profile["user_data"] = {"selfClient": True, "username": profile["email"], "id": "u1"}
    session = fake_session()

    result = await service._show_role_based_menu(PHONE, profile, session)

    assert result["status"] == "role_menu_presented"
    assert result["user_type"] == "buyer"
    args = service.whatsapp_service.send_configurable_buttons.call_args_list[0][0]
    body_text = args[1]
    header = args[3]
    button_ids = [b["id"] for b in args[2]]
    assert "create_rfq" not in button_ids
    assert "Important RFQ Policy" in body_text
    assert "there" in header


@pytest.mark.asyncio
async def test_role_menu_buyer_rfq_enabled_with_name():
    service = build_service(rfq_enabled=True)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    profile = fake_profile(role="buyer")
    profile["user_data"] = {
        "selfClient": True,
        "username": profile["email"],
        "id": "u1",
        "fullName": "jane buyer",
    }
    session = fake_session()

    result = await service._show_role_based_menu(PHONE, profile, session)

    assert result["status"] == "role_menu_presented"
    args = service.whatsapp_service.send_configurable_buttons.call_args_list[0][0]
    header = args[3]
    button_ids = [b["id"] for b in args[2]]
    assert "Jane Buyer" in header
    assert "create_rfq" in button_ids


@pytest.mark.asyncio
async def test_role_menu_seller():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "profile_selected_and_authenticated"}
    )
    profile = fake_profile(role="seller", email="seller@procucev.com")
    profile["user_data"] = {"selfClient": False, "username": profile["email"], "id": "u2"}
    session = fake_session()

    result = await service._show_role_based_menu(PHONE, profile, session)

    assert result["status"] == "role_menu_presented"
    assert result["user_type"] == "seller"
    args = service.whatsapp_service.send_configurable_buttons.call_args_list[0][0]
    button_ids = [b["id"] for b in args[2]]
    assert "rfq_status" in button_ids
    assert "contact_support" in button_ids


@pytest.mark.asyncio
async def test_role_menu_verification_required_short_circuits():
    service = build_service(rfq_enabled=False)
    service._set_active_profile_and_proceed = AsyncMock(
        return_value={"status": "verification_required"}
    )
    session = fake_session()

    result = await service._show_role_based_menu(PHONE, fake_profile(role="buyer"), session)

    assert result["status"] == "verification_required"
    assert not service.whatsapp_service.send_configurable_buttons.called


# ---------------------------------------------------------------------------
# 5. _set_active_profile_and_proceed existing-token vs no-token branch
# ---------------------------------------------------------------------------

def build_service_with_auth(existing_token=None, access_granted=True, store_ok=True):
    service = build_service(rfq_enabled=False)
    service.authentication_service = SimpleNamespace(
        auth_redis_service=SimpleNamespace(retrieve=AsyncMock(return_value=existing_token)),
        verification_check_service=SimpleNamespace(
            check_and_enforce_verification=AsyncMock(
                return_value={"access_granted": access_granted}
            )
        ),
        store_user_session=AsyncMock(return_value=store_ok),
    )
    return service


@pytest.mark.asyncio
async def test_set_active_profile_with_existing_token():
    service = build_service_with_auth(existing_token="tok")
    session = fake_session()

    result = await service._set_active_profile_and_proceed(
        PHONE, fake_profile(role="buyer"), session, "msg", "buy_something"
    )

    assert result["status"] == "profile_selected_and_authenticated"
    assert result["redirect_to_main_flow"] is True
    assert session.user_type == UserType.buyer
    # Token short-circuit: verification was NOT awaited (arc 1034->1049).
    assert not service.authentication_service.verification_check_service.check_and_enforce_verification.called


@pytest.mark.asyncio
async def test_set_active_profile_no_token_verification_passes():
    service = build_service_with_auth(existing_token=None, access_granted=True)
    session = fake_session()

    result = await service._set_active_profile_and_proceed(
        PHONE, fake_profile(role="seller", email="seller@procucev.com"),
        session, "msg", "sell_something"
    )

    assert result["status"] == "profile_selected_and_authenticated"
    assert session.user_type == UserType.seller
    assert service.authentication_service.verification_check_service.check_and_enforce_verification.called


@pytest.mark.asyncio
async def test_set_active_profile_session_store_fails():
    service = build_service_with_auth(existing_token="tok", store_ok=False)
    session = fake_session()

    result = await service._set_active_profile_and_proceed(
        PHONE, fake_profile(role="buyer"), session, "msg", "buy_something"
    )

    assert result["status"] == "error"
    assert "store session" in result["error"].lower()


# ---------------------------------------------------------------------------
# 6. _parse_profile_selection deterministic fast-path branches (no AI)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parse_numeric_fast_path():
    service = build_service()
    options = [
        {"number": 1, "profile": fake_profile()},
        {"number": 2, "profile": fake_profile(email="other@procucev.com")},
    ]

    result = await service._parse_profile_selection("2", options)

    assert result["number"] == 2


@pytest.mark.asyncio
async def test_parse_keyword_register_buyer():
    service = build_service()
    options = [{"number": 1, "action": "register_buyer", "display": "Register as Buyer"}]

    result = await service._parse_profile_selection("buyer", options)

    assert result["action"] == "register_buyer"


@pytest.mark.asyncio
async def test_parse_keyword_register_seller():
    service = build_service()
    options = [{"number": 1, "action": "register_seller", "display": "Register as Seller"}]

    result = await service._parse_profile_selection("seller", options)

    assert result["action"] == "register_seller"


@pytest.mark.asyncio
async def test_parse_keyword_exit():
    service = build_service()
    options = [{"number": 1, "action": "exit", "display": "Exit"}]

    result = await service._parse_profile_selection("cancel", options)

    assert result["action"] == "exit"


@pytest.mark.asyncio
async def test_parse_no_match_returns_none_without_tool():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    options = [{"number": 1, "profile": fake_profile()}]

    result = await service._parse_profile_selection("gibberish", options)

    assert result is None


# ---------------------------------------------------------------------------
# 7. _enhanced_simple_parse_profile_selection phrase-detection branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enhanced_parse_new_registration_phrase():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    options = [{"number": 1, "action": "register_buyer", "display": "Register as Buyer"}]

    result = await service._enhanced_simple_parse_profile_selection("create new", options)

    assert result["action"] == "register_buyer"


@pytest.mark.asyncio
async def test_enhanced_parse_existing_account_phrase():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    options = [{"number": 1, "profile": fake_profile()}]

    result = await service._enhanced_simple_parse_profile_selection("use existing", options)

    assert "profile" in result


@pytest.mark.asyncio
async def test_enhanced_parse_numeric():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    options = [{"number": 1, "profile": fake_profile()}]

    result = await service._enhanced_simple_parse_profile_selection("1", options)

    assert result["number"] == 1


@pytest.mark.asyncio
async def test_enhanced_parse_returns_none():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    options = [{"number": 1, "profile": fake_profile()}]

    result = await service._enhanced_simple_parse_profile_selection("xyz", options)

    assert result is None


# ---------------------------------------------------------------------------
# 8. _handle_registration_type_response keyword-fallback branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registration_type_keyword_buyer():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    service._redirect_to_buyer_registration = AsyncMock(
        return_value={"status": "redirected_to_buyer_registration"}
    )
    session = fake_session()
    session.workflow_state = {"awaiting_registration_type": True}

    result = await service._handle_registration_type_response(PHONE, "1", session)

    assert result["status"] == "redirected_to_buyer_registration"
    assert "awaiting_registration_type" not in session.workflow_state


@pytest.mark.asyncio
async def test_registration_type_keyword_seller():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    service._redirect_to_seller_registration = AsyncMock(
        return_value={"status": "redirected_to_seller_registration"}
    )
    session = fake_session()
    session.workflow_state = {"awaiting_registration_type": True}

    result = await service._handle_registration_type_response(PHONE, "sell", session)

    assert result["status"] == "redirected_to_seller_registration"


@pytest.mark.asyncio
async def test_registration_type_unclear_clarification():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value=None)
    session = fake_session()
    session.workflow_state = {"awaiting_registration_type": True}

    result = await service._handle_registration_type_response(PHONE, "maybe later", session)

    assert result["status"] == "registration_type_clarification_sent"
    assert service.whatsapp_service.send_message.called


# ---------------------------------------------------------------------------
# 9. _detect_registration_intent phrase fast-path and fall-through
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_registration_intent_buyer_phrase():
    service = build_service()

    result = await service._detect_registration_intent("register as buyer")

    assert result == "buyer"


@pytest.mark.asyncio
async def test_detect_registration_intent_seller_phrase():
    service = build_service()

    result = await service._detect_registration_intent("create seller account")

    assert result == "seller"


@pytest.mark.asyncio
async def test_detect_registration_intent_none():
    service = build_service()

    result = await service._detect_registration_intent("hello")

    assert result is None


# ---------------------------------------------------------------------------
# 10. Intent-mismatch creation + response branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_intent_mismatch_seller_message():
    service = build_service()
    session = fake_session()

    result = await service._handle_intent_mismatch(
        PHONE, session, "seller", [fake_profile(role="buyer")]
    )

    assert result["status"] == "intent_mismatch_handled"
    assert result["target_role"] == "seller"
    assert result["existing_role"] == "buyer"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Seller profile" in sent


@pytest.mark.asyncio
async def test_handle_intent_mismatch_buyer_message():
    service = build_service()
    session = fake_session()

    result = await service._handle_intent_mismatch(
        PHONE, session, "buyer", [fake_profile(role="seller")]
    )

    assert result["target_role"] == "buyer"
    sent = service.whatsapp_service.send_message.call_args_list[0][0][1]
    assert "Buyer profile" in sent


@pytest.mark.asyncio
async def test_intent_mismatch_response_register():
    service = build_service()
    service._parse_profile_selection = AsyncMock(
        return_value={"action": "register_seller"}
    )
    service._redirect_to_seller_registration = AsyncMock(
        return_value={"status": "redirected_to_seller_registration"}
    )
    session = fake_session()
    session.workflow_state = {
        "target_role": "seller",
        "profile_selection_stage": "intent_mismatch",
        "profile_options": [{"number": 1, "action": "register_seller"}],
    }

    result = await service._handle_intent_mismatch_response(PHONE, "1", session)

    assert result["status"] == "redirected_to_seller_registration"
    assert "target_role" not in session.workflow_state


@pytest.mark.asyncio
async def test_intent_mismatch_response_exit():
    service = build_service()
    service._parse_profile_selection = AsyncMock(return_value={"action": "exit"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit_completed"})
    session = fake_session()
    session.workflow_state = {
        "target_role": "seller",
        "profile_selection_stage": "intent_mismatch",
        "profile_options": [{"number": 2, "action": "exit"}],
    }

    result = await service._handle_intent_mismatch_response(PHONE, "2", session)

    assert result["status"] == "exit_completed"


@pytest.mark.asyncio
async def test_intent_mismatch_response_invalid_retry():
    service = build_service()
    service._parse_profile_selection = AsyncMock(return_value=None)
    session = fake_session()
    session.workflow_state = {
        "target_role": "seller",
        "profile_selection_stage": "intent_mismatch",
        "profile_options": [{"number": 1, "action": "register_seller"}],
    }

    result = await service._handle_intent_mismatch_response(PHONE, "nope", session)

    assert result["status"] == "intent_mismatch_retry_sent"
    assert service.whatsapp_service.send_message.called


# ---------------------------------------------------------------------------
# 11. No-accounts response handlers register/exit/retry branches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buyer_no_accounts_register():
    service = build_service()
    service._parse_profile_selection = AsyncMock(
        return_value={"action": "register_buyer"}
    )
    service._redirect_to_buyer_registration = AsyncMock(
        return_value={"status": "redirected_to_buyer_registration"}
    )
    session = fake_session()
    session.workflow_state = {
        "profile_selection_stage": "buyer_intent_no_accounts",
        "profile_options": [{"number": 1, "action": "register_buyer"}],
    }

    result = await service._handle_buyer_no_accounts_response(PHONE, "1", session)

    assert result["status"] == "redirected_to_buyer_registration"
    assert "profile_selection_stage" not in session.workflow_state


@pytest.mark.asyncio
async def test_buyer_no_accounts_exit():
    service = build_service()
    service._parse_profile_selection = AsyncMock(return_value={"action": "exit"})
    service._handle_exit_action = AsyncMock(return_value={"status": "exit_completed"})
    session = fake_session()
    session.workflow_state = {
        "profile_selection_stage": "buyer_intent_no_accounts",
        "profile_options": [{"number": 2, "action": "exit"}],
    }

    result = await service._handle_buyer_no_accounts_response(PHONE, "2", session)

    assert result["status"] == "exit_completed"


@pytest.mark.asyncio
async def test_buyer_no_accounts_invalid_retry():
    service = build_service()
    service._parse_profile_selection = AsyncMock(return_value=None)
    session = fake_session()
    session.workflow_state = {
        "profile_selection_stage": "buyer_intent_no_accounts",
        "profile_options": [{"number": 1, "action": "register_buyer"}],
    }

    result = await service._handle_buyer_no_accounts_response(PHONE, "nope", session)

    assert result["status"] == "buyer_no_accounts_retry_sent"
    assert service.whatsapp_service.send_message.called


@pytest.mark.asyncio
async def test_seller_no_accounts_register():
    service = build_service()
    service._parse_profile_selection = AsyncMock(
        return_value={"action": "register_seller"}
    )
    service._redirect_to_seller_registration = AsyncMock(
        return_value={"status": "redirected_to_seller_registration"}
    )
    session = fake_session()
    session.workflow_state = {
        "profile_selection_stage": "seller_intent_no_accounts",
        "profile_options": [{"number": 1, "action": "register_seller"}],
    }

    result = await service._handle_seller_no_accounts_response(PHONE, "1", session)

    assert result["status"] == "redirected_to_seller_registration"


@pytest.mark.asyncio
async def test_seller_no_accounts_invalid_retry():
    service = build_service()
    service._parse_profile_selection = AsyncMock(return_value=None)
    session = fake_session()
    session.workflow_state = {
        "profile_selection_stage": "seller_intent_no_accounts",
        "profile_options": [{"number": 1, "action": "register_seller"}],
    }

    result = await service._handle_seller_no_accounts_response(PHONE, "nope", session)

    assert result["status"] == "seller_no_accounts_retry_sent"
    assert service.whatsapp_service.send_message.called


# ---------------------------------------------------------------------------
# 12. handle_profile_selection dict-message coercion + registration dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_profile_selection_dict_message_registration():
    service = build_service()
    service._detect_registration_intent = AsyncMock(return_value="buyer")
    service._redirect_to_buyer_registration = AsyncMock(
        return_value={"status": "redirected_to_buyer_registration"}
    )
    session = fake_session()

    result = await service.handle_profile_selection(
        PHONE,
        {"button_reply": {"title": "register as buyer"}},
        session,
        {"intent": "greeting", "confidence": 0},
    )

    assert result["status"] == "redirected_to_buyer_registration"
    service._redirect_to_buyer_registration.assert_awaited_once()
