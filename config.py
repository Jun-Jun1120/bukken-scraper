"""Search criteria and application configuration."""

import os
from dataclasses import dataclass, field
from typing import Final

# Target location: 北参道駅 (副都心線) — 2026-04-09: 北参道にフォーカス決定
# 旧ターゲット(渋谷DTビル 35.656619, 139.697314)は下記 DT_LAT/LNG で補助的にキープ
# 北参道は DTビルまで直線約2.2km(LUUP8分/副都心線4分)、東新宿まで副都心線直通4分で最適駅
TARGET_LAT: Final[float] = 35.6744
TARGET_LNG: Final[float] = 139.7078
SEARCH_RADIUS_KM: Final[float] = 1.2  # 徒歩15分相当。北参道+周辺エリアをカバー

# Backup: 渋谷DTビル (家賃補助3km制約の中心点)
DT_LAT: Final[float] = 35.656619
DT_LNG: Final[float] = 139.697314
DT_RADIUS_KM: Final[float] = 3.0


@dataclass(frozen=True)
class SearchCriteria:
    """Immutable search criteria for property scraping."""

    rent_min: int = 50000  # 5万円
    rent_max: int = 150000  # 検索上限15万（管理費込み13万以下はパイプラインでフィルター）
    layouts: tuple[str, ...] = ("1R", "1K", "1DK", "1LDK", "2K")
    structures: tuple[str, ...] = ("RC", "SRC", "鉄骨")  # 木造以外OK。防音性能でAIがスコア調整
    max_age_years: int = 25  # 築25年以内（リノベ物件を拾うため緩和）
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
