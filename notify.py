"""Notification module for property alerts.

Supports LINE Notify, Discord Webhook, or stdout fallback.
Configure via environment variables:
  LINE_NOTIFY_TOKEN - LINE Notify API token
  DISCORD_WEBHOOK_URL - Discord webhook URL
"""

import json
import logging
import os
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://bukken-dashboard.pages.dev"


def _send_line(message: str) -> bool:
    """Send notification via LINE Notify."""
    token = os.environ.get("LINE_NOTIFY_TOKEN", "")
    if not token:
        return False
    try:
        data = urllib.parse.urlencode({"message": message}).encode()
        req = urllib.request.Request(
            "https://notify-api.line.me/api/notify",
            data=data,
            headers={"Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req)
        logger.info("LINE notification sent")
        return True
    except Exception:
        logger.exception("LINE notification failed")
        return False


def _discord_post(payload: dict) -> bool:
    """Post JSON payload to Discord webhook."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "bukken-scraper/1.0",
        },
    )
    urllib.request.urlopen(req)
    return True


def _score_color(score: int) -> int:
    if score >= 90:
        return 0xFF4500
    if score >= 85:
        return 0xFF8C00
    if score >= 80:
        return 0xFFD700
    return 0x32CD32


def _score_emoji(score: int) -> str:
    if score >= 90:
        return "\U0001f525"
    if score >= 85:
        return "\u2b50"
    if score >= 80:
        return "\U0001f44d"
    return "\u2705"


def send(message: str) -> None:
    """Send notification via available channel (plain text)."""
    if _send_line(message):
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if url:
        try:
            for chunk in _split_message(message, limit=2000):
                _discord_post({"content": chunk})
            logger.info("Discord notification sent")
        except Exception:
            logger.exception("Discord notification failed")
    else:
        logger.info("Notification (no channel configured): %s", message)


def _split_message(message: str, limit: int = 2000) -> list[str]:
    """Split a message into chunks that fit within the character limit."""
    if len(message) <= limit:
        return [message]
    chunks = []
    lines = message.split("\n")
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line[:limit]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def notify_new_properties(data_path: str, score_threshold: int = 75) -> None:
    """Notify about new high-score properties using Discord embeds."""
    path = Path(data_path)
    if not path.exists():
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    total = len(data)
    top = [p for p in data if p.get("score", 0) >= score_threshold]

    if not top:
        logger.info("No properties above score %d", score_threshold)
        return

    top.sort(key=lambda p: p.get("score", 0), reverse=True)

    # Group by building name, pick best room per building
    grouped: dict[str, list] = {}
    for p in top:
        # Normalize name: remove "の賃貸物件情報" suffix for grouping
        raw_name = p.get("name", "?") or "?"
        key = raw_name.replace("の賃貸物件情報", "").strip()
        grouped.setdefault(key, []).append(p)

    # Pick top-scored room per building, keep extra rooms as sub-info
    buildings = []
    for key, rooms in grouped.items():
        rooms.sort(key=lambda r: r.get("score", 0), reverse=True)
        buildings.append({"best": rooms[0], "rooms": rooms, "key": key})
    buildings.sort(key=lambda b: b["best"].get("score", 0), reverse=True)

    display = buildings[:9]  # 9 + summary = 10 embeds (Discord max)

    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        # Fallback to plain text
        _notify_plain([b["best"] for b in display], total)
        return

    # Build embeds: summary + top 10 properties (compact)
    sweet = len([p for p in data if 0 < p.get("total_rent", 0) <= 125000])
    summary = {
        "title": "\U0001f3e0 \u672c\u65e5\u306e\u304a\u3059\u3059\u3081\u7269\u4ef6 Top 10",
        "description": (
            f"\u7dcf\u7269\u4ef6\u6570: **{total}**\u4ef6 | "
            f"75\u70b9\u4ee5\u4e0a: **{len(top)}**\u4ef6 | "
            f"12.5\u4e07\u4ee5\u4e0b: **{sweet}**\u4ef6\n"
            f"[\U0001f4ca \u30c0\u30c3\u30b7\u30e5\u30dc\u30fc\u30c9\u3092\u958b\u304f]({DASHBOARD_URL})"
        ),
        "color": 0x5865F2,
    }

    embeds = [summary]
    for bldg in display:
        p = bldg["best"]
        rooms = bldg["rooms"]
        score = p.get("score", 0)
        rent = p.get("total_rent", 0)
        name = bldg["key"][:40]
        layout = p.get("layout", "")
        area = p.get("area_sqm", 0)
        station = (p.get("station_access", "") or "")[:60]
        bt = p.get("building_type", "")
        yr = p.get("year_built", "")
        prop_url = p.get("url", "")

        # Compact description with key info
        info_parts = []
        if rent:
            info_parts.append(f"\U0001f4b0 **{rent // 10000}\u4e07\u5186**/\u6708")
        detail_parts = []
        if layout:
            detail_parts.append(layout)
        if area:
            detail_parts.append(f"{area}m\u00b2")
        if bt:
            detail_parts.append(bt)
        if yr:
            detail_parts.append(yr)
        if detail_parts:
            info_parts.append("\U0001f4d0 " + " / ".join(detail_parts))
        if station:
            info_parts.append(f"\U0001f689 {station}")

        # Show other available rooms in same building
        if len(rooms) > 1:
            room_lines = []
            for r in rooms[1:4]:  # Show up to 3 more rooms
                r_rent = r.get("total_rent", 0)
                r_floor = r.get("floor", "")
                r_area = r.get("area_sqm", 0)
                parts = []
                if r_floor:
                    parts.append(r_floor)
                if r_area:
                    parts.append(f"{r_area}m\u00b2")
                if r_rent:
                    parts.append(f"{r_rent // 10000}\u4e07\u5186")
                room_lines.append(" / ".join(parts))
            extra = len(rooms) - 1
            info_parts.append(
                f"\U0001f3e2 \u4ed6{extra}\u90e8\u5c4b: " + " | ".join(room_lines)
            )

        embed: dict = {
            "title": f"{_score_emoji(score)} {score}\u70b9 {name}",
            "description": "\n".join(info_parts),
            "color": _score_color(score),
        }
        if prop_url:
            embed["url"] = prop_url

        # Thumbnail for top 3
        image_url = p.get("image_url", "")
        if image_url and len(embeds) <= 3:
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            if image_url.startswith("http"):
                embed["thumbnail"] = {"url": image_url}

        embeds.append(embed)

    # Send: first batch (max 10), then overflow
    try:
        _discord_post({"embeds": embeds[:10]})
        if len(embeds) > 10:
            _discord_post({"embeds": embeds[10:]})
        logger.info("Discord notification sent (%d embeds)", len(embeds))
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        logger.error("Discord embed failed: %d %s", e.code, body)
        _notify_plain(display, total)
    except Exception:
        logger.exception("Discord notification failed")
        _notify_plain([b["best"] for b in display], total)


def _notify_plain(display: list, total: int) -> None:
    """Plain text fallback notification."""
    lines = [f"\U0001f3e0 \u672c\u65e5\u306e\u304a\u3059\u3059\u3081\u7269\u4ef6 ({total}\u4ef6\u4e2d)"]
    for p in display:
        rent = p.get("total_rent", 0)
        lines.append(
            f"\n{_score_emoji(p.get('score', 0))} {p.get('score', 0)}\u70b9 {p.get('name', '?')}"
            f"\n  {rent // 10000}\u4e07\u5186/\u6708 {p.get('layout', '')} {p.get('area_sqm', '')}m\u00b2"
            f"\n  {p.get('url', '')}"
        )
    lines.append(f"\n\U0001f4ca {DASHBOARD_URL}")
    send("\n".join(lines))


def notify_delisted(data_path: str, likes_path: str) -> None:
    """Notify if liked properties were delisted."""
    dp = Path(data_path)
    lp = Path(likes_path)
    if not dp.exists() or not lp.exists():
        return

    data = json.loads(dp.read_text(encoding="utf-8"))
    likes = set(json.loads(lp.read_text(encoding="utf-8")))

    if not likes:
        return

    current_urls = {p.get("url", "") for p in data}
    delisted = likes - current_urls

    if not delisted:
        return

    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if url:
        lines = [f"\U0001f517 {u}" for u in list(delisted)[:5]]
        embed = {
            "title": f"\u26a0\ufe0f \u3044\u3044\u306d\u7269\u4ef6\u304c\u63b2\u8f09\u7d42\u4e86 ({len(delisted)}\u4ef6)",
            "description": "\n".join(lines) + "\n\n\u65e9\u3081\u306e\u554f\u3044\u5408\u308f\u305b\u3092\u304a\u3059\u3059\u3081\u3057\u307e\u3059\u3002",
            "color": 0xFF0000,
        }
        try:
            _discord_post({"embeds": [embed]})
            return
        except Exception:
            pass

    text_lines = [f"\u26a0\ufe0f \u3044\u3044\u306d\u7269\u4ef6\u304c\u63b2\u8f09\u7d42\u4e86 ({len(delisted)}\u4ef6)"]
    for u in list(delisted)[:5]:
        text_lines.append(f"  {u}")
    text_lines.append("\u65e9\u3081\u306e\u554f\u3044\u5408\u308f\u305b\u3092\u304a\u3059\u3059\u3081\u3057\u307e\u3059\u3002")
    send("\n".join(text_lines))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python notify.py <data.json> [likes.json]")
        sys.exit(1)

    notify_new_properties(sys.argv[1])
    if len(sys.argv) >= 3:
        notify_delisted(sys.argv[1], sys.argv[2])
