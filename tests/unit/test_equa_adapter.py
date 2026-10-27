"""EquaAdapter: QUA endpoints answered through Enterprise QUA's existing APIs, in QUA's response shape."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.procucev_apis import equa_adapter as ea
from app.procucev_apis.equa_adapter import EquaAdapter

BUYER = {"id": "u-buyer", "email": "buyer@acme.com", "role": "buyer", "orgId": "org-b", "vendorId": None}
SELLER = {"id": "u-seller", "email": "seller@steel.com", "role": "vendor", "orgId": "org-s", "vendorId": "v-1"}
NOT_FOUND = {"success": False, "status_code": 404, "message": "not found", "data": {}}


def utc_stamp(days_ago: float = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def rfq(**overrides):
    base = {
        "id": "rfq-1", "rfqNumber": "RFQ1", "title": "Steel plates", "category": "Steel", "status": "Quotes Pending",
        "createdAt": utc_stamp(), "targetDeliveryDate": "2099-01-01", "deliveryCity": "Pune", "deliveryState": "Maharashtra",
        "deliveryPincode": "411001", "deliveryLocation": "Pune, Maharashtra", "quotes": [], "quotesCount": 0,
        "extractedEntities": [{"itemName": "Plate", "majorCategory": "Steel", "quantity": 2, "unit": "Nos", "brand": "Tata", "technicalSpecs": "10mm"}],
    }
    base.update(overrides)
    return base


class FakeRedis:
    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    async def get(self, key, as_json=False):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[key] = value
        return True


class FakeClient:
    """Stands in for ProcucevAPIClient; routes map (method, endpoint) to a response or a callable."""

    def __init__(self, routes=None, redis=None, **settings):
        self.base_url = "https://equa.example/api"
        self.settings = SimpleNamespace(**{"equa_rfq_sourcing_mode": "mode_0", "equa_rfq_create_timeout_seconds": 90.0, **settings})
        self.routes = routes or {}
        self.calls = []
        self.redis_service = redis or FakeRedis()

    async def send_request(self, method, endpoint, **kwargs):
        assert kwargs.pop("raw") is True
        self.calls.append((method, endpoint, kwargs))
        response = self.routes.get((method, endpoint))
        if callable(response):
            response = response(kwargs)
        return response if response is not None else NOT_FOUND

    def sent(self, method, endpoint):
        return [kwargs for m, e, kwargs in self.calls if (m, e) == (method, endpoint)]


def session_route(user):
    return {("POST", "/bot/session"): {"success": True, "token": f"tok-{user['id']}", "user": user}}


def adapter(routes=None, **kwargs):
    client = FakeClient(routes, **kwargs)
    return EquaAdapter(client), client


# ── Helpers ─────────────────────────────────────────────────────────────────

def test_normalize_mobile_and_time_helpers():
    assert ea.normalize_mobile("9876543210") == "+919876543210"
    assert ea.normalize_mobile("919876543210") == "+919876543210"
    assert ea.normalize_mobile("+91 98765 43210") == "+919876543210"
    assert ea.normalize_mobile("14155550100") == "+14155550100"
    assert ea.normalize_mobile(None) == ""
    assert ea.to_time("2026-10-19 10:30 UTC") == datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc).timestamp()
    assert ea.to_time("2026-10-19T10:30:00Z") == datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc).timestamp()
    assert ea.to_time("2026-10-19") == datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp()
    assert ea.to_time("soon") == 0 and ea.to_time(None) == 0
    assert ea.to_iso("2026-10-19") == "2026-10-19T00:00:00.000Z"
    assert ea.to_iso("soon") == "soon" and ea.to_iso(None) is None


def test_rfq_id_matching_and_qua_shapes():
    record = rfq(rfqNumber="ACM1")
    assert ea.matches_rfq_id(record, "acm1") and ea.matches_rfq_id(record, "RFQACM1") and ea.matches_rfq_id(record, "rfq-1")
    assert not ea.matches_rfq_id(record, "OTHER") and not ea.matches_rfq_id(record, "")
    assert ea.normalize_requested_ids(["12", "RFQ9", None, " "]) == ["RFQ12", "RFQ9"]
    assert ea.normalize_requested_ids("x") == []

    mapped = ea.to_qua_rfq(rfq(deliveryState="", targetDeliveryDate=None, deadline="soon"))
    assert mapped["rfqId"] == "RFQ1" and mapped["deliveryDate"] == "soon"
    assert mapped["clientdeliverylocationrfq"][0]["state"] == "Maharashtra"
    assert mapped["rfqItem"][0] == {"serialNo": 1001, "description": "Plate", "category": "Steel", "quantity": 2, "unitofMeasures": "Nos", "brand": "Tata", "remarks": "10mm"}
    bare = ea.to_qua_rfq({"id": "r", "lineItems": [{"description": "Bolt", "category": "Fasteners"}], "deliveryLocation": "Pune"})
    assert bare["rfqId"] == "r" and bare["rfqItem"][0]["description"] == "Bolt" and bare["clientdeliverylocationrfq"][0]["state"] == ""
    assert ea.to_qua_rfq({"id": "r"})["rfqItem"] == []


def test_buyer_status_words_follow_qua():
    assert ea.buyer_rfq_status(rfq(quotesCount=1)) == "Quotation_Received"
    assert ea.buyer_rfq_status(rfq(status="Quotes Received")) == "Quotation_Received"
    assert ea.buyer_rfq_status(rfq(quotes=[{"vendorId": "v"}])) == "Quotation_Received"
    assert ea.buyer_rfq_status(rfq()) == "Published To Multiple Sellers"
    assert ea.buyer_rfq_status(rfq(status="PO Generated")) == "PO Generated"
    assert ea.buyer_rfq_status(rfq(status=None)) == "Unknown"


# ── Dispatch and sessions ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_strips_the_base_url_and_handles_unknown_and_bfs():
    bridge, client = adapter()
    unknown = await bridge.handle("POST", "https://equa.example/api/rest/client/getClientRfqIds?x=1", {"id": "1"})
    assert unknown["success"] is False and unknown["status_code"] == 404
    bfs = await bridge.handle("POST", "https://equa.example/api/rest/bfs/getBfsItemsByCategory", [{"category": ["x"]}])
    assert bfs["status_code"] == 501 and bfs["message"] == ea.MSG["BFS_UNAVAILABLE"]
    client.base_url = None
    assert (await bridge.handle("post", "rest/gmt/acceptSelfRegisterClient"))["statusCode"] == "200"
    assert client.calls == []


@pytest.mark.asyncio
async def test_sessions_are_reused_and_aliased():
    bridge, client = adapter(session_route(SELLER))
    assert await bridge._session("u-seller") == ("tok-u-seller", SELLER)
    assert await bridge._session("v-1") == ("tok-u-seller", SELLER)  # alias by vendor id
    assert len(client.sent("POST", "/bot/session")) == 1
    await bridge._session(email="seller@steel.com", phone="9876543210")
    assert client.sent("POST", "/bot/session")[-1]["json_data"] == {"email": "seller@steel.com", "phone": "+919876543210"}
    assert await bridge._session() == (None, None)

    expired, _ = adapter()
    expired._sessions["x"] = ("old", {}, time.time() - 1)
    expired.client.routes = {("POST", "/bot/session"): {"success": False, "message": "No active account found."}}
    assert await expired._session("x") == (None, None)


@pytest.mark.asyncio
async def test_authenticate_endpoint_issues_the_session_token():
    bridge, _ = adapter(session_route(BUYER))
    ok = await bridge.handle("POST", "/authenticate", {"username": "buyer@acme.com", "phone": "919876543210"})
    assert ok["status"] == "success" and ok["access_token"] == "tok-u-buyer" and ok["token_type"] == "Bearer"
    bridge.client.routes = {}
    bridge._sessions.clear()
    assert (await bridge.handle("POST", "/authenticate", {"username": "x@y.com", "phone": "1"}))["status"] == "error"


# ── Users by phone ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_users_by_phone_in_qua_user_shape():
    users = [
        {"id": "u-buyer", "email": "buyer@acme.com", "name": "Bea", "role": "buyer", "orgId": "org-b", "orgName": "Acme", "mobile": "+919876543210",
         "uniqueId": "USR1", "verificationStatus": "PENDING_EMAIL_VERIFICATION", "isApproved": True, "isActive": True, "vendorId": None},
        {"id": "u-seller", "email": "seller@steel.com", "name": "Sam", "role": "vendor", "orgId": "org-s", "orgName": None, "mobile": "+919876543210",
         "uniqueId": None, "verificationStatus": None, "isApproved": True, "isActive": True, "vendorId": "v-1"},
        {"id": "u-new", "email": "new@steel.com", "role": "vendor", "orgId": "org-n", "vendorId": None},
    ]
    bridge, client = adapter({("GET", "/bot/users"): {"success": True, "data": users}})
    response = await bridge.handle("GET", "/partialvendor/getUsersByPhoneNumber/919876543210")
    assert response["statusCode"] == "200" and response["status"] == "Success" and response["success"] is True
    buyer, seller, unlinked = response["data"]["users"]
    assert buyer == {
        "id": "u-buyer", "username": "buyer@acme.com", "email": "buyer@acme.com", "fullName": "Bea", "phone": "+919876543210",
        "selfClient": True, "companyName": "Acme", "orgId": "org-b", "uniqueId": "USR1",
        "verificationStatus": "PENDING_EMAIL_VERIFICATION", "approved": True, "active": True,
    }
    assert seller["selfClient"] is False and seller["orgId"] == "v-1" and seller["verificationStatus"] == "EMAIL_VERIFIED"
    assert unlinked["orgId"] == "org-n"
    assert client.sent("GET", "/bot/users")[0]["params"] == {"phone": "919876543210"}

    bridge.client.routes = {("GET", "/bot/users"): {"success": True, "data": []}}
    none = await bridge.handle("GET", "/partialvendor/getUsersByPhoneNumber/919000000000")
    assert none["statusCode"] == "204" and none["message"] == "No user found with phone number: 919000000000"

    bridge.client.routes = {}
    assert (await bridge.handle("GET", "/partialvendor/getUsersByPhoneNumber/1")) is NOT_FOUND


# ── Registration and OTP ────────────────────────────────────────────────────

SELLER_PAYLOAD = {
    "name": "Sam", "companyName": "Steel Co Pvt Ltd", "email": "Seller@steel.com", "zipCode": "411001", "address1": "Pune",
    "gstin": "27AAAAA0000A1Z5", "details": "steel plates", "organizationPhonenumber": "919876543210",
    "divisionCategories": [{"category": "Plates", "division": ""}, {"division": "Hardware"}, {}, "junk"],
}


@pytest.mark.asyncio
async def test_registration_validation_and_failures_in_qua_shape():
    bridge, client = adapter()
    bad = await bridge.handle("POST", "/partialvendor/buyerRegistration", {"email": "nope"})
    assert bad["status_code"] == 409 and bad["data"]["statusCode"] == "409" and bad["data"]["data"] == {"confirmationFlag": False, "error": "Invalid email format: [nope]"}
    assert (await bridge.handle("POST", "/partialvendor/buyerRegistration", {"email": "a@b.com"}))["data"]["message"] == "Mobile number is required."

    payload = {"email": "a@b.com", "organizationPhonenumber": "919876543210"}
    for message, status, expected_status, expected_message in [
        ("This email and mobile number are already registered. An account already exists.", 400, 409, ea.MSG["ALREADY_EXISTS"]),
        ("Enter a valid 10-digit Indian mobile number.", 400, 409, "Enter a valid 10-digit Indian mobile number."),
        ("Network error", 500, 500, ea.MSG["SOMETHING_WRONG"]),
        ("", 400, 500, ea.MSG["SOMETHING_WRONG"]),
    ]:
        client.routes = {("POST", "/auth/register"): {"success": False, "status_code": status, "message": message, "data": {}}}
        result = await bridge.handle("POST", "/partialvendor/buyerRegistration", payload)
        assert result["status_code"] == expected_status and result["data"]["message"] == expected_message


@pytest.mark.asyncio
async def test_buyer_registration_uses_auth_register_and_marks_whatsapp_pending():
    registered = {"success": True, "token": "t", "user": {"id": "u-new", "orgId": "org-1", "orgName": "Acme", "name": "Bea"}}
    bridge, client = adapter({("POST", "/auth/register"): registered, ("PUT", "/bot/users/u-new/whatsapp"): {"success": True}})
    result = await bridge.handle("POST", "/partialvendor/buyerRegistration",
                                 {"name": "Bea", "email": "Bea@Acme.com", "zipCode": "411001", "organizationPhonenumber": "919876543210"})
    assert result["statusCode"] == "200" and result["status"] == "Success" and result["message"] == ea.MSG["BUYER_CREATED"]
    assert result["data"] == {
        "confirmationFlag": True, "id": "u-new", "email": "bea@acme.com", "uniqueId": None, "orgId": "org-1", "companyId": None,
        "companyName": "Acme", "selfClient": True, "fullName": "Bea", "verificationStatus": "PENDING_EMAIL_VERIFICATION",
    }
    sent = client.sent("POST", "/auth/register")[0]["json_data"]
    assert sent["mobile"] == "+919876543210" and sent["role"] == "buyer" and sent["orgName"] == "Bea" and len(sent["password"]) >= 20
    assert client.sent("PUT", "/bot/users/u-new/whatsapp")[0]["json_data"] == {"registeredViaWhatsApp": True, "verificationStatus": "PENDING_EMAIL_VERIFICATION"}


@pytest.mark.asyncio
async def test_registration_is_sent_once_with_its_own_timeout():
    registered = {"success": True, "token": "t", "user": {"id": "u-new", "orgId": "org-1"}}
    bridge, client = adapter({("POST", "/auth/register"): registered}, equa_register_timeout_seconds=45.0)
    await bridge.handle("POST", "/partialvendor/buyerRegistration", {"email": "a@b.com", "organizationPhonenumber": "919876543210"})
    sent = client.sent("POST", "/auth/register")[0]
    assert sent["total_timeout"] == 45.0 and sent["max_retries"] == 1


TIMED_OUT = {"success": False, "status_code": 500, "message": "Request timeout. Please check your connection and try again.", "data": None}


@pytest.mark.asyncio
async def test_registration_without_an_answer_continues_with_the_account_it_created():
    # The response never arrived, but EQUA saved the account: carry on as a success.
    created = {"id": "u-new", "email": "Bea@Acme.com", "role": "buyer", "orgId": "org-1", "orgName": "Acme", "name": "Bea"}
    routes = {
        ("POST", "/auth/register"): TIMED_OUT,
        ("GET", "/bot/users"): {"success": True, "data": ["junk", {"id": "u-old", "email": "bea@acme.com", "role": "vendor"}, created]},
        **session_route(created),
    }
    bridge, client = adapter(routes)
    result = await bridge.handle("POST", "/partialvendor/buyerRegistration",
                                 {"name": "Bea", "email": "bea@acme.com", "organizationPhonenumber": "919876543210"})
    assert result["message"] == ea.MSG["BUYER_CREATED"] and result["data"]["id"] == "u-new" and result["data"]["orgId"] == "org-1"
    assert result["data"]["companyName"] == "Acme" and result["data"]["fullName"] == "Bea"
    assert client.sent("GET", "/bot/users")[0]["params"] == {"phone": "919876543210"}
    assert client.sent("POST", "/bot/session")[0]["json_data"] == {"id": "u-new"}
    assert client.sent("PUT", "/bot/users/u-new/whatsapp")[0]["json_data"] == {"registeredViaWhatsApp": True, "verificationStatus": "PENDING_EMAIL_VERIFICATION"}


@pytest.mark.asyncio
async def test_seller_registration_without_an_answer_still_classifies_products():
    created = {"id": "u-seller", "email": "seller@steel.com", "role": "vendor", "vendorId": "v-1", "orgId": "org-s", "name": "Sam"}
    routes = {
        ("POST", "/auth/register"): TIMED_OUT,
        ("GET", "/bot/users"): {"success": True, "data": [created]},
        ("POST", "/rfqs/ingest"): {"success": True, "data": {"extractedEntities": [{"majorCategory": "Steel", "minorCategory": "Plates"}]}},
        **session_route(created),
    }
    bridge, client = adapter(routes)
    result = await bridge.handle("POST", "/partialvendor/sellerRegistration", SELLER_PAYLOAD)
    assert result["message"] == ea.MSG["SELLER_CREATED"] and result["data"]["orgId"] == "v-1"
    assert client.sent("PUT", "/vendors/v-1")[0]["headers"] == {"Authorization": "Bearer tok-u-seller"}


@pytest.mark.asyncio
async def test_registration_failure_stands_when_no_matching_account_exists():
    payload = {"email": "bea@acme.com", "organizationPhonenumber": "919876543210"}
    # Server error, and the phone only has an account with another email or role.
    bridge, client = adapter({
        ("POST", "/auth/register"): TIMED_OUT,
        ("GET", "/bot/users"): {"success": True, "data": [
            {"id": "u-1", "email": "other@acme.com", "role": "buyer"}, {"id": "u-2", "email": "bea@acme.com", "role": "vendor"}]},
    })
    result = await bridge.handle("POST", "/partialvendor/buyerRegistration", payload)
    assert result["status_code"] == 500 and result["data"]["message"] == ea.MSG["SOMETHING_WRONG"]
    assert client.sent("PUT", "/bot/users/u-1/whatsapp") == [] and client.sent("PUT", "/bot/users/u-2/whatsapp") == []

    # A clear rejection is final: no lookup, and the message is passed on as before.
    client.calls.clear()
    client.routes[("POST", "/auth/register")] = {"success": False, "status_code": 400, "message": "An account already exists.", "data": {}}
    result = await bridge.handle("POST", "/partialvendor/buyerRegistration", payload)
    assert result["status_code"] == 409 and result["data"]["message"] == ea.MSG["ALREADY_EXISTS"]
    assert client.sent("GET", "/bot/users") == []

    # No status at all is treated like a server error.
    client.routes[("POST", "/auth/register")] = {"success": False, "message": ""}
    client.routes[("GET", "/bot/users")] = {"success": False}
    result = await bridge.handle("POST", "/partialvendor/buyerRegistration", payload)
    assert result["status_code"] == 500 and len(client.sent("GET", "/bot/users")) == 1


@pytest.mark.asyncio
async def test_seller_registration_classifies_products_onto_the_vendor_record():
    registered = {"success": True, "token": "t-seller", "user": {"id": "u-seller", "orgId": "org-s", "name": "Sam"}}
    ingest = {"success": True, "data": {"extractedEntities": [
        {"majorCategory": "Steel", "minorCategory": "Plates"}, {"majorCategory": "Steel", "minorCategory": ""}, {"majorCategory": "Fasteners", "minorCategory": "Bolts"}]}}
    routes = {
        ("POST", "/auth/register"): registered,
        ("GET", "/bot/users"): {"success": True, "data": [{"id": "other", "vendorId": "v-x"}, {"id": "u-seller", "vendorId": "v-1"}]},
        ("POST", "/rfqs/ingest"): ingest,
    }
    bridge, client = adapter(routes)
    result = await bridge.handle("POST", "/partialvendor/sellerRegistration", SELLER_PAYLOAD)
    assert result["message"] == ea.MSG["SELLER_CREATED"] and result["data"]["orgId"] == "v-1" and result["data"]["selfClient"] is False
    assert result["data"]["companyName"] == "Steel Co Pvt Ltd"
    assert client.sent("POST", "/rfqs/ingest")[0]["json_data"]["lineItems"] == [
        {"itemName": "Plates", "category": "Plates", "majorCategory": None},
        {"itemName": "Hardware", "category": None, "majorCategory": "Hardware"},
    ]
    assert client.sent("PUT", "/vendors/v-1")[0]["json_data"] == {"majorCategory": "Steel", "minorCategories": ["Plates", "Bolts"]}
    assert client.sent("PUT", "/vendors/v-1/categories")[0]["json_data"] == {"vendorSelectedCategories": ["Steel", "Fasteners", "Plates", "Bolts"]}
    assert client.sent("PUT", "/vendors/v-1")[0]["headers"] == {"Authorization": "Bearer t-seller"}

    # Nothing classifiable: products text is tried, then the defaults stay.
    client.calls.clear()
    client.routes[("POST", "/rfqs/ingest")] = {"success": False, "status_code": 422, "message": "No usable line items"}
    await bridge.handle("POST", "/partialvendor/sellerRegistration", {**SELLER_PAYLOAD, "divisionCategories": None})
    assert client.sent("POST", "/rfqs/ingest")[0]["json_data"]["lineItems"] == [{"itemName": "steel plates"}]
    assert client.sent("PUT", "/vendors/v-1") == []

    client.calls.clear()
    await bridge.handle("POST", "/partialvendor/sellerRegistration", {**SELLER_PAYLOAD, "divisionCategories": [], "details": ""})
    assert client.sent("POST", "/rfqs/ingest") == []

    # No vendor record yet: the organisation id is returned, as before.
    client.routes[("GET", "/bot/users")] = {"success": True, "data": []}
    assert (await bridge.handle("POST", "/partialvendor/sellerRegistration", SELLER_PAYLOAD))["data"]["orgId"] == "org-s"


@pytest.mark.asyncio
async def test_otp_uses_auth_request_and_verify_with_one_phone_spelling():
    verified = {"success": True, "token": "t-v", "user": {"id": "u-buyer", "role": "buyer"}}
    bridge, client = adapter({("POST", "/auth/request-otp"): {"success": True}, ("POST", "/auth/verify-otp"): verified})
    sent = await bridge.handle("POST", "/rest/users/sendOtp", {"email": "a@b.com", "organizationPhonenumber": "919876543210"})
    assert sent["statusCode"] == "200" and sent["status"] == "Success" and sent["message"] == ea.MSG["OTP_SENT"]
    valid = await bridge.handle("POST", "/rest/users/validateOtp", {"email": "a@b.com", "emailOtp": 123456, "organizationPhonenumber": "+919876543210"})
    assert valid["message"] == ea.MSG["OTP_VALID"]
    assert client.sent("POST", "/auth/request-otp")[0]["json_data"]["mobile"] == client.sent("POST", "/auth/verify-otp")[0]["json_data"]["mobile"] == "+919876543210"
    assert client.sent("POST", "/auth/verify-otp")[0]["json_data"]["code"] == "123456"
    assert client.sent("PUT", "/bot/users/u-buyer/whatsapp")[0]["json_data"] == {"verificationStatus": "EMAIL_VERIFIED"}
    assert bridge._sessions["a@b.com|+919876543210"][0] == "t-v"

    client.routes = {("POST", "/auth/verify-otp"): {"success": True, "user": {}}}
    assert (await bridge.handle("POST", "/rest/users/validateOtp", {"email": "a@b.com", "emailOtp": "1"}))["statusCode"] == "200"

    client.routes = {}
    failed_send = await bridge.handle("POST", "/rest/users/sendOtp", {})
    assert failed_send["status_code"] == 404 and failed_send["data"]["message"] == ea.MSG["OTP_NOT_SENT"]
    failed_check = await bridge.handle("POST", "/rest/users/validateOtp", {})
    assert failed_check["status_code"] == 400 and failed_check["data"]["status"] == "Failure"


# ── Email ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_email_maps_qua_results():
    bridge, client = adapter({("POST", "/bot/email"): {"success": True, "sent": True}})
    ok = await bridge.handle("POST", "/rest/gmt/sendEmail", {"to": ["ops@x.com"], "subject": "S", "body": "B",
                                                              "attachments": [{"fileName": "r.xlsx", "fileData": "AA=="}]})
    assert ok["statusCode"] == "200" and ok["status"] == "Success"
    assert client.sent("POST", "/bot/email")[0]["json_data"] == {"to": ["ops@x.com"], "cc": [], "bcc": [], "subject": "S", "text": "B",
                                                                "attachments": [{"fileName": "r.xlsx", "fileData": "AA=="}]}
    client.routes = {("POST", "/bot/email"): {"success": False, "status_code": 400, "data": {"invalid": ["bad"]}}}
    partial = await bridge.handle("POST", "/rest/gmt/sendEmail", {"to": ["bad"]})
    assert partial["statusCode"] == "206" and partial["status"] == "Partial Failure" and partial["errorMsg"] == ["bad"]
    client.routes = {}
    failed = await bridge.handle("POST", "/rest/gmt/sendEmail", {})
    assert failed["status_code"] == 500 and failed["data"]["message"] == ea.MSG["EMAIL_FAILED"]


# ── Buyer RFQs ──────────────────────────────────────────────────────────────

GMT_RFQ = {
    "createdBy": "u-buyer", "projectDesc": "Steel plates", "deliveryDate": "2099-03-01T00:00:00.000Z",
    "rfqItem": [
        {"description": "Plate", "quantity": "2", "unitofMeasures": "Nos", "brand": "Tata", "remarks": "10mm"},
        {"description": "Rod", "quantity": "1", "remarks": "Rod"},
        {"quantity": "1", "remarks": "N/A"},
    ],
    "clientdeliverylocationrfq": [{"city": "Pune", "state": "Maharashtra", "pincode": "411001"}],
    "rfqDocument": [{"fileName": "spec.pdf", "file": "JVBERi0="}, {"fileName": "virus.exe", "file": "TVo="}, {"fileName": "empty.pdf"}, None],
}
DRAFT = {"success": True, "data": {"title": "Plate", "category": "Steel", "targetDeliveryDate": "2099-02-01",
                                   "extractedEntities": [{"itemName": "Plate", "majorCategory": "Steel", "targetDate": "2000-01-01"}]}}


def create_routes(overrides=None):
    routes = {**session_route(BUYER), ("POST", "/rfqs/ingest"): DRAFT,
              ("POST", "/rfqs/attachments"): lambda kw: {"success": True, "data": {"id": "att-1"}} if kw["json_data"]["fileName"] == "spec.pdf" else {"success": False, "message": "unsupported"},
              ("POST", "/rfqs"): {"success": True, "data": {"id": "rfq-9", "rfqNumber": "RFQ9"}}}
    routes.update(overrides or {})
    return routes


@pytest.mark.asyncio
async def test_create_rfq_goes_ingest_attachments_create_as_the_buyer():
    bridge, client = adapter(create_routes())
    result = await bridge.handle("POST", "/rest/gmt/createRFQByClient", GMT_RFQ)
    assert result["statusCode"] == "200" and result["status"] == "Success" and result["data"] == {"rfqId": "RFQ9", "rfquuid": "rfq-9"}

    assert client.sent("POST", "/rfqs/ingest")[0]["json_data"] == {"title": "Steel plates", "lineItems": [
        {"itemName": "Plate", "quantity": "2", "unit": "Nos", "brand": "Tata", "technicalSpecs": "10mm"},
        {"itemName": "Rod", "quantity": "1", "unit": None, "brand": "", "technicalSpecs": ""},
        {"itemName": "", "quantity": "1", "unit": None, "brand": "", "technicalSpecs": ""},
    ]}
    assert [c["json_data"]["mimeType"] for c in client.sent("POST", "/rfqs/attachments")] == ["application/pdf", "application/octet-stream"]
    create = client.sent("POST", "/rfqs")[0]
    assert create["headers"] == {"Authorization": "Bearer tok-u-buyer"}
    assert create["total_timeout"] == 90.0 and create["max_retries"] == 1
    assert create["json_data"] == {
        "title": "Steel plates", "category": "Steel", "deliveryLocation": "Pune, Maharashtra", "deliveryCity": "Pune",
        "deliveryState": "Maharashtra", "deliveryPincode": "411001", "targetDeliveryDate": "2099-03-01",
        "extractedEntities": [{"itemName": "Plate", "majorCategory": "Steel", "targetDate": "2099-03-01"}],
        "attachments": [{"id": "att-1"}], "source": "whatsapp", "sourcingMode": "mode_0",
    }


@pytest.mark.asyncio
async def test_create_rfq_defaults_and_failures():
    no_category = {"success": True, "data": {**DRAFT["data"], "category": ""}}
    bridge, client = adapter(create_routes({("POST", "/rfqs/ingest"): no_category}), equa_rfq_sourcing_mode="mode_2")
    await bridge.handle("POST", "/rest/gmt/createRFQByClient", {"user": "u-buyer", "deliveryDate": "2001-01-01",
                                                                 "rfqItem": [{"description": "Plate"}], "clientdeliverylocationrfq": [{"pincode": "411001"}]})
    body = client.sent("POST", "/rfqs")[0]["json_data"]
    assert body["targetDeliveryDate"] == "2099-02-01" and body["deliveryLocation"] == "411001" and body["title"] == "Plate"
    assert "category" not in body and body["sourcingMode"] == "mode_2"
    await bridge.handle("POST", "/rest/gmt/createRFQByClient", {"user": "u-buyer", "projectDesc": "X", "clientdeliverylocationrfq": [None]})
    assert client.sent("POST", "/rfqs")[1]["json_data"]["title"] == "X.."

    seller_bridge, _ = adapter(session_route(SELLER))
    refused = await seller_bridge.handle("POST", "/rest/gmt/createRFQByClient", GMT_RFQ)
    assert refused["statusCode"] == "500" and refused["status"] == "Failure" and refused["data"] == {} and "No buyer account" in refused["errorMsg"][0]

    empty = {"success": True, "data": {"extractedEntities": []}}
    for overrides, reason in [
        ({("POST", "/rfqs/ingest"): empty}, "The RFQ has no line items with a description."),
        ({("POST", "/rfqs/ingest"): {"success": False, "message": "ingest down"}}, "ingest down"),
        ({("POST", "/rfqs"): {"success": False, "message": "plan does not include mode"}}, "plan does not include mode"),
        ({("POST", "/rfqs"): {"success": True, "data": None}}, "RFQ creation failed"),
    ]:
        failing, _ = adapter(create_routes(overrides))
        assert (await failing.handle("POST", "/rest/gmt/createRFQByClient", GMT_RFQ))["errorMsg"] == [reason]


@pytest.mark.asyncio
async def test_buyer_rfq_status_by_id_latest_and_none():
    rfqs = [rfq(), rfq(id="rfq-2", rfqNumber="ACM2", quotesCount=2), rfq(id="rfq-3", rfqNumber="RFQ3", status="PO Generated")]
    bridge, client = adapter({**session_route(BUYER), ("GET", "/rfqs"): {"success": True, "data": rfqs}})
    by_id = await bridge.handle("POST", "/rest/gmt/rfqStatus", {"clientId": "u-buyer", "rfqIds": ["RFQ1", "ACM2", "3", "RFQ999"]})
    assert by_id["data"] == [
        {"rfqid": "RFQ1", "status": "Published To Multiple Sellers"},
        {"rfqid": "ACM2", "status": "Quotation_Received"},
        {"rfqid": "RFQ3", "status": "PO Generated"},
        {"rfqid": "RFQ999", "status": "Invalid RFQID"},
    ]
    client.routes[("GET", "/rfqs")] = {"success": True, "data": [rfq(id=f"r{n}", rfqNumber=None, createdAt=utc_stamp(n)) for n in range(4)]}
    assert [d["rfqid"] for d in (await bridge.handle("POST", "/rest/gmt/rfqStatus", {"clientId": "u-buyer"}))["data"]] == ["r0", "r1", "r2"]
    client.routes[("GET", "/rfqs")] = NOT_FOUND
    assert (await bridge.handle("POST", "/rest/gmt/rfqStatus", {"clientId": "u-buyer"}))["data"] == [{"rfqid": None, "status": "Not Found"}]
    nobody, _ = adapter()
    assert (await nobody.handle("POST", "/rest/gmt/rfqStatus", {}))["data"][0]["status"] == "Not Found"


# ── Seller RFQs ─────────────────────────────────────────────────────────────

def seller_routes(vendor=None, rfqs=None, by_id=None, extra=None):
    vendor = vendor if vendor is not None else {"id": "v-1", "email": "Seller@steel.com", "unlockedRfqIds": []}
    by_id = by_id or {}
    routes = {**session_route(SELLER), ("GET", "/vendors/v-1"): {"success": True, "data": vendor},
              ("GET", "/rfqs"): {"success": True, "data": rfqs or []}}
    for key, value in by_id.items():
        routes[("GET", f"/rfqs/{key}")] = {"success": True, "data": value}
    routes.update(extra or {})
    return routes


@pytest.mark.asyncio
async def test_seller_feed_lists_new_open_untaken_rfqs():
    rfqs = [rfq(id="new"), rfq(id="old", createdAt=utc_stamp(10)), rfq(id="closed", status="Closed"), rfq(id="taken"), rfq(id="fwd", rfqNumber="RFQFWD")]
    redis = FakeRedis()
    redis.store["equa:forwarded_rfqs:v-1"] = ["RFQFWD"]
    bridge, _ = adapter(seller_routes(vendor={"id": "v-1", "unlockedRfqIds": ["taken"]}, rfqs=rfqs), redis=redis)
    feed = await bridge.handle("POST", "/rest/gmt/getRfqByCategory", {"id": "v-1"})
    assert feed["data"]["count"] == 2 and [r["id"] for r in feed["data"]["rfqs"]] == ["new"]
    nobody, _ = adapter()
    assert (await nobody.handle("POST", "/rest/gmt/getRfqByCategory", {"id": "x"}))["data"] == {"rfqs": [], "count": 0}
    buyer_only, _ = adapter(session_route(BUYER))
    assert (await buyer_only.handle("POST", "/rest/gmt/getRfqByCategory", {"id": "u-buyer"}))["data"]["count"] == 0


@pytest.mark.asyncio
async def test_seller_credits_free_subscribed_and_unknown():
    bridge, client = adapter(seller_routes(extra={("GET", "/vendors/rfq-credits"): {"success": True, "data": {"freeCreditsRemaining": 3, "isSubscribed": False}}}))
    assert (await bridge.handle("POST", "/rest/gmt/getSellerRfqCredits", {"id": "v-1"}))["data"] == {"creditsAvailable": 3}
    client.routes[("GET", "/vendors/rfq-credits")] = {"success": True, "data": {"isSubscribed": True, "subscriptionPlan": "select", "freeCreditsRemaining": 0}}
    assert (await bridge.handle("POST", "/rest/gmt/getSellerRfqCredits", {"id": "v-1"}))["data"]["creditsAvailable"] == 100
    client.routes[("GET", "/vendors/rfq-credits")] = {"success": True, "data": {"isSubscribed": True, "subscriptionPlan": "premium", "freeCreditsRemaining": 2}}
    assert (await bridge.handle("POST", "/rest/gmt/getSellerRfqCredits", {"id": "v-1"}))["data"]["creditsAvailable"] == 2
    client.routes[("GET", "/vendors/rfq-credits")] = NOT_FOUND
    assert (await bridge.handle("POST", "/rest/gmt/getSellerRfqCredits", {"id": "v-1"}))["data"]["creditsAvailable"] == 0
    nobody, _ = adapter()
    assert (await nobody.handle("POST", "/rest/gmt/getSellerRfqCredits", {"id": "x"}))["data"]["creditsAvailable"] == 0


@pytest.mark.asyncio
async def test_seller_status_and_open_rfqs_from_unlocked_and_forwarded():
    vendor = {"id": "v-1", "email": "Seller@steel.com", "unlockedRfqIds": ["rfq-1", "RFQ1", "rfq-2", "gone"]}
    by_id = {
        "rfq-1": rfq(), "RFQ1": rfq(),
        "rfq-2": rfq(id="rfq-2", rfqNumber=None, quotes=[None, {"vendorEmail": "seller@steel.com"}]),
        "rfq-c": rfq(id="rfq-c", rfqNumber="RFQC", status="Closed"),
        "rfq-d": rfq(id="rfq-d", rfqNumber=None, title="", category=None, createdAt=None, targetDeliveryDate=None,
                     deliveryCity="", deliveryState="", deliveryLocation=""),
    }
    redis = FakeRedis()
    redis.store["equa:forwarded_rfqs:v-1"] = ["rfq-c", "rfq-d"]
    bridge, _ = adapter(seller_routes(vendor=vendor, by_id=by_id), redis=redis)

    latest = await bridge.handle("POST", "/rest/gmt/getSellerRfqStatus", {"clientId": "v-1"})
    assert latest["data"] == [
        {"rfqid": "rfq-d", "status": "Downloaded"},
        {"rfqid": "RFQC", "status": "Downloaded"},
        {"rfqid": "rfq-2", "status": "Quote_Submitted"},
        {"rfqid": "RFQ1", "status": "Downloaded"},
    ]
    asked = await bridge.handle("POST", "/rest/gmt/getSellerRfqStatus", {"clientId": "v-1", "rfqIds": ["RFQ1", "rfq-2", "RFQ5"]})
    assert asked["data"] == [
        {"rfqid": "RFQ1", "status": "Downloaded"},
        {"rfqid": "RFQrfq-2", "status": "Quote_Submitted"},
        {"rfqid": "RFQ5", "status": "Un_Known"},
    ]

    open_rfqs = await bridge.handle("POST", "/rest/gmt/getOpenRfqs", {"id": "v-1"})
    assert open_rfqs["total_count"] == 2 and open_rfqs["metadata"]["seller_id"] == "v-1"
    first, second = open_rfqs["open_rfqs"]
    assert first == {"rfq_id": "rfq-d", "project_desc": "", "category": None, "location": None, "submission_deadline": None,
                     "email_sent_date": None, "status": "open_for_bidding"}
    assert second["rfq_id"] == "RFQ1" and second["location"] == "Pune,Maharashtra" and second["days_remaining"] > 0
    assert second["submission_deadline"].endswith("Z")

    nobody, _ = adapter()
    assert (await nobody.handle("POST", "/rest/gmt/getSellerRfqStatus", {}))["data"] == [{"rfqid": None, "status": "Not Found"}]
    assert (await nobody.handle("POST", "/rest/gmt/getOpenRfqs", {}))["open_rfqs"] == []


@pytest.mark.asyncio
async def test_missing_vendor_record_and_redis_errors_degrade_to_nothing_taken():
    bridge, _ = adapter(seller_routes(extra={("GET", "/vendors/v-1"): NOT_FOUND}), redis=FakeRedis(fail=True))
    assert (await bridge.handle("POST", "/rest/gmt/getSellerRfqStatus", {"clientId": "v-1"}))["data"][0]["status"] == "Not Found"
    await bridge._remember_forwarded("v-1", ["x"])  # logged, not raised


@pytest.mark.asyncio
async def test_forward_unlocks_through_download_and_emails_the_rfq():
    def download(kwargs):
        return {"success": True, "data": {"subject": "[OFFICIAL RFQ] RFQ1", "htmlBody": "<p>RFQ</p>"}}

    routes = seller_routes(extra={
        ("GET", "/rfqs/RFQ1"): {"success": True, "data": rfq()},
        ("GET", "/rfqs/RFQ2"): {"success": True, "data": rfq(id="rfq-2")},
        ("GET", "/rfqs/RFQ3"): {"success": True, "data": rfq(id="rfq-3")},
        ("GET", "/rfqs/RFQ4"): {"success": True, "data": rfq(id="rfq-4")},
        ("POST", "/rfqs/RFQ1/download"): download,
        ("POST", "/rfqs/RFQ2/download"): {"success": False, "status_code": 403, "message": "credits"},
        ("POST", "/rfqs/RFQ3/download"): {"success": False, "status_code": 500, "message": "store failed"},
        ("POST", "/rfqs/RFQ4/download"): {"success": True, "data": None},
        ("POST", "/bot/email"): lambda kw: {"success": kw["json_data"]["subject"] == "[OFFICIAL RFQ] RFQ1"},
    })
    redis = FakeRedis()
    bridge, client = adapter(routes, redis=redis)
    result = await bridge.handle("POST", "/rest/gmt/forwardRfqsToVendor",
                                 {"rfqIds": ["RFQ1", "MISSING", "RFQ2", "RFQ3", "RFQ4"], "email": "seller@steel.com", "sellerId": "v-1"})
    assert result["success"] is True
    assert result["results"]["successful"] == [{"rfq_id": "RFQ1", "email_sent": True, "message": "Email sent successfully"}]
    assert [(f["rfq_id"], f["error_code"]) for f in result["results"]["failed"]] == [
        ("MISSING", "RFQ_NOT_FOUND"), ("RFQ2", "NO_CREDITS"), ("RFQ3", "SYSTEM_ERROR"), ("RFQ4", "SYSTEM_ERROR")]
    assert result["summary"] == {"total_requests": 5, "successful": 1, "failed": 4}
    assert client.sent("POST", "/bot/email")[0]["json_data"] == {"to": ["seller@steel.com"], "subject": "[OFFICIAL RFQ] RFQ1", "html": "<p>RFQ</p>"}
    assert redis.store["equa:forwarded_rfqs:v-1"] == ["RFQ1", "RFQ4"]

    bad_email = await bridge.handle("POST", "/rest/gmt/forwardRfqsToVendor", {"rfqIds": ["RFQ1"], "email": "nope", "sellerId": "v-1"})
    assert bad_email["results"]["failed"][0]["error_code"] == "INVALID_EMAIL"
    nobody, _ = adapter()
    no_org = await nobody.handle("POST", "/rest/gmt/forwardRfqsToVendor", {"rfqIds": ["RFQ1"], "email": "s@x.com", "sellerId": "x"})
    assert no_org["results"]["failed"][0]["error_code"] == "ORG_NOT_FOUND"
    assert (await nobody.handle("POST", "/rest/gmt/forwardRfqsToVendor", {}))["summary"]["total_requests"] == 0


# ── Plans, payment link, opt-out ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_plans_in_qua_shape():
    plans = [{"id": "connect", "name": "Connect Model (Marketplace Expansion)", "description": "d", "features": ["f"],
              "amount": 6355.08, "amountWithGst": 7499, "rfqBundleSize": 50, "periodMonths": 3}]
    bridge, client = adapter({("GET", "/bot/vendor-plans"): {"success": True, "data": plans}})
    result = await bridge.handle("GET", "/rest/gmt/getSubscriptionPlans")
    assert result["statusCode"] == "200" and result["status"] == "Success"
    plan = result["data"]["plans"][0]
    assert plan["id"] == "connect" and plan["planName"] == "Connect" and plan["subscriptionPrice"] == plan["launchOfferPrice"] == 6355.08
    assert plan["rfqBundleSize"] == 50 and plan["subscriptionPeriodMonths"] == 3
    client.routes = {}
    assert (await bridge.handle("GET", "/rest/gmt/getSubscriptionPlans"))["status"] == "Failure"


@pytest.mark.asyncio
async def test_payment_link_via_vendor_payment_link_api():
    bridge, client = adapter({**session_route(SELLER),
                              ("POST", "/vendors/v-1/payment-link"): {"success": True, "data": {"paymentUrl": "https://pay", "paymentLinkId": "pl-1", "status": "created"}}})
    args = {"planId": "connect", "userEmail": "seller@steel.com", "userPhone": "9876543210"}
    link = await bridge.handle("POST", "/rest/api/payments/link/generate", args)
    assert link["success"] is True and {k: link[k] for k in ("id", "paymentUrl", "status")} == {"id": "pl-1", "paymentUrl": "https://pay", "status": "created"}
    assert client.sent("POST", "/vendors/v-1/payment-link")[0]["json_data"] == {"plan": "connect"}

    assert (await bridge.handle("POST", "/rest/api/payments/link/generate", {}))["message"] == "Plan ID is required"
    assert (await bridge.handle("POST", "/rest/api/payments/link/generate", {"planId": "connect"}))["message"] == "User phone number is required"
    assert (await bridge.handle("POST", "/rest/api/payments/link/generate", {"planId": "connect", "userPhone": "1"}))["status_code"] == 400

    client.routes[("POST", "/vendors/v-1/payment-link")] = {"success": False, "status_code": 400, "message": "plan must be one of"}
    assert (await bridge.handle("POST", "/rest/api/payments/link/generate", args))["message"] == "plan must be one of"
    client.routes[("POST", "/vendors/v-1/payment-link")] = {"success": True, "data": None}
    assert (await bridge.handle("POST", "/rest/api/payments/link/generate", args))["status_code"] == 502

    buyer, _ = adapter(session_route(BUYER))
    assert (await buyer.handle("POST", "/rest/api/payments/link/generate", args))["status_code"] == 404


@pytest.mark.asyncio
async def test_seller_opt_out_goes_to_the_bridge():
    bridge, client = adapter({("PUT", "/bot/vendors/v-1/whatsapp-opt-out"): {"success": True, "vendorId": "v-1", "optedOut": True}})
    assert (await bridge.handle("POST", "/whatsapp/seller-opt-out", {"sellerId": "v-1", "optedOut": True}))["success"] is True
    assert client.sent("PUT", "/bot/vendors/v-1/whatsapp-opt-out")[0]["json_data"] == {"optedOut": True}
    await bridge.handle("POST", "/whatsapp/seller-opt-out", {"sellerId": "v-1", "optedOut": "yes"})
    assert client.sent("PUT", "/bot/vendors/v-1/whatsapp-opt-out")[1]["json_data"] == {"optedOut": False}


# ── Client routing ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_routes_qua_calls_to_the_adapter_only_on_equa(monkeypatch):
    import app.procucev_apis.procucev_api_client as client_mod

    settings = SimpleNamespace(gmt_base_url="https://equa.example/api", gmt_username=None, gmt_password=None, gmt_phone=None,
                               gmt_client_id=None, gmt_client_secret=None, gmt_max_retries=1, gmt_retry_delay=0,
                               bot_api_key="k", backend_platform="equa")
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(client_mod, "get_redis_service", lambda: FakeRedis())
    client = client_mod.ProcucevAPIClient()
    seen = []

    async def fake_handle(method, url, json_data=None, params=None):
        seen.append((method, url, json_data))
        return {"success": True, "routed": True}

    adapter_instance = client._equa_adapter()
    assert client._equa_adapter() is adapter_instance
    monkeypatch.setattr(adapter_instance, "handle", fake_handle)
    assert (await client.post("/rest/gmt/rfqStatus", json_data={"clientId": "u"}, require_auth=True))["routed"] is True
    assert seen == [("POST", "/rest/gmt/rfqStatus", {"clientId": "u"})]
