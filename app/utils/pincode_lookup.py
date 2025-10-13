"""
pincode_lookup.py
-----------------
Utility module for fetching city and state information
for Indian postal pincodes using the official India Post API.
Docs: https://api.postalpincode.in
"""

import requests
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class PincodeLookupError(Exception):
    """Custom exception for Pincode lookup errors."""
    pass


def get_location_from_pincode(pincode: str) -> Dict[str, str]:
    """
    Fetch location details (City, State) for a given Indian Postal Pincode.
    Parameters
    ----------
    pincode : str
        6-digit Indian postal pincode.
    Returns
    -------
    dict
        Example:
        {
            'pincode': '110001',
            'city': 'Central Delhi',
            'state': 'Delhi'
        }
    Raises
    ------
    ValueError
        If pincode is not a valid 6-digit number.
    PincodeLookupError
        If API returns an error, or the data cannot be retrieved.
    """

    if not pincode.isdigit() or len(pincode) != 6:
        raise ValueError("Invalid pincode format. Must be a 6-digit number.")

    url = f"https://api.postalpincode.in/pincode/{pincode}"

    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    headers = {
        "User-Agent": "Python-Requests/2.x (compatible; PincodeLookup/1.0; +https://mohap.ai)"
    }

    try:
        logger.info(f"Fetching location details for pincode: {pincode}")
        response = session.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()[0]

        if data.get("Status") != "Success" or not data.get("PostOffice"):
            raise PincodeLookupError(f"No records found for Pincode {pincode}")

        post_office = data["PostOffice"][0]
        result = {
            "pincode": pincode,
            "city": post_office["District"],
            "state": post_office["State"],
        }
        logger.info(f"Successfully fetched location for {pincode}: {result['city']}, {result['state']}")
        return result

    except requests.exceptions.RequestException as e:
        logger.error(f"Network or API error for pincode {pincode}: {e}")
        raise PincodeLookupError(f"Network or API error: {e}")
    except (KeyError, IndexError, ValueError) as e:
        logger.error(f"Malformed response for pincode {pincode}: {e}")
        raise PincodeLookupError(f"Malformed response for Pincode {pincode}: {e}")


async def get_location_from_pincode_async(pincode: str) -> Optional[Dict[str, str]]:
    """
    Async wrapper for get_location_from_pincode.
    Returns None instead of raising exceptions for better error handling.
    
    Parameters
    ----------
    pincode : str
        6-digit Indian postal pincode.
        
    Returns
    -------
    dict or None
        Location details if found, None if error occurs.
    """
    try:
        return get_location_from_pincode(pincode)
    except (ValueError, PincodeLookupError) as e:
        logger.warning(f"Could not fetch location for pincode {pincode}: {e}")
        return None


# Optional: quick test if executed directly
if __name__ == "__main__":
    try:
        result = get_location_from_pincode("222222")
        print(result)
    except Exception as e:
        print(f"Error: {e}")