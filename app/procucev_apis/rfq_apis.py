"""
RFQ API Service.

This service handles RFQ operations including creation, status checking,
and bulk upload operations with the GMT Procucev backend.
"""

import logging
import base64
import json
import re
import unicodedata
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.procucev_apis.procucev_api_client import get_procucev_api_client

logger = logging.getLogger(__name__)

# Symbols buyers routinely type into WhatsApp specs that the GMT backend rejects.
# Every RFQ payload observed failing with "Error occurred while creating RFQ" carried
# one of these in an item's remarks (10KΩ, ±5%, 1000µF), while payloads with plain
# ASCII text were accepted, so outbound text is transliterated to an ASCII equivalent
# that preserves the buyer's meaning.
_GMT_TEXT_REPLACEMENTS = {
    "\u2126": "ohm", "\u03a9": "ohm", "\u03c9": "ohm",          # Ω ohm sign / omega
    "\u00b5": "u", "\u03bc": "u",                                # µ micro sign / mu
    "\u00b1": "+/-",                                             # ±
    "\u00b0": "deg",                                             # °
    "\u00d7": "x",                                               # ×
    "\u00f7": "/",                                               # ÷
    "\u2013": "-", "\u2014": "-", "\u2212": "-",                 # – — −
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",  # single quotes
    "\u201c": '"', "\u201d": '"', "\u201e": '"',                 # double quotes
    "\u2026": "...",                                             # …
    "\u00b2": "2", "\u00b3": "3",                                # ² ³
    "\u00bd": "1/2", "\u00bc": "1/4", "\u00be": "3/4",           # ½ ¼ ¾
    "\u20b9": "INR ",                                            # ₹
    "\u2122": "(TM)", "\u00ae": "(R)", "\u00a9": "(C)",          # ™ ® ©
    "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",              # ≤ ≥ ≠
    "\u00a0": " ",                                               # non-breaking space
}


def sanitize_gmt_text(value: Any) -> Any:
    """
    Convert a text field to ASCII the GMT backend accepts.

    Known technical symbols become readable ASCII (Ω -> ohm, ± -> +/-, µ -> u); anything
    else outside ASCII is decomposed by Unicode compatibility rules and, if still not
    representable, dropped. Non-string values pass through untouched so numbers, None and
    nested structures keep their type.

    Args:
        value: Field value from the RFQ, usually free text typed by the buyer

    Returns:
        An ASCII-only string for text input, otherwise the value unchanged
    """
    if not isinstance(value, str) or value.isascii():
        return value

    original = value
    for symbol, replacement in _GMT_TEXT_REPLACEMENTS.items():
        value = value.replace(symbol, replacement)

    if not value.isascii():
        # NFKD splits accented characters into base + combining mark; encoding with
        # "ignore" then keeps the base letter and discards the mark.
        value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")

    # Collapse whitespace left behind by dropped characters.
    value = re.sub(r"[ \t]{2,}", " ", value).strip()

    logger.info(f"Sanitized non-ASCII text for GMT API: {original!r} -> {value!r}")
    return value


def _sanitize_gmt_payload(payload: Any) -> Any:
    """Apply sanitize_gmt_text to every string in a nested payload structure."""
    if isinstance(payload, dict):
        return {key: _sanitize_gmt_payload(item) for key, item in payload.items()}
    if isinstance(payload, list):
        return [_sanitize_gmt_payload(item) for item in payload]
    return sanitize_gmt_text(payload)

class RFQAPIService:
    """
    Service for handling RFQ operations.
    Provides methods for RFQ creation, status checking, and bulk operations.
    """

    def __init__(self):
        self.api_client = get_procucev_api_client()

    async def create_rfq(self, rfq_data: Dict[str, Any], user_id: str = None, org_id: str = None) -> Dict[str, Any]:
        """Create RFQ in GMT system."""
        import time
        start_time = time.time()
        
        try:
            gmt_rfq_data = self._transform_rfq_to_gmt_format(rfq_data, user_id, org_id)
            logger.info(f"Payload for creating RFQ: {gmt_rfq_data}")
            
            endpoint = "/rest/gmt/createRFQByClient"
            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=gmt_rfq_data,
                require_auth=True,
                api_title="Create RFQ API"
            )
            
            if (response.get('statusCode') == '200' and response.get('status') == 'Success'):
                rfq_id = response.get('data', {}).get('rfqId') if response.get('data') else None
                logger.info(f"RFQ successfully created in GMT system")
                if rfq_id:
                    logger.info(f"RFQ ID: {rfq_id}")
                
                return {
                    "success": True,
                    "response": response,
                    "rfq_id": rfq_id
                }
            else:
                # The upstream detail sits in errorMsg, not message, and "message" alone
                # is always the generic "Failed to create RFQ". Log both plus the payload
                # so a repeat failure can be diagnosed without reproducing it.
                logger.error(f"GMT API returned error: {response}")
                logger.error(
                    "GMT RFQ creation rejected | statusCode=%s | errorMsg=%s | payload=%s",
                    response.get("statusCode"),
                    response.get("errorMsg"),
                    json.dumps(gmt_rfq_data, default=str),
                )
                return {
                    "success": False,
                    "error": f"GMT API error: {response.get('message', 'Unknown error')}"
                }

        except Exception as e:
            logger.error(f"Error creating RFQ in GMT system: {e}")
            return {"success": False, "error": str(e)}
    
    async def bulk_upload_rfq(self, excel_data: Dict[str, str]) -> Dict[str, Any]:
        """Upload RFQ data using GMT bulk upload API."""
        try:
            endpoint = "/rest/gmt/convertRfqBoq"
            
            boq_filename = excel_data.get('boqFileName', 'rfq_items.xlsx')
            boq_file_data = excel_data.get('boqfile')
            
            if not boq_file_data:
                return {"success": False, "error": "Missing Excel file data"}
            
            logger.info(f"Submitting bulk RFQ upload: {boq_filename}")
            
            json_data = {
                "boqFileName": boq_filename,
                "boqfile": boq_file_data
            }
            
            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=json_data,
                require_auth=True,
                api_title="Bulk Upload RFQ API"
            )
            
            if response.get('success'):
                response_data = response.get('data', {})
                
                if isinstance(response_data, list):
                    response_data = response_data[0] if response_data else {}
                
                if (response_data.get('status') == 'Failure' or 
                    response_data.get('errorCode') == 500 or
                    'File format is not correct' in str(response_data.get('errorMessage', ''))):
                    logger.error(f"GMT API returned error: {response_data}")
                    return {"success": False, "error": response_data.get('errorMessage', 'GMT API error')}
                
                logger.info(f"Bulk upload successful: {response_data}")
                return {"success": True, "response": response_data}
            else:
                return {"success": False, "error": response.get('message', 'Bulk upload failed')}
                        
        except Exception as e:
            logger.error(f"Error in bulk upload: {e}")
            return {"success": False, "error": str(e)}

    async def get_rfq_status(self, client_id: str, rfq_ids: List[str] = None) -> Dict[str, Any]:
        """Get RFQ status for given RFQ IDs or last 3 recent RFQs if no IDs provided."""
        try:
            endpoint = "/rest/gmt/rfqStatus"
            
            data = {"clientId": client_id}
            if rfq_ids and any(rfq_id is not None for rfq_id in rfq_ids):
                valid_rfq_ids = [
                    f"RFQ{rfq_id}" if not str(rfq_id).upper().startswith('RFQ') else rfq_id
                    for rfq_id in rfq_ids
                    if rfq_id is not None
                ]
                
                if valid_rfq_ids:
                    data["rfqIds"] = valid_rfq_ids

            print("data", data)

            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="Get RFQ Status API"
            )
            
            if response.get('success'):
                return {"success": True, "data": response.get('data')}
            else:
                return {"success": False, "error": response.get('message', 'Failed to get RFQ status')}

        except Exception as e:
            logger.error(f"Error calling unified RFQ status API: {e}")
            return {"success": False, "error": str(e)}

    async def get_client_rfqs(self, client_id: str = "4004") -> Dict[str, Any]:
        """Get RFQ IDs for a client."""
        try:
            endpoint = "/rest/client/getClientRfqIds"
            data = {"id": client_id}
            
            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="Get Client RFQs API"
            )
            
            if response.get('success'):
                return {"success": True, "rfq_ids": response.get('data')}
            else:
                return {"success": False, "error": response.get('message', 'Failed to get client RFQs')}
                        
        except Exception as e:
            logger.error(f"Error getting client RFQs: {e}")
            return {"success": False, "error": str(e)}

    async def get_rfq_details_by_client(self, client_id: str = "4004") -> Dict[str, Any]:
        """Get detailed RFQ information with status for a client."""
        try:
            endpoint = "/rest/rfq/getNoPrRfqByClient"
            data = {"id": client_id}
            
            response = await self.api_client.post(
                endpoint=endpoint,
                json_data=data,
                require_auth=True,
                api_title="Get RFQ Details API"
            )
            
            if response.get('success'):
                return {"success": True, "rfq_details": response.get('data')}
            else:
                return {"success": False, "error": response.get('message', 'Failed to get RFQ details')}
                        
        except Exception as e:
            logger.error(f"Error getting RFQ details: {e}")
            return {"success": False, "error": str(e)}

    def _transform_rfq_to_gmt_format(self, rfq_data: Dict[str, Any], user_id: str = None, org_id: str = None) -> Dict[str, Any]:
        """Transform our internal RFQ format to GMT API format."""
        logger.info(f"Transforming RFQ data with {len(rfq_data.get('items', []))} items")
        
        # Handle multiple items from schema (combined RFQ)
        rfq_items = []
        items = rfq_data.get("items", [])
        
        if items and isinstance(items, list):
            # Multiple items case - create rfq_item for each
            for i, item in enumerate(items):
                rfq_item = {
                    "brand": item.get("brand") or "",  # Use item's own brand only, don't fallback to preferred_brand
                    "unitofMeasures": item.get("unit_of_measures") or "unit(s)",  # Default to unit(s) if not provided
                    "quantity": str(item.get("quantity", 1)),
                    "description": item.get("description", f"Item {i+1}"),
                    "category": None,
                    "createdBy": None,
                    "createdTS": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                    "itemcode": None,
                    "serialNo": 1001 + i,
                    "remarks": item.get("remarks", item.get("description", "N/A"))
                }
                rfq_items.append(rfq_item)
        else:
            # Single item case (legacy compatibility)
            rfq_item = {
                "brand": rfq_data.get("preferred_brand", "Generic"),
                "unitofMeasures": rfq_data.get("unit_of_measure") or "unit(s)",  # Default to unit(s) if not provided
                "quantity": rfq_data.get("quantity", 1),
                "description": rfq_data.get("product_name", "Product"),
                "category": None,
                "createdBy": None,
                "createdTS": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                "itemcode": None,
                "serialNo": 1001,
                "remarks": rfq_data.get("specifications", "N/A")
            }
            rfq_items.append(rfq_item)
        
        delivery_location = {
            "state": rfq_data.get("delivery_state", "Karnataka"),
            "city": rfq_data.get("delivery_city", "Bangalore"),
            "pincode": rfq_data.get("delivery_pincode", "560001")
        }

        # Handle delivery date
        delivery_date = rfq_data.get("deadline")
        formatted_delivery_date = None

        if delivery_date:
            try:
                if isinstance(delivery_date, datetime):
                    formatted_delivery_date = delivery_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
                elif isinstance(delivery_date, str):
                    try:
                        parsed_date = datetime.fromisoformat(delivery_date.replace('Z', '+00:00'))
                        formatted_delivery_date = parsed_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
                    except ValueError as e:
                        logger.error(f"Failed to parse date string '{delivery_date}': {e}")
                        formatted_delivery_date = None
            except Exception as e:
                logger.error(f"Error processing delivery date: {e}")
                formatted_delivery_date = None

        # Handle attachments
        rfq_documents = []
        attachments = rfq_data.get("attachments", [])
        
        for i, attachment in enumerate(attachments):
            if attachment.get("file_content") and attachment.get("file_name"):
                # Ensure file content is base64 encoded
                file_content = attachment["file_content"]
                if not self._is_base64(file_content):
                    # If not already base64, encode it
                    file_content = base64.b64encode(file_content.encode('utf-8')).decode('utf-8')
                
                attachment_payload = {
                    "fileName": attachment["file_name"],
                    "file": file_content  # Use 'file' key as shown in your example
                }
                rfq_documents.append(attachment_payload)

        # Generate project description
        project_desc = self._generate_project_desc(rfq_data)
        
        gmt_payload = {
            "createdBy": user_id,
            "projectDesc": project_desc,
            "deliveryDate": formatted_delivery_date,
            "noPrFlag": True,
            "procurementFlag": True,
            "sourceType": rfq_data.get("sourceType", "W"),
            "org": {"id": org_id},
            "rfqItem": rfq_items,
            "vendors": [],
            "clientdeliverylocationrfq": [delivery_location],
            "remarks": rfq_data.get("remarks", "Created via AI Procurement WhatsApp Bot"),
            "rfqDocument": rfq_documents,
            "user": user_id
        }

        # Buyer-typed specs reach us straight from WhatsApp and can contain symbols the
        # GMT backend cannot store, which it reports as a generic creation failure.
        # Normalize once here so every field in the payload is ASCII-safe.
        return _sanitize_gmt_payload(gmt_payload)

    def _generate_project_desc(self, rfq_data: Dict[str, Any]) -> str:
        """Generate project description from product names with 100 character limit."""
        product_names = []

        if rfq_data.get("product_name"):
            product_names.append(rfq_data["product_name"])

        rfq_items = rfq_data.get("rfq_items", [])
        if isinstance(rfq_items, list):
            for item in rfq_items:
                if isinstance(item, dict):
                    desc = (item.get("description") or
                           item.get("product_name") or
                           item.get("item_description"))
                    if desc and desc not in product_names:
                        product_names.append(desc)

        entities = rfq_data.get("entities", [])
        if isinstance(entities, list):
            for entity in entities:
                if isinstance(entity, dict):
                    desc = (entity.get("product_name") or
                           entity.get("description") or
                           entity.get("projectDesc"))
                    if desc and desc not in product_names:
                        product_names.append(desc)

        unique_names = []
        seen = set()
        for name in product_names:
            if name and isinstance(name, str):
                clean_name = name.strip()
                if clean_name and clean_name.lower() not in seen:
                    unique_names.append(clean_name)
                    seen.add(clean_name.lower())

        if unique_names:
            project_desc = ", ".join(unique_names)
            if len(project_desc) > 100:
                project_desc = project_desc[:97] + "..."
            return project_desc
        else:
            return f"RFQ_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    def _is_base64(self, s: str) -> bool:
        """Check if a string is valid base64."""
        try:
            if isinstance(s, str):
                # Check if string is valid base64
                base64.b64decode(s, validate=True)
                return True
        except Exception:
            pass
        return False