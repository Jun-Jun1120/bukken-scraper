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


def _send_discord(message: str) -> bool:
    """Send notification via Discord Webhook."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        data = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "bukken-scraper/1.0",
            },
        )
        urllib.request.urlopen(req)
        logger.info("Discord notification sent")
        return True
    except Exception:
        logger.exception("Discord notification failed")
        return False


def send(message: str) -> None:
    """Send notification via available channel."""
    if not _send_line(message):
        if not _send_discord(message):
            logger.info("Notification (no channel configured): %s", message)


def notify_new_properties(data_path: str, score_threshold: int = 75) -> None:
    """Notify about new high-score properties."""
    path = Path(data_path)
    if not path.exists():
        return

    data = json.loads(path.read_text(encoding="utf-8"))
    top = [p for p in data if p.get("score", 0) >= score_threshold]

    if not top:
        logger.info("No properties above score %d", score_threshold)
        return

    top.sort(key=lambda p: p.get("score", 0), reverse=True)

    lines = [f"\n🏠 本日のおすすめ物件 ({len(top)}件)"]
    for p in top[:10]:
        rent = p.get("total_rent", 0)
        lines.append(
            f"\n⭐ {p.get('score', 0)}点 {p.get('name', '?')}"
            f"\n  {rent // 10000}万円/月 {p.get('layout', '')} {p.get('area_sqm', '')}㎡"
            f"\n  {p.get('station_access', '')}"
            f"\n  {p.get('url', '')}"
        )

    lines.append(f"\n📊 ダッシュボード: {DASHBOARD_URL}")
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

    lines = [f"\n⚠️ いいねした物件が掲載終了 ({len(delisted)}件)"]
    for url in list(delisted)[:5]:
        lines.append(f"  {url}")
    lines.append(f"\n早めの問い合わせをおすすめします。")

    send("\n".join(lines))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("Usage: python notify.py <data.json> [likes.json]")
        sys.exit(1)

    notify_new_properties(sys.argv[1])
    if len(sys.argv) >= 3:
        notify_delisted(sys.argv[1], sys.argv[2])
