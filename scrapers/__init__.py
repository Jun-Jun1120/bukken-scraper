"""Property scrapers for Japanese real estate sites."""

from dataclasses import dataclass, field


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
