"""Geocoding and distance filtering for properties.

Uses the Geolonia Address API (free, no key required) to geocode
Japanese addresses and filter by distance from target location.
"""

import logging
import math
import urllib.parse

import httpx

from config import TARGET_LAT, TARGET_LNG, SEARCH_RADIUS_KM
from scrapers import Property

logger = logging.getLogger(__name__)

GEOLONIA_API = "https://geolonia.github.io/japanese-addresses-api/ja"


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance in km between two lat/lng points."""
    r = 6371.0  # Earth radius in km
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Known address → coordinates mapping for common areas
# (avoids API calls for frequently seen addresses)
KNOWN_COORDS: dict[str, tuple[float, float]] = {
    "渋谷区": (35.6640, 139.6982),
    "目黒区": (35.6414, 139.6981),
    "世田谷区": (35.6461, 139.6532),
    "新宿区": (35.6938, 139.7035),
    "港区": (35.6581, 139.7514),
    "中野区": (35.7078, 139.6638),
    "杉並区": (35.6994, 139.6366),
    "品川区": (35.6092, 139.7300),
    "豊島区": (35.7263, 139.7169),
    "文京区": (35.7081, 139.7521),
}


def _estimate_distance_from_address(address: str) -> float | None:
    """Estimate distance from target using known ward coordinates.

    Returns approximate distance in km, or None if ward not recognized.
    """
    for ward, (lat, lng) in KNOWN_COORDS.items():
        if ward in address:
            return haversine_km(TARGET_LAT, TARGET_LNG, lat, lng)
    return None


async def geocode_address(address: str, client: httpx.AsyncClient) -> tuple[float, float] | None:
    """Geocode a Japanese address using free Geolonia API.

    Returns (lat, lng) or None on failure.
    """
    try:
        # Extract prefecture and city for API lookup
        # Format: 東京都渋谷区xxx → tokyo/shibuya
        encoded = urllib.parse.quote(address)
        response = await client.get(
            f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded}",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                coords = data[0].get("geometry", {}).get("coordinates", [])
                if len(coords) == 2:
                    # GSI returns [lng, lat]
                    return (coords[1], coords[0])
    except Exception:
        logger.debug("Geocoding failed for: %s", address)
    return None


async def filter_by_distance(
    properties: list[Property],
    radius_km: float = SEARCH_RADIUS_KM,
) -> list[Property]:
    """Filter properties to those within radius of target location.

    Uses ward-level estimation first, then geocoding API for borderline cases.
    """
    filtered: list[Property] = []
    needs_geocoding: list[Property] = []

    # Phase 1: Quick ward-level filtering
    # Ward centers can be 3-5km from their edges, so use a generous buffer
    # to avoid falsely excluding properties near ward boundaries
    ward_buffer_km = 4.0  # wards are large; center ≠ actual location
    for prop in properties:
        dist = _estimate_distance_from_address(prop.address)
        if dist is not None:
            if dist <= radius_km + ward_buffer_km:
                # Ward is close enough that some addresses might be in range
                needs_geocoding.append(prop)
            # else: ward center is very far, safe to skip
        else:
            # Unknown ward, try geocoding
            needs_geocoding.append(prop)

    # Phase 2: Precise geocoding for borderline properties
    if needs_geocoding:
        async with httpx.AsyncClient() as client:
            for prop in needs_geocoding:
                coords = await geocode_address(prop.address, client)
                if coords:
                    dist = haversine_km(TARGET_LAT, TARGET_LNG, coords[0], coords[1])
                    if dist <= radius_km:
                        filtered.append(prop)
                    else:
                        logger.debug(
                            "Filtered out (%.1fkm): %s - %s",
                            dist, prop.name, prop.address,
                        )
                else:
                    # Can't geocode - include to be safe
                    filtered.append(prop)

    logger.info(
        "Distance filter: %d → %d properties (within %.1fkm)",
        len(properties), len(filtered), radius_km,
    )
    return filtered
