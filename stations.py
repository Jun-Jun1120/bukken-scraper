"""Target stations for property search.

Each station defines a search center with its own walk radius.
Properties must be within `walk_radius_km` of at least one station
AND within DT_RADIUS_KM of the DT building (see config.py).
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


STATIONS: Final[tuple[Station, ...]] = (
    Station(
        name="北参道",
        lat=35.6744,
        lng=139.7078,
        suumo_ek_code="ek_80835",
        lines=("副都心線",),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="代々木",
        lat=35.6830,
        lng=139.7024,
        suumo_ek_code="ek_41280",
        lines=("大江戸線", "JR山手線", "JR総武線"),
        ward="渋谷区",
        ward_code="13113",
    ),
    Station(
        name="国立競技場",
        lat=35.6793,
        lng=139.7147,
        suumo_ek_code="ek_14730",
        lines=("大江戸線",),
        ward="新宿区",
        ward_code="13104",
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
    ),
)


WARD_CODES: Final[tuple[str, ...]] = tuple(sorted({s.ward_code for s in STATIONS}))
