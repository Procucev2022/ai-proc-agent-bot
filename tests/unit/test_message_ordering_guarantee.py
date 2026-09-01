"""
Unit tests verifying guaranteed sequential ordering of the Qua welcome greeting
and the Buy/Sell role selection message under various execution conditions
(synchronous, delayed/slow responses, retries, and concurrent requests).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.models import ConversationSession, WorkflowType
from app.services.profile_selection_service import ProfileSelectionService
from app.services.welcome_message_service import WelcomeMessageService


def make_session(phone: str = "919876543210") -> ConversationSession:
    """Create a minimal ConversationSession for testing."""
    session = ConversationSession(
        session_id=f"whatsapp_{phone}_20260826",
        external_user_id=phone,
        workflow_type=WorkflowType.authentication,
        workflow_state={},
    )
    return session


@pytest.fixture
def fake_redis():
    """In-memory mock for Redis operations with SET NX support."""
    store = {}
    locks = set()

    class FakeRedisService:
        async def get(self, key, as_json=False):
            return store.get(key)

        async def set(self, key, value, ex=None, nx=False):
            if nx:
                if key in store or key in locks:
                    return False
                store[key] = value
                locks.add(key)
                return True
            store[key] = value
            return True

        async def exists(self, key):
            return key in store

        async def delete(self, *keys):
            deleted = 0
            for k in keys:
                if k in store:
                    del store[k]
                    deleted += 1
                locks.discard(k)
            return deleted

        async def expireat(self, key, timestamp):
            return key in store

    return FakeRedisService()


@pytest.mark.asyncio
async def test_greeting_always_precedes_buy_sell_message(fake_redis):
    """
    Verify that when a user starts neutral greeting for the first time:
    1. Greeting ('Hello Namaste...') is sent first.
    2. Confirmation of greeting is verified.
    3. Buy/Sell message ('Reply with the number or word...') is sent second.
    """
    phone = "919876543210"
    session = make_session(phone)
    sent_messages = []

    mock_whatsapp = MagicMock()

    async def mock_send(recipient_id, message, **kwargs):
        sent_messages.append({
            "recipient": recipient_id,
            "message": message,
            "timestamp": time.time(),
            "kwargs": kwargs,
        })
        return SimpleNamespace(success=True, message_id="mid_123")

    mock_whatsapp.send_message = AsyncMock(side_effect=mock_send)

    welcome_service = WelcomeMessageService.__new__(WelcomeMessageService)
    welcome_service.redis_service = fake_redis
    welcome_service.welcome_flag_prefix = "welcome_msg"
    welcome_service.welcome_in_progress_prefix = "welcome_in_progress"

    profile_service = ProfileSelectionService.__new__(ProfileSelectionService)
    profile_service.whatsapp_service = mock_whatsapp
    profile_service.user_cache_service = AsyncMock()

    import app.services.welcome_message_service as welcome_mod
    orig_service = welcome_mod._welcome_service
    welcome_mod._welcome_service = welcome_service

    try:
        profiles = [
            {"email": "user@example.com", "role": "buyer", "name": "Buyer"},
            {"email": "user@example.com", "role": "seller", "name": "Seller"},
        ]
        result = await profile_service._handle_neutral_greeting(phone, profiles, session)

        assert result["status"] == "profile_selection_sent"
        assert len(sent_messages) == 2

        # 1. First message must be the Qua greeting
        assert "Hello Namaste" in sent_messages[0]["message"]
        assert sent_messages[0]["kwargs"].get("clear_pending_reply") is False

        # 2. Second message must be the Buy/Sell choice
        assert "1. Buy" in sent_messages[1]["message"]
        assert "2. Sell" in sent_messages[1]["message"]

        # 3. First message sent timestamp <= second message timestamp
        assert sent_messages[0]["timestamp"] <= sent_messages[1]["timestamp"]

        # 4. Welcome flag is now marked in Redis
        welcome_key = welcome_service._get_welcome_key(phone)
        assert await fake_redis.exists(welcome_key) is True

    finally:
        welcome_mod._welcome_service = orig_service


@pytest.mark.asyncio
async def test_slow_greeting_gateway_response_guarantees_order(fake_redis):
    """
    Verify that even when the WhatsApp API gateway has high latency or delay
    on sending the greeting, the Buy/Sell message is NEVER dispatched until
    the greeting response is received and confirmed.
    """
    phone = "919876543210"
    session = make_session(phone)
    call_log = []

    mock_whatsapp = MagicMock()

    async def mock_slow_send(recipient_id, message, **kwargs):
        label = "GREETING" if "Hello Namaste" in message else "BUY_SELL"
        call_log.append(f"{label}_START")
        if label == "GREETING":
            # Simulate 100ms gateway delay for greeting
            await asyncio.sleep(0.1)
        call_log.append(f"{label}_FINISH")
        return SimpleNamespace(success=True, message_id="mid_delayed")

    mock_whatsapp.send_message = AsyncMock(side_effect=mock_slow_send)

    welcome_service = WelcomeMessageService.__new__(WelcomeMessageService)
    welcome_service.redis_service = fake_redis
    welcome_service.welcome_flag_prefix = "welcome_msg"
    welcome_service.welcome_in_progress_prefix = "welcome_in_progress"

    profile_service = ProfileSelectionService.__new__(ProfileSelectionService)
    profile_service.whatsapp_service = mock_whatsapp
    profile_service.user_cache_service = AsyncMock()

    import app.services.welcome_message_service as welcome_mod
    orig_service = welcome_mod._welcome_service
    welcome_mod._welcome_service = welcome_service

    try:
        await profile_service._handle_neutral_greeting(phone, [], session)

        # Exact sequence of execution
        assert call_log == [
            "GREETING_START",
            "GREETING_FINISH",
            "BUY_SELL_START",
            "BUY_SELL_FINISH",
        ]
    finally:
        welcome_mod._welcome_service = orig_service


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_duplicate_greeting_or_reorder(fake_redis):
    """
    Verify that when two concurrent requests arrive for the same user:
    - In-progress locking prevents duplicate greeting sends.
    - Greeting is sent exactly once.
    - Both requests guarantee Buy/Sell message comes after the greeting.
    """
    phone = "919876543210"
    session1 = make_session(phone)
    session2 = make_session(phone)
    sent_messages = []

    mock_whatsapp = MagicMock()

    async def mock_send(recipient_id, message, **kwargs):
        sent_messages.append({"msg": message[:20], "time": time.time()})
        await asyncio.sleep(0.05)
        return SimpleNamespace(success=True, message_id="mid_concurrent")

    mock_whatsapp.send_message = AsyncMock(side_effect=mock_send)

    welcome_service = WelcomeMessageService.__new__(WelcomeMessageService)
    welcome_service.redis_service = fake_redis
    welcome_service.welcome_flag_prefix = "welcome_msg"
    welcome_service.welcome_in_progress_prefix = "welcome_in_progress"

    profile_service1 = ProfileSelectionService.__new__(ProfileSelectionService)
    profile_service1.whatsapp_service = mock_whatsapp
    profile_service1.user_cache_service = AsyncMock()

    profile_service2 = ProfileSelectionService.__new__(ProfileSelectionService)
    profile_service2.whatsapp_service = mock_whatsapp
    profile_service2.user_cache_service = AsyncMock()

    import app.services.welcome_message_service as welcome_mod
    orig_service = welcome_mod._welcome_service
    welcome_mod._welcome_service = welcome_service

    try:
        # Run both requests concurrently
        results = await asyncio.gather(
            profile_service1._handle_neutral_greeting(phone, [], session1),
            profile_service2._handle_neutral_greeting(phone, [], session2),
        )

        assert all(r["status"] == "profile_selection_sent" for r in results)

        # Greeting must have been sent exactly once
        greetings = [m for m in sent_messages if "Hello Namaste" in m["msg"]]
        assert len(greetings) == 1

        # The very first message sent overall must be the greeting
        assert "Hello Namaste" in sent_messages[0]["msg"]
    finally:
        welcome_mod._welcome_service = orig_service


@pytest.mark.asyncio
async def test_subsequent_greeting_turn_skips_welcome_and_sends_buy_sell_only(fake_redis):
    """
    Verify that if a user has already received the welcome greeting today,
    subsequent greeting turns send only the Buy/Sell message without repeating the greeting.
    """
    phone = "919876543210"
    session = make_session(phone)
    sent_messages = []

    mock_whatsapp = MagicMock()

    async def mock_send(recipient_id, message, **kwargs):
        sent_messages.append(message)
        return SimpleNamespace(success=True, message_id="mid_repeat")

    mock_whatsapp.send_message = AsyncMock(side_effect=mock_send)

    welcome_service = WelcomeMessageService.__new__(WelcomeMessageService)
    welcome_service.redis_service = fake_redis
    welcome_service.welcome_flag_prefix = "welcome_msg"
    welcome_service.welcome_in_progress_prefix = "welcome_in_progress"

    # Pre-populate welcome flag in Redis
    await welcome_service.mark_welcome_sent(phone)

    profile_service = ProfileSelectionService.__new__(ProfileSelectionService)
    profile_service.whatsapp_service = mock_whatsapp
    profile_service.user_cache_service = AsyncMock()

    import app.services.welcome_message_service as welcome_mod
    orig_service = welcome_mod._welcome_service
    welcome_mod._welcome_service = welcome_service

    try:
        result = await profile_service._handle_neutral_greeting(phone, [], session)

        assert result["status"] == "profile_selection_sent"
        assert len(sent_messages) == 1
        assert "Reply with the number or word" in sent_messages[0]
        assert "Hello Namaste" not in sent_messages[0]
    finally:
        welcome_mod._welcome_service = orig_service


@pytest.mark.asyncio
async def test_message_queue_preserves_batch_session_on_intermediate_sends():
    """
    Verify that MessageQueueService.wrapped_send does not trigger cleanup
    or clear processing locks when sending an intermediate message (clear_pending_reply=False).
    """
    from app.services.message_queue_service import MessageQueueService, ProcessingSession

    mq = MessageQueueService.__new__(MessageQueueService)
    mq.redis = AsyncMock()
    mq.whatsapp_service = MagicMock()
    mq.whatsapp_service.send_message = AsyncMock(return_value=SimpleNamespace(success=True, message_id="mid_mq"))
    mq._should_suppress_response = AsyncMock(return_value=False)
    mq._cleanup_and_next = AsyncMock()
    mq.response_ready_ttl = 60

    session_obj = ProcessingSession(batch_id="batch_123", started_at=time.time())
    mq.redis.get = AsyncMock(return_value=session_obj.to_json())
    mq.redis.setex = AsyncMock()

    # 1. Intermediate send (e.g. welcome message)
    send_fn = mq.__getattr__("send_message")
    res1 = await send_fn("919876543210", "Hello Namaste", clear_pending_reply=False)
    assert res1.success is True
    # Cleanup must NOT have been called
    mq._cleanup_and_next.assert_not_awaited()

    # 2. Final response send (e.g. Buy/Sell message)
    res2 = await send_fn("919876543210", "Reply with 1 or 2", clear_pending_reply=True)
    assert res2.success is True
    # Cleanup MUST be called on final response
    mq._cleanup_and_next.assert_awaited_once_with("batch_123", "919876543210", True)


@pytest.mark.asyncio
async def test_profile_selection_branches_and_error_handling(fake_redis):
    """
    Test edge branches in ProfileSelectionService including registration intents
    and error handling during neutral greeting.
    """
    phone = "919876543210"
    session = make_session(phone)

    mock_whatsapp = MagicMock()
    mock_whatsapp.send_message = AsyncMock(side_effect=RuntimeError("gateway error"))

    welcome_service = WelcomeMessageService.__new__(WelcomeMessageService)
    welcome_service.redis_service = fake_redis
    welcome_service.welcome_flag_prefix = "welcome_msg"
    welcome_service.welcome_in_progress_prefix = "welcome_in_progress"

    profile_service = ProfileSelectionService.__new__(ProfileSelectionService)
    profile_service.whatsapp_service = mock_whatsapp
    profile_service.user_cache_service = AsyncMock()

    import app.services.welcome_message_service as welcome_mod
    orig_service = welcome_mod._welcome_service
    welcome_mod._welcome_service = welcome_service

    try:
        # Error handling branch
        res = await profile_service._handle_neutral_greeting(phone, [], session)
        assert res["status"] == "error"

        # Registration intent branches in handle_profile_selection
        profile_service._detect_registration_intent = AsyncMock(return_value="buyer")
        profile_service._redirect_to_buyer_registration = AsyncMock(return_value={"status": "buyer_reg"})
        profile_service._redirect_to_seller_registration = AsyncMock(return_value={"status": "seller_reg"})

        r1 = await profile_service.handle_profile_selection(
            phone, {"button_reply": {"title": "Register as Buyer"}}, session, {"intent": "general"}
        )
        assert r1["status"] == "buyer_reg"

        profile_service._detect_registration_intent.return_value = "seller"
        r2 = await profile_service.handle_profile_selection(
            phone, {"other": "dict"}, session, {"intent": "general"}
        )
        assert r2["status"] == "seller_reg"

        profile_service._detect_registration_intent.side_effect = [None, "buyer"]
        r3 = await profile_service.handle_profile_selection(
            phone, "reg", session, {"intent": "register_account"}
        )
        assert r3["status"] == "buyer_reg"

        profile_service._detect_registration_intent.side_effect = [None, "seller"]
        r4 = await profile_service.handle_profile_selection(
            phone, "reg", session, {"intent": "register_account"}
        )
        assert r4["status"] == "seller_reg"
    finally:
        welcome_mod._welcome_service = orig_service


@pytest.mark.asyncio
async def test_enhanced_whatsapp_service_forwards_kwargs():
    """Verify EnhancedWhatsAppService forwards all kwargs (like clear_pending_reply) cleanly."""
    from app.services.enhanced_whatsapp_service import EnhancedWhatsAppService
    from unittest.mock import patch, AsyncMock, MagicMock

    service = EnhancedWhatsAppService.__new__(EnhancedWhatsAppService)
    service.service_monitor = MagicMock()
    service.service_monitor.monitor_whatsapp_operation = MagicMock()

    class MockContext:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    service.service_monitor.monitor_whatsapp_operation.return_value = MockContext()

    with patch("app.services.whatsapp_service.WhatsAppService.send_message", new_callable=AsyncMock) as mock_super_send:
        mock_super_send.return_value = SimpleNamespace(success=True)
        res = await service.send_message("919876543210", "hello", clear_pending_reply=False)
        assert res.success is True
        mock_super_send.assert_awaited_once_with("919876543210", "hello", clear_pending_reply=False)


@pytest.mark.asyncio
async def test_api_chat_endpoint_captures_both_greeting_and_buysell_in_order(monkeypatch, fake_redis):
    """
    Verify that calling /api/chat with 'Hi' captures both the greeting message
    and the Buy/Sell role selection message in strict sequential order.
    """
    from app.main import process_chat_message, ChatMessage
    from starlette.requests import Request
    from unittest.mock import AsyncMock, MagicMock

    phone = "919876543210"
    chat_message = ChatMessage(phone=phone, message="Hi")
    mock_request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/chat",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })

    from app.main import limiter
    monkeypatch.setattr(limiter, "enabled", False)

    from contextlib import contextmanager
    @contextmanager
    def mock_db_ctx():
        yield MagicMock()
    monkeypatch.setattr("app.main.get_db_session_context", mock_db_ctx)

    # Set up clean welcome service
    welcome_service = WelcomeMessageService.__new__(WelcomeMessageService)
    welcome_service.redis_service = fake_redis
    welcome_service.welcome_flag_prefix = "welcome_msg"
    welcome_service.welcome_in_progress_prefix = "welcome_in_progress"

    import app.services.welcome_message_service as welcome_mod
    orig_service = welcome_mod._welcome_service
    welcome_mod._welcome_service = welcome_service

    # Mock ChatService dependencies so process_message goes through neutral greeting
    async def mock_process_message(self, user_phone, content, message_type):
        # Trigger welcome send via self.whatsapp_service
        await welcome_service.check_and_send_welcome(user_phone, self.whatsapp_service)
        # Then trigger Buy/Sell message
        await self.whatsapp_service.send_message(
            user_phone,
            "Reply with the number or word:\n1. Buy — Create or check my RFQs\n2. Sell — View or respond to RFQs\n\n(Just type 1 or 2, or type 'Buy' or 'Sell' to continue.)"
        )
        return {"status": "processed"}

    monkeypatch.setattr("app.services.chat_service.ChatService.process_message", mock_process_message)
    monkeypatch.setattr("app.services.chat_service.ChatService.cleanup", AsyncMock())

    try:
        response = await process_chat_message(mock_request, chat_message)
        assert response["success"] is True
        responses = response["responses"]
        assert len(responses) == 2
        # Verify strict order
        assert "Hello Namaste" in responses[0]
        assert "Reply with the number or word" in responses[1]
    finally:
        welcome_mod._welcome_service = orig_service

