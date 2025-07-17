"""
GMT API Service for Procucev Integration.

This service handles OAuth authentication and API calls to the GMT Procucev backend system.
It provides methods for creating RFQs, managing vendors, and other procurement operations.
"""

import logging
import asyncio
import base64
from typing import Dict, Any, Optional, List
from datetime import datetime
import aiohttp
import json

from app.config import get_settings

logger = logging.getLogger(__name__)

class GMTAPIService:
    """
    Service for integrating with GMT Procucev API.
    
    Handles OAuth authentication, RFQ creation, and other procurement operations
    with the GMT backend system.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.base_url = self.settings.gmt_base_url
        self.client_id = self.settings.gmt_client_id
        self.client_secret = self.settings.gmt_client_secret
        self.username = self.settings.gmt_username
        self.password = self.settings.gmt_password
        self.token = None
        self.token_expires_at = None
        
    async def authenticate(self) -> bool:
        """Authenticate with GMT API using OAuth2 password grant."""
        token_url = f"{self.base_url}/procucev/oauth/token"
        
        # Create Basic Auth header for client credentials
        client_auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        
        headers = {
            'Authorization': f'Basic {client_auth}',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json'
        }
        
        data = {
            'username': self.username,
            'password': self.password,
            'grant_type': 'password'
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(token_url, data=data, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        token_data = await response.json()
                        self.token = token_data['access_token']
                        
                        # Calculate token expiry (subtract 5 minutes for safety)
                        expires_in = token_data.get('expires_in', 7200) - 300
                        self.token_expires_at = datetime.now().timestamp() + expires_in
                        
                        logger.info(f"GMT API authentication successful. Token expires in {expires_in} seconds")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"GMT API authentication failed: {response.status} - {error_text}")
                        return False
                        
        except Exception as e:
            logger.error(f"GMT API authentication error: {e}")
            return False
    
    async def ensure_authenticated(self) -> bool:
        """Ensure we have a valid authentication token."""
        if not self.token or (self.token_expires_at and datetime.now().timestamp() > self.token_expires_at):
            logger.info("Token expired or missing, re-authenticating...")
            return await self.authenticate()
        return True
    
    async def create_rfq(self, rfq_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create RFQ in GMT system.
        
        Args:
            rfq_data: RFQ data dictionary from our local database
            
        Returns:
            Dict with success status and response data
        """
        try:
            if not await self.ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}
            
            # Transform our RFQ data to GMT API format
            gmt_rfq_data = self._transform_rfq_to_gmt_format(rfq_data)
            
            # GMT API endpoint for creating RFQ
            create_url = f"{self.base_url}/procucev/rest/categoryManager/createRFQForNoPrByClient"
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(create_url, json=gmt_rfq_data, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        response_data = await response.json()
                        
                        # Check if GMT API indicates success
                        if response_data.get('statusCode') == '1021':
                            logger.info(f"RFQ successfully created in GMT system")
                            return {
                                "success": True,
                                "response": response_data,
                                "backend_reference": f"GMT_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                            }
                        else:
                            logger.error(f"GMT API returned error: {response_data}")
                            return {
                                "success": False,
                                "error": f"GMT API error: {response_data.get('message', 'Unknown error')}"
                            }
                    else:
                        error_text = await response.text()
                        logger.error(f"GMT API request failed: {response.status} - {error_text}")
                        return {
                            "success": False,
                            "error": f"HTTP {response.status}: {error_text}"
                        }
                        
        except Exception as e:
            logger.error(f"Error creating RFQ in GMT system: {e}")
            return {"success": False, "error": str(e)}
    
    def _transform_rfq_to_gmt_format(self, rfq_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transform our internal RFQ format to GMT API format.
        
        Args:
            rfq_data: Internal RFQ data structure
            
        Returns:
            GMT API compatible RFQ data
        """
        # Default values from successful test
        default_org_id = "570f5874-0676-42c7-a4c4-1b1712642bc8"
        default_user_id = "4004"
        
        # Create GMT-compatible RFQ item
        rfq_item = {
            "brand": rfq_data.get("preferred_brand", "Generic"),
            "unitofMeasures": rfq_data.get("unit_of_measure", "pcs"),
            "quantity": rfq_data.get("quantity", 1),
            "description": rfq_data.get("product_name", "Product"),
            "category": None,
            "createdBy": None,
            "createdTS": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            "itemcode": None,
            "serialNo": 1001,
            "remarks": rfq_data.get("specifications", "N/A")
        }
        
        # Create delivery location
        delivery_location = {
            "state": rfq_data.get("delivery_state", "Karnataka"),
            "city": rfq_data.get("delivery_city", "Bangalore"),
            "pincode": rfq_data.get("delivery_pincode", "560001")
        }
        
        # Format delivery date
        delivery_date = rfq_data.get("deadline")
        if delivery_date:
            if isinstance(delivery_date, str):
                # Try to parse and reformat
                try:
                    parsed_date = datetime.fromisoformat(delivery_date.replace('Z', '+00:00'))
                    delivery_date = parsed_date.strftime('%Y-%m-%dT%H:%M:%S.000Z')
                except:
                    delivery_date = "2025-12-31T18:30:00.000Z"
            else:
                delivery_date = "2025-12-31T18:30:00.000Z"
        else:
            delivery_date = "2025-12-31T18:30:00.000Z"
        
        # Build GMT API payload
        gmt_payload = {
            "createdBy": "AI_Procurement_Agent",
            "projectDesc": rfq_data.get("product_name", f"RFQ_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
            "deliveryDate": delivery_date,
            "noPrFlag": True,
            "org": {
                "id": default_org_id
            },
            "rfqItem": [rfq_item],
            "vendors": [],  # Will be populated later
            "clientdeliverylocationrfq": [delivery_location],
            "remarks": rfq_data.get("remarks", "Created via AI Procurement WhatsApp Bot"),
            "rfqDocument": [],
            "user": default_user_id,
            "division": rfq_data.get("division", "Admin & IT")
        }
        
        logger.info(f"Transformed RFQ data for GMT API: {json.dumps(gmt_payload, indent=2)}")
        return gmt_payload
    
    async def get_client_rfqs(self, client_id: str = "4004") -> Dict[str, Any]:
        """Get RFQ IDs for a client."""
        try:
            if not await self.ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}
            
            url = f"{self.base_url}/procucev/rest/client/getClientRfqIds"
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            data = {"id": client_id}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        rfq_ids = await response.json()
                        return {"success": True, "rfq_ids": rfq_ids}
                    else:
                        error_text = await response.text()
                        return {"success": False, "error": f"HTTP {response.status}: {error_text}"}
                        
        except Exception as e:
            logger.error(f"Error getting client RFQs: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_divisions(self) -> Dict[str, Any]:
        """Get all available divisions."""
        try:
            if not await self.ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}
            
            url = f"{self.base_url}/procucev/rest/categoryManager/getAllDivisions"
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/json'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        divisions = await response.json()
                        return {"success": True, "divisions": divisions}
                    else:
                        error_text = await response.text()
                        return {"success": False, "error": f"HTTP {response.status}: {error_text}"}
                        
        except Exception as e:
            logger.error(f"Error getting divisions: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_categories(self) -> Dict[str, Any]:
        """Get all available categories."""
        try:
            if not await self.ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}
            
            url = f"{self.base_url}/procucev/rest/categoryManager/getAllCategories"
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/json'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        categories = await response.json()
                        return {"success": True, "categories": categories}
                    else:
                        error_text = await response.text()
                        return {"success": False, "error": f"HTTP {response.status}: {error_text}"}
                        
        except Exception as e:
            logger.error(f"Error getting categories: {e}")
            return {"success": False, "error": str(e)}
    
    async def get_rfq_details_by_client(self, client_id: str = "4004") -> Dict[str, Any]:
        """Get detailed RFQ information with status for a client."""
        try:
            if not await self.ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}
            
            url = f"{self.base_url}/procucev/rest/rfq/getNoPrRfqByClient"
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            
            data = {"id": client_id}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers, timeout=30) as response:
                    if response.status == 200:
                        rfq_details = await response.json()
                        return {"success": True, "rfq_details": rfq_details}
                    else:
                        error_text = await response.text()
                        return {"success": False, "error": f"HTTP {response.status}: {error_text}"}
                        
        except Exception as e:
            logger.error(f"Error getting RFQ details: {e}")
            return {"success": False, "error": str(e)}
    
    async def bulk_upload_rfq(self, excel_data: Dict[str, str]) -> Dict[str, Any]:
        """
        Upload RFQ data using GMT bulk upload API.
        
        Args:
            excel_data: Dict with 'boqFileName' and 'boqfile' (base64 encoded Excel)
            
        Returns:
            Dict with success status and response data
        """
        try:
            # Ensure we're authenticated
            if not await self.ensure_authenticated():
                return {"success": False, "error": "Authentication failed"}
            
            url = f"{self.base_url}/procucev/rest/categoryManager/convertRfqBoq"
            
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/json'
            }
            
            # Prepare data according to API specification
            boq_filename = excel_data.get('boqFileName', 'rfq_items.xlsx')
            boq_file_data = excel_data.get('boqfile')
            
            if not boq_file_data:
                return {"success": False, "error": "Missing Excel file data"}
            
            logger.info(f"Submitting bulk RFQ upload: {boq_filename}")
            
            # Try both approaches - JSON first, then multipart/form-data if JSON fails
            async with aiohttp.ClientSession() as session:
                # First attempt: JSON format (as currently implemented)
                json_data = {
                    "boqFileName": boq_filename,
                    "boqfile": boq_file_data
                }
                
                json_headers = {**headers, 'Content-Type': 'application/json'}
                async with session.post(url, json=json_data, headers=json_headers, timeout=60) as response:
                    response_text = await response.text()
                    
                    if response.status == 200:
                        try:
                            response_data = await response.json() if response.content_type == 'application/json' else {"message": response_text}
                            
                            # Handle case where response might be a list
                            if isinstance(response_data, list):
                                response_data = response_data[0] if response_data else {}
                            
                            # Check if GMT API returned an error despite HTTP 200
                            if (response_data.get('status') == 'Failure' or 
                                response_data.get('errorCode') == 500 or
                                'File format is not correct' in str(response_data.get('errorMessage', ''))):
                                logger.error(f"GMT API returned error: {response_data}")
                                return {"success": False, "error": response_data.get('errorMessage', 'GMT API error')}
                            
                            logger.info(f"Bulk upload successful: {response_data}")
                            return {"success": True, "response": response_data}
                        except json.JSONDecodeError:
                            # Handle non-JSON success response
                            return {"success": True, "response": {"message": response_text}}
                    elif "File format is not correct" in response_text:
                        # Try multipart/form-data format as fallback
                        logger.info("JSON format failed, trying multipart/form-data...")
                        
                        # Decode base64 to actual file bytes
                        import base64
                        file_bytes = base64.b64decode(boq_file_data)
                        
                        # Create multipart form data
                        form_data = aiohttp.FormData()
                        form_data.add_field('boqFileName', boq_filename)
                        form_data.add_field('boqfile', file_bytes, filename=boq_filename, 
                                          content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                        
                        # Try multipart upload
                        async with session.post(url, data=form_data, headers=headers, timeout=60) as multipart_response:
                            multipart_text = await multipart_response.text()
                            
                            if multipart_response.status == 200:
                                try:
                                    multipart_data = await multipart_response.json() if multipart_response.content_type == 'application/json' else {"message": multipart_text}
                                    logger.info(f"Multipart upload successful: {multipart_data}")
                                    return {"success": True, "response": multipart_data}
                                except json.JSONDecodeError:
                                    return {"success": True, "response": {"message": multipart_text}}
                            else:
                                logger.error(f"Multipart upload also failed: {multipart_response.status} - {multipart_text}")
                                return {"success": False, "error": f"Both JSON and multipart failed. Last error: HTTP {multipart_response.status}: {multipart_text}"}
                    else:
                        logger.error(f"Bulk upload failed: {response.status} - {response_text}")
                        return {"success": False, "error": f"HTTP {response.status}: {response_text}"}
                        
        except Exception as e:
            logger.error(f"Error in bulk upload: {e}")
            return {"success": False, "error": str(e)}