"""Search criteria and application configuration."""

import os
from dataclasses import dataclass, field
from typing import Final

# Search targets are defined in stations.py (multi-station).
# The DT building (渋谷 DTビル) is the employer and imposes a hard 3km constraint
# from the rent subsidy program — every property must fall within this radius
# regardless of which station it is close to.
DT_LAT: Final[float] = 35.656619
DT_LNG: Final[float] = 139.697314
DT_RADIUS_KM: Final[float] = 3.0

# Optional Google Geocoding fallback. Leave unset to rely on GSI only.
GOOGLE_MAPS_API_KEY: Final[str] = os.environ.get("GOOGLE_MAPS_API_KEY", "")


@dataclass(frozen=True)
class SearchCriteria:
    """Immutable search criteria for property scraping.

    Only fields that map to site URL filters are used to narrow the search at
    source. Other preferences are applied at AI scoring time (see
    ai/evaluator.py) so the hard filter stays broad and we don't drop good
    candidates over a single missing spec.
    """

    # Rent / layout / walk — absolute constraints
    rent_min: int = 50000  # 5万円
    rent_max: int = 150000  # 検索上限15万（13.5万超はAI側でスコア0）
    layouts: tuple[str, ...] = ("1R", "1K", "1DK", "1LDK", "2K")
    max_walk_minutes: int = 10  # 駅徒歩10分以内

    # Building — 軽量鉄骨/木造は弾く、鉄骨もAI減点方向
    structures: tuple[str, ...] = ("RC", "SRC", "重量鉄骨")
    max_age_years: int = 20  # 築20年以内（以前は25年。防音と設備古さを重視して短縮）

    # Hard filters kept at source
    bath_toilet_separate: bool = True  # BT別は絶対条件
    indoor_drying: bool = True  # 室内物干し（生活必需）

    # Previously hard, now soft (AI scoring handles precedence)
    prefer_south_facing: bool = True  # 日当たり・南向きはAI加点
    min_stove_burners: int = 1  # 2口は nice-to-have に緩和
    washlet: bool = False  # 洗浄機能不要。暖房便座はAI側で要求
    city_gas: bool = False  # 都市ガスは nice-to-have
    indoor_laundry: bool = True  # 室内洗濯機置場
    delivery_box: bool = False  # 宅配BOXは nice-to-have

    # Nice-to-have (not a filter, only an AI signal)
    anytime_trash: bool = True  # 24時間ゴミ出し


@dataclass(frozen=True)
class ScrapingConfig:
    """Immutable scraping behavior configuration."""

    headless: bool = True
    max_pages_per_site: int = 20
    request_delay_sec: float = 2.0  # polite crawling delay
    timeout_sec: int = 30
    detail_enrichment_cap: int = 500  # skip detail page visits above this count


@dataclass(frozen=True)
class SheetsConfig:
    """Google Sheets output configuration."""

    credentials_path: str = "credentials.json"
    spreadsheet_id: str = field(default_factory=lambda: os.environ.get("SHEETS_ID", ""))
    worksheet_name: str = "データ"
    share_with_email: str = field(default_factory=lambda: os.environ.get("SHEETS_EMAIL", ""))


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    search: SearchCriteria = field(default_factory=SearchCriteria)
    scraping: ScrapingConfig = field(default_factory=ScrapingConfig)
    sheets: SheetsConfig = field(default_factory=SheetsConfig)
    gemini_model: str = "gemini-3-flash-preview"
    schedule_interval_hours: int = 24
