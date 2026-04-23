"""Target stations for property search.

Each station defines a search center with its own walk radius.
Properties must be within `walk_radius_km` of at least one station
AND within DT_RADIUS_KM of the DT building (see config.py).

`priority=2` stations are prioritised in the dashboard (ranking / badge).
These are stations with a non-geographic preference (e.g. partner access).
`priority=1` stations are "DT 通勤に便利な住みやすいエリア" — expanded
coverage, not specifically optimised for partner access.
"""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Station:
    """Immutable target station definition."""

    name: str
    lat: float
    lng: float
    suumo_ek_code: str
    lines: tuple[str, ...]
    ward: str
    ward_code: str
    walk_radius_km: float = 0.8
    priority: int = 1  # 2 = パートナー通勤優先 / 1 = 通常


STATIONS: Final[tuple[Station, ...]] = (
    # === priority=2: パートナー通勤で優先したい駅 ===
    Station(
        name="北参道",
        lat=35.6744,
        lng=139.7078,
        suumo_ek_code="ek_80835",
        lines=("副都心線",),
        ward="渋谷区",
        ward_code="13113",
        priority=2,
    ),
    Station(
        name="代々木",
        lat=35.6830,
        lng=139.7024,
        suumo_ek_code="ek_41280",
        lines=("大江戸線", "JR山手線", "JR総武線"),
        ward="渋谷区",
        ward_code="13113",
        priority=2,
    ),
    Station(
        name="国立競技場",
        lat=35.6793,
        lng=139.7147,
        suumo_ek_code="ek_14730",
        lines=("大江戸線",),
        ward="新宿区",
        ward_code="13104",
        priority=2,
    ),
    Station(
        name="青山一丁目",
        lat=35.6724,
        lng=139.7236,
        suumo_ek_code="ek_00250",
        lines=("大江戸線", "半蔵門線", "銀座線"),
        ward="港区",
        ward_code="13103",
        walk_radius_km=0.5,
        priority=2,
    ),
    # === priority=1: DT 3km 圏内、住みやすい駅近エリア ===
    # 山手線西側
    Station(
        name="恵比寿",
        lat=35.6468,
        lng=139.7100,
        suumo_ek_code="ek_05050",
        lines=("JR山手線", "日比谷線"),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="代官山",
        lat=35.6485,
        lng=139.7027,
        suumo_ek_code="ek_21850",
        lines=("東急東横線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="中目黒",
        lat=35.6440,
        lng=139.6992,
        suumo_ek_code="ek_27580",
        lines=("東急東横線", "日比谷線"),
        ward="目黒区",
        ward_code="13110",
    ),
    # 表参道ライン
    Station(
        name="表参道",
        lat=35.6654,
        lng=139.7122,
        suumo_ek_code="ek_07240",
        lines=("銀座線", "千代田線", "半蔵門線"),
        ward="港区",
        ward_code="13103",
    ),
    Station(
        name="明治神宮前",
        lat=35.6702,
        lng=139.7024,
        suumo_ek_code="ek_39010",
        lines=("千代田線", "副都心線"),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="外苑前",
        lat=35.6705,
        lng=139.7179,
        suumo_ek_code="ek_07450",
        lines=("銀座線",),
        ward="港区",
        ward_code="13103",
    ),
    Station(
        name="乃木坂",
        lat=35.6661,
        lng=139.7268,
        suumo_ek_code="ek_30010",
        lines=("千代田線",),
        ward="港区",
        ward_code="13103",
    ),
    # 小田急
    Station(
        name="代々木八幡",
        lat=35.6658,
        lng=139.6852,
        suumo_ek_code="ek_41310",
        lines=("小田急小田原線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="代々木上原",
        lat=35.6680,
        lng=139.6790,
        suumo_ek_code="ek_41290",
        lines=("小田急小田原線", "千代田線"),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="東北沢",
        lat=35.6666,
        lng=139.6725,
        suumo_ek_code="ek_31840",
        lines=("小田急小田原線",),
        ward="世田谷区",
        ward_code="13112",
    ),
    # 京王新線 / 井の頭線
    Station(
        name="初台",
        lat=35.6782,
        lng=139.6870,
        suumo_ek_code="ek_30800",
        lines=("京王新線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="幡ヶ谷",
        lat=35.6744,
        lng=139.6781,
        suumo_ek_code="ek_30610",
        lines=("京王新線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="神泉",
        lat=35.6558,
        lng=139.6936,
        suumo_ek_code="ek_19790",
        lines=("京王井の頭線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="駒場東大前",
        lat=35.6609,
        lng=139.6819,
        suumo_ek_code="ek_15370",
        lines=("京王井の頭線",),
        ward="目黒区",
        ward_code="13110",
    ),
    Station(
        name="池ノ上",
        lat=35.6601,
        lng=139.6721,
        suumo_ek_code="ek_02030",
        lines=("京王井の頭線",),
        ward="世田谷区",
        ward_code="13112",
    ),
    # 田園都市線
    Station(
        name="池尻大橋",
        lat=35.6504,
        lng=139.6843,
        suumo_ek_code="ek_02000",
        lines=("東急田園都市線",),
        ward="世田谷区",
        ward_code="13112",
    ),
    Station(
        name="三軒茶屋",
        lat=35.6436,
        lng=139.6695,
        suumo_ek_code="ek_16720",
        lines=("東急田園都市線", "東急世田谷線"),
        ward="世田谷区",
        ward_code="13112",
    ),
    # DT 直近 / JR 補完
    Station(
        name="渋谷",
        lat=35.6580,
        lng=139.7016,
        suumo_ek_code="ek_17640",
        lines=("JR山手線", "東急東横線", "副都心線", "半蔵門線", "銀座線", "東急田園都市線"),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="原宿",
        lat=35.6702,
        lng=139.7027,
        suumo_ek_code="ek_31250",
        lines=("JR山手線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="千駄ヶ谷",
        lat=35.6811,
        lng=139.7112,
        suumo_ek_code="ek_21520",
        lines=("JR総武線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="信濃町",
        lat=35.6801,
        lng=139.7199,
        suumo_ek_code="ek_17470",
        lines=("JR総武線",),
        ward="新宿区",
        ward_code="13104",
    ),
    Station(
        name="参宮橋",
        lat=35.6774,
        lng=139.6943,
        suumo_ek_code="ek_16710",
        lines=("小田急小田原線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="代々木公園",
        lat=35.6680,
        lng=139.6886,
        suumo_ek_code="ek_41300",
        lines=("千代田線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="下北沢",
        lat=35.6615,
        lng=139.6680,
        suumo_ek_code="ek_18010",
        lines=("小田急小田原線", "京王井の頭線"),
        ward="世田谷区",
        ward_code="13112",
    ),
    # 目黒駅は品川区（駅名の目黒区と混同注意）
    Station(
        name="目黒",
        lat=35.6337,
        lng=139.7156,
        suumo_ek_code="ek_39110",
        lines=("JR山手線", "東急目黒線", "都営三田線", "南北線"),
        ward="品川区",
        ward_code="13109",
    ),
)


WARD_CODES: Final[tuple[str, ...]] = tuple(sorted({s.ward_code for s in STATIONS}))
