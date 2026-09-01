from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.services.handlers import attachment_decision_handler as attachment_mod
from app.services.handlers import format_modification_handler as format_mod
from app.services.processors import image_message_processor as image_mod
from app.services import message_queue_service as queue_mod
from app.tasks import vector_store_sync_task as vector_mod


class AsyncLock:
    def __init__(self, acquired: bool = True, release_error: Exception | None = None):
        self.acquired = acquired
        self.release_error = release_error
        self.released = False

    async def acquire(self):
        return self.acquired

    async def release(self):
        self.released = True
        if self.release_error:
            raise self.release_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def make_user(**kwargs):
    values = {"phone_number": "919999999999", "is_registered": True}
    values.update(kwargs)
    return SimpleNamespace(**values)


def make_session(**state):
    return SimpleNamespace(
        session_id="round5-session",
        workflow_state=dict(state),
        workflow_type=None,
        conversation_history={"messages": []},
    )


@pytest.mark.asyncio
async def test_attachment_decision_handler_direct_approval_rejection_and_error(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock())
    responses = SimpleNamespace(generate_contextual_response=AsyncMock(return_value="context"))
    purchase = SimpleNamespace(handle_purchase_intent=AsyncMock(return_value={"status": "continued"}))
    manager = SimpleNamespace(save_session=AsyncMock())
    handler = attachment_mod.AttachmentDecisionHandler(whatsapp, responses, purchase, manager)
    user = make_user()
    session = make_session(awaiting_attachment_decision=True)
    callback = AsyncMock()

    monkeypatch.setattr(attachment_mod.AttachmentHelpers, "approve_pending_attachment", Mock(return_value=True))
    result = await handler._handle_attachment_approval(
        user, session, "yes", {"approved_count": 2}, callback
    )
    assert result["status"] == "continued"
    assert manager.save_session.await_count == 1
    assert responses.generate_contextual_response.await_args.args[2] == "attachment_approved"
    assert purchase.handle_purchase_intent.await_args.args[-1] is callback

    monkeypatch.setattr(attachment_mod.AttachmentHelpers, "approve_pending_attachment", Mock(return_value=False))
    await handler._handle_attachment_approval(user, session, "attach", {"approved_count": 0})
    assert responses.generate_contextual_response.await_args.args[2] == "attachment_approval_failed"

    monkeypatch.setattr(attachment_mod.AttachmentHelpers, "reject_pending_attachments", Mock())
    result = await handler._handle_attachment_rejection(user, session, "no", callback)
    assert result["status"] == "continued"
    attachment_mod.AttachmentHelpers.reject_pending_attachments.assert_called_once_with(session)

    session.workflow_state["awaiting_attachment_decision"] = True
    result = await handler._handle_attachment_error(user, session, "maybe", RuntimeError("broken"), callback)
    assert result["status"] == "continued"
    assert session.workflow_state["awaiting_attachment_decision"] is False
    assert responses.generate_contextual_response.await_args.args[2] == "attachment_decision_error"

    monkeypatch.setattr(
        attachment_mod.AttachmentHelpers,
        "get_attachment_summary",
        Mock(side_effect=RuntimeError("summary")),
    )
    result = await handler.handle_attachment_decision(user, session, "unknown")
    assert result["status"] == "continued"


@pytest.mark.asyncio
async def test_format_modification_parser_seams_and_success_paths(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    handler = format_mod.FormatModificationHandler(whatsapp)
    handler.session_manager = SimpleNamespace(save_session=AsyncMock())
    user = make_user()
    session = make_session(extracted_entities=[], incomplete_products=[{"x": 1}], complete_products=[{"x": 2}])

    assert handler._parse_delivery_format("text", 0)["success"] is False
    assert handler._parse_items_format("text", 0)["success"] is False

    workflow = format_mod.WorkflowManager
    monkeypatch.setattr(workflow, "get_retry_count", lambda _session: 1)
    monkeypatch.setattr(workflow, "set_delivery_details", Mock())
    monkeypatch.setattr(workflow, "confirm_delivery", Mock())
    monkeypatch.setattr(workflow, "clear_awaiting_modification", Mock())
    monkeypatch.setattr(workflow, "reset_retry_count", Mock())
    monkeypatch.setattr(workflow, "get_delivery_details", lambda _session: {
        "delivery_date": "2026-01-10", "pincode": "411005", "city": "Pune", "state": "MH"
    })
    handler._parse_delivery_format = Mock(return_value={
        "success": True,
        "data": {"delivery_date": "2026-01-10", "pincode": "411005", "city": "Pune", "state": "MH"},
    })
    result = await handler.handle_format_modification("delivery", make_session(), user)
    assert result["status"] == "error"  # the router is only active when its state flag is set

    awaiting_delivery = make_session(awaiting_format_modification=True)
    monkeypatch.setattr(workflow, "is_awaiting_modification", lambda _session: (True, "delivery"))
    result = await handler.handle_format_modification("delivery", awaiting_delivery, user)
    assert result["status"] == "delivery_updated"
    assert "Delivery Date: 2026-01-10" in whatsapp.send_message.await_args.args[1]

    awaiting_items = make_session(
        awaiting_format_modification=True,
        incomplete_products=[{"x": 1}],
        complete_products=[{"x": 2}],
    )
    handler._parse_items_format = Mock(return_value={
        "success": True,
        "data": [{"description": "Pump", "quantity": 2}, {"description": "Bolt"}],
    })
    monkeypatch.setattr(workflow, "get_delivery_details", lambda _session: {
        "delivery_date": "2026-01-10", "pincode": "411005", "city": "Pune", "state": "MH"
    })
    monkeypatch.setattr(workflow, "is_awaiting_modification", lambda _session: (True, "items"))
    result = await handler.handle_format_modification("items", awaiting_items, user)
    assert result == {"status": "items_updated", "message": "Items updated", "total_items": 2}
    assert awaiting_items.workflow_state["extracted_entities"][0]["deliveryDate"] == "2026-01-10"
    assert "incomplete_products" not in awaiting_items.workflow_state
    assert "complete_products" not in awaiting_items.workflow_state
    assert whatsapp.send_configurable_buttons.await_count == 1


class PollerRedis:
    def __init__(self, lock: AsyncLock, keys=None, timer_exists=False, message_count=1):
        self.lock_value = lock
        self.keys = keys or []
        self.timer_exists = timer_exists
        self.message_count = message_count
        self.scan_calls = 0
        self.lock_calls = 0

    def lock(self, *_args, **_kwargs):
        self.lock_calls += 1
        return self.lock_value

    async def scan(self, **_kwargs):
        self.scan_calls += 1
        return 0, self.keys

    async def exists(self, _key):
        return self.timer_exists

    async def zcard(self, _key):
        return self.message_count


@pytest.mark.asyncio
async def test_message_queue_poller_lifecycle_and_cancellation(monkeypatch):
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service._running = True
    lock = AsyncLock()
    service.redis = PollerRedis(lock, ["1:incoming"], timer_exists=False, message_count=2)
    service._create_batch = AsyncMock(side_effect=lambda _phone: setattr(service, "_running", False))

    async def one_cycle_sleep(_seconds):
        return None

    monkeypatch.setattr(queue_mod.asyncio, "sleep", one_cycle_sleep)
    await service.run_batch_poller()
    service._create_batch.assert_awaited_once_with("1")
    assert lock.released

    # A competing worker is skipped, then the next sleep ends the bounded loop.
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service._running = True
    competing = AsyncLock(acquired=False)
    service.redis = PollerRedis(competing, ["1:incoming"])
    sleeps = 0

    async def stop_after_skip(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            service._running = False

    monkeypatch.setattr(queue_mod.asyncio, "sleep", stop_after_skip)
    await service.run_batch_poller()
    assert service.redis.lock_calls == 1

    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service._running = True
    service.redis = PollerRedis(AsyncLock(), ["1:incoming"])

    async def cancelled_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(queue_mod.asyncio, "sleep", cancelled_sleep)
    with pytest.raises(asyncio.CancelledError):
        await service.run_batch_poller()


class MonitoringRedis:
    def __init__(self, keys):
        self.keys = keys
        self.get_counts = {}
        self.sent_intervals = []
        self.deleted = []
        self.setex_calls = []
        self.session_data = {
            key: queue_mod.ProcessingSession(key.split(":")[0], 0, please_wait_sent_count=count).to_json()
            for key, count in (
                ("ready:session", 0), ("busy:session", 0), ("recheck:session", 0),
                ("gone:session", 0), ("max:session", 3), ("already:session", 2),
                ("claimed:session", 0), ("senderr:session", 0), ("absent:session", 0),
                ("slow:session", 3), ("logerror:session", 3), ("empty:session", 0),
                ("bad:session", 0),
            )
        }

    async def scan(self, **_kwargs):
        if _kwargs.get("match") == "*:session" and self.keys is not None:
            keys, self.keys = self.keys, None
            return 0, keys
        return 0, []

    async def get(self, key, *_args, **_kwargs):
        if key.endswith(":session"):
            self.get_counts[key] = self.get_counts.get(key, 0) + 1
            if key == "empty:session":
                return None
            if key == "gone:session" and self.get_counts[key] > 1:
                return None
            if key == "bad:session":
                return "not-json"
            return self.session_data[key]
        if key == "ready:response_ready":
            return "ready"
        if key == "recheck:response_ready":
            return "ready" if self.get_counts.get(key, 0) else None
        self.get_counts[key] = self.get_counts.get(key, 0) + 1
        return None

    async def set(self, key, *_args, **kwargs):
        if ":lock:monitor_log" in key and key.startswith("logerror"):
            raise RuntimeError("log coordination")
        if ":lock:monitor" in key:
            return not key.startswith("busy")
        if ":please_wait:interval:" in key:
            self.sent_intervals.append(key)
            return not key.startswith("claimed")
        return True

    async def exists(self, key):
        return not key.startswith("absent")

    async def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        return True

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


@pytest.mark.asyncio
async def test_message_queue_monitoring_branches_are_bounded(monkeypatch):
    keys = [
        "empty:session", "ready:session", "busy:session", "recheck:session",
        "gone:session", "max:session", "already:session", "claimed:session",
        "senderr:session", "absent:session", "slow:session", "logerror:session", "bad:session",
    ]
    redis = MonitoringRedis(keys)
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = redis
    service._running = True
    service.please_wait_threshold = 15
    service.max_please_wait_count = 3
    service.monitoring_poll_interval = 1
    service.monitor_lock_ttl = 30
    service.please_wait_interval_ttl = 60
    service._send_please_wait = AsyncMock()

    async def send_please_wait(phone):
        if phone == "senderr":
            raise RuntimeError("send failed")
        if phone == "absent":
            service._running = False

    service._send_please_wait.side_effect = send_please_wait
    monkeypatch.setattr(queue_mod.time, "time", lambda: 60.0)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(queue_mod.asyncio, "sleep", no_sleep)
    await service.run_monitoring_loop()
    assert any(key.startswith("absent:please_wait:interval") for key in redis.sent_intervals)
    assert any(key.startswith("senderr:please_wait:interval") for key in redis.deleted)
    assert redis.setex_calls

    # An outer cycle failure while shutting down exits rather than spinning.
    class ErrorRedis:
        async def scan(self, **_kwargs):
            service._running = False
            raise RuntimeError("scan failed")

    service.redis = ErrorRedis()
    service._running = True
    await service.run_monitoring_loop()

    async def cancelled(_seconds):
        raise asyncio.CancelledError

    service._running = True
    monkeypatch.setattr(queue_mod.asyncio, "sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.run_monitoring_loop()


@pytest.mark.asyncio
async def test_message_queue_cleanup_health_attribute_and_shutdown_edges(monkeypatch):
    service = queue_mod.MessageQueueService.__new__(queue_mod.MessageQueueService)
    service.redis = SimpleNamespace()
    service.redis.delete = AsyncMock()
    service.redis.scan = AsyncMock(side_effect=[(1, ["1:please_wait:interval:1"]), (0, [])])
    service.redis.zcard = AsyncMock(return_value=1)
    service.redis.llen = AsyncMock(return_value=0)
    service._create_batch = AsyncMock()
    service._try_start_processing = AsyncMock()
    await service._cleanup_and_next("batch", "1", True)
    service._create_batch.assert_awaited_once_with("1")

    service.redis.scan = AsyncMock(return_value=(0, []))
    service.redis.zcard = AsyncMock(return_value=0)
    service.redis.llen = AsyncMock(return_value=0)
    service._create_batch.reset_mock()
    await service._cleanup_and_next("batch", "1", False)
    service._try_start_processing.assert_awaited()

    service.whatsapp_service = SimpleNamespace(value="plain")
    assert service.value == "plain"
    with pytest.raises(AttributeError):
        _ = service.no_such_attribute

    class HealthRedis:
        async def scan(self, **kwargs):
            match = kwargs["match"]
            if match == "*:processing":
                return 0, ["1:processing"]
            if match == "*:incoming":
                return 0, ["1:incoming", "2:incoming"]
            if match == "*:outgoing":
                return 0, ["2:outgoing"]
            if match == "*:session":
                return 0, ["1:session", "bad:session"]
            return 0, []

        async def zcard(self, key):
            return 2 if key == "1:incoming" else 0

        async def llen(self, key):
            return 3 if key == "2:outgoing" else 0

        async def get(self, key):
            if key == "1:session":
                return queue_mod.ProcessingSession("b", 0).to_json()
            return "bad-json"

    service.redis = HealthRedis()
    monkeypatch.setattr(queue_mod.time, "time", lambda: 100.0)
    metrics = await service.get_health_metrics()
    assert metrics["processing_count"] == 1
    assert metrics["total_incoming"] == 2 and metrics["total_outgoing"] == 3
    assert metrics["slow_batches"] == 1 and metrics["active_users"] == 2

    class DoneTask:
        def __init__(self, done):
            self._done = done
            self.cancelled = False
        def done(self):
            return self._done
        def cancel(self):
            self.cancelled = True
        def __await__(self):
            async def wait():
                return None
            return wait().__await__()

    service._background_tasks = [DoneTask(True), DoneTask(False)]
    service.redis.close = AsyncMock()
    service._running = True
    await service.shutdown()
    assert service._background_tasks == []
    assert service.redis.close.await_count == 1


@pytest.mark.asyncio
async def test_image_processor_payload_replay_and_confirmation_paths(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    responses = SimpleNamespace(
        generate_contextual_response=AsyncMock(return_value="context"),
        generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"),
    )
    processor = image_mod.ImageMessageProcessor(whatsapp, responses, session_manager=SimpleNamespace())
    user = make_user()

    replay = make_session()
    replay.conversation_history = {"messages": [{
        "role": "assistant",
        "content": {"body": {"text": "Choose"}, "header": {"text": "Header"},
                    "footer": {"text": "Footer"}, "action": {"buttons": [
                        {"reply": {"id": "yes", "title": "Yes"}}, {"title": "No"}, "bad"
                    ]}},
    }]}
    result = await processor._handle_irrelevant_attachment(user, replay)
    assert result["response"] == "attachment_ignored"
    assert whatsapp.send_configurable_buttons.await_args.kwargs["header"] == "Header"

    empty_replay = make_session()
    empty_replay.conversation_history = {"messages": [{"role": "assistant", "content": {}}]}
    await processor._handle_irrelevant_attachment(user, empty_replay)
    assert whatsapp.send_message.await_args.args[1] == "How can I help you?"

    non_dict = make_session(pending_rfq={"entities": {}})
    monkeypatch.setattr(image_mod.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": True}))
    monkeypatch.setattr(image_mod.AttachmentHelpers, "approve_pending_attachment", Mock())
    monkeypatch.setattr(processor, "_determine_next_step", AsyncMock(return_value={"status": "handled", "response": "added"}))
    result = await processor.process_image_message(user, non_dict, "raw")
    assert result["response"] == "no_file_data"

    download = AsyncMock(return_value={
        "success": True,
        "attachment": {"file_name": "media.jpg", "file_type": "image/jpeg", "file_content": "data"},
    })
    monkeypatch.setattr(image_mod.AttachmentHelpers, "download_and_encode_attachment", download)
    result = await processor.process_image_message(
        user, non_dict, {"id": "media-1", "mime_type": "image/jpeg", "caption": "caption"}
    )
    assert result["response"] == "added"
    assert download.await_args.args[0].endswith("media-1")

    ordinary_failure = make_session(pending_rfq={"entities": {}})
    monkeypatch.setattr(image_mod.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": False, "error": "cannot save"}))
    result = await processor.process_image_message(user, ordinary_failure, {"data": "x"})
    assert result == {"status": "error", "response": "attachment_add_failed"}

    max_failure = make_session(pending_rfq={"entities": {}})
    monkeypatch.setattr(image_mod.AttachmentHelpers, "add_attachment_to_session", Mock(return_value={"success": False, "error": "Maximum attachments reached"}))
    processor._regenerate_confirmation_with_error = AsyncMock(return_value={"status": "handled", "response": "confirmation_with_error"})
    result = await processor.process_image_message(user, max_failure, {"data": "x"})
    assert result["response"] == "confirmation_with_error"

    assert (await processor._handle_save_error(user))["response"] == "failed_to_save_attachment"


@pytest.mark.asyncio
async def test_image_processor_regenerates_single_combined_and_error_confirmations(monkeypatch):
    whatsapp = SimpleNamespace(send_message=AsyncMock(), send_configurable_buttons=AsyncMock())
    responses = SimpleNamespace(generate_rfq_summary_and_confirmation=AsyncMock(return_value="summary"))
    processor = image_mod.ImageMessageProcessor(whatsapp, responses)
    user = make_user()
    schema_factory = Mock(return_value=SimpleNamespace())
    monkeypatch.setattr(
        "app.services.helpers.chat_service_helpers.ChatServiceHelpers.create_rfq_schema_from_entities",
        schema_factory,
    )
    single = make_session(
        pending_rfq={"entities": {"description": "Pump"}},
        extracted_entities=[{"attachments": [{"file_name": "a.pdf"}]}],
        attachment_caption="caption",
    )
    result = await processor._regenerate_existing_confirmation(user, single, "new.pdf")
    assert result["response"] == "confirmation_regenerated"
    assert single.workflow_state["pending_rfq"]["entities"]["remarks"] == "caption"

    monkeypatch.setattr(image_mod, "RFQValidationSchema", lambda **kwargs: SimpleNamespace(**kwargs))
    combined = make_session(
        pending_combined_rfq={
            "combined_schema": {"remarks": ""},
            "products": [{"entities": {"description": "Pump"}}, {"entities": {"remarks": "existing"}}],
        },
        extracted_entities=[{"attachments": [{}, {}, {}]}],
        attachment_caption="combined caption",
    )
    result = await processor._regenerate_existing_confirmation(user, combined, "combined.pdf")
    assert result["response"] == "confirmation_regenerated"
    assert combined.workflow_state["pending_combined_rfq"]["combined_schema"]["remarks"] == "combined caption"

    none = make_session()
    assert (await processor._regenerate_existing_confirmation(user, none, "x"))["response"] == "no_pending_confirmation_found"

    processor.response_helpers.generate_rfq_summary_and_confirmation.return_value = "summary"
    single_error = make_session(pending_rfq={"entities": {"description": "Pump"}})
    assert (await processor._regenerate_confirmation_with_error(user, single_error, "limit"))["response"] == "confirmation_with_error"
    combined_error = make_session(pending_combined_rfq={"combined_schema": {}, "products": [{"entities": {}}]})
    assert (await processor._regenerate_confirmation_with_error(user, combined_error, "limit"))["response"] == "confirmation_with_error"
    fallback = make_session()
    assert (await processor._regenerate_confirmation_with_error(user, fallback, "limit"))["response"] == "no_pending_confirmation_found"


class Field:
    def __eq__(self, _value):
        return True


class MappingModel:
    seller_id = Field()
    original_category = Field()

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class LearningModel:
    id = Field()


class Query:
    def __init__(self, values=(), first_value=None):
        self.values = list(values)
        self.first_value = first_value

    def all(self):
        return self.values

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.first_value


class MappingDB:
    def __init__(self, categories, existing_categories, selected_values):
        self.categories = categories
        self.existing_categories = set(existing_categories)
        self.selected_values = list(selected_values)
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._category_loaded = False

    def query(self, model):
        if model is LearningModel and not self._category_loaded:
            self._category_loaded = True
            return Query(self.categories)
        if model is MappingModel:
            # The caller supplies the category through a boolean expression in
            # this test double, so consume the known existing values in order.
            value = getattr(self, "_next_existing", False)
            self._next_existing = False
            return Query(first_value=value)
        value = self.selected_values.pop(0) if self.selected_values else None
        return Query(first_value=value)

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_vector_async_mapping_failure_cache_existing_missing_and_seller_exception(monkeypatch):
    category = SimpleNamespace(id="c1", level_1_category="L1", level_2_category="L2", level_3_category="L3")

    class PhaseBadSeller:
        seller_id = "phase-bad"
        _reads = 0
        @property
        def categories(self):
            self._reads += 1
            if self._reads > 2:
                raise RuntimeError("seller categories")
            return ["bad"]

    sellers = [
        SimpleNamespace(seller_id="existing", categories=["existing"]),
        SimpleNamespace(seller_id="new", categories=["new", "missing", "notfound"]),
        SimpleNamespace(seller_id="empty", categories=[]),
        PhaseBadSeller(),
    ]
    db = MappingDB([category], [], [category, None, category])
    # Mark only the first seller/category pair as already mapped.
    original_query = db.query
    mapping_calls = 0

    def query(model):
        nonlocal mapping_calls
        if model is MappingModel:
            mapping_calls += 1
            return Query(first_value=(mapping_calls == 1))
        return original_query(model)

    db.query = query
    import app.database as database
    import app.models as models
    import app.services.openai_service as openai_module
    import app.services.seller_data_adapter as adapter_module

    monkeypatch.setattr(database, "get_db_session", lambda: db)
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: SimpleNamespace(
        map_seller_categories_batch=AsyncMock(return_value={
            "new": {"success": True, "selected_category": {"id": "c1"}, "similarity_score": .9,
                    "reasoning": "mapped", "token_usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}},
            "missing": {"success": False, "error": "AI rejected"},
            "notfound": {"success": True, "selected_category": {"id": "missing"}},
            "bad": {"success": True, "selected_category": {"id": "c1"}},
        })
    ))
    monkeypatch.setattr(adapter_module, "SellerDataAdapter", lambda: SimpleNamespace(
        test_connection=lambda: True, get_sellers_from_remote=lambda: sellers
    ))
    monkeypatch.setattr(models, "LearningCategory", LearningModel)
    monkeypatch.setattr(models, "SellerLearningMapping", MappingModel)
    result = await vector_mod._async_map_sellers_to_categories(batch_size=2)
    assert result["success"] is True
    assert result["existing_mappings"] == 1
    assert result["created_mappings"] == 1
    assert result["cached_mappings"] == 1
    assert any("AI rejected" in error for error in result["errors"])
    assert any("not found" in error for error in result["errors"])
    assert any("seller categories" in error for error in result["errors"])
    assert db.rollbacks == 1 and db.commits >= 2 and db.closed


@pytest.mark.asyncio
async def test_vector_async_mapping_outer_database_failure_and_cli_guard(monkeypatch):
    class BrokenDB:
        def query(self, *_args):
            raise RuntimeError("outer db")
        def rollback(self):
            self.rolled_back = True
        def close(self):
            self.closed = True

    import app.config as config
    import app.database as database
    import app.models as models
    import app.services.openai_service as openai_module
    import app.services.seller_data_adapter as adapter_module

    broken = BrokenDB()
    monkeypatch.setattr(database, "get_db_session", lambda: broken)
    monkeypatch.setattr(openai_module, "OpenAIService", lambda: object())
    monkeypatch.setattr(adapter_module, "SellerDataAdapter", lambda: SimpleNamespace(
        test_connection=lambda: True, get_sellers_from_remote=lambda: [SimpleNamespace(seller_id="s", categories=["x"])]
    ))
    monkeypatch.setattr(models, "LearningCategory", LearningModel)
    monkeypatch.setattr(models, "SellerLearningMapping", MappingModel)
    result = await vector_mod._async_map_sellers_to_categories()
    assert result["success"] is False and result["error"] == "outer db"
    assert broken.rolled_back and broken.closed

    import celery
    import app.config as config

    def fake_shared_task(*_args, **_kwargs):
        def decorator(function):
            def task_call(*args, **kwargs):
                return function(None, *args, **kwargs)
            task_call.apply_async = Mock(return_value=SimpleNamespace(id="cli"))
            return task_call
        return decorator

    monkeypatch.setattr(celery, "shared_task", fake_shared_task)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(enable_vector_store_sync=False))
    monkeypatch.setattr("sys.argv", ["vector_store_sync_task.py"])
    runpy.run_path(str(Path(vector_mod.__file__)), run_name="__main__")


# Function-only singleton and initialization coverage ----------------------


def test_configuration_database_and_singleton_initializers(monkeypatch):
    import app.config as config
    import app.database as database
    import app.procucev_apis.bfs_apis as bfs_api
    import app.procucev_apis.procucev_api_client as client_mod
    import app.services.media_downloader_service as media_mod
    import app.services.welcome_message_service as welcome_mod
    import app.tools.interaction_logger as interaction_mod

    settings = object()
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(config, "_settings", None)
    config.load_environment()
    assert config.get_settings() is settings
    config._settings = None
    assert config.get_settings() is settings

    class Pool:
        def size(self): return 5
        def checkedin(self): return 3
        def checkedout(self): return 2
        def overflow(self): return 0
        def invalid(self): return 0

    engine = SimpleNamespace(pool=Pool())
    monkeypatch.setattr(database, "engine", engine)
    database._log_pool_status("unit")
    monkeypatch.setattr(database, "engine", SimpleNamespace(pool=SimpleNamespace(checkedout=Mock(side_effect=RuntimeError("pool")))))
    database._log_pool_status("broken")

    class SeedDB:
        def __init__(self):
            self.added = []
            self.committed = False
            self.closed = False
        def query(self, _model): return SimpleNamespace(count=lambda: 0)
        def add(self, value): self.added.append(value)
        def commit(self): self.committed = True
        def rollback(self): pass
        def close(self): self.closed = True

    seed_db = SeedDB()
    db_engine = SimpleNamespace(pool=Pool())
    db_settings = SimpleNamespace(database_mode="local", PROJECT_ROOT=".", get_database_url=lambda: "sqlite://")
    monkeypatch.setattr(database, "get_settings", lambda: db_settings)
    monkeypatch.setattr(database, "_get_ssl_connect_args", lambda _settings: {})
    monkeypatch.setattr(database, "create_engine", lambda *args, **kwargs: db_engine)
    monkeypatch.setattr(database, "sessionmaker", lambda **kwargs: lambda: seed_db)
    monkeypatch.setattr(database.Base.metadata, "create_all", Mock())
    monkeypatch.setattr(database, "_log_pool_status", Mock())
    database.init_database()
    assert seed_db.committed and seed_db.closed and len(seed_db.added) == 13

    local_session = SimpleNamespace()
    monkeypatch.setattr(database, "SessionLocal", None)
    monkeypatch.setattr(database, "sessionmaker", lambda **kwargs: lambda: local_session)
    assert database.get_db_session() is local_session

    remote_session = SimpleNamespace(execute=Mock())
    remote_settings = SimpleNamespace(
        enable_remote_categorization=True,
        get_remote_database_url=lambda: "sqlite://remote",
        database_mode="local", PROJECT_ROOT=".", sql_debug=False,
    )
    monkeypatch.setattr(database, "get_settings", lambda: remote_settings)
    monkeypatch.setattr(database, "RemoteSessionLocal", None)
    monkeypatch.setattr(database, "sessionmaker", lambda **kwargs: lambda: remote_session)
    assert database.get_remote_db_session() is remote_session

    manager = database.DatabaseManager.__new__(database.DatabaseManager)
    manager._owns_session = False
    manager.session = None
    monkeypatch.setattr(database, "engine", SimpleNamespace(pool=Pool()))
    monkeypatch.setattr(database, "remote_engine", SimpleNamespace(pool=Pool()))
    status = manager.get_connection_pool_status()
    assert status["main_db"]["pool_status"] == "healthy"
    assert status["remote_db"]["size"] == 5

    fake_bfs = object()
    bfs_api._bfs_api_service = None
    monkeypatch.setattr(bfs_api, "BFSAPIService", lambda: fake_bfs)
    assert bfs_api.get_bfs_api_service() is fake_bfs
    assert bfs_api.get_bfs_api_service() is fake_bfs

    class FakeClient:
        def __init__(self):
            self.create_session = AsyncMock()
            self.close_session = AsyncMock()

    client_mod._global_client = None
    monkeypatch.setattr(client_mod, "ProcucevAPIClient", FakeClient)
    first = client_mod.get_procucev_api_client()
    assert first is client_mod.get_procucev_api_client()
    client_mod._global_client = None
    initialized = __import__("asyncio").run(client_mod.init_procucev_api_client())
    initialized.create_session.assert_awaited_once()
    __import__("asyncio").run(client_mod.close_procucev_api_client())
    initialized.close_session.assert_awaited_once()

    media = object()
    media_mod._media_downloader = None
    monkeypatch.setattr(media_mod, "MediaDownloaderService", lambda: media)
    assert media_mod.get_media_downloader() is media
    welcome = object()
    welcome_mod._welcome_service = None
    monkeypatch.setattr(welcome_mod, "WelcomeMessageService", lambda: welcome)
    assert welcome_mod.get_welcome_service() is welcome
    welcome_mod._welcome_service = None
    interaction = object()
    interaction_mod._interaction_logger = None
    monkeypatch.setattr(interaction_mod, "InteractionLogger", lambda: interaction)
    assert interaction_mod.get_interaction_logger() is interaction
    interaction_mod._interaction_logger = None
    media_mod._media_downloader = None


def test_pincode_distance_lazy_singleton_and_cache(monkeypatch):
    from app.utils import pincode_distance as distance

    distance._nomi = None
    fake_nomi = object()
    monkeypatch.setattr(distance.pgeocode, "Nominatim", lambda _country: fake_nomi)
    assert distance._get_nominatim() is fake_nomi
    assert distance._get_nominatim() is fake_nomi
    distance._pincode_cache["411005"] = (18.5, 73.8)
    distance.clear_pincode_cache()
    assert distance.get_cache_size() == 0
    distance._nomi = None
    monkeypatch.setattr(distance.pgeocode, "Nominatim", Mock(side_effect=RuntimeError("offline")))
    assert distance._get_nominatim() is None
