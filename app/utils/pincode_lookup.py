import asyncio
import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Fallback location mapping based on 2-digit postal circle prefixes in India
PINCODE_PREFIX_FALLBACK = {
    # Northern Zone
    "11": {"city": "Delhi", "state": "Delhi"},
    "12": {"city": "Faridabad", "state": "Haryana"},
    "13": {"city": "Karnal", "state": "Haryana"},
    "14": {"city": "Ludhiana", "state": "Punjab"},
    "15": {"city": "Bhatinda", "state": "Punjab"},
    "16": {"city": "Chandigarh", "state": "Punjab"},
    "17": {"city": "Shimla", "state": "Himachal Pradesh"},
    "18": {"city": "Jammu", "state": "Jammu & Kashmir"},
    "19": {"city": "Srinagar", "state": "Jammu & Kashmir"},
    "20": {"city": "Noida", "state": "Uttar Pradesh"},
    "21": {"city": "Kanpur", "state": "Uttar Pradesh"},
    "22": {"city": "Lucknow", "state": "Uttar Pradesh"},
    "23": {"city": "Varanasi", "state": "Uttar Pradesh"},
    "24": {"city": "Dehradun", "state": "Uttarakhand"},
    "25": {"city": "Meerut", "state": "Uttar Pradesh"},
    "26": {"city": "Haldwani", "state": "Uttarakhand"},
    "27": {"city": "Gorakhpur", "state": "Uttar Pradesh"},
    "28": {"city": "Agra", "state": "Uttar Pradesh"},
    # Western Zone
    "30": {"city": "Jaipur", "state": "Rajasthan"},
    "31": {"city": "Udaipur", "state": "Rajasthan"},
    "32": {"city": "Kota", "state": "Rajasthan"},
    "33": {"city": "Bikaner", "state": "Rajasthan"},
    "34": {"city": "Jodhpur", "state": "Rajasthan"},
    "36": {"city": "Rajkot", "state": "Gujarat"},
    "37": {"city": "Kutch", "state": "Gujarat"},
    "38": {"city": "Ahmedabad", "state": "Gujarat"},
    "39": {"city": "Surat", "state": "Gujarat"},
    "40": {"city": "Mumbai", "state": "Maharashtra"},
    "41": {"city": "Pune", "state": "Maharashtra"},
    "42": {"city": "Nashik", "state": "Maharashtra"},
    "43": {"city": "Aurangabad", "state": "Maharashtra"},
    "44": {"city": "Nagpur", "state": "Maharashtra"},
    "45": {"city": "Indore", "state": "Madhya Pradesh"},
    "46": {"city": "Bhopal", "state": "Madhya Pradesh"},
    "47": {"city": "Gwalior", "state": "Madhya Pradesh"},
    "48": {"city": "Jabalpur", "state": "Madhya Pradesh"},
    "49": {"city": "Raipur", "state": "Chhattisgarh"},
    # Southern Zone
    "50": {"city": "Hyderabad", "state": "Telangana"},
    "51": {"city": "Tirupati", "state": "Andhra Pradesh"},
    "52": {"city": "Vijayawada", "state": "Andhra Pradesh"},
    "53": {"city": "Visakhapatnam", "state": "Andhra Pradesh"},
    "56": {"city": "Bengaluru", "state": "Karnataka"},
    "57": {"city": "Mysuru", "state": "Karnataka"},
    "58": {"city": "Hubballi", "state": "Karnataka"},
    "59": {"city": "Belagavi", "state": "Karnataka"},
    "60": {"city": "Chennai", "state": "Tamil Nadu"},
    "61": {"city": "Tiruchirappalli", "state": "Tamil Nadu"},
    "62": {"city": "Madurai", "state": "Tamil Nadu"},
    "63": {"city": "Coimbatore", "state": "Tamil Nadu"},
    "64": {"city": "Coimbatore", "state": "Tamil Nadu"},
    "67": {"city": "Kozhikode", "state": "Kerala"},
    "68": {"city": "Kochi", "state": "Kerala"},
    "69": {"city": "Thiruvananthapuram", "state": "Kerala"},
    # Eastern Zone
    "70": {"city": "Kolkata", "state": "West Bengal"},
    "71": {"city": "Durgapur", "state": "West Bengal"},
    "72": {"city": "Kharagpur", "state": "West Bengal"},
    "73": {"city": "Siliguri", "state": "West Bengal"},
    "74": {"city": "Nadia", "state": "West Bengal"},
    "75": {"city": "Bhubaneswar", "state": "Odisha"},
    "76": {"city": "Berhampur", "state": "Odisha"},
    "77": {"city": "Rourkela", "state": "Odisha"},
    "78": {"city": "Guwahati", "state": "Assam"},
    "79": {"city": "Shillong", "state": "Meghalaya"},
    "80": {"city": "Patna", "state": "Bihar"},
    "81": {"city": "Bhagalpur", "state": "Bihar"},
    "82": {"city": "Gaya", "state": "Bihar"},
    "83": {"city": "Ranchi", "state": "Jharkhand"},
    "84": {"city": "Muzaffarpur", "state": "Bihar"},
    "85": {"city": "Purnea", "state": "Bihar"},
}


def get_fallback_location(pincode: str) -> Optional[Dict[str, str]]:
    """Get location from fallback mapping based on first 2 digits of 6-digit Indian pincode."""
    clean_pincode = str(pincode).strip()
    if not clean_pincode.isdigit() or len(clean_pincode) != 6:
        return None
    prefix = clean_pincode[:2]
    fallback = PINCODE_PREFIX_FALLBACK.get(prefix)
    if fallback:
        return {
            "pincode": clean_pincode,
            "city": fallback["city"],
            "state": fallback["state"],
        }
    return None


def get_location_from_pincode(pincode: str) -> Optional[Dict[str, str]]:
    """Synchronous version of pincode lookup with fallback."""
    return get_fallback_location(pincode)


def get_pincode_details(pincode, max_retries=1, timeout=3):
    """Fetch location details for a given Indian pincode."""
    url = f"https://api.postalpincode.in/pincode/{pincode}"

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/91.0.4472.124 Safari/537.36'
        ),
        'Accept': 'application/json',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive'
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout, verify=False)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Attempt {attempt + 1}/{max_retries}: Connection error - {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
        except requests.exceptions.Timeout:
            logger.warning(f"Attempt {attempt + 1}/{max_retries}: Request timeout")
            if attempt < max_retries - 1:
                time.sleep(1)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data: {e}")
            return None
    return None


async def get_location_from_pincode_async(pincode: str) -> Optional[Dict[str, str]]:
    """Get location from pincode using non-blocking thread execution."""
    clean_pincode = str(pincode).strip()
    if not clean_pincode.isdigit() or len(clean_pincode) != 6:
        return None

    try:
        logger.info(f"Fetching location details for pincode: {clean_pincode}")
        data = await asyncio.to_thread(get_pincode_details, clean_pincode)

        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        result = data[0]
        if not isinstance(result, dict) or result.get("Status") != "Success" or not result.get("PostOffice"):
            return None

        post_office = result["PostOffice"][0]
        if not isinstance(post_office, dict) or "District" not in post_office or "State" not in post_office:
            return None

        location = {
            "pincode": clean_pincode,
            "city": post_office["District"],
            "state": post_office["State"],
        }
        logger.info(
            f"Successfully fetched location for {clean_pincode}: "
            f"{location['city']}, {location['state']}"
        )
        return location

    except Exception as e:
        logger.warning(
            f"Could not fetch location for pincode {clean_pincode} returning none: {e}"
        )
        return None
