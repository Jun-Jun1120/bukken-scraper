"""Check if liked properties are still listed (no AI, no cost)."""

import json
import logging
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://bukken-dashboard.pages.dev"


def check_url(url: str) -> bool:
    """Return True if URL is still accessible (not 404/410)."""
    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": "bukken-scraper/1.0"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status < 400
    except urllib.error.HTTPError as e:
        if e.code in (404, 410, 403):
            return False
        # Other errors (500, etc.) — assume still listed
        logger.warning("HTTP %d for %s", e.code, url)
        return True
    except Exception:
        logger.warning("Failed to check: %s", url)
        return True  # Network error — don't flag as delisted


def notify_discord(delisted: list[dict]) -> None:
    """Send Discord notification about delisted properties."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook or not delisted:
        return

    lines = []
    for p in delisted[:10]:
        name = p.get("name", "?")
        rent = p.get("total_rent", 0)
        url = p.get("url", "")
        lines.append(f"**{name}** ({rent // 10000}万円)\n{url}")

    embed = {
        "title": f"\u26a0\ufe0f \u3044\u3044\u306d\u7269\u4ef6\u304c\u63b2\u8f09\u7d42\u4e86 ({len(delisted)}\u4ef6)",
        "description": "\n\n".join(lines),
        "color": 0xFF0000,
        "footer": {"text": "\u65e9\u3081\u306e\u554f\u3044\u5408\u308f\u305b\u3092\u304a\u3059\u3059\u3081\u3057\u307e\u3059\u3002"},
    }

    payload = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "bukken-scraper/1.0",
        },
    )
    try:
        urllib.request.urlopen(req)
        logger.info("Discord notification sent")
    except Exception:
        logger.exception("Discord notification failed")


def mark_delisted_in_data(delisted_urls: set[str], data_path: Path) -> None:
    """Mark delisted properties in data.json."""
    if not data_path.exists() or not delisted_urls:
        return

    data = json.loads(data_path.read_text(encoding="utf-8"))
    changed = False
    for p in data:
        if p.get("url", "") in delisted_urls and not p.get("delisted"):
            p["delisted"] = True
            changed = True

    if changed:
        data_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Marked %d properties as delisted in data.json", len(delisted_urls))


def main() -> None:
    data_path = Path("docs/data.json")
    likes_path = Path("docs/likes.json")

    if not likes_path.exists():
        logger.info("No likes.json found")
        return

    likes = set(json.loads(likes_path.read_text(encoding="utf-8")))
    if not likes:
        logger.info("No liked properties")
        return

    # Load property data for names/rents
    data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else []
    data_by_url = {p.get("url", ""): p for p in data}

    logger.info("Checking %d liked properties...", len(likes))

    delisted = []
    for url in likes:
        prop = data_by_url.get(url, {})
        if prop.get("delisted"):
            continue  # Already known
        if not check_url(url):
            logger.info("DELISTED: %s (%s)", prop.get("name", "?"), url)
            delisted.append({**prop, "url": url})
        else:
            logger.info("OK: %s", prop.get("name", url[:50]))

    if delisted:
        logger.info("%d liked properties delisted!", len(delisted))
        notify_discord(delisted)
        mark_delisted_in_data({p["url"] for p in delisted}, data_path)
    else:
        logger.info("All liked properties still listed")


if __name__ == "__main__":
    main()
