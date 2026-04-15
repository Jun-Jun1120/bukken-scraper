"""Property scrapers for Japanese real estate sites."""

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Property:
    """Immutable property data collected from scraping."""

    source: str  # "suumo", "homes", "athome"
    url: str
    name: str  # 物件名
    address: str  # 住所
    rent: int  # 家賃 (円)
    management_fee: int = 0  # 管理費 (円)
    deposit: int = 0  # 敷金 (円)
    key_money: int = 0  # 礼金 (円)
    layout: str = ""  # 間取り (1K, 1LDK, etc.)
    area_sqm: float = 0.0  # 専有面積 (㎡)
    floor: str = ""  # 階数
    building_type: str = ""  # 構造 (RC, SRC, etc.)
    year_built: str = ""  # 築年
    direction: str = ""  # 向き (南, 南東, etc.)
    station_access: str = ""  # 最寄り駅・徒歩分
    features: tuple[str, ...] = field(default_factory=tuple)  # 設備・条件
    image_url: str = ""  # 物件画像URL（元サイトから直接表示）

    # Pre-computed by geo.filter_by_distance — AI reads these directly so it
    # never has to parse the station_access string to figure out which target
    # station is closest.
    nearest_station_name: str = ""
    nearest_station_distance_km: float = 0.0

    @property
    def total_rent(self) -> int:
        """Rent including management fee."""
        return self.rent + self.management_fee

    @property
    def is_female_only(self) -> bool:
        """Check if this property is female-only."""
        keywords = ("女性限定", "女性専用", "女性のみ", "レディース")
        text = f"{self.name} {' '.join(self.features)}"
        return any(k in text for k in keywords)


def needs_ai_fallback(prop: Property) -> bool:
    """Check if critical fields are missing and AI extraction should be attempted."""
    return not prop.station_access or not prop.address


# Canonical feature tag → Japanese keywords that should resolve to it across
# sites. Keywords use substring match (case-insensitive). This normalization
# lets the AI evaluator check specific capabilities without each scraper
# having to standardize raw strings.
FEATURE_TAGS: dict[str, tuple[str, ...]] = {
    # 暖房便座 (seat-heating): ウォシュレット units almost always include it,
    # so presence of either keyword implies heated_seat=True.
    "heated_seat": ("暖房便座", "温水洗浄便座", "ウォシュレット"),
    "washlet": ("ウォシュレット", "温水洗浄便座"),  # 洗浄機能 specifically
    "indoor_drying": ("室内物干し", "室内干し", "ランドリールーム"),
    "indoor_laundry": ("室内洗濯機", "室内洗濯置"),
    "delivery_box": ("宅配ボックス", "宅配BOX", "宅配box"),
    "city_gas": ("都市ガス",),
    "two_burner": ("2口コンロ", "二口コンロ", "2口ガス", "2口IH", "二口ガス", "2口以上"),
    "anytime_trash": ("24時間ゴミ", "ゴミ出し24", "24時間ごみ"),
    "elevator": ("エレベーター", "EV"),
    "auto_lock": ("オートロック",),
    "delivery_free": ("仲介手数料不要", "仲介手数料無", "AD無料"),
    "no_key_money": ("礼金なし", "礼金0", "礼金０"),
    "south_facing": ("南向き", "南面"),
}


def has_feature(features: tuple[str, ...], tag: str) -> bool:
    """Check if a property has the given normalized capability tag."""
    keywords = FEATURE_TAGS.get(tag, ())
    if not keywords:
        return False
    text = " ".join(features).lower()
    return any(k.lower() in text for k in keywords)


def normalized_features(features: tuple[str, ...]) -> dict[str, bool]:
    """Return {tag: present} for every canonical feature tag."""
    return {tag: has_feature(features, tag) for tag in FEATURE_TAGS}


# Errors that should not be retried (they won't succeed on retry)
_NON_RETRIABLE_ERROR_SNIPPETS = (
    "too_many_redirects",
    "err_too_many_redirects",
    "err_aborted",  # Usually means navigation was aborted intentionally
    "frame was detached",
)


async def goto_with_retry(
    page,
    url: str,
    timeout_ms: int = 30000,
    max_retries: int = 3,
    wait_until: str = "domcontentloaded",
    logger: Optional[logging.Logger] = None,
):
    """Load a URL with exponential backoff retry on transient errors.

    Retries on transient errors like ERR_INTERNET_DISCONNECTED, timeouts, etc.
    Does NOT retry on non-recoverable errors like redirect loops.

    Args:
        page: Playwright page object.
        url: URL to navigate to.
        timeout_ms: Per-attempt timeout in milliseconds.
        max_retries: Maximum number of attempts (initial + retries).
        wait_until: Playwright wait_until value ("domcontentloaded", "load", "commit").
        logger: Optional logger for retry messages.

    Returns:
        The Playwright Response object from the successful navigation
        (may be None if the URL did not produce a response, e.g. about:blank).

    Raises:
        The last exception encountered, if all retries failed.
    """
    last_err: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            return await page.goto(url, timeout=timeout_ms, wait_until=wait_until)
        except Exception as exc:
            last_err = exc
            err_str = str(exc).lower()

            # Non-retriable errors — fail fast
            if any(snippet in err_str for snippet in _NON_RETRIABLE_ERROR_SNIPPETS):
                if logger:
                    logger.warning("Non-retriable error, giving up: %s", err_str[:150])
                raise

            # Last attempt — re-raise
            if attempt == max_retries - 1:
                if logger:
                    logger.error(
                        "goto failed after %d attempts: %s",
                        max_retries, err_str[:150],
                    )
                raise

            # Exponential backoff with jitter: 2s, 4s, 8s (+ 0-1s jitter)
            wait_sec = (2 ** (attempt + 1)) + random.uniform(0, 1)
            if logger:
                logger.warning(
                    "goto failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries, wait_sec, err_str[:150],
                )
            await asyncio.sleep(wait_sec)

    # Should not reach here, but for type-safety
    if last_err:
        raise last_err
    return None
