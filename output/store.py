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
DISLIKES_PATH = Path(__file__).parent.parent / "docs" / "dislikes.json"
MAYBES_PATH = Path(__file__).parent.parent / "docs" / "maybes.json"
NOTES_PATH = Path(__file__).parent.parent / "docs" / "notes.json"


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
    """Save results, accumulating across runs.

    - New properties are added
    - Existing properties get their scores/evaluations updated
    - Properties no longer in results (delisted) are removed
    - Liked/disliked status is preserved across updates
    """
    existing = load_all()
    existing_by_url = {p["url"]: p for p in existing}
    likes_urls = _load_likes_urls()
    dislikes_urls = _load_dislikes_urls()

    # Build new result set
    current_urls: set[str] = set()
    new_count = 0
    updated_count = 0
    merged: list[dict] = []

    for prop, ev in results:
        current_urls.add(prop.url)
        item = _to_dict(prop, ev)

        # Preserve liked/disliked status
        old = existing_by_url.get(prop.url)
        if old:
            item["liked"] = old.get("liked", False)
            item["scraped_at"] = old.get("scraped_at", item["scraped_at"])
            updated_count += 1
        else:
            new_count += 1

        # Apply likes/dislikes from synced files
        if prop.url in likes_urls:
            item["liked"] = True
        if prop.url in dislikes_urls:
            item["disliked"] = True

        merged.append(item)

    # Keep liked/disliked properties even if delisted (user explicitly marked)
    kept_delisted = 0
    for old in existing:
        url = old.get("url", "")
        if url not in current_urls:
            if old.get("liked") or url in likes_urls:
                old["delisted"] = True
                merged.append(old)
                kept_delisted += 1
            # Otherwise: property is delisted and not liked → drop it

    _save_all(merged)
    removed = len(existing) - updated_count - kept_delisted
    logger.info(
        "Store: %d new, %d updated, %d removed (delisted), %d kept (liked+delisted), total: %d",
        new_count, updated_count, max(0, removed), kept_delisted, len(merged),
    )
    return new_count


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


def _load_dislikes_urls() -> set[str]:
    """Load disliked URLs from docs/dislikes.json."""
    if DISLIKES_PATH.exists():
        try:
            return set(json.loads(DISLIKES_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _load_maybes_urls() -> set[str]:
    """Load maybe URLs from docs/maybes.json."""
    if MAYBES_PATH.exists():
        try:
            return set(json.loads(MAYBES_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _load_notes() -> dict[str, dict]:
    """Load notes from docs/notes.json."""
    if NOTES_PATH.exists():
        try:
            return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get_liked() -> list[dict]:
    """Return all liked properties from likes.json or data.json."""
    likes_urls = _load_likes_urls()
    all_props = load_all()
    if likes_urls:
        return [p for p in all_props if p.get("url") in likes_urls]
    return [p for p in all_props if p.get("liked")]


def get_disliked() -> list[dict]:
    """Return all disliked properties."""
    dislike_urls = _load_dislikes_urls()
    if not dislike_urls:
        return []
    return [p for p in load_all() if p.get("url") in dislike_urls]


def _count_items(items: list[str]) -> list[tuple[str, int]]:
    """Count occurrences and return sorted (item, count) pairs."""
    counts: dict[str, int] = {}
    for item in items:
        if item:
            counts[item] = counts.get(item, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])


def _extract_stations(props: list[dict]) -> list[str]:
    """Extract station names from properties."""
    stations = []
    for p in props:
        if not p.get("station_access"):
            continue
        import re
        matches = re.findall(r"([^\s/]+駅)", p["station_access"])
        if matches:
            stations.append(matches[0])
    return stations


def _extract_features(props: list[dict]) -> list[str]:
    """Extract all equipment features from properties."""
    features = []
    for p in props:
        features.extend(p.get("features", []))
    return features


def _extract_year_built(props: list[dict]) -> list[int]:
    """Extract build years as integers."""
    import re
    years = []
    for p in props:
        yb = p.get("year_built", "")
        m = re.search(r"(\d{4})", yb)
        if m:
            years.append(int(m.group(1)))
    return years


def get_maybe() -> list[dict]:
    """Return all maybe (微妙) properties."""
    maybe_urls = _load_maybes_urls()
    if not maybe_urls:
        return []
    return [p for p in load_all() if p.get("url") in maybe_urls]


def get_notes_with_properties() -> list[dict]:
    """Return properties that have notes, with note text attached."""
    notes = _load_notes()
    if not notes:
        return []
    all_props = load_all()
    result = []
    for p in all_props:
        url = p.get("url", "")
        if url in notes:
            note = notes[url]
            result.append({**p, "note_text": note.get("text", ""), "note_status": note.get("status", "")})
    return result


def get_preferences() -> dict:
    """Analyze liked/disliked/maybe properties and notes to extract detailed preferences."""
    liked = get_liked()
    disliked = get_disliked()
    maybe = get_maybe()
    noted = get_notes_with_properties()

    if not liked and not disliked and not maybe and not noted:
        return {"count": 0, "dislike_count": 0, "maybe_count": 0, "summary": "まだいいねがありません", "patterns": {}, "dislike_patterns": {}}

    # --- Liked patterns ---
    l_rents = [p["total_rent"] for p in liked if p.get("total_rent")]
    l_areas = [p["area_sqm"] for p in liked if p.get("area_sqm")]
    l_layouts = [p["layout"] for p in liked if p.get("layout")]
    l_stations = _extract_stations(liked)
    l_features = _extract_features(liked)
    l_years = _extract_year_built(liked)
    l_directions = [p["direction"] for p in liked if p.get("direction")]
    l_building_types = [p["building_type"] for p in liked if p.get("building_type")]
    l_floors = [p["floor"] for p in liked if p.get("floor")]

    patterns = {
        "avg_rent": int(sum(l_rents) / len(l_rents)) if l_rents else 0,
        "rent_range": [min(l_rents), max(l_rents)] if l_rents else [],
        "avg_area": round(sum(l_areas) / len(l_areas), 1) if l_areas else 0,
        "area_range": [min(l_areas), max(l_areas)] if l_areas else [],
        "preferred_layouts": _count_items(l_layouts),
        "preferred_stations": _count_items(l_stations)[:8],
        "preferred_features": _count_items(l_features)[:15],
        "preferred_directions": _count_items(l_directions),
        "preferred_building_types": _count_items(l_building_types),
        "preferred_floors": _count_items(l_floors)[:5],
        "year_range": [min(l_years), max(l_years)] if l_years else [],
        "avg_year": int(sum(l_years) / len(l_years)) if l_years else 0,
    }

    # --- Disliked patterns (what to avoid) ---
    d_rents = [p["total_rent"] for p in disliked if p.get("total_rent")]
    d_layouts = [p["layout"] for p in disliked if p.get("layout")]
    d_stations = _extract_stations(disliked)
    d_features = _extract_features(disliked)
    d_building_types = [p["building_type"] for p in disliked if p.get("building_type")]

    # Features that appear in dislikes but NOT in likes
    liked_feature_set = {f for f in l_features}
    dislike_only_features = [f for f in d_features if f not in liked_feature_set]

    dislike_patterns = {
        "avoided_layouts": _count_items(d_layouts),
        "avoided_stations": _count_items(d_stations)[:5],
        "avoided_features": _count_items(dislike_only_features)[:10],
        "avoided_building_types": _count_items(d_building_types),
    }

    # --- Summary ---
    summary_parts = []
    if l_rents:
        summary_parts.append(f"家賃{min(l_rents)//10000}〜{max(l_rents)//10000}万円")
    if l_areas:
        summary_parts.append(f"面積{min(l_areas)}〜{max(l_areas)}㎡")
    if patterns["preferred_layouts"]:
        summary_parts.append(f"{patterns['preferred_layouts'][0][0]}が好み")
    if patterns["preferred_stations"]:
        summary_parts.append(f"{patterns['preferred_stations'][0][0]}周辺")
    if l_years:
        summary_parts.append(f"築{2026 - max(l_years)}〜{2026 - min(l_years)}年")

    return {
        "count": len(liked),
        "dislike_count": len(disliked),
        "maybe_count": len(maybe),
        "summary": "、".join(summary_parts) if summary_parts else "分析中...",
        "patterns": patterns,
        "dislike_patterns": dislike_patterns,
        "liked_properties": [
            {"name": p["name"], "total_rent": p["total_rent"], "layout": p["layout"],
             "area_sqm": p["area_sqm"], "station_access": p["station_access"],
             "year_built": p.get("year_built", ""), "building_type": p.get("building_type", ""),
             "direction": p.get("direction", ""), "features": p.get("features", []),
             "pros": p.get("pros", []), "cons": p.get("cons", [])}
            for p in liked
        ],
        "maybe_properties": [
            {"name": p["name"], "total_rent": p["total_rent"], "layout": p["layout"],
             "cons": p.get("cons", []), "pros": p.get("pros", [])}
            for p in maybe[:5]
        ],
        "disliked_properties": [
            {"name": p["name"], "total_rent": p["total_rent"], "layout": p["layout"],
             "cons": p.get("cons", [])}
            for p in disliked[:5]
        ],
        "user_notes": [
            {"name": n["name"], "note": n["note_text"], "status": n["note_status"]}
            for n in noted if n.get("note_text")
        ],
    }
