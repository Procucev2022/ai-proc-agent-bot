"""
Location service for handling geographic operations.

This service handles:
- Converting pincodes to latitude/longitude coordinates
- Calculating distances between locations
- Managing location-based seller filtering
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from geopy.distance import geodesic
import json
import os
from ..utils.pincode_lookup import get_location_from_pincode_async

logger = logging.getLogger(__name__)

class LocationService:
    """
    Service for location-based operations including pincode to coordinate conversion
    and distance calculations for seller recommendation system.
    """
    
    def __init__(self):
        # Major Indian city coordinates (pincode to lat/lng mapping)
        # In production, this would come from a comprehensive database or API
        self.pincode_coordinates = {
            # Karnataka
            "560001": {"lat": 12.9716, "lng": 77.5946, "city": "Bangalore", "state": "Karnataka"},
            "560002": {"lat": 12.9810, "lng": 77.5946, "city": "Bangalore", "state": "Karnataka"},
            "560003": {"lat": 12.9647, "lng": 77.5834, "city": "Bangalore", "state": "Karnataka"},
            "560004": {"lat": 12.9520, "lng": 77.5838, "city": "Bangalore", "state": "Karnataka"},
            "560005": {"lat": 12.9591, "lng": 77.6069, "city": "Bangalore", "state": "Karnataka"},
            "560010": {"lat": 12.9698, "lng": 77.6124, "city": "Bangalore", "state": "Karnataka"},
            "560025": {"lat": 12.9352, "lng": 77.6245, "city": "Bangalore", "state": "Karnataka"},
            "560050": {"lat": 12.9279, "lng": 77.6271, "city": "Bangalore", "state": "Karnataka"},
            
            # Maharashtra
            "400001": {"lat": 18.9388, "lng": 72.8354, "city": "Mumbai", "state": "Maharashtra"},
            "400002": {"lat": 18.9365, "lng": 72.8297, "city": "Mumbai", "state": "Maharashtra"},
            "400020": {"lat": 18.9667, "lng": 72.8333, "city": "Mumbai", "state": "Maharashtra"},
            "411001": {"lat": 18.5204, "lng": 73.8567, "city": "Pune", "state": "Maharashtra"},
            "411002": {"lat": 18.5089, "lng": 73.8553, "city": "Pune", "state": "Maharashtra"},
            
            # Delhi
            "110001": {"lat": 28.6448, "lng": 77.2167, "city": "Delhi", "state": "Delhi"},
            "110002": {"lat": 28.6507, "lng": 77.2334, "city": "Delhi", "state": "Delhi"},
            "110005": {"lat": 28.6469, "lng": 77.2545, "city": "Delhi", "state": "Delhi"},
            
            # Tamil Nadu
            "600001": {"lat": 13.0827, "lng": 80.2707, "city": "Chennai", "state": "Tamil Nadu"},
            "600002": {"lat": 13.0878, "lng": 80.2785, "city": "Chennai", "state": "Tamil Nadu"},
            "600020": {"lat": 13.0569, "lng": 80.2407, "city": "Chennai", "state": "Tamil Nadu"},
            
            # Gujarat
            "380001": {"lat": 23.0225, "lng": 72.5714, "city": "Ahmedabad", "state": "Gujarat"},
            "380015": {"lat": 23.0395, "lng": 72.5660, "city": "Ahmedabad", "state": "Gujarat"},
            
            # West Bengal
            "700001": {"lat": 22.5726, "lng": 88.3639, "city": "Kolkata", "state": "West Bengal"},
            "700020": {"lat": 22.5448, "lng": 88.3426, "city": "Kolkata", "state": "West Bengal"},
            
            # Telangana
            "500001": {"lat": 17.3850, "lng": 78.4867, "city": "Hyderabad", "state": "Telangana"},
            "500020": {"lat": 17.4065, "lng": 78.4772, "city": "Hyderabad", "state": "Telangana"},
            
            # Uttar Pradesh
            "201301": {"lat": 28.4595, "lng": 77.0266, "city": "Gurgaon", "state": "Haryana"},
            "226001": {"lat": 26.8467, "lng": 80.9462, "city": "Lucknow", "state": "Uttar Pradesh"},
            
            # Rajasthan
            "302001": {"lat": 26.9124, "lng": 75.7873, "city": "Jaipur", "state": "Rajasthan"},
            "302012": {"lat": 26.9124, "lng": 75.7873, "city": "Jaipur", "state": "Rajasthan"},
            
            # Kerala
            "695001": {"lat": 8.5241, "lng": 76.9366, "city": "Thiruvananthapuram", "state": "Kerala"},
            "682001": {"lat": 9.9312, "lng": 76.2673, "city": "Kochi", "state": "Kerala"},
            
            # Punjab
            "160001": {"lat": 30.7333, "lng": 76.7794, "city": "Chandigarh", "state": "Punjab"},
        }
        
        # Default coordinates for unknown pincodes (Bangalore as fallback)
        self.default_coordinates = {"lat": 12.9716, "lng": 77.5946, "city": "Unknown", "state": "Unknown"}
        
        logger.info(f"LocationService initialized with {len(self.pincode_coordinates)} pincode mappings")
    
    async def get_coordinates_from_pincode(self, pincode: str) -> Dict[str, Any]:
        """
        Convert pincode to city/state using India Post API only.
        
        Args:
            pincode: 6-digit Indian pincode
            
        Returns:
            Dictionary with city, state information or None if invalid
        """
        try:
            clean_pincode = str(pincode).strip()
            
            if not clean_pincode.isdigit() or len(clean_pincode) != 6:
                logger.warning(f"Invalid pincode format: {pincode}")
                return None
            
            # Use API result only
            api_result = await get_location_from_pincode_async(clean_pincode)
            
            if api_result:
                logger.info(f"Found location via API for pincode {clean_pincode}: {api_result['city']}, {api_result['state']}")
                return {
                    "city": api_result.get("city", ""),
                    "state": api_result.get("state", "")
                }
            
            # If API fails, use regional approximation for state only
            logger.info(f"API lookup failed for pincode {clean_pincode}, using regional approximation")
            region = clean_pincode[:2]
            region_states = {
                "11": "Delhi", "12": "Punjab", "13": "Haryana", "30": "Rajasthan",
                "38": "Gujarat", "40": "Maharashtra", "41": "Maharashtra", 
                "50": "Telangana", "56": "Karnataka", "60": "Tamil Nadu",
                "68": "Kerala", "70": "West Bengal"
            }
            return {
                "city": "",
                "state": region_states.get(region, "")
            }
                
        except Exception as e:
            logger.error(f"Error converting pincode {pincode} to coordinates: {str(e)}")
            return None
    
    async def _get_approximate_coordinates(self, pincode: str) -> Dict[str, Any]:
        """
        Get approximate coordinates based on pincode region (first 2 digits).
        
        Indian postal regions:
        1x - Delhi, Punjab, Haryana
        2x - Himachal Pradesh, Punjab  
        3x - Rajasthan, Gujarat
        4x - Maharashtra, Goa
        5x - Andhra Pradesh, Telangana
        6x - Tamil Nadu, Kerala
        7x - West Bengal, Odisha
        8x - Bihar, Jharkhand
        """
        region = pincode[:2]
        
        region_centers = {
            "11": {"lat": 28.6139, "lng": 77.2090, "city": "Delhi Region", "state": "Delhi"},
            "12": {"lat": 30.7333, "lng": 76.7794, "city": "Punjab Region", "state": "Punjab"},
            "13": {"lat": 29.0588, "lng": 76.0856, "city": "Haryana Region", "state": "Haryana"},
            "30": {"lat": 26.9124, "lng": 75.7873, "city": "Rajasthan Region", "state": "Rajasthan"},
            "38": {"lat": 23.0225, "lng": 72.5714, "city": "Gujarat Region", "state": "Gujarat"},
            "40": {"lat": 18.9388, "lng": 72.8354, "city": "Maharashtra Region", "state": "Maharashtra"},
            "41": {"lat": 18.5204, "lng": 73.8567, "city": "Maharashtra Region", "state": "Maharashtra"},
            "50": {"lat": 17.3850, "lng": 78.4867, "city": "Telangana Region", "state": "Telangana"},
            "56": {"lat": 12.9716, "lng": 77.5946, "city": "Karnataka Region", "state": "Karnataka"},
            "60": {"lat": 13.0827, "lng": 80.2707, "city": "Tamil Nadu Region", "state": "Tamil Nadu"},
            "68": {"lat": 9.9312, "lng": 76.2673, "city": "Kerala Region", "state": "Kerala"},
            "70": {"lat": 22.5726, "lng": 88.3639, "city": "West Bengal Region", "state": "West Bengal"},
        }
        
        return region_centers.get(region, self.default_coordinates.copy())
    
    async def calculate_distance(self, coord1: Dict[str, float], coord2: Dict[str, float]) -> float:
        """
        Calculate distance between two coordinate points in kilometers.
        
        Args:
            coord1: Dictionary with 'lat' and 'lng' keys
            coord2: Dictionary with 'lat' and 'lng' keys
            
        Returns:
            Distance in kilometers
        """
        try:
            if not all(key in coord1 for key in ['lat', 'lng']) or not all(key in coord2 for key in ['lat', 'lng']):
                logger.error("Invalid coordinate format for distance calculation")
                return float('inf')  # Return infinite distance for invalid coordinates
            
            point1 = (coord1['lat'], coord1['lng'])
            point2 = (coord2['lat'], coord2['lng'])
            
            distance = geodesic(point1, point2).kilometers
            
            logger.debug(f"Distance calculated: {distance:.2f} km between {point1} and {point2}")
            return round(distance, 2)
            
        except Exception as e:
            logger.error(f"Error calculating distance: {str(e)}")
            return float('inf')
    
    async def get_delivery_location(self, delivery_location: Dict[str, str]) -> Dict[str, Any]:
        """
        Get location info for RFQ delivery location.
        
        Args:
            delivery_location: Dictionary with 'state', 'city', 'pincode'
            
        Returns:
            Dictionary with location info
        """
        try:
            pincode = delivery_location.get('pincode', '')
            city = delivery_location.get('city', '')
            state = delivery_location.get('state', '')
            
            # Get location from pincode
            location = await self.get_coordinates_from_pincode(pincode)
            
            if location:
                # Use provided city/state if API didn't return them
                if not location.get('city') and city:
                    location['city'] = city
                if not location.get('state') and state:
                    location['state'] = state
            else:
                location = {'city': city, 'state': state}
            
            logger.info(f"Delivery location: {location.get('city', '')}, {location.get('state', '')} ({pincode})")
            return location
            
        except Exception as e:
            logger.error(f"Error getting delivery location: {str(e)}")
            return {'city': '', 'state': ''}
    
    async def filter_sellers_by_distance(self, sellers: List[Dict[str, Any]], 
                                       delivery_coordinates: Dict[str, float], 
                                       max_distance_km: float) -> List[Dict[str, Any]]:
        """
        Filter sellers based on distance from delivery location.
        
        Args:
            sellers: List of seller dictionaries with location data
            delivery_coordinates: Delivery location coordinates
            max_distance_km: Maximum distance in kilometers
            
        Returns:
            Filtered list of sellers with distance information added
        """
        try:
            filtered_sellers = []
            
            for seller in sellers:
                seller_location = seller.get('location', {})
                
                # Ensure seller has coordinates
                if not all(key in seller_location for key in ['lat', 'lng']):
                    logger.warning(f"Seller {seller.get('seller_id')} missing coordinates")
                    continue
                
                # Calculate distance
                distance = await self.calculate_distance(seller_location, delivery_coordinates)
                
                # Check if within range
                if distance <= max_distance_km:
                    seller_with_distance = seller.copy()
                    seller_with_distance['distance_km'] = distance
                    filtered_sellers.append(seller_with_distance)
                    
                    logger.debug(f"Seller {seller.get('seller_name')} is {distance}km away (within {max_distance_km}km)")
                else:
                    logger.debug(f"Seller {seller.get('seller_name')} is {distance}km away (outside {max_distance_km}km)")
            
            # Sort by distance (nearest first)
            filtered_sellers.sort(key=lambda x: x['distance_km'])
            
            logger.info(f"Filtered {len(filtered_sellers)} sellers within {max_distance_km}km of delivery location")
            
            return filtered_sellers
            
        except Exception as e:
            logger.error(f"Error filtering sellers by distance: {str(e)}")
            return []
    
    def add_pincode_mapping(self, pincode: str, lat: float, lng: float, city: str, state: str) -> None:
        """
        Add a new pincode to coordinate mapping.
        
        Args:
            pincode: 6-digit pincode
            lat: Latitude
            lng: Longitude  
            city: City name
            state: State name
        """
        self.pincode_coordinates[pincode] = {
            "lat": lat,
            "lng": lng,
            "city": city,
            "state": state
        }
        logger.info(f"Added pincode mapping: {pincode} -> {city}, {state}")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get location service statistics."""
        return {
            "total_pincode_mappings": len(self.pincode_coordinates),
            "supported_states": len(set(coord['state'] for coord in self.pincode_coordinates.values())),
            "supported_cities": len(set(coord['city'] for coord in self.pincode_coordinates.values())),
            "service_name": "LocationService"
        }