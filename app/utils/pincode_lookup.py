"""
pincode_lookup.py
-----------------
Utility module for fetching city and state information
for Indian postal pincodes using the official India Post API.
"""

import requests
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def get_pincode_details(pincode, max_retries=3, timeout=10):
    """Fetch location details for a given Indian pincode."""
    url = f"https://api.postalpincode.in/pincode/{pincode}"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive'
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout, verify=True)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Attempt {attempt + 1}/{max_retries}: Connection error - {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except requests.exceptions.Timeout:
            logger.warning(f"Attempt {attempt + 1}/{max_retries}: Request timeout")
            if attempt < max_retries - 1:
                time.sleep(2)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data: {e}")
            return None
    return None


async def get_location_from_pincode_async(pincode: str) -> Optional[Dict[str, str]]:
    """Get location from pincode using the working API implementation."""
    if not pincode.isdigit() or len(pincode) != 6:
        return None
        
    try:
        logger.info(f"Fetching location details for pincode: {pincode}")
        data = get_pincode_details(pincode)
        
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
            
        result = data[0]
        if result.get("Status") != "Success" or not result.get("PostOffice"):
            return None
            
        post_office = result["PostOffice"][0]
        location = {
            "pincode": pincode,
            "city": post_office["District"],
            "state": post_office["State"]
        }
        logger.info(f"Successfully fetched location for {pincode}: {location['city']}, {location['state']}")
        return location
        
    except Exception as e:
        logger.warning(f"Could not fetch location for pincode {pincode} returning none: {e}")
        return None


if __name__ == "__main__":
    result = get_pincode_details("500013")
    print("result", result)