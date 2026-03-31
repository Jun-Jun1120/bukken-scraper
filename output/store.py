"""JSON-based property data store for the web app."""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ai.evaluator import Evaluation
from scrapers import Property

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
STORE_PATH = Path(__file__).parent / "data.json"
LIKES_PATH = Path(__file__).parent.parent / "docs" / "likes.json"


def _to_dict(prop: Property, ev: Evaluation) -> dict:
    """Convert property + evaluation to a serializable dict."""
    return {
        "source": prop.source,
        "url": prop.url,
        "name": prop.name,
        "address": prop.address,
        "rent": prop.rent,
        "management_fee": prop.management_fee,
        "total_rent": prop.total_rent,
        "deposit": prop.deposit,
        "key_money": prop.key_money,
        "layout": prop.layout,
        "area_sqm": prop.area_sqm,
        "floor": prop.floor,
        "building_type": prop.building_type,
        "year_built": prop.year_built,
        "direction": prop.direction,
        "station_access": prop.station_access,
        "features": list(prop.features),
        "image_url": prop.image_url,
        "score": ev.score,
        "recommendation": ev.recommendation,
        "pros": list(ev.pros),
        "cons": list(ev.cons),
        "comment": ev.comment,
        "liked": False,
        "scraped_at": datetime.now(JST).isoformat(),
    }


def _save_all(data: list[dict]) -> None:
    """Write all data to store."""
    STORE_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_results(results: list[tuple[Property, Evaluation]]) -> int:
    """Save results to JSON store, deduplicating by URL. Returns new count."""
    existing = load_all()
    existing_urls = {p["url"] for p in existing}

    new_items = [
        _to_dict(prop, ev)
        for prop, ev in results
        if prop.url not in existing_urls
    ]

    if not new_items:
        logger.info("No new properties to save")
        return 0

    all_items = existing + new_items
    _save_all(all_items)
    logger.info("Saved %d new properties (total: %d)", len(new_items), len(all_items))
    return len(new_items)


def load_all() -> list[dict]:
    """Load all properties from JSON store."""
    if not STORE_PATH.exists():
        return []
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def toggle_like(prop_url: str) -> bool:
    """Toggle liked status for a property. Returns new liked state."""
    data = load_all()
    new_liked = False
    for item in data:
        if item["url"] == prop_url:
            item["liked"] = not item.get("liked", False)
            new_liked = item["liked"]
            break
    _save_all(data)
    return new_liked


def _load_likes_urls() -> set[str]:
    """Load liked URLs from docs/likes.json (synced from browser)."""
    if LIKES_PATH.exists():
        try:
            return set(json.loads(LIKES_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def get_liked() -> list[dict]:
    """Return all liked properties from likes.json or data.json."""
    likes_urls = _load_likes_urls()
    all_props = load_all()
    if likes_urls:
        return [p for p in all_props if p.get("url") in likes_urls]
    return [p for p in all_props if p.get("liked")]


def get_preferences() -> dict:
    """Analyze liked properties to extract user preferences."""
    liked = get_liked()
    if not liked:
        return {"count": 0, "summary": "まだいいねがありません", "patterns": {}}

    rents = [p["total_rent"] for p in liked if p["total_rent"]]
    areas = [p["area_sqm"] for p in liked if p["area_sqm"]]
    layouts = [p["layout"] for p in liked if p["layout"]]
    stations = []
    for p in liked:
        if p.get("station_access"):
            # Extract station name
            parts = p["station_access"].split("/")
            for part in parts:
                if "駅" in part:
                    station = part.split("駅")[0].split("/")[-1].strip() + "駅"
                    stations.append(station)
                    break

    # Count layout preferences
    layout_counts = {}
    for l in layouts:
        layout_counts[l] = layout_counts.get(l, 0) + 1

    # Count station preferences
    station_counts = {}
    for s in stations:
        station_counts[s] = station_counts.get(s, 0) + 1

    # Top liked features
    liked_pros = []
    for p in liked:
        liked_pros.extend(p.get("pros", []))

    patterns = {
        "avg_rent": int(sum(rents) / len(rents)) if rents else 0,
        "rent_range": [min(rents), max(rents)] if rents else [],
        "avg_area": round(sum(areas) / len(areas), 1) if areas else 0,
        "area_range": [min(areas), max(areas)] if areas else [],
        "preferred_layouts": sorted(layout_counts.items(), key=lambda x: -x[1]),
        "preferred_stations": sorted(station_counts.items(), key=lambda x: -x[1])[:5],
        "liked_features": liked_pros[:10],
    }

    summary_parts = []
    if rents:
        summary_parts.append(f"家賃{min(rents)//10000}〜{max(rents)//10000}万円")
    if areas:
        summary_parts.append(f"面積{min(areas)}〜{max(areas)}㎡")
    if layout_counts:
        top_layout = max(layout_counts, key=layout_counts.get)
        summary_parts.append(f"{top_layout}が好み")
    if station_counts:
        top_station = max(station_counts, key=station_counts.get)
        summary_parts.append(f"{top_station}周辺")

    return {
        "count": len(liked),
        "summary": "、".join(summary_parts) if summary_parts else "分析中...",
        "patterns": patterns,
        "liked_properties": [
            {"name": p["name"], "total_rent": p["total_rent"], "layout": p["layout"],
             "area_sqm": p["area_sqm"], "station_access": p["station_access"],
             "pros": p.get("pros", [])}
            for p in liked
        ],
    }
