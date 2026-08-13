"""
Pincode Distance Helper for calculating distances between Indian pincodes.

This utility uses pgeocode library to convert pincodes to lat/lng coordinates
and calculates distances using the geodesic (great circle) formula.

Key features:
- Converts Indian pincodes to coordinates using pgeocode
- Caches pincode lookups for performance
- Calculates accurate distances using geopy.distance.geodesic
- Handles invalid/missing pincodes gracefully
"""

import logging
from typing import Dict, Optional, Tuple
import pgeocode
from geopy.distance import geodesic

logger = logging.getLogger(__name__)

# Global pgeocode instance (initialized once)
_nomi = None

# Cache for pincode lookups (pincode -> (lat, lng))
_pincode_cache: Dict[str, Optional[Tuple[float, float]]] = {}


def _get_nominatim():
    """Get or initialize the pgeocode Nominatim instance."""
    pass
    global _nomi
    if _nomi is None:
        try:
            _nomi = pgeocode.Nominatim('in')
            logger.info("pgeocode Nominatim initialized for India")
        except Exception as e:
            logger.error(f"Failed to initialize pgeocode: {e}")
    return _nomi


def get_coordinates_from_pincode(pincode: str) -> Optional[Tuple[float, float]]:
    """
    Convert Indian pincode to lat/lng coordinates.

    Args:
        pincode: 6-digit Indian pincode

    Returns:
        Tuple of (latitude, longitude) or None if invalid/not found
    """
    if not pincode or not pincode.strip():
        logger.debug("Empty pincode provided")
        return None

    pincode = pincode.strip()

    # Check cache first
    if pincode in _pincode_cache:
        return _pincode_cache[pincode]

    # Validate pincode format
    if not pincode.isdigit() or len(pincode) != 6:
        logger.warning(f"Invalid pincode format: {pincode}")
        _pincode_cache[pincode] = None
        return None

    nomi = _get_nominatim()
    if nomi is None:
        logger.error("pgeocode not initialized")
        return None

    try:
        # Query pgeocode
        location = nomi.query_postal_code(pincode)

        if location is not None and not location.isna().all():
            lat = float(location.latitude)
            lng = float(location.longitude)

            # Validate coordinates are finite (not NaN or infinite)
            import math
            if math.isnan(lat) or math.isnan(lng) or math.isinf(lat) or math.isinf(lng):
                logger.warning(f"Pincode {pincode} returned invalid coordinates: ({lat}, {lng})")
                _pincode_cache[pincode] = None
                return None

            # Cache the result
            _pincode_cache[pincode] = (lat, lng)

            logger.debug(f"Pincode {pincode} → ({lat}, {lng})")
            return (lat, lng)
        else:
            logger.warning(f"Pincode {pincode} not found in pgeocode database")
            _pincode_cache[pincode] = None
            return None

    except Exception as e:
        logger.error(f"Error looking up pincode {pincode}: {e}")
        _pincode_cache[pincode] = None
        return None


def calculate_distance_between_pincodes(
    pincode1: str,
    pincode2: str
) -> Optional[float]:
    """
    Calculate distance in kilometers between two pincodes.

    Args:
        pincode1: First pincode
        pincode2: Second pincode

    Returns:
        Distance in kilometers, or None if either pincode is invalid
    """
    coords1 = get_coordinates_from_pincode(pincode1)
    coords2 = get_coordinates_from_pincode(pincode2)

    if coords1 is None or coords2 is None:
        logger.debug(f"Cannot calculate distance: pincode1={pincode1} coords={coords1}, pincode2={pincode2} coords={coords2}")
        return None

    try:
        # Calculate geodesic distance
        distance_km = geodesic(coords1, coords2).kilometers

        # Validate distance is finite
        import math
        if math.isnan(distance_km) or math.isinf(distance_km):
            logger.warning(f"Invalid distance calculated between {pincode1} and {pincode2}")
            return None

        logger.debug(f"Distance from {pincode1} to {pincode2}: {distance_km:.2f} km")
        return distance_km

    except Exception as e:
        logger.debug(f"Could not calculate distance between {pincode1} and {pincode2}: {str(e)}")
        return None


def calculate_distance_from_pincode_to_coords(
    pincode: str,
    target_lat: float,
    target_lng: float
) -> Optional[float]:
    """
    Calculate distance from a pincode to specific coordinates.

    Useful when RFQ delivery location already has lat/lng.

    Args:
        pincode: Seller pincode
        target_lat: Delivery latitude
        target_lng: Delivery longitude

    Returns:
        Distance in kilometers, or None if pincode is invalid
    """
    coords = get_coordinates_from_pincode(pincode)

    if coords is None:
        return None

    try:
        distance_km = geodesic(coords, (target_lat, target_lng)).kilometers
        return distance_km

    except Exception as e:
        logger.error(f"Error calculating distance from pincode {pincode} to coords ({target_lat}, {target_lng}): {e}")
        return None


def clear_pincode_cache():
    """Clear the pincode coordinate cache."""
    pass
    global _pincode_cache
    _pincode_cache.clear()
    logger.info("Pincode cache cleared")


def get_cache_size() -> int:
    """Get the number of cached pincode lookups."""
    return len(_pincode_cache)
