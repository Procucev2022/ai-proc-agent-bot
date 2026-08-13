from __future__ import annotations

import base64
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import app.procucev_apis.procucev_api_client as client_module
import app.procucev_apis.rfq_apis as rfq_module
from app.procucev_apis.procucev_api_client import ProcucevAPIClient
from app.procucev_apis.rfq_apis import RFQAPIService


class Response:
    def __init__(self, status=200, data=None, text="body", content_type="application/json", json_error=False):
        self.status = status
        self._data = {} if data is None else data
        self._text = text
        self.headers = {"Content-Type": content_type}
        self.json_error = json_error

    async def text(self):
        return self._text

    async def json(self):
        if self.json_error:
            raise ValueError("invalid json")
        return self._data


class Context:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def __aexit__(self, *args):
        return False


class Session:
    closed = False

    def __init__(self, responses=(), post_responses=None):
        self.responses = list(responses)
        self.post_responses = list(post_responses if post_responses is not None else responses)
        self.request_calls = []
        self.post_calls = []
        self.close = AsyncMock()

    def request(self, *args, **kwargs):
        self.request_calls.append((args, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return Context(response)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        response = self.post_responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return Context(response)


class Redis:
    def __init__(self, value=None):
        self.value = value
        self.get = AsyncMock(return_value=value)
        self.set = AsyncMock(return_value=True)


def settings():
    return SimpleNamespace(
        gmt_base_url="https://gmt.example",
        gmt_username="user",
        gmt_password="password",
        gmt_phone="+91 9876543210",
        gmt_client_id="client",
        gmt_client_secret="secret",
        gmt_retry_delay=0,
        gmt_max_retries=3,
    )


def make_client(monkeypatch, redis=None):
    redis = redis or Redis()
    monkeypatch.setattr(client_module, "get_settings", settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: redis)
    return ProcucevAPIClient(), redis


@pytest.mark.asyncio
async def test_auth_cache_expiry_refresh_and_authentication_edge_cases(monkeypatch):
    client, redis = make_client(monkeypatch)
    assert client.retry_delay == 0.5

    client.send_request = AsyncMock(return_value={"token": "short-lived", "expires": 100})
    assert await client.authenticate()
    assert client.auth_token == "short-lived"
    assert redis.set.call_args.kwargs["ex"] == 60
    assert redis.set.call_args.args[0] == client.token_cache_key

    client.send_request.return_value = {"access_token": "no-expiry", "expires_in": 1}
    assert await client.authenticate()
    assert redis.set.call_args.kwargs["ex"] == 60

    client.send_request.return_value = {"message": "missing token"}
    assert not await client.authenticate()
    redis.set.side_effect = RuntimeError("redis write")
    client.send_request.return_value = {"access_token": "cached", "expires_in": 900}
    assert not await client.authenticate()

    redis.set.side_effect = None
    client.auth_token = None
    client.token_expiry = None
    redis.get.return_value = {
        "access_token": "from-cache",
        "expires_at": datetime.now(UTC).timestamp() + 300,
    }
    assert await client._ensure_authenticated()
    assert client.auth_token == "from-cache"

    client.auth_token = None
    client.token_expiry = None
    redis.get.return_value = {"access_token": "expired", "expires_at": 1}
    client.authenticate = AsyncMock(side_effect=lambda: (setattr(client, "auth_token", "new-token"), True)[1])
    assert await client._ensure_authenticated()
    assert client.auth_token == "new-token"

    client.auth_token = None
    client.token_expiry = None
    redis.get.return_value = {"access_token": "expired", "expires_at": 1}
    client.authenticate = AsyncMock(return_value=True)
    client.auth_token = "refreshed"
    client.token_expiry = datetime.now(UTC) + timedelta(minutes=1)
    assert await client._ensure_authenticated()
    client.authenticate.assert_not_awaited()

    client.auth_token = None
    client.token_expiry = None
    redis.get.return_value = ["not", "a", "dict"]
    client.authenticate = AsyncMock(return_value=False)
    assert not await client._ensure_authenticated()
    client.authenticate.assert_awaited_once()

    redis.get.side_effect = RuntimeError("redis read")
    client.auth_token = None
    client.token_expiry = None
    client.authenticate = AsyncMock(return_value=False)
    assert not await client._ensure_authenticated()

    redis.get.side_effect = None
    redis.get.return_value = {"expires_at": datetime.now(UTC).timestamp() + 60}
    assert await client._ensure_authenticated()
    assert client.auth_token is None


@pytest.mark.asyncio
async def test_session_constructor_context_manager_and_close_branches(monkeypatch):
    client, _ = make_client(monkeypatch)
    timeout = MagicMock()
    connector = MagicMock()
    created = Session()
    timeout_factory = MagicMock(return_value=timeout)
    connector_factory = MagicMock(return_value=connector)
    session_factory = MagicMock(return_value=created)
    monkeypatch.setattr(client_module.aiohttp, "ClientTimeout", timeout_factory)
    monkeypatch.setattr(client_module.aiohttp, "TCPConnector", connector_factory)
    monkeypatch.setattr(client_module.aiohttp, "ClientSession", session_factory)

    await client.create_session()
    timeout_factory.assert_called_once_with(total=15, connect=5, sock_read=10)
    connector_factory.assert_called_once_with(
        limit_per_host=50, limit=100, ttl_dns_cache=300,
        use_dns_cache=True, enable_cleanup_closed=True,
    )
    session_factory.assert_called_once_with(timeout=timeout, connector=connector)
    await client.create_session()
    session_factory.assert_called_once()

    entered = await client.__aenter__()
    assert entered is client
    await client.__aexit__(None, None, None)
    assert client.session is None
    created.close.assert_awaited_once()

    await client.close_session()
    client.session = SimpleNamespace(closed=True, close=AsyncMock())
    await client.close_session()
    client.session.close.assert_not_awaited()
    assert client.session is not None


@pytest.mark.asyncio
async def test_dynamic_auth_creates_sessions_and_handles_response_shapes(monkeypatch):
    client, _ = make_client(monkeypatch)
    fresh = Session(post_responses=[Response(status=201, data={"access_token": "dynamic"})])
    client.create_session = AsyncMock(side_effect=lambda: setattr(client, "session", fresh))
    assert await client.authenticate_dynamic("alice", "9876543210") == "dynamic"
    assert fresh.post_calls[0][0] == ("https://gmt.example/authenticate",)
    assert fresh.post_calls[0][1]["json"] == {"username": "alice", "phone": "+919876543210"}
    assert fresh.post_calls[0][1]["headers"] == {}

    client.session = Session(post_responses=[Response(status=200, data={"message": "none"})])
    assert await client.authenticate_dynamic("alice", "1") is None
    closed = Session(post_responses=[Response(status=200, data={"token": "closed-session"})])
    closed.closed = True
    replacement = Session(post_responses=[Response(status=200, data={"token": "replacement"})])
    client.session = closed
    client.create_session = AsyncMock(side_effect=lambda: setattr(client, "session", replacement))
    assert await client.authenticate_dynamic("alice", "1") == "replacement"
    client.session = Session(post_responses=[Response(status=400, text="bad", content_type="text/plain")])
    assert await client.authenticate_dynamic("alice", "1") is None
    client.session = None
    client.create_session = AsyncMock(side_effect=RuntimeError("session failed"))
    assert await client.authenticate_dynamic("alice", "1") is None


@pytest.mark.asyncio
async def test_send_request_normalization_auth_headers_and_http_errors(monkeypatch):
    client, _ = make_client(monkeypatch)
    monkeypatch.setattr(client_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(client_module, "manual_log_api_call", MagicMock())

    client.session = Session([Response(data={"ok": True})])
    result = await client.send_request(
        "GET", "/items", params={"page": 1}, json_data={"x": 1}, data="raw",
        headers={"Authorization": "do-not-log", "X-Test": "yes"}, api_title="Items",
    )
    assert result["ok"] is True
    args, kwargs = client.session.request_calls[0]
    assert args == ("GET", "https://gmt.example/items")
    assert kwargs["params"] == {"page": 1}
    assert kwargs["headers"]["Authorization"] == "do-not-log"
    logged_input = client_module.manual_log_api_call.call_args.args[2]
    assert "Authorization" not in logged_input["headers"]

    client.session = Session([Response(data=[1, 2])])
    assert (await client.send_request("GET", "/list"))["data"] == [1, 2]
    client.session = Session([Response(data=3)])
    assert (await client.send_request("GET", "/scalar"))["data"] == "body"
    client.session = Session([Response(data={}, text="plain", content_type="text/plain")])
    assert (await client.send_request("GET", "/text"))["data"] == "plain"
    client.session = Session([Response(data={}, json_error=True)])
    with pytest.raises(ValueError, match="invalid json"):
        await client.send_request("GET", "/bad-json")

    client.session = Session([Response(status=418, data={"error": "teapot"})])
    result = await client.send_request("GET", "/teapot")
    assert result["message"] == "teapot"
    client.session = Session([Response(status=400, text="not-json", json_error=True)])
    assert (await client.send_request("GET", "/plain-error"))["message"] == "not-json"
    handler = AsyncMock(side_effect=RuntimeError("handler unavailable"))
    monkeypatch.setattr("app.services.global_error_handler.handle_api_error", handler)
    client.session = Session([Response(status=500, data={"error": "down"})])
    assert (await client.send_request("GET", "/down"))["status_code"] == 500
    handler.assert_awaited_once_with("Procucev API", "/down", "down")


@pytest.mark.asyncio
async def test_send_request_dynamic_auth_401_and_auth_failure_paths(monkeypatch):
    client, _ = make_client(monkeypatch)
    monkeypatch.setattr(client_module, "manual_log_api_call", MagicMock())
    client.session = Session([Response(data={"ok": "dynamic"})])
    result = await client.send_request("GET", "/dynamic", dynamic_token="direct-token")
    assert result["ok"] == "dynamic"
    assert client.session.request_calls[0][1]["headers"]["Authorization"] == "Bearer direct-token"

    client.session = Session([Response(data={"ok": "email"})])
    client.authenticate_dynamic = AsyncMock(return_value="email-token")
    await client.send_request("GET", "/dynamic", dynamic_token={"email": "e", "phone": "1"})
    assert client.authenticate_dynamic.await_args.args == ("e", "1")
    assert client.session.request_calls[0][1]["headers"]["Authorization"] == "Bearer email-token"

    client.session = Session([Response(data={"ok": "fallback"})])
    client.authenticate_dynamic = AsyncMock(return_value=None)
    client._ensure_authenticated = AsyncMock(return_value=False)
    await client.send_request("GET", "/fallback", require_auth=True, dynamic_token={"username": "u"})
    assert client._ensure_authenticated.await_count == 1
    assert client.session.request_calls[0][1]["headers"]["Authorization"] == "Bearer None"

    client.max_retries = 2
    client.session = Session([Response(status=401, data={})])
    await client.send_request("GET", "/dynamic-401", dynamic_token="token")
    assert len(client.session.request_calls) == 1

    client.session = Session([Response(status=401, data={}), Response(data={"ok": True})])
    client.auth_token = "old"
    client._ensure_authenticated = AsyncMock()
    assert (await client.send_request("GET", "/refresh", require_auth=True))["ok"]
    assert client._ensure_authenticated.await_count == 2


@pytest.mark.asyncio
async def test_send_request_retry_error_classification_and_fallback(monkeypatch):
    client, _ = make_client(monkeypatch)
    monkeypatch.setattr(client_module, "manual_log_api_call", MagicMock())
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())
    client.max_retries = 3
    client.retry_delay = 0.6
    client.session = Session([
        aiohttp.ClientError("network"),
        aiohttp.ClientConnectorError(MagicMock(), OSError("connection")),
        Response(data={"retried": True}),
    ])
    assert (await client.send_request("GET", "/retry"))["retried"]
    assert client_module.asyncio.sleep.await_args_list[0].args == (0.6,)
    assert client_module.asyncio.sleep.await_args_list[1].args == (1.3,)

    for failure, expected in [
        (asyncio.TimeoutError("slow"), "Request timeout"),
        (aiohttp.ClientResponseError(MagicMock(), (), status=502, message="bad"), "HTTP error"),
    ]:
        client.max_retries = 1
        client.session = Session([failure])
        client._handle_timeout_error = AsyncMock()
        result = await client.send_request("GET", "/failure")
        assert expected in result["message"]
        client._handle_timeout_error.assert_awaited_once()

    client.max_retries = 0
    client.session = Session([])
    result = await client.send_request("GET", "/zero-retries")
    assert result["message"] == "Service temporarily unavailable. Please try again later."


@pytest.mark.asyncio
async def test_client_verb_wrappers_notifications_and_singleton_helpers(monkeypatch):
    client, _ = make_client(monkeypatch)
    client.send_request = AsyncMock(return_value={"success": True})
    assert await client.get("/g", params={"x": 1})
    assert await client.post("/p")
    assert await client.put("/u")
    assert await client.delete("/d")
    assert [call.args[:2] for call in client.send_request.await_args_list] == [
        ("GET", "/g"), ("POST", "/p"), ("PUT", "/u"), ("DELETE", "/d"),
    ]

    handler = AsyncMock()
    monkeypatch.setattr("app.services.global_error_handler.handle_api_error", handler)
    await client._handle_500_error("/500", "down")
    await client._handle_timeout_error("/timeout", "slow")
    assert handler.await_args_list[0].args == ("Procucev API", "/500", "down")
    assert handler.await_args_list[1].args == ("Procucev API", "/timeout", "slow")
    handler.side_effect = RuntimeError("ignored")
    await client._handle_500_error("/500", "down")
    await client._handle_timeout_error("/timeout", "slow")

    client_module._global_client = None
    await client_module.close_procucev_api_client()
    temporary = MagicMock()
    monkeypatch.setattr(client_module, "ProcucevAPIClient", lambda: temporary)
    assert client_module.get_procucev_api_client() is temporary
    assert client_module.get_procucev_api_client() is temporary
    client_module._global_client = None
    initialized = MagicMock()
    initialized.create_session = AsyncMock()
    initialized.close_session = AsyncMock()
    monkeypatch.setattr(client_module, "ProcucevAPIClient", lambda: initialized)
    assert await client_module.init_procucev_api_client() is initialized
    assert await client_module.init_procucev_api_client() is initialized
    initialized.create_session.assert_awaited_once()
    await client_module.close_procucev_api_client()
    initialized.close_session.assert_awaited_once()
    assert client_module._global_client is None


@pytest.fixture
def rfq_service(monkeypatch):
    api = AsyncMock()
    monkeypatch.setattr(rfq_module, "get_procucev_api_client", lambda: api)
    return RFQAPIService(), api


@pytest.mark.asyncio
async def test_rfq_client_and_detail_paths(rfq_service):
    service, api = rfq_service
    api.post.return_value = {"success": True, "data": ["R1"]}
    assert await service.get_client_rfqs("client") == {"success": True, "rfq_ids": ["R1"]}
    assert api.post.call_args.kwargs == {
        "endpoint": "/rest/client/getClientRfqIds", "json_data": {"id": "client"},
        "require_auth": True, "api_title": "Get Client RFQs API",
    }
    api.post.return_value = {"success": False}
    assert (await service.get_client_rfqs())['error'] == "Failed to get client RFQs"
    api.post.side_effect = RuntimeError("client lookup")
    assert "client lookup" in (await service.get_client_rfqs())["error"]

    api.post.side_effect = None
    api.post.return_value = {"success": True, "data": {"id": "R2"}}
    assert await service.get_rfq_details_by_client("client") == {"success": True, "rfq_details": {"id": "R2"}}
    api.post.return_value = {"success": False}
    assert (await service.get_rfq_details_by_client())["error"] == "Failed to get RFQ details"
    api.post.side_effect = RuntimeError("details lookup")
    assert "details lookup" in (await service.get_rfq_details_by_client())["error"]


@pytest.mark.asyncio
async def test_rfq_create_bulk_and_status_remaining_branches(rfq_service):
    service, api = rfq_service
    api.post.return_value = {"statusCode": "200", "status": "Success"}
    result = await service.create_rfq({}, "u", "o")
    assert result == {"success": True, "response": api.post.return_value, "rfq_id": None}
    api.post.return_value = {"statusCode": 200, "status": "Success", "data": {"rfqId": "wrong-type"}}
    assert not (await service.create_rfq({}, "u", "o"))["success"]
    api.post.return_value = {"statusCode": "400"}
    assert (await service.create_rfq({}, "u", "o"))["error"] == "GMT API error: Unknown error"
    service._transform_rfq_to_gmt_format = MagicMock(side_effect=RuntimeError("transform"))
    assert "transform" in (await service.create_rfq({}, "u", "o"))["error"]

    api.post.side_effect = None
    api.post.return_value = {"success": True, "data": []}
    assert await service.bulk_upload_rfq({"boqfile": "encoded"}) == {"success": True, "response": {}}
    assert api.post.call_args.kwargs["json_data"]["boqFileName"] == "rfq_items.xlsx"
    api.post.return_value = {"success": True, "data": {"errorCode": 500}}
    assert (await service.bulk_upload_rfq({"boqfile": "x"}))["error"] == "GMT API error"
    api.post.return_value = {"success": True, "data": {"errorMessage": "File format is not correct for x"}}
    assert not (await service.bulk_upload_rfq({"boqfile": "x"}))["success"]
    api.post.return_value = {"success": False}
    assert (await service.bulk_upload_rfq({"boqfile": "x"}))["error"] == "Bulk upload failed"
    api.post.side_effect = RuntimeError("upload exception")
    assert "upload exception" in (await service.bulk_upload_rfq({"boqfile": "x"}))["error"]

    api.post.side_effect = None
    api.post.return_value = {"success": True, "data": ["ok"]}
    assert (await service.get_rfq_status("c", [None, None]))["data"] == ["ok"]
    status_payload = api.post.call_args.kwargs["json_data"]
    assert status_payload == {"clientId": "c"}
    await service.get_rfq_status("c", ["12", "rfq13", None])
    assert api.post.call_args.kwargs["json_data"]["rfqIds"] == ["RFQ12", "rfq13"]
    api.post.return_value = {"success": False}
    assert (await service.get_rfq_status("c"))["error"] == "Failed to get RFQ status"
    api.post.side_effect = RuntimeError("status exception")
    assert "status exception" in (await service.get_rfq_status("c"))["error"]


def test_rfq_transform_defaults_dates_attachments_and_descriptions(monkeypatch, rfq_service):
    service, _ = rfq_service
    multi = service._transform_rfq_to_gmt_format(
        {
            "items": [{}, {"quantity": 3, "unit_of_measures": "kg", "description": "Bolts"}],
            "deadline": datetime(2025, 2, 3, 4, 5, 6),
            "attachments": [
                {"file_name": "raw.txt", "file_content": "hello"},
                {"file_name": "encoded.txt", "file_content": base64.b64encode(b"ok").decode()},
                {"file_name": "missing-content"},
                {"file_content": "missing-name"},
            ],
            "delivery_state": "MH",
            "delivery_city": "Pune",
            "delivery_pincode": "411001",
        },
        "user", "org",
    )
    assert multi["rfqItem"][0]["quantity"] == "1"
    assert multi["rfqItem"][0]["unitofMeasures"] == "unit(s)"
    assert multi["rfqItem"][0]["description"] == "Item 1"
    assert multi["rfqItem"][1]["serialNo"] == 1002
    assert multi["deliveryDate"] == "2025-02-03T04:05:06.000Z"
    assert multi["rfqDocument"] == [
        {"fileName": "raw.txt", "file": base64.b64encode(b"hello").decode()},
        {"fileName": "encoded.txt", "file": base64.b64encode(b"ok").decode()},
    ]
    assert multi["clientdeliverylocationrfq"] == [{"state": "MH", "city": "Pune", "pincode": "411001"}]

    legacy = service._transform_rfq_to_gmt_format(
        {"preferred_brand": "Brand", "unit_of_measure": "box", "quantity": 4, "specifications": "spec", "deadline": "bad-date"}
    )
    assert legacy["rfqItem"][0]["quantity"] == 4
    assert legacy["rfqItem"][0]["description"] == "Product"
    assert legacy["deliveryDate"] is None
    unsupported = service._transform_rfq_to_gmt_format({"deadline": 123})
    assert unsupported["deliveryDate"] is None

    long = "A" * 95
    description = service._generate_project_desc({
        "product_name": "  Widget  ",
        "rfq_items": [{"description": "widget"}, {"product_name": long}, "ignored", {"item_description": 3}],
        "entities": [{"description": "Other"}, {"projectDesc": "other"}, 4],
    })
    assert description == ("Widget, " + long + ", Other")[:97] + "..."
    fixed_now = MagicMock()
    fixed_now.strftime.return_value = "20250102_030405"
    fake_datetime = MagicMock()
    fake_datetime.now.return_value = fixed_now
    monkeypatch.setattr(rfq_module, "datetime", fake_datetime)
    assert service._generate_project_desc({}) == "RFQ_20250102_030405"
    assert service._is_base64("")
