"""Search criteria and application configuration."""

import os
from dataclasses import dataclass, field
from typing import Final

# Target location: Shibuya DT Building (渋谷DTビル, 道玄坂1-16-10)
TARGET_LAT: Final[float] = 35.656619
TARGET_LNG: Final[float] = 139.697314
SEARCH_RADIUS_KM: Final[float] = 3.0


@dataclass(frozen=True)
class SearchCriteria:
    """Immutable search criteria for property scraping."""

    rent_min: int = 50000  # 5万円
    rent_max: int = 130000  # 13万円（管理費込み上限、理想は12〜12.5万）
    layouts: tuple[str, ...] = ("1R", "1K", "1DK", "1LDK", "2K")
    structures: tuple[str, ...] = ("RC", "SRC")  # 鉄筋コンクリート, 鉄骨鉄筋コンクリート
    max_age_years: int = 15  # 築15年以内
    max_walk_minutes: int = 10  # 駅徒歩10分以内

    # Must-have conditions
    bath_toilet_separate: bool = True  # BT別
    prefer_south_facing: bool = True  # 南向き優先
    min_stove_burners: int = 2  # 2口コンロ以上
    washlet: bool = True  # ウォシュレット
    indoor_drying: bool = True  # 室内物干し
    city_gas: bool = True  # 都市ガス
    indoor_laundry: bool = True  # 室内洗濯機置場
    delivery_box: bool = True  # 宅配ボックス

    # Nice-to-have
    anytime_trash: bool = True  # 24時間ゴミ出し


@dataclass(frozen=True)
class ScrapingConfig:
    """Immutable scraping behavior configuration."""

    headless: bool = True
    max_pages_per_site: int = 20
    request_delay_sec: float = 2.0  # polite crawling delay
    timeout_sec: int = 30


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
