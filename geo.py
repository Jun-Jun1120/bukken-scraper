"""Geocoding and distance filtering for properties.

Two-stage filter:
  1. Near at least one target station (walk_radius_km per station)
  2. Within DT_RADIUS_KM of the DT building (hard constraint from rent subsidy)

Primary geocoder: GSI (Japanese government, free). Falls back to Google
Geocoding API when available and GSI fails.
"""

import dataclasses
import logging
import math
import urllib.parse

import httpx

from config import DT_LAT, DT_LNG, DT_RADIUS_KM, GOOGLE_MAPS_API_KEY
from scrapers import Property
from stations import STATIONS, Station

logger = logging.getLogger(__name__)


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


# Known ward centers for quick rough filtering (centers can be 3-5km from edges).
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


def _nearest_station(lat: float, lng: float) -> tuple[Station, float]:
    """Return closest station and its distance in km."""
    best: tuple[Station, float] | None = None
    for station in STATIONS:
        d = haversine_km(lat, lng, station.lat, station.lng)
        if best is None or d < best[1]:
            best = (station, d)
    assert best is not None  # STATIONS is non-empty by construction
    return best


def _max_search_radius_km() -> float:
    """Widest station walk radius — used for ward pre-filtering."""
    return max(s.walk_radius_km for s in STATIONS)


def _ward_passes_prefilter(address: str) -> bool:
    """Quick ward-center filter. True = keep for geocoding; False = too far.

    Uses a generous buffer since ward centers differ from actual coordinates
    by several km. Only excludes properties whose ward is clearly out of range.
    """
    ward_buffer_km = 4.0
    for ward, (lat, lng) in KNOWN_COORDS.items():
        if ward in address:
            # Reject only if ward center is far from both DT and every station.
            if haversine_km(lat, lng, DT_LAT, DT_LNG) > DT_RADIUS_KM + ward_buffer_km:
                return False
            min_station_dist = min(
                haversine_km(lat, lng, s.lat, s.lng) for s in STATIONS
            )
            if min_station_dist > _max_search_radius_km() + ward_buffer_km:
                return False
            return True
    # Unknown ward — keep to be safe.
    return True


async def _geocode_gsi(address: str, client: httpx.AsyncClient) -> tuple[float, float] | None:
    """Geocode via Japan GSI address search (free, no key)."""
    try:
        encoded = urllib.parse.quote(address)
        response = await client.get(
            f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={encoded}",
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            if data:
                coords = data[0].get("geometry", {}).get("coordinates", [])
                if len(coords) == 2:
                    return (coords[1], coords[0])  # GSI returns [lng, lat]
    except Exception:
        logger.debug("GSI geocode failed: %s", address)
    return None


async def _geocode_google(
    address: str, client: httpx.AsyncClient
) -> tuple[float, float] | None:
    """Geocode via Google Maps Geocoding API (requires GOOGLE_MAPS_API_KEY)."""
    if not GOOGLE_MAPS_API_KEY:
        return None
    try:
        response = await client.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={
                "address": address,
                "key": GOOGLE_MAPS_API_KEY,
                "region": "jp",
                "language": "ja",
            },
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            if results:
                loc = results[0].get("geometry", {}).get("location", {})
                lat = loc.get("lat")
                lng = loc.get("lng")
                if lat is not None and lng is not None:
                    return (float(lat), float(lng))
    except Exception:
        logger.debug("Google geocode failed: %s", address)
    return None


async def geocode_address(
    address: str, client: httpx.AsyncClient
) -> tuple[float, float] | None:
    """Geocode an address, trying GSI first then Google as fallback."""
    coords = await _geocode_gsi(address, client)
    if coords is not None:
        return coords
    return await _geocode_google(address, client)


async def filter_by_distance(properties: list[Property]) -> list[Property]:
    """Keep properties close to any target station AND within DT 3km.

    Stage 1: Ward-level prefilter (drop obviously-out-of-range wards).
    Stage 2: Geocode address, check (min station dist ≤ walk_radius)
             AND (DT distance ≤ DT_RADIUS_KM).
    """
    candidates: list[Property] = [
        p for p in properties if _ward_passes_prefilter(p.address)
    ]
    logger.info(
        "Ward prefilter: %d → %d candidates",
        len(properties), len(candidates),
    )

    filtered: list[Property] = []
    geocode_failures = 0
    async with httpx.AsyncClient() as client:
        for prop in candidates:
            coords = await geocode_address(prop.address, client)
            if coords is None:
                # Can't decide — include to be safe.
                geocode_failures += 1
                filtered.append(prop)
                continue

            lat, lng = coords
            dt_dist = haversine_km(lat, lng, DT_LAT, DT_LNG)
            if dt_dist > DT_RADIUS_KM:
                logger.debug(
                    "Drop (DT %.2fkm): %s - %s", dt_dist, prop.name, prop.address,
                )
                continue

            station, station_dist = _nearest_station(lat, lng)
            if station_dist > station.walk_radius_km:
                logger.debug(
                    "Drop (nearest %s %.2fkm > %.2fkm): %s",
                    station.name, station_dist, station.walk_radius_km, prop.name,
                )
                continue

            # Attach pre-computed nearest-station info so the AI evaluator
            # doesn't have to re-parse station_access strings.
            enriched = dataclasses.replace(
                prop,
                nearest_station_name=station.name,
                nearest_station_distance_km=round(station_dist, 2),
            )
            filtered.append(enriched)

    logger.info(
        "Distance filter: %d → %d properties (station ∩ DT 3km); "
        "%d ungeocodeable kept",
        len(candidates), len(filtered), geocode_failures,
    )
    return filtered
