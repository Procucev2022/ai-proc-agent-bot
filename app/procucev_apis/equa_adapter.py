"""
Enterprise QUA adapter.

On BACKEND_PLATFORM=equa the API client hands every request here instead of
sending it. The bot's API services and conversation flow are written against
QUA's endpoints and response bodies; this module does the same work through
Enterprise QUA's existing APIs (/api/auth, /api/rfqs, /api/vendors, ...) and
returns the response QUA would have returned, so nothing above the client
changes. Only what EQUA has no API for goes through its bot bridge (/api/bot):
acting as an account by id/phone, finding accounts by phone, WhatsApp account
flags, plain email and the vendor plan list.

Responses are returned in the form ProcucevAPIClient.send_request produces: a
2xx JSON body with success/timestamp defaults, or for an error status
{success: False, status_code, message, data}.
"""

import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
RFQ_ID_PREFIX = "RFQ"
SESSION_REUSE_SECONDS = 20 * 60 * 60  # EQUA sessions last 24h
FORWARDED_TTL_SECONDS = 365 * 24 * 60 * 60

# Limits copied from the QUA queries the bot was built against.
BUYER_RECENT_RFQ_LIMIT = 3
SELLER_RECENT_RFQ_LIMIT = 5
SELLER_FEED_LIMIT = 5
SELLER_FEED_WINDOW_DAYS = 7
OPEN_RFQ_LIMIT = 3
SUBMISSION_WINDOW_DAYS = 5

# EQUA RFQ statuses: still out with vendors, quotes in, or finished.
OPEN_STATUSES = {"quotes pending", "open", "in progress", "inprogress", "active", "published"}
QUOTED_STATUSES = {"quotes received", "in evaluation"}
CLOSED_STATUSES = {"closed", "po generated", "cancelled", "canceled", "awarded", "completed"}

MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

PENDING_VERIFICATION = "PENDING_EMAIL_VERIFICATION"
EMAIL_VERIFIED = "EMAIL_VERIFIED"

# Messages QUA (p2pservices ApplicationConstants) sent, which the bot relays and logs.
MSG = {
    "USERS_FOUND": "Users fetched successfully",
    "NO_USER_FOR_PHONE": "No user found with phone number: ",
    "BUYER_CREATED": "Thanks for yor interest in procucev services.our associate will confirm/contact you for any query",
    "SELLER_CREATED": "Seller Created Successfully",
    "ALREADY_EXISTS": "User already exists with this email and phone number.",
    "SOMETHING_WRONG": "Something went wrong",
    "OTP_SENT": "OTP Generated Successfully",
    "OTP_NOT_SENT": "User not registered or OTP could not be sent.",
    "OTP_VALID": "OTP is valid. Email validated successfully.",
    "OTP_INVALID": "Invalid OTP. Email validation failed.",
    "USER_ACCEPTED": "User Accepted Successfully",
    "EMAIL_SENT": "Email sent successfully",
    "EMAIL_FAILED": "Email Sent Failed",
    "INVALID_EMAILS": "Invalid email addresses found",
    "RFQ_CREATED": "RFQ created successfully",
    "RFQ_FAILED": "Failed to create RFQ",
    "PLANS_FOUND": "Subscription plans fetched successfully",
    "NO_PLANS": "No subscription plans found",
    "BFS_UNAVAILABLE": "Buy From Stock is not available on Enterprise QUA.",
    "NOT_AVAILABLE": "This operation is not available on Enterprise QUA.",
}


# ── Response shapes ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(body: Dict[str, Any]) -> Dict[str, Any]:
    """A 2xx JSON body as the client hands it back."""
    result = dict(body)
    result.setdefault("success", True)
    result.setdefault("timestamp", _now_iso())
    return result


def _error(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """An error status as the client hands it back."""
    return {
        "success": False,
        "status_code": status,
        "message": body.get("error", "Request failed"),
        "data": body,
        "timestamp": _now_iso(),
    }


def _message(status_code: str, message: str, status: str, data: Any = None, error_msg: Any = None) -> Dict[str, Any]:
    """QUA's MessageResponse body."""
    return {
        "statusCode": status_code,
        "message": message,
        "errorMsg": error_msg,
        "timestamp": _now_iso(),
        "status": status,
        "type": None,
        "data": data,
    }


# ── Small helpers ───────────────────────────────────────────────────────────

def normalize_mobile(raw: Any) -> str:
    """+91XXXXXXXXXX for an Indian number, the digits with '+' otherwise."""
    digits = re.sub(r"\D", "", str(raw or "")).lstrip("0")
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return f"+{digits}" if digits else ""


def to_time(value: Any) -> float:
    """Epoch seconds for EQUA's 'YYYY-MM-DD HH:MM UTC' or an ISO string; 0 if unreadable."""
    if not value:
        return 0.0
    text = str(value).strip()
    text = re.sub(r" UTC$", "+00:00", text)
    text = re.sub(r"^(\d{4}-\d{2}-\d{2}) ", r"\1T", text).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def to_iso(value: Any) -> Any:
    seconds = to_time(value)
    if not seconds:
        return value or None
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _status_of(rfq: Dict[str, Any]) -> str:
    return str(rfq.get("status") or "").strip().lower()


def is_closed(rfq: Dict[str, Any]) -> bool:
    return _status_of(rfq) in CLOSED_STATUSES


def newest_first(rfqs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rfqs, key=lambda r: to_time(r.get("createdAt")), reverse=True)


def normalize_requested_ids(rfq_ids: Any) -> List[str]:
    """QUA prefixes every requested id with "RFQ" and echoes it back that way."""
    if not isinstance(rfq_ids, list):
        return []
    ids = [str(i) for i in rfq_ids if i is not None and str(i).strip()]
    return [i if i.startswith(RFQ_ID_PREFIX) else f"{RFQ_ID_PREFIX}{i}" for i in ids]


def matches_rfq_id(rfq: Dict[str, Any], requested: Any) -> bool:
    """Whether an RFQ is the one asked about, by number or id (with or without the bot's "RFQ")."""
    wanted = str(requested or "").strip().upper()
    if not wanted:
        return False
    keys = {str(k).upper() for k in (rfq.get("rfqNumber"), rfq.get("id")) if k}
    if wanted in keys:
        return True
    return wanted.startswith(RFQ_ID_PREFIX) and wanted[len(RFQ_ID_PREFIX):] in keys


def line_items_of(rfq: Dict[str, Any]) -> List[Dict[str, Any]]:
    entities = rfq.get("extractedEntities")
    if isinstance(entities, list) and entities:
        return entities
    return rfq.get("lineItems") if isinstance(rfq.get("lineItems"), list) else []


def _last_segment(text: Any) -> str:
    parts = [p.strip() for p in str(text or "").split(",") if p.strip()]
    return parts[-1] if len(parts) > 1 else ""


def to_qua_rfq(rfq: Dict[str, Any]) -> Dict[str, Any]:
    """An EQUA RFQ in the shape QUA's Rfq entity serialised to."""
    return {
        "id": rfq.get("id"),
        "rfqId": rfq.get("rfqNumber") or rfq.get("id"),
        "projectDesc": rfq.get("title") or "",
        "category": rfq.get("category"),
        "status": rfq.get("status"),
        "createdTS": to_iso(rfq.get("createdAt")),
        "deliveryDate": to_iso(rfq.get("targetDeliveryDate") or rfq.get("deadline")),
        "clientdeliverylocationrfq": [{
            "city": rfq.get("deliveryCity") or "",
            "state": rfq.get("deliveryState") or _last_segment(rfq.get("deliveryLocation")),
            "pincode": rfq.get("deliveryPincode") or "",
            "location": rfq.get("deliveryLocation") or "",
        }],
        "rfqItem": [
            {
                "serialNo": 1001 + index,
                "description": item.get("itemName") or item.get("description") or "",
                "category": item.get("majorCategory") or item.get("category"),
                "quantity": item.get("quantity"),
                "unitofMeasures": item.get("unit") or "",
                "brand": item.get("brand") or "",
                "remarks": item.get("technicalSpecs") or "",
            }
            for index, item in enumerate(line_items_of(rfq))
        ],
    }


def buyer_rfq_status(rfq: Dict[str, Any]) -> str:
    """QUA's buyer status words. EQUA hides V0 quotes from buyers but keeps the count and status."""
    status = _status_of(rfq)
    if (rfq.get("quotesCount") or 0) > 0 or rfq.get("quotes") or status in QUOTED_STATUSES:
        return "Quotation_Received"
    if not status:
        return "Unknown"
    return "Published To Multiple Sellers" if status in OPEN_STATUSES else rfq.get("status")


def _unique(values: List[Any]) -> List[Any]:
    seen, result = set(), []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ── Adapter ─────────────────────────────────────────────────────────────────

class EquaAdapter:
    """Answers QUA endpoints with Enterprise QUA's APIs."""

    def __init__(self, client: Any):
        self.client = client
        self.settings = client.settings
        self._sessions: Dict[str, Tuple[str, Dict[str, Any], float]] = {}
        self._routes = {
            ("POST", "/authenticate"): self._authenticate,
            ("POST", "/partialvendor/buyerRegistration"): lambda body: self._register(body, seller=False),
            ("POST", "/partialvendor/sellerRegistration"): lambda body: self._register(body, seller=True),
            ("POST", "/rest/users/sendOtp"): self._send_otp,
            ("POST", "/rest/users/validateOtp"): self._validate_otp,
            ("POST", "/rest/gmt/acceptSelfRegisterClient"): self._accept_client,
            ("POST", "/rest/gmt/sendEmail"): self._send_email,
            ("POST", "/rest/gmt/createRFQByClient"): self._create_rfq,
            ("POST", "/rest/gmt/rfqStatus"): self._rfq_status,
            ("POST", "/rest/gmt/getRfqByCategory"): self._rfqs_by_category,
            ("POST", "/rest/gmt/getSellerRfqCredits"): self._seller_credits,
            ("POST", "/rest/gmt/getSellerRfqStatus"): self._seller_rfq_status,
            ("POST", "/rest/gmt/forwardRfqsToVendor"): self._forward_rfqs,
            ("POST", "/rest/gmt/getOpenRfqs"): self._open_rfqs,
            ("GET", "/rest/gmt/getSubscriptionPlans"): self._subscription_plans,
            ("POST", "/rest/api/payments/link/generate"): self._payment_link,
            ("POST", "/whatsapp/seller-opt-out"): self._seller_opt_out,
        }

    # ── Dispatch ───────────────────────────────────────────────────────────

    def _path(self, url_or_endpoint: str) -> str:
        base = (self.client.base_url or "").rstrip("/")
        path = url_or_endpoint[len(base):] if base and url_or_endpoint.startswith(base) else url_or_endpoint
        return "/" + path.split("?", 1)[0].lstrip("/")

    async def handle(self, method: str, url_or_endpoint: str, json_data: Any = None, params: Any = None) -> Dict[str, Any]:
        path = self._path(url_or_endpoint)
        method = method.upper()
        if method == "GET" and path.startswith("/partialvendor/getUsersByPhoneNumber/"):
            return await self._users_by_phone(path.rsplit("/", 1)[-1])
        if path.startswith("/rest/bfs/"):
            return _error(501, {"error": MSG["BFS_UNAVAILABLE"]})
        handler = self._routes.get((method, path))
        if handler is None:
            logger.warning(f"[EQUA] No Enterprise QUA equivalent for {method} {path}")
            return _error(404, {"error": MSG["NOT_AVAILABLE"]})
        return await handler(json_data if isinstance(json_data, dict) else {})

    # ── EQUA calls ─────────────────────────────────────────────────────────

    async def _call(self, method: str, endpoint: str, token: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return await self.client.send_request(method, endpoint, headers=headers, raw=True, **kwargs)

    async def _session(self, account_id: Any = None, email: Any = None, phone: Any = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """A session (token, user) for the account, reused until shortly before it expires."""
        key = f"{email}|{normalize_mobile(phone)}" if email and phone else str(account_id or "")
        if not key or key == "None":
            return None, None
        cached = self._sessions.get(key)
        if cached and cached[2] > time.time():
            return cached[0], cached[1]
        body = {"email": email, "phone": normalize_mobile(phone)} if email and phone else {"id": str(account_id)}
        response = await self._call("POST", "/bot/session", json_data=body)
        if not response.get("success") or not response.get("token"):
            logger.warning(f"[EQUA] No session for {key}: {response.get('message') or response.get('error')}")
            return None, None
        self._remember(key, response["token"], response.get("user") or {})
        return response["token"], response.get("user") or {}

    def _remember(self, key: str, token: str, user: Dict[str, Any]) -> None:
        expires = time.time() + SESSION_REUSE_SECONDS
        self._sessions[key] = (token, user, expires)
        for alias in (user.get("id"), user.get("vendorId")):
            if alias:
                self._sessions[str(alias)] = (token, user, expires)

    async def _rfqs_for(self, token: str) -> List[Dict[str, Any]]:
        response = await self._call("GET", "/rfqs", token)
        data = response.get("data")
        return data if response.get("success") and isinstance(data, list) else []

    async def _vendor(self, token: str, vendor_id: Any) -> Dict[str, Any]:
        response = await self._call("GET", f"/vendors/{vendor_id}", token)
        data = response.get("data")
        return data if response.get("success") and isinstance(data, dict) else {}

    def _forwarded_key(self, vendor_id: Any) -> str:
        return f"equa:forwarded_rfqs:{vendor_id}"

    async def _forwarded(self, vendor_id: Any) -> List[str]:
        try:
            stored = await self.client.redis_service.get(self._forwarded_key(vendor_id), as_json=True)
        except Exception as e:
            logger.warning(f"[EQUA] Could not read forwarded RFQs for {vendor_id}: {e}")
            return []
        return stored if isinstance(stored, list) else []

    async def _remember_forwarded(self, vendor_id: Any, rfq_ids: List[str]) -> None:
        try:
            merged = _unique(await self._forwarded(vendor_id) + rfq_ids)
            await self.client.redis_service.set(self._forwarded_key(vendor_id), merged, ex=FORWARDED_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[EQUA] Could not record forwarded RFQs for {vendor_id}: {e}")

    async def _taken_rfqs(self, token: str, vendor_id: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """The seller's vendor record and the RFQs they took (unlocked or forwarded), latest first."""
        vendor = await self._vendor(token, vendor_id)
        unlocked = vendor.get("unlockedRfqIds") if isinstance(vendor.get("unlockedRfqIds"), list) else []
        ids = list(reversed(_unique(unlocked + await self._forwarded(vendor_id))))
        rfqs, seen = [], set()
        for rfq_id in ids:
            response = await self._call("GET", f"/rfqs/{rfq_id}", token)
            rfq = response.get("data") if response.get("success") else None
            if isinstance(rfq, dict) and rfq.get("id") not in seen:
                seen.add(rfq.get("id"))
                rfqs.append(rfq)
        return vendor, rfqs

    @staticmethod
    def _taken_by(vendor: Dict[str, Any], vendor_id: Any, forwarded: List[str], rfq: Dict[str, Any]) -> bool:
        taken = {str(i).lower() for i in (vendor.get("unlockedRfqIds") or []) + forwarded}
        return any(str(k).lower() in taken for k in (rfq.get("id"), rfq.get("rfqNumber")) if k)

    @staticmethod
    def _quoted_by(vendor: Dict[str, Any], vendor_id: Any, rfq: Dict[str, Any]) -> bool:
        email = str(vendor.get("email") or "").lower()
        for quote in rfq.get("quotes") or []:
            if quote and (quote.get("vendorId") == vendor_id or (email and str(quote.get("vendorEmail") or "").lower() == email)):
                return True
        return False

    # ── Accounts ───────────────────────────────────────────────────────────

    async def _authenticate(self, body: Dict[str, Any]) -> Dict[str, Any]:
        token, _ = await self._session(email=body.get("username"), phone=body.get("phone"))
        if not token:
            return _ok({"status": "error", "message": "Invalid phone number and username", "methodType": "validate"})
        return _ok({"status": "success", "access_token": token, "token_type": "Bearer", "expires_in": SESSION_REUSE_SECONDS, "methodType": "authenticated"})

    async def _users_by_phone(self, phone: str) -> Dict[str, Any]:
        response = await self._call("GET", "/bot/users", params={"phone": phone})
        if not response.get("success"):
            return response
        users = []
        for user in response.get("data") or []:
            is_seller = user.get("role") == "vendor"
            users.append({
                "id": user.get("id"),
                "username": user.get("email"),
                "email": user.get("email"),
                "fullName": user.get("name"),
                "phone": user.get("mobile"),
                "selfClient": not is_seller,
                "companyName": user.get("orgName"),
                # Sellers are addressed by their vendor record everywhere else.
                "orgId": (user.get("vendorId") or user.get("orgId")) if is_seller else user.get("orgId"),
                "uniqueId": user.get("uniqueId"),
                # Web accounts verify their email by OTP when they register and carry no status.
                "verificationStatus": user.get("verificationStatus") or EMAIL_VERIFIED,
                "approved": user.get("isApproved"),
                "active": user.get("isActive"),
            })
        if not users:
            return _ok(_message("204", f"{MSG['NO_USER_FOR_PHONE']}{phone}", "Failure"))
        return _ok(_message("200", MSG["USERS_FOUND"], "Success", data={"users": users}))

    def _registration_failure(self, status: int, message: str) -> Dict[str, Any]:
        return _error(status, _message(str(status), message, "Failure", data={"confirmationFlag": False, "error": message}))

    async def _seller_categories(self, token: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The seller's stated categories settled onto EQUA's taxonomy by its own classifier (/rfqs/ingest)."""
        items = []
        for entry in payload.get("divisionCategories") or []:
            stated = isinstance(entry, dict) and (entry.get("category") or entry.get("division"))
            if stated:
                items.append({"itemName": entry.get("item") or stated, "category": entry.get("category"), "majorCategory": entry.get("division") or None})
        if not items and payload.get("details"):
            items.append({"itemName": str(payload["details"])})
        if not items:
            return None
        response = await self._call("POST", "/rfqs/ingest", token, json_data={"lineItems": items})
        entities = (response.get("data") or {}).get("extractedEntities") if response.get("success") else None
        majors = [e.get("majorCategory") for e in entities or [] if e.get("majorCategory")]
        minors = [e.get("minorCategory") for e in entities or [] if e.get("minorCategory")]
        if not majors:
            return None
        major = max(set(majors), key=majors.count)
        return {"majorCategory": major, "minorCategories": _unique(minors), "vendorSelectedCategories": _unique(majors + minors)}

    async def _register(self, payload: Dict[str, Any], seller: bool) -> Dict[str, Any]:
        email = str(payload.get("email") or "").strip().lower()
        phone = payload.get("organizationPhonenumber")
        if not EMAIL_PATTERN.match(email):
            return self._registration_failure(409, f"Invalid email format: [{payload.get('email') or ''}]")
        if not phone:
            return self._registration_failure(409, "Mobile number is required.")

        response = await self._call("POST", "/auth/register", json_data={
            "name": payload.get("name"),
            "email": email,
            # Never sent to anyone: the account signs in with an emailed OTP, as QUA
            # WhatsApp accounts did. Random so it cannot be guessed.
            "password": secrets.token_urlsafe(18),
            "mobile": normalize_mobile(phone),
            "role": "vendor" if seller else "buyer",
            "orgName": payload.get("companyName") or payload.get("name"),
            "address": payload.get("address1") or "",
            "pincode": payload.get("zipCode") or "",
            "gstin": payload.get("gstin") or "",
        })
        if not response.get("success"):
            message = str(response.get("message") or "")
            if "already exists" in message.lower():
                return self._registration_failure(409, MSG["ALREADY_EXISTS"])
            if response.get("status_code") == 400 and message:
                return self._registration_failure(409, message)
            return self._registration_failure(500, MSG["SOMETHING_WRONG"])

        user = response.get("user") or {}
        token = response.get("token")
        # QUA created WhatsApp accounts unverified and the bot then runs its OTP step.
        await self._call("PUT", f"/bot/users/{user.get('id')}/whatsapp",
                         json_data={"registeredViaWhatsApp": True, "verificationStatus": PENDING_VERIFICATION})

        org_id = user.get("orgId")
        if seller and token:
            listed = await self._call("GET", "/bot/users", params={"phone": phone})
            vendor_id = next((u.get("vendorId") for u in listed.get("data") or [] if u.get("id") == user.get("id")), None)
            if vendor_id:
                org_id = vendor_id
                categories = await self._seller_categories(token, payload)
                if categories:
                    await self._call("PUT", f"/vendors/{vendor_id}", token, json_data={
                        "majorCategory": categories["majorCategory"], "minorCategories": categories["minorCategories"]})
                    await self._call("PUT", f"/vendors/{vendor_id}/categories", token, json_data={
                        "vendorSelectedCategories": categories["vendorSelectedCategories"]})

        return _ok(_message("200", MSG["SELLER_CREATED"] if seller else MSG["BUYER_CREATED"], "Success", data={
            "confirmationFlag": True,
            "id": user.get("id"),
            "email": email,
            "uniqueId": None,
            "orgId": org_id,
            "companyId": None,
            "companyName": user.get("orgName") or payload.get("companyName"),
            "selfClient": not seller,
            "fullName": user.get("name"),
            "verificationStatus": PENDING_VERIFICATION,
        }))

    async def _send_otp(self, body: Dict[str, Any]) -> Dict[str, Any]:
        response = await self._call("POST", "/auth/request-otp", json_data={
            "email": body.get("email"), "mobile": normalize_mobile(body.get("organizationPhonenumber"))})
        if response.get("success"):
            return _ok(_message("200", MSG["OTP_SENT"], "Success"))
        return _error(404, _message("404", MSG["OTP_NOT_SENT"], "Failure"))

    async def _validate_otp(self, body: Dict[str, Any]) -> Dict[str, Any]:
        mobile = normalize_mobile(body.get("organizationPhonenumber"))
        response = await self._call("POST", "/auth/verify-otp", json_data={
            "email": body.get("email"), "code": str(body.get("emailOtp") or ""), "mobile": mobile})
        if not response.get("success"):
            return _error(400, _message("400", MSG["OTP_INVALID"], "Failure"))
        user = response.get("user") or {}
        if response.get("token") and user.get("id"):
            self._remember(f"{body.get('email')}|{mobile}", response["token"], user)
            await self._call("PUT", f"/bot/users/{user['id']}/whatsapp", json_data={"verificationStatus": EMAIL_VERIFIED})
        return _ok(_message("200", MSG["OTP_VALID"], "Success"))

    async def _accept_client(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # Enterprise QUA approves every account when it is registered.
        return _ok(_message("200", MSG["USER_ACCEPTED"], "Success"))

    async def _send_email(self, body: Dict[str, Any]) -> Dict[str, Any]:
        response = await self._call("POST", "/bot/email", json_data={
            "to": body.get("to") or [], "cc": body.get("cc") or [], "bcc": body.get("bcc") or [],
            "subject": body.get("subject") or "", "text": body.get("body") or "",
            "attachments": body.get("attachments") or [],
        })
        if response.get("success"):
            return _ok(_message("200", MSG["EMAIL_SENT"], "Success"))
        invalid = (response.get("data") or {}).get("invalid")
        if invalid:
            return _ok(_message("206", MSG["INVALID_EMAILS"], "Partial Failure", error_msg=invalid))
        return _error(500, _message("500", MSG["EMAIL_FAILED"], "Failure"))

    # ── Buyer RFQs ─────────────────────────────────────────────────────────

    def _rfq_failure(self, reason: str) -> Dict[str, Any]:
        logger.warning(f"[EQUA] WhatsApp RFQ creation rejected: {reason}")
        return _ok(_message("500", MSG["RFQ_FAILED"], "Failure", data={}, error_msg=[reason]))

    async def _create_rfq(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        token, user = await self._session(payload.get("createdBy") or payload.get("user"))
        if not token or (user or {}).get("role") != "buyer":
            return self._rfq_failure("No buyer account found for this WhatsApp user.")

        items = []
        for item in payload.get("rfqItem") or []:
            description = str(item.get("description") or "").strip()
            remarks = str(item.get("remarks") or "").strip()
            items.append({
                "itemName": description,
                "quantity": item.get("quantity"),
                "unit": item.get("unitofMeasures"),
                "brand": item.get("brand") or "",
                "technicalSpecs": remarks if remarks and remarks not in (description, "N/A") else "",
            })
        # EQUA's own classifier — the same one a BOQ upload goes through.
        draft_response = await self._call("POST", "/rfqs/ingest", token, json_data={"lineItems": items, "title": payload.get("projectDesc")})
        draft = draft_response.get("data") if draft_response.get("success") else None
        if not isinstance(draft, dict) or not draft.get("extractedEntities"):
            return self._rfq_failure(draft_response.get("message") or "The RFQ has no line items with a description.")

        attachments = []
        for doc in payload.get("rfqDocument") or []:
            if not isinstance(doc, dict) or not doc.get("fileName") or not doc.get("file"):
                continue
            extension = str(doc["fileName"]).rsplit(".", 1)[-1].lower()
            stored = await self._call("POST", "/rfqs/attachments", token, json_data={
                "fileName": doc["fileName"], "mimeType": MIME_BY_EXTENSION.get(extension, "application/octet-stream"), "content": doc["file"]})
            if stored.get("success") and stored.get("data"):
                attachments.append(stored["data"])
            else:
                logger.warning(f"[EQUA] RFQ attachment {doc['fileName']} not stored: {stored.get('message')}")

        location = (payload.get("clientdeliverylocationrfq") or [{}])[0] or {}
        city, state, pincode = (str(location.get(k) or "").strip() for k in ("city", "state", "pincode"))
        requested = str(payload.get("deliveryDate") or "")[:10]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = requested if re.match(r"^\d{4}-\d{2}-\d{2}$", requested) and requested >= today else draft.get("targetDeliveryDate")
        title = str(payload.get("projectDesc") or draft.get("title") or "").strip()[:200].ljust(3, ".")

        body = {
            "title": title,
            **({"category": draft["category"]} if draft.get("category") else {}),
            "deliveryLocation": ", ".join(p for p in (city, state) if p) or pincode,
            "deliveryCity": city,
            "deliveryState": state,
            "deliveryPincode": pincode,
            "targetDeliveryDate": target,
            "extractedEntities": [{**entity, "targetDate": target} for entity in draft["extractedEntities"]],
            "attachments": attachments,
            "source": "whatsapp",
            "sourcingMode": getattr(self.settings, "equa_rfq_sourcing_mode", "mode_0"),
        }
        # Creating an RFQ is not idempotent and EQUA writes an AI summary while doing
        # it, so it gets a longer budget and a single attempt.
        created = await self._call("POST", "/rfqs", token, json_data=body,
                                   total_timeout=getattr(self.settings, "equa_rfq_create_timeout_seconds", 90.0), max_retries=1)
        rfq = created.get("data") if created.get("success") else None
        if not isinstance(rfq, dict):
            return self._rfq_failure(created.get("message") or "RFQ creation failed")
        return _ok(_message("200", MSG["RFQ_CREATED"], "Success", data={"rfqId": rfq.get("rfqNumber") or rfq.get("id"), "rfquuid": rfq.get("id")}))

    async def _rfq_status(self, body: Dict[str, Any]) -> Dict[str, Any]:
        token, _ = await self._session(body.get("clientId"))
        rfqs = await self._rfqs_for(token) if token else []
        requested = normalize_requested_ids(body.get("rfqIds"))
        if requested:
            data = []
            for rfq_id in requested:
                rfq = next((r for r in rfqs if matches_rfq_id(r, rfq_id)), None)
                data.append({"rfqid": rfq.get("rfqNumber") or rfq_id, "status": buyer_rfq_status(rfq)} if rfq else {"rfqid": rfq_id, "status": "Invalid RFQID"})
        else:
            latest = newest_first(rfqs)[:BUYER_RECENT_RFQ_LIMIT]
            data = [{"rfqid": r.get("rfqNumber") or r.get("id"), "status": buyer_rfq_status(r)} for r in latest] or [{"rfqid": None, "status": "Not Found"}]
        return _ok({"success": True, "data": data})

    # ── Seller RFQs ────────────────────────────────────────────────────────

    async def _seller_session(self, account_id: Any) -> Tuple[Optional[str], Optional[str]]:
        token, user = await self._session(account_id)
        vendor_id = (user or {}).get("vendorId")
        return (token, vendor_id) if token and vendor_id else (None, None)

    async def _rfqs_by_category(self, body: Dict[str, Any]) -> Dict[str, Any]:
        token, vendor_id = await self._seller_session(body.get("id"))
        if not token:
            return _ok({"success": True, "data": {"rfqs": [], "count": 0}})
        vendor = await self._vendor(token, vendor_id)
        forwarded = await self._forwarded(vendor_id)
        candidates = [r for r in await self._rfqs_for(token) if not is_closed(r) and not self._taken_by(vendor, vendor_id, forwarded, r)]
        since = time.time() - SELLER_FEED_WINDOW_DAYS * 24 * 60 * 60
        recent = [r for r in newest_first(candidates) if to_time(r.get("createdAt")) >= since][:SELLER_FEED_LIMIT]
        return _ok({"success": True, "data": {"rfqs": [to_qua_rfq(r) for r in recent], "count": len(candidates)}})

    async def _seller_credits(self, body: Dict[str, Any]) -> Dict[str, Any]:
        token, _ = await self._seller_session(body.get("id"))
        credits = 0
        if token:
            response = await self._call("GET", "/vendors/rfq-credits", token)
            info = response.get("data") if response.get("success") else None
            if isinstance(info, dict):
                bundle = {"connect": 50, "select": 100}.get(info.get("subscriptionPlan"))
                # Subscribers are not metered by EQUA; report their plan's bundle rather than zero.
                credits = bundle if info.get("isSubscribed") and bundle else int(info.get("freeCreditsRemaining") or 0)
        return _ok({"success": True, "data": {"creditsAvailable": credits}})

    async def _seller_rfq_status(self, body: Dict[str, Any]) -> Dict[str, Any]:
        token, vendor_id = await self._seller_session(body.get("clientId"))
        vendor, taken = await self._taken_rfqs(token, vendor_id) if token else ({}, [])

        def status(rfq: Dict[str, Any]) -> str:
            return "Quote_Submitted" if self._quoted_by(vendor, vendor_id, rfq) else "Downloaded"

        requested = normalize_requested_ids(body.get("rfqIds"))
        if requested:
            data = []
            for rfq_id in requested:
                rfq = next((r for r in taken if matches_rfq_id(r, rfq_id)), None)
                data.append({"rfqid": rfq.get("rfqNumber") or rfq_id, "status": status(rfq)} if rfq else {"rfqid": rfq_id, "status": "Un_Known"})
        else:
            latest = taken[:SELLER_RECENT_RFQ_LIMIT]
            data = [{"rfqid": r.get("rfqNumber") or r.get("id"), "status": status(r)} for r in latest] or [{"rfqid": None, "status": "Not Found"}]
        return _ok({"success": True, "data": data})

    async def _forward_rfqs(self, body: Dict[str, Any]) -> Dict[str, Any]:
        rfq_ids = body.get("rfqIds") if isinstance(body.get("rfqIds"), list) else []
        email = str(body.get("email") or "")
        token, vendor_id = await self._seller_session(body.get("sellerId"))
        successful, failed, forwarded = [], [], []

        for rfq_id in rfq_ids:
            def fail(code: str, error: str) -> None:
                failed.append({"rfq_id": rfq_id, "email_sent": False, "error_code": code, "error": error})

            if not token:
                fail("ORG_NOT_FOUND", "Organization not found")
                continue
            found = await self._call("GET", f"/rfqs/{rfq_id}", token)
            if not found.get("success"):
                fail("RFQ_NOT_FOUND", "RFQ is not found")
                continue
            if not EMAIL_PATTERN.match(email):
                fail("INVALID_EMAIL", "Invalid email format")
                continue
            # EQUA's own unlock: charges a credit unless subscribed or buyer-mapped, and
            # returns the RFQ email it would send the vendor.
            unlocked = await self._call("POST", f"/rfqs/{rfq_id}/download", token)
            if not unlocked.get("success"):
                if unlocked.get("status_code") == 403:
                    fail("NO_CREDITS", "Organization has no RFQ credits")
                else:
                    fail("SYSTEM_ERROR", unlocked.get("message") or "RFQ could not be unlocked")
                continue
            forwarded.append(rfq_id)
            payload = unlocked.get("data") or {}
            sent = await self._call("POST", "/bot/email", json_data={
                "to": [email], "subject": payload.get("subject") or f"RFQ {rfq_id}", "html": payload.get("htmlBody") or ""})
            if sent.get("success"):
                successful.append({"rfq_id": rfq_id, "email_sent": True, "message": "Email sent successfully"})
            else:
                fail("SYSTEM_ERROR", "Failed to send email or added to queue")

        if forwarded:
            await self._remember_forwarded(vendor_id, forwarded)
        return _ok({
            "success": True,
            "results": {"successful": successful, "failed": failed},
            "summary": {"total_requests": len(rfq_ids), "successful": len(successful), "failed": len(failed)},
        })

    async def _open_rfqs(self, body: Dict[str, Any]) -> Dict[str, Any]:
        token, vendor_id = await self._seller_session(body.get("id"))
        vendor, taken = await self._taken_rfqs(token, vendor_id) if token else ({}, [])
        open_rfqs = []
        for rfq in [r for r in taken if not is_closed(r) and not self._quoted_by(vendor, vendor_id, r)][:OPEN_RFQ_LIMIT]:
            created = to_time(rfq.get("createdAt"))
            target = to_time(rfq.get("targetDeliveryDate") or rfq.get("deadline"))
            entry = {
                "rfq_id": rfq.get("rfqNumber") or rfq.get("id"),
                "project_desc": rfq.get("title") or "",
                "category": rfq.get("category"),
                "location": ",".join(p for p in (rfq.get("deliveryCity"), rfq.get("deliveryState")) if p) or rfq.get("deliveryLocation") or None,
                "submission_deadline": to_iso(datetime.fromtimestamp(created, timezone.utc) + timedelta(days=SUBMISSION_WINDOW_DAYS)) if created else None,
                "email_sent_date": None,
            }
            if target:
                entry["days_remaining"] = int(-(-(target - time.time()) // 86400))
            entry["status"] = "open_for_bidding"
            open_rfqs.append(entry)
        return _ok({
            "success": True,
            "open_rfqs": open_rfqs,
            "total_count": len(open_rfqs),
            "metadata": {
                "seller_id": body.get("id"),
                "filter_criteria": {"status": "open_for_bidding", "email_sent": True, "bid_submitted": False},
            },
        })

    # ── Subscriptions and preferences ──────────────────────────────────────

    async def _subscription_plans(self, body: Dict[str, Any]) -> Dict[str, Any]:
        response = await self._call("GET", "/bot/vendor-plans")
        plans = [
            {
                "id": plan.get("id"),
                "planName": str(plan.get("name") or "").split(" ")[0],
                "planDescription": plan.get("description"),
                "features": plan.get("features"),
                # What the payment link charges before GST, so the quoted price is the paid price.
                "subscriptionPrice": plan.get("amount"),
                "launchOfferPrice": plan.get("amount"),
                "subscriptionPeriodMonths": plan.get("periodMonths"),
                "rfqBundleSize": plan.get("rfqBundleSize"),
                "rfqNotification": "YES",
                "whatsappAiAssistant": "YES",
                "viewAllRfqs": "YES",
                "planStatus": "ACTIVE",
                "launchedStatus": "YES",
            }
            for plan in (response.get("data") if response.get("success") else None) or []
        ]
        if plans:
            return _ok(_message("200", MSG["PLANS_FOUND"], "Success", data={"plans": plans}))
        return _ok(_message("200", MSG["NO_PLANS"], "Failure"))

    async def _payment_link(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not body.get("planId"):
            return _error(400, {"error": "Plan ID is required"})
        if not body.get("userPhone"):
            return _error(400, {"error": "User phone number is required"})
        if not EMAIL_PATTERN.match(str(body.get("userEmail") or "")):
            return _error(400, {"error": f"Invalid email address: {body.get('userEmail') or ''}"})
        token, user = await self._session(email=body.get("userEmail"), phone=body.get("userPhone"))
        vendor_id = (user or {}).get("vendorId")
        if not token or not vendor_id:
            return _error(404, {"error": "User not found"})
        response = await self._call("POST", f"/vendors/{vendor_id}/payment-link", token, json_data={"plan": body.get("planId")})
        link = response.get("data") if response.get("success") else None
        if not isinstance(link, dict):
            return response if not response.get("success") else _error(502, {"error": "Payment link could not be created"})
        return _ok({"id": link.get("paymentLinkId"), "paymentUrl": link.get("paymentUrl"), "status": link.get("status")})

    async def _seller_opt_out(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._call("PUT", f"/bot/vendors/{body.get('sellerId')}/whatsapp-opt-out", json_data={"optedOut": body.get("optedOut") is True})
