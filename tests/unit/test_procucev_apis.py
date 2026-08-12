from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import aiohttp

import app.procucev_apis.procucev_api_client as client_module
from app.procucev_apis.auth_apis import AuthAPIService
from app.procucev_apis.bfs_apis import BFSAPIService
from app.procucev_apis.category_apis import CategoryAPIService
from app.procucev_apis.email_service_api import EmailServiceAPI
from app.procucev_apis.procucev_api_client import ProcucevAPIClient
from app.procucev_apis.register_apis import RegisterAPIService
from app.procucev_apis.rfq_apis import RFQAPIService
from app.procucev_apis.seller_apis import SellerAPIService


class FakeResponse:
    def __init__(self, status=200, data=None, text="body", content_type="application/json", json_error=False):
        self.status = status
        self._data = data if data is not None else {}
        self._text = text
        self.headers = {"Content-Type": content_type}
        self._json_error = json_error

    async def text(self):
        return self._text

    async def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._data


class AsyncContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.set = AsyncMock(side_effect=self._set)
        self.get = AsyncMock(side_effect=self._get)

    async def _set(self, key, value, ex=None):
        self.values[key] = value
        return True

    async def _get(self, key, as_json=False):
        return self.values.get(key)


@pytest.fixture
def api_client_mock():
    return AsyncMock()


def make_service(monkeypatch, module, cls, api_client_mock):
    monkeypatch.setattr(module, "get_procucev_api_client", lambda: api_client_mock)
    return cls()


@pytest.mark.asyncio
async def test_auth_api_all_response_shapes(monkeypatch, api_client_mock):
    import app.procucev_apis.auth_apis as module

    service = make_service(monkeypatch, module, AuthAPIService, api_client_mock)
    user = {"id": "u1", "username": "a@example.com", "selfClient": True}
    api_client_mock.get.return_value = {"success": True, "data": {"users": [user]}}
    result = await service.authenticate_user("999")
    assert result["success"] and result["data"][0]["id"] == "u1"

    api_client_mock.get.return_value = {"success": True, "data": {"users": []}}
    assert (await service.authenticate_user("999"))["is_registered"] is False
    api_client_mock.get.return_value = {"statusCode": "200", "status": "Success", "data": {"users": [user]}}
    assert (await service.authenticate_user("999"))["status_code"] == 200
    api_client_mock.get.return_value = {"statusCode": "200", "status": "Success", "data": {"users": []}}
    assert (await service.authenticate_user("999"))["success"] is False
    api_client_mock.get.return_value = {"statusCode": "204", "message": "none"}
    assert (await service.authenticate_user("999"))["status_code"] == 204
    api_client_mock.get.return_value = {"statusCode": "500", "message": "down"}
    assert (await service.authenticate_user("999"))["message"] == "down"
    api_client_mock.get.side_effect = RuntimeError("network")
    assert (await service.authenticate_user("999"))["status_code"] == 500


@pytest.mark.asyncio
async def test_bfs_api_success_failure_exception_and_singleton(monkeypatch, api_client_mock):
    import app.procucev_apis.bfs_apis as module

    service = make_service(monkeypatch, module, BFSAPIService, api_client_mock)
    api_client_mock.post.return_value = {"success": True, "data": ["x"]}
    assert (await service.search_bfs_items([{"description": ["steel"]}]))["data"] == ["x"]
    api_client_mock.post.return_value = {"success": False, "message": "bad"}
    assert (await service.search_bfs_items([]))["error"] == "bad"
    api_client_mock.post.side_effect = RuntimeError("oops")
    assert "oops" in (await service.search_bfs_items([]))["error"]

    api_client_mock.post.side_effect = None
    api_client_mock.post.return_value = {"success": True, "data": {"id": 1}}
    assert (await service.request_bfs_item("i", 2, 3, 4, "o", "u", "p"))["success"]
    assert (await service.accept_bid_by_seller("b"))["bfs_user_id"] == "b"
    assert (await service.reject_bid_by_seller("b"))["success"]
    api_client_mock.post.return_value = {"success": False, "message": "no"}
    assert (await service.accept_bid_by_seller("b"))["error"] == "no"
    api_client_mock.post.side_effect = ValueError("x")
    assert (await service.reject_bid_by_seller("b"))["error"] == "x"
    module._bfs_api_service = None
    assert module.get_bfs_api_service() is module.get_bfs_api_service()


@pytest.mark.asyncio
async def test_category_and_email_api_contracts(monkeypatch, api_client_mock):
    import app.procucev_apis.category_apis as category_module
    import app.procucev_apis.email_service_api as email_module

    category = make_service(monkeypatch, category_module, CategoryAPIService, api_client_mock)
    api_client_mock.get.return_value = {"success": True, "data": ["a"]}
    assert (await category.get_divisions())["divisions"] == ["a"]
    assert (await category.get_categories())["categories"] == ["a"]
    api_client_mock.get.return_value = {"success": False, "message": "failed"}
    assert (await category.get_divisions())["error"] == "failed"
    api_client_mock.get.side_effect = RuntimeError("offline")
    assert "offline" in (await category.get_categories())["error"]

    email = make_service(monkeypatch, email_module, EmailServiceAPI, api_client_mock)
    api_client_mock.post.side_effect = None
    api_client_mock.post.return_value = {"status": "Success", "statusCode": "200", "message": "sent", "data": {"id": 1}}
    result = await email.send_email({"to": ["a"], "attachments": ["file"]})
    assert result["status"] == "Success" and result["data"] == {"id": 1}
    api_client_mock.post.return_value = {"status": "Failure", "statusCode": "400", "message": "bad"}
    assert (await email.send_email({}))["status"] == "Failure"
    api_client_mock.post.side_effect = RuntimeError("mail")
    assert (await email.send_email({}))["statusCode"] == "500"


@pytest.mark.asyncio
async def test_register_api_registration_otp_and_approval(monkeypatch, api_client_mock):
    import app.procucev_apis.register_apis as module

    service = make_service(monkeypatch, module, RegisterAPIService, api_client_mock)
    success = {"success": True, "status": "Success", "statusCode": 200, "data": {"message": "ok"}}
    api_client_mock.post.return_value = success
    assert (await service.register_seller({"name": "s"}))["status"] == "Success"
    assert (await service.register_buyer({"name": "b"}))["statusCode"] == 200
    api_client_mock.post.return_value = {"status_code": 409, "data": {"error": "exists"}, "message": "fallback"}
    assert (await service.register_seller({}))["message"] == "fallback"
    api_client_mock.post.return_value = {"status": "Success", "statusCode": 200, "message": "sent"}
    assert (await service.send_otp("a", "9199"))["statusCode"] == 200
    api_client_mock.post.return_value = {"status": "Failure", "statusCode": 400, "message": "bad"}
    assert (await service.send_otp("a")["statusCode"] if False else (await service.send_otp("a"))["statusCode"]) == 400
    api_client_mock.post.return_value = {"status": "Success", "statusCode": 200, "message": "valid"}
    assert (await service.validate_otp("a", "1234", "9199"))["status"] == "Success"
    assert api_client_mock.post.call_args.kwargs["dynamic_token"] == {"username": "a", "phone": "9199"}
    api_client_mock.post.return_value = {"status": "Failure", "statusCode": 400}
    assert (await service.validate_otp("a", "bad"))["status"] == "Failure"
    api_client_mock.post.return_value = {"status": "Success", "statusCode": 200, "message": "approved"}
    assert (await service.user_approval("u"))["status"] == "Success"
    api_client_mock.post.return_value = {"status": "Failure", "statusCode": 400, "message": "no"}
    assert (await service.user_approval("u"))["message"] == "no"
    api_client_mock.post.side_effect = RuntimeError("register")
    assert (await service.register_buyer({}))["statusCode"] == "500"
    assert (await service.user_approval("u"))["success"] is False


@pytest.mark.asyncio
async def test_seller_api_transformations_and_errors(monkeypatch, api_client_mock):
    import app.procucev_apis.seller_apis as module

    settings = SimpleNamespace(PROCUCEV_PORTAL_URL="https://portal")
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    service = make_service(monkeypatch, module, SellerAPIService, api_client_mock)
    rfqs = [{"rfqId": str(i), "deliveryDate": "2025-01-02T00:00:00Z", "projectDesc": "p", "rfqItem": [{"category": "A"}, {"category": "A"}], "clientdeliverylocationrfq": [{"state": "MH"}]} for i in range(6)]
    api_client_mock.post.return_value = {"success": True, "data": {"rfqs": rfqs, "count": 6}}
    result = await service.fetch_active_rfqs("org")
    assert result["total_count"] == 6 and len(result["rfqs"]) == 5 and result["rfqs"][0]["categories"] == "A"
    api_client_mock.post.return_value = {"success": False, "message": "bad"}
    assert (await service.fetch_active_rfqs("org"))["error"] == "bad"
    api_client_mock.post.side_effect = RuntimeError("fetch")
    assert "fetch" in (await service.check_seller_credits("o"))["error"]

    api_client_mock.post.side_effect = None
    api_client_mock.post.return_value = {"success": True, "data": {"creditsAvailable": 7}}
    assert (await service.check_seller_credits("o"))["credits_available"] == 7
    api_client_mock.post.return_value = {"success": True, "data": ["s"]}
    assert (await service.check_seller_rfq_status("s", ["1", "RFQ2", None]))["success"]
    assert "RFQ1" in api_client_mock.post.call_args.kwargs["json_data"]["rfqIds"]
    api_client_mock.post.return_value = {"success": False, "message": "status"}
    assert (await service.check_seller_rfq_status("s", []))["error"] == "status"
    api_client_mock.post.return_value = {"success": True}
    assert (await service.send_rfq_email([], "a", "s"))["email_sent"]
    assert (await service.fetch_seller_open_rfqs_for_reminder("s"))["open_rfqs"] == []
    api_client_mock.post.return_value = {"success": False, "message": "no"}
    assert (await service.send_rfq_email([], "a", "s"))["error"] == "no"
    api_client_mock.post.return_value = {"status_code": 201}
    assert (await service.update_rfq_seller_sent_flag("r", "s"))["flag_updated"]
    api_client_mock.post.return_value = {"status_code": 200, "message": "no"}
    assert (await service.update_rfq_seller_sent_flag("r", "s"))["success"] is False
    api_client_mock.get.return_value = {"success": True, "data": {"plans": [1]}}
    assert (await service.get_subscription_plans())["plans"] == [1]
    api_client_mock.get.return_value = {"success": False, "message": "plans"}
    assert (await service.get_subscription_plans())["error"] == "plans"
    api_client_mock.post.return_value = {"success": True, "data": {"paymentUrl": "u"}}
    assert (await service.generate_payment_link("p", "e", "1"))["payment_url"] == "u"
    api_client_mock.post.return_value = {"success": True, "paymentUrl": "root"}
    assert (await service.generate_payment_link("p"))["payment_url"] == "root"
    api_client_mock.post.return_value = {"success": False}
    assert (await service.generate_payment_link("p"))["payment_url"] == "https://portal"


@pytest.mark.asyncio
async def test_rfq_api_transform_and_delegation(monkeypatch, api_client_mock):
    import app.procucev_apis.rfq_apis as module

    service = make_service(monkeypatch, module, RFQAPIService, api_client_mock)
    data = {"items": [{"description": "Laptop", "quantity": 2, "brand": "Dell", "remarks": "i7"}], "deadline": "2025-01-02T00:00:00Z", "attachments": [{"file_name": "a.txt", "file_content": "hello"}, {"file_name": "b", "file_content": "aGVsbG8="}], "product_name": "Laptop", "rfq_items": [{"description": "Monitor"}, {"description": "laptop"}], "entities": [{"projectDesc": "Desk"}]}
    transformed = service._transform_rfq_to_gmt_format(data, "u", "o")
    assert transformed["createdBy"] == "u" and len(transformed["rfqDocument"]) == 2 and transformed["projectDesc"] == "Laptop, Monitor, Desk"
    legacy = service._transform_rfq_to_gmt_format({"product_name": "Chair", "deadline": datetime(2025, 1, 1)}, None, None)
    assert legacy["rfqItem"][0]["description"] == "Chair"
    assert service._is_base64("aGVsbG8=") and not service._is_base64("bad!") and not service._is_base64(3)
    api_client_mock.post.return_value = {"statusCode": "200", "status": "Success", "data": {"rfqId": "R1"}}
    assert (await service.create_rfq(data, "u", "o"))["rfq_id"] == "R1"
    api_client_mock.post.return_value = {"statusCode": "500", "message": "bad"}
    assert (await service.create_rfq({}, "u", "o"))["success"] is False
    api_client_mock.post.return_value = {"success": True, "data": [{"status": "Success"}]}
    assert (await service.bulk_upload_rfq({"boqfile": "x"}))["success"]
    assert (await service.bulk_upload_rfq({}))["success"] is False
    api_client_mock.post.return_value = {"success": True, "data": {"status": "Failure", "errorMessage": "bad file"}}
    assert (await service.bulk_upload_rfq({"boqfile": "x"}))["success"] is False
    api_client_mock.post.return_value = {"success": False, "message": "upload"}
    assert (await service.bulk_upload_rfq({"boqfile": "x"}))["error"] == "upload"
    api_client_mock.post.return_value = {"success": True, "data": [1, 2]}
    assert (await service.get_rfq_status("c", ["1", "RFQ2", None]))["success"]
    assert (await service.get_rfq_status("c", []))["success"]
    api_client_mock.post.return_value = {"success": False, "message": "status"}
    assert (await service.get_rfq_status("c"))["error"] == "status"
    api_client_mock.post.side_effect = RuntimeError("rfq")
    assert "rfq" in (await service.get_client_rfqs())["error"]


@pytest.mark.asyncio
async def test_client_singletons_auth_normalization_and_sessions(monkeypatch):
    redis = FakeRedis()
    settings = SimpleNamespace(gmt_base_url="https://gmt", gmt_username="u", gmt_password="p", gmt_phone="9876543210", gmt_client_id="c", gmt_client_secret="s", gmt_retry_delay=0, gmt_max_retries=3)
    monkeypatch.setattr(client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: redis)
    client = ProcucevAPIClient()
    assert client.base_url == "https://gmt"
    client.send_request = AsyncMock(return_value={"access_token": "tok", "expires_in": 500})
    assert await client.authenticate()
    assert client.auth_token == "tok" and await redis.get(client.token_cache_key)
    client.send_request.return_value = {"message": "no"}
    assert not await client.authenticate()
    client.auth_token = None
    client.token_expiry = None
    assert await client._ensure_authenticated()
    client.auth_token = "memory"
    client.token_expiry = datetime.now(UTC) + timedelta(minutes=5)
    assert await client._ensure_authenticated()
    assert await client.normalize_response(FakeResponse(data={"x": 1}), "")
    assert (await client.normalize_response(FakeResponse(data=[1]), ""))["data"] == [1]
    assert (await client.normalize_response(FakeResponse(content_type="text/plain"), "raw"))["data"] == "raw"
    client.session = SimpleNamespace(closed=False, close=AsyncMock())
    await client.close_session()
    assert client.session is None
    client.session = None
    await client.create_session()
    assert client.session is not None
    await client.close_session()
    client_module._global_client = None
    monkeypatch.setattr(client_module, "ProcucevAPIClient", lambda: "client")
    assert client_module.get_procucev_api_client() == "client"
    client_module._global_client = None
    fake = MagicMock()
    fake.create_session = AsyncMock()
    fake.close_session = AsyncMock()
    monkeypatch.setattr(client_module, "ProcucevAPIClient", lambda: fake)
    assert await client_module.init_procucev_api_client() is fake
    assert await client_module.init_procucev_api_client() is fake
    await client_module.close_procucev_api_client()
    assert client_module._global_client is None


@pytest.mark.asyncio
async def test_client_send_request_success_errors_dynamic_and_retry(monkeypatch):
    settings = SimpleNamespace(gmt_base_url="https://gmt", gmt_username="u", gmt_password="p", gmt_phone="9876543210", gmt_client_id="c", gmt_client_secret="s", gmt_retry_delay=0, gmt_max_retries=3)
    redis = FakeRedis()
    monkeypatch.setattr(client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: redis)
    client = ProcucevAPIClient()

    class Session:
        closed = False
        def __init__(self, responses):
            self.responses = list(responses)
        def request(self, *args, **kwargs):
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return AsyncContext(response)
        def post(self, *args, **kwargs):
            return AsyncContext(self.responses.pop(0))

    monkeypatch.setattr(client_module, "manual_log_api_call", MagicMock())
    client.session = Session([FakeResponse(data={"ok": 1})])
    result = await client.send_request("GET", "/items", headers={"Authorization": "secret", "X": "1"}, api_title="items")
    assert result["ok"] == 1
    assert client_module.manual_log_api_call.call_count == 1

    client.session = Session([FakeResponse(status=400, data={"error": "bad"})])
    assert (await client.send_request("GET", "/bad"))["status_code"] == 400
    handler = AsyncMock()
    monkeypatch.setattr("app.services.global_error_handler.handle_api_error", handler)
    client.session = Session([FakeResponse(status=500, data={"error": "down"})])
    assert (await client.send_request("GET", "/down"))["status_code"] == 500
    handler.assert_awaited_once()

    client.session = Session([FakeResponse(status=400, text="plain", json_error=True)])
    assert (await client.send_request("GET", "/bad"))["message"] == "plain"
    client.session = Session([FakeResponse(status=200, data={"ok": 2})])
    assert (await client.send_request("GET", "https://other/x", dynamic_token="dynamic"))["ok"] == 2
    client.session = Session([FakeResponse(status=200, data={"ok": 3})])
    client.authenticate_dynamic = AsyncMock(return_value="dynamic")
    assert (await client.send_request("GET", "/x", dynamic_token={"email": "e", "phone": "1"}))["ok"] == 3

    client.max_retries = 2
    client.session = Session([aiohttp.ClientError("net"), FakeResponse(status=200, data={"retried": True})])
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())
    assert (await client.send_request("GET", "/retry"))["retried"]
    client.max_retries = 1
    client.session = Session([aiohttp.ClientError("net")])
    monkeypatch.setattr(client, "_handle_timeout_error", AsyncMock())
    assert (await client.send_request("GET", "/timeout"))["status_code"] == 500

    client.session = Session([FakeResponse(status=200, data={"ok": 4})])
    client.auth_token = "old"
    client.token_expiry = datetime.now(UTC) + timedelta(minutes=2)
    assert (await client.get("/x"))["ok"] == 4
    client.session = Session([FakeResponse(status=200, data={"ok": 5})])
    assert (await client.post("/x", json_data={"a": 1}))["ok"] == 5
    client.session = Session([FakeResponse(status=200, data={"ok": 6})])
    assert (await client.put("/x"))["ok"] == 6
    client.session = Session([FakeResponse(status=200, data={"ok": 7})])
    assert (await client.delete("/x"))["ok"] == 7


@pytest.mark.asyncio
async def test_client_auth_refresh_and_dynamic_auth_responses(monkeypatch):
    settings = SimpleNamespace(gmt_base_url="https://gmt", gmt_username="u", gmt_password="p", gmt_phone="9876543210", gmt_client_id="c", gmt_client_secret="s", gmt_retry_delay=0, gmt_max_retries=3)
    monkeypatch.setattr(client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(client_module, "get_redis_service", lambda: FakeRedis())
    client = ProcucevAPIClient()

    class Session:
        closed = False
        def __init__(self, responses): self.responses = list(responses)
        def request(self, *args, **kwargs): return AsyncContext(self.responses.pop(0))
        def post(self, *args, **kwargs): return AsyncContext(self.responses.pop(0))

    client.max_retries = 2
    client.session = Session([FakeResponse(status=401, data={}), FakeResponse(status=200, data={"ok": True})])
    client._ensure_authenticated = AsyncMock()
    assert (await client.send_request("GET", "/secure", require_auth=True))["ok"]
    assert client._ensure_authenticated.await_count == 2
    client.session = Session([FakeResponse(status=201, data={"access_token": "dynamic"})])
    assert await client.authenticate_dynamic("user", "9876543210") == "dynamic"
    client.session = Session([FakeResponse(status=200, data={"token": "dynamic"})])
    assert await client.authenticate_dynamic("user", "9876543210") == "dynamic"
    client.session = Session([FakeResponse(status=200, data={"message": "none"})])
    assert await client.authenticate_dynamic("user", "9876543210") is None
    client.session = Session([FakeResponse(status=500, text="bad", content_type="text/plain")])
    assert await client.authenticate_dynamic("user", "9876543210") is None
    client.session = Session([RuntimeError("auth")])
    assert await client.authenticate_dynamic("user", "9876543210") is None
