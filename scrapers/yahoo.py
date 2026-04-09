"""Yahoo!不動産 property scraper using Playwright.

Site: realestate.yahoo.co.jp
Structure: SSR with __SERVER_SIDE_CONTEXT__ JSON containing properties array.
Each building has GroupProperties with individual room listings.
"""

import asyncio
import json
import logging
import re

from playwright.async_api import async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property, goto_with_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://realestate.yahoo.co.jp/rent/search/"

FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")

# StructureId to building structure name mapping
STRUCTURE_MAP = {
    "1": "RC", "2": "SRC", "3": "鉄骨", "4": "軽量鉄骨", "5": "木造",
    "rc": "RC", "src": "SRC", "s": "鉄骨", "w": "木造",
}

# KindName values that are property types, not structural types
NON_STRUCTURAL_KINDS = {"マンション", "アパート", "一戸建て", "テラスハウス", "タウンハウス", ""}

# DetailRoomLayout numeric code to layout string mapping
LAYOUT_MAP_REVERSE = {
    1: "1R",
    2: "1K",
    3: "1DK",
    4: "1LDK",
    5: "2K",
    6: "2DK",
    7: "2LDK",
    8: "3K",
    9: "3DK",
    10: "3LDK",
}


def _build_search_url(criteria: SearchCriteria) -> str:
    """Build Yahoo search URL from criteria."""
    params = [
        "sc_02=13113",  # 渋谷区
        "sc_02=13104",  # 新宿区 (北参道フォーカス 2026-04-09: 目黒/港除外)
        f"rent_min={criteria.rent_min // 10000}",
        f"rent_max={criteria.rent_max // 10000}",
    ]

    layout_map = {"1R": "1", "1K": "2", "1DK": "3", "1LDK": "4", "2K": "5"}
    for layout in criteria.layouts:
        if code := layout_map.get(layout):
            params.append(f"layout={code}")

    if criteria.bath_toilet_separate:
        params.append("option=0002")

    if criteria.city_gas:
        params.append("option=0028")

    query = "&".join(params)
    return f"{BASE_URL}?{query}&page={{}}"


def _parse_rent(text: str) -> int:
    """Parse rent string like '19.2万円' or '7,000円' to yen integer."""
    if not text:
        return 0
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_area(text: str) -> float:
    """Parse area string like '43.84m²' or '43.84m<sup>2</sup>' to float."""
    clean = re.sub(r"<[^>]+>", "", text or "")
    match = re.search(r"([\d.]+)\s*m", clean)
    return float(match.group(1)) if match else 0.0


def _extract_properties_from_json(data: dict) -> list[Property]:
    """Extract properties from __SERVER_SIDE_CONTEXT__ JSON data."""
    properties: list[Property] = []

    page_data = data.get("page", {})
    buildings = page_data.get("properties", [])

    if not buildings:
        return properties

    for building in buildings:
        try:
            building_name = building.get("BuildingName", "")
            location_view = building.get("LocationView", {})
            address = location_view.get("AddressName", "")
            kind_name = building.get("KindName", "")
            built_on = building.get("BuiltOn", "")
            years_old = building.get("YearsOld")
            structure_id = building.get("StructureId", "")
            total_floor_num = building.get("TotalFloorNum", "")

            # Resolve building_type from StructureId when KindName is not structural
            building_type = kind_name
            if kind_name in NON_STRUCTURAL_KINDS:
                structure_name = STRUCTURE_MAP.get(str(structure_id).lower(), "")
                building_type = structure_name if structure_name else kind_name

            # Year built formatting
            year_built = ""
            if built_on:
                year_built = built_on
            elif years_old is not None:
                year_built = f"築{years_old}年"

            # Station access from Transports
            transports = building.get("Transports", [])
            station_parts = []
            for t in transports:
                label = t.get("Label", "")
                if label:
                    station_parts.append(label)
                else:
                    line = t.get("LineName", "")
                    station = t.get("StationName", "")
                    minutes = t.get("MinutesFromStation", "")
                    if station:
                        part = f"{line} {station}駅" if line else f"{station}駅"
                        if minutes:
                            part += f" 徒歩{minutes}分"
                        station_parts.append(part)
            station_access = " / ".join(station_parts[:3])

            # Image URL
            image_url = building.get("ExternalImageUrl", "")
            if not image_url:
                resized = building.get("ResizedExternalImageUrls", [])
                if resized:
                    image_url = resized[0] if isinstance(resized[0], str) else ""

            # Room listings from GroupProperties
            group_props = building.get("GroupProperties", [])
            for gp in group_props:
                try:
                    property_id = gp.get("PropertyId", "")
                    price_label = gp.get("PriceLabel", "")
                    mgmt_label = gp.get("MonthlyManagementCostLabel", "")
                    key_money_label = gp.get("KeyMoneyLabel", "")
                    deposit_label = gp.get("SecurityDepositLabel", "")
                    area_label = gp.get("MonopolyAreaLabel", "")
                    floor_num = gp.get("FloorNum", "")
                    detail_layout = gp.get("DetailRoomLayout")

                    rent = _parse_rent(price_label)
                    if not rent:
                        continue

                    mgmt_fee = _parse_rent(mgmt_label)
                    key_money = _parse_rent(key_money_label)
                    deposit = _parse_rent(deposit_label)
                    area = _parse_area(area_label)

                    layout = ""
                    if detail_layout and isinstance(detail_layout, int):
                        layout = LAYOUT_MAP_REVERSE.get(detail_layout, "")

                    floor = f"{floor_num}階" if floor_num else ""

                    url = (
                        f"https://realestate.yahoo.co.jp/rent/detail/{property_id}/"
                        if property_id
                        else ""
                    )

                    properties.append(Property(
                        source="yahoo",
                        url=url,
                        name=building_name,
                        address=address,
                        rent=rent,
                        management_fee=mgmt_fee,
                        deposit=deposit,
                        key_money=key_money,
                        layout=layout,
                        area_sqm=area,
                        floor=floor,
                        building_type=building_type,
                        year_built=year_built,
                        station_access=station_access,
                        image_url=image_url,
                    ))
                except Exception:
                    logger.debug(
                        "Failed to extract Yahoo room from building: %s",
                        building_name,
                    )
        except Exception:
            logger.debug("Failed to extract Yahoo building")

    return properties


async def _enrich_from_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit Yahoo detail page to get features, direction.

    Returns enriched Property, or None if female-only.
    """
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000, wait_until="domcontentloaded")

        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("Yahoo: skipped female-only: %s", prop.name)
            return None

        building_type = prop.building_type
        direction = prop.direction
        address = prop.address
        features: list[str] = list(prop.features)

        # Try extracting from JSON context on detail page too
        try:
            json_text = await page.evaluate(
                "() => JSON.stringify(window.__SERVER_SIDE_CONTEXT__ || {})"
            )
            detail_data = json.loads(json_text) if json_text else {}
            detail_page = detail_data.get("page", {})
            property_info = detail_page.get("property", detail_page)

            if isinstance(property_info, dict):
                if not direction:
                    direction = property_info.get("Direction", "")
                if not building_type:
                    building_type = property_info.get("KindName", prop.building_type)
                equip = property_info.get("Equipments", [])
                if isinstance(equip, list):
                    for item in equip:
                        name = item if isinstance(item, str) else item.get("Name", "")
                        if name and name not in features:
                            features.append(name)
        except Exception:
            pass

        # Fallback: extract from HTML table rows
        rows = page.locator("table tr, dl")
        for ri in range(min(50, await rows.count())):
            th_el = rows.nth(ri).locator("th, dt")
            td_el = rows.nth(ri).locator("td, dd")
            if await th_el.count() == 0 or await td_el.count() == 0:
                continue
            th = (await th_el.first.text_content() or "").strip()
            td = (await td_el.first.text_content() or "").strip()
            if not th or not td:
                continue

            if ("所在地" in th or "住所" in th) and not address:
                address = td.replace("\n", "").strip()
            elif "構造" in th and not building_type:
                building_type = td
            elif ("向き" in th or "方位" in th) and not direction:
                direction = td
            elif "設備" in th or "条件" in th:
                for item in re.split(r"[/／・、,\n]", td):
                    item = item.strip()
                    if item and item not in features:
                        features.append(item)

        # Regex body-text fallback for fields still empty
        if not building_type:
            bt_match = re.search(
                r"(SRC|RC|鉄骨鉄筋コンクリート|鉄筋コンクリート|鉄骨|軽量鉄骨|木造)",
                body_text,
            )
            if bt_match:
                building_type = bt_match.group(1)

        if not direction:
            dir_match = re.search(
                r"(?:向き|方位)[：:\s]*(南東|南西|北東|北西|南|北|東|西)",
                body_text,
            )
            if dir_match:
                direction = dir_match.group(1)

        if not features:
            equip_match = re.search(
                r"設備[/／条件]*[：:\s]*(.*?)(?:\n\n|取扱|周辺|\Z)",
                body_text,
                re.DOTALL,
            )
            if equip_match:
                for item in re.split(r"[/／・、,\n]", equip_match.group(1)):
                    item = item.strip()
                    if item and item not in features and len(item) < 30:
                        features.append(item)

        await asyncio.sleep(delay)

        return Property(
            source=prop.source,
            url=prop.url,
            name=prop.name,
            address=address,
            rent=prop.rent,
            management_fee=prop.management_fee,
            deposit=prop.deposit,
            key_money=prop.key_money,
            layout=prop.layout,
            area_sqm=prop.area_sqm,
            floor=prop.floor,
            building_type=building_type,
            year_built=prop.year_built,
            direction=direction,
            station_access=prop.station_access,
            features=tuple(features),
            image_url=prop.image_url,
        )
    except Exception:
        logger.warning("Failed to enrich Yahoo detail: %s", prop.url)
        return prop


async def scrape_yahoo(config: AppConfig) -> list[Property]:
    """Scrape Yahoo!不動産 listings matching search criteria."""
    url_template = _build_search_url(config.search)
    properties: list[Property] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=config.scraping.headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        for page_num in range(1, config.scraping.max_pages_per_site + 1):
            search_url = url_template.format(page_num)
            logger.info("Scraping Yahoo page %d", page_num)

            try:
                await goto_with_retry(
                    page,
                    search_url,
                    timeout_ms=config.scraping.timeout_sec * 1000,
                    logger=logger,
                )
            except Exception:
                logger.exception(
                    "Failed to load Yahoo page %d after retries", page_num,
                )
                break

            # Extract JSON data from __SERVER_SIDE_CONTEXT__
            try:
                json_text = await page.evaluate(
                    "() => JSON.stringify(window.__SERVER_SIDE_CONTEXT__ || {})"
                )
                data = json.loads(json_text) if json_text else {}
            except Exception:
                logger.debug("Failed to extract Yahoo JSON from page %d", page_num)
                data = {}

            page_properties = _extract_properties_from_json(data)

            if not page_properties:
                logger.info("No properties on Yahoo page %d", page_num)
                break

            properties.extend(page_properties)

            # Check if there's a next page by trying page+1
            # Yahoo doesn't expose total pages, so we stop when we get 0 results
            await asyncio.sleep(config.scraping.request_delay_sec)

        # Deduplicate by URL
        seen: set[str] = set()
        unique: list[Property] = []
        for p in properties:
            key = p.url or f"{p.name}_{p.rent}_{p.layout}"
            if key not in seen:
                seen.add(key)
                unique.append(p)

        logger.info(
            "Yahoo: %d unique from %d total, enriching details...",
            len(unique), len(properties),
        )

        # Phase 2: Visit detail pages in parallel (multiple tabs)
        concurrency = 5
        semaphore = asyncio.Semaphore(concurrency)
        enriched_count = 0

        async def _enrich_one(idx: int, prop: Property) -> Property | None:
            nonlocal enriched_count
            async with semaphore:
                tab = await context.new_page()
                try:
                    result = await _enrich_from_detail(
                        tab, prop, config.scraping.request_delay_sec,
                    )
                    enriched_count += 1
                    if enriched_count % 20 == 0:
                        logger.info(
                            "Yahoo: enriched %d/%d...",
                            enriched_count, len(unique),
                        )
                    return result
                finally:
                    await tab.close()

        results = await asyncio.gather(
            *(_enrich_one(i, p) for i, p in enumerate(unique)),
        )
        enriched = [r for r in results if r is not None]

        await browser.close()

    logger.info("Yahoo: Found %d properties (after detail check)", len(enriched))
    return enriched
