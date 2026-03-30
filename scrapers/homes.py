"""HOME'S (LIFULL HOME'S) property scraper using Playwright.

Site: homes.co.jp
Known issue: CAPTCHA/bot detection blocks headless browsers.
Strategy: Use realistic browser fingerprints, random delays,
and gracefully degrade when blocked.
"""

import asyncio
import logging
import random
import re

from playwright.async_api import Locator, async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property

logger = logging.getLogger(__name__)

# HOME'S covers multiple areas - build separate URLs per area
AREA_URLS = {
    "shibuya": "https://www.homes.co.jp/chintai/tokyo/shibuya-city/list/",
    "meguro": "https://www.homes.co.jp/chintai/tokyo/meguro-city/list/",
    "shinjuku": "https://www.homes.co.jp/chintai/tokyo/shinjuku-city/list/",
    "minato": "https://www.homes.co.jp/chintai/tokyo/minato-city/list/",
}

FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


def _build_search_params(criteria: SearchCriteria) -> str:
    """Build query parameters for HOME'S search."""
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    layout_map = {"1R": "010", "1K": "020", "1DK": "030", "1LDK": "040", "2K": "050"}
    layout_codes = [layout_map[ly] for ly in criteria.layouts if ly in layout_map]

    params = [
        f"priceMin={rent_min}",
        f"priceMax={rent_max}",
    ]
    for code in layout_codes:
        params.append(f"madori={code}")

    if criteria.bath_toilet_separate:
        params.append("cond=0002")

    if criteria.city_gas:
        params.append("cond=0028")

    return "&".join(params)


def _parse_rent_man(text: str) -> int:
    """Parse rent in 万円 format like '6.9' to yen integer."""
    match = re.search(r"([\d.]+)", text)
    if match:
        return int(float(match.group(1)) * 10000)
    return 0


def _parse_fee(text: str) -> int:
    """Parse fee string like '1,000円' to yen integer."""
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_area(text: str) -> float:
    """Parse area string like '14.12m2' to float."""
    match = re.search(r"([\d.]+)\s*m", text)
    return float(match.group(1)) if match else 0.0


async def _safe_text(locator: Locator) -> str:
    """Safely get text content."""
    try:
        if await locator.count() > 0:
            return (await locator.first.text_content() or "").strip()
    except Exception:
        pass
    return ""


async def _is_captcha_page(page) -> bool:
    """Detect if the current page is a CAPTCHA challenge."""
    try:
        title = (await page.title() or "").lower()
        url = page.url.lower()
        body = await page.text_content("body") or ""

        captcha_indicators = [
            "verification" in title,
            "認証" in title,
            "captcha" in title,
            "captcha" in url,
            "challenge" in url,
            "あなたがロボットでないことを確認" in body,
            "アクセスが集中" in body,
        ]
        return any(captcha_indicators)
    except Exception:
        return False


async def _extract_rooms_from_building(building: Locator) -> list[Property]:
    """Extract all room listings from a single building card."""
    properties: list[Property] = []

    # Building-level info - try multiple selector patterns
    name = await _safe_text(
        building.locator(
            "h2.heading a span.bukkenName, "
            "h2 a.prg-bukkenNameAnchor span.bukkenName, "
            "h2 a span, h2 a"
        )
    )

    # Spec table: address, station, age
    spec_rows = building.locator("div.bukkenSpec table tr, .mod-buildingSpec tr")
    address = ""
    station_access = ""
    year_built = ""

    if await spec_rows.count() >= 1:
        address = await _safe_text(spec_rows.nth(0).locator("td"))
    if await spec_rows.count() >= 2:
        station_els = spec_rows.nth(1).locator("td span.prg-stationText, td span")
        stations = []
        for si in range(await station_els.count()):
            t = await station_els.nth(si).text_content()
            if t and ("駅" in t or "徒歩" in t):
                stations.append(t.strip())
        station_access = " / ".join(stations)
    if await spec_rows.count() >= 3:
        year_built = await _safe_text(spec_rows.nth(2).locator("td"))

    # Room rows
    rooms = building.locator("tr.prg-room[data-href], tr.prg-room")
    room_count = await rooms.count()

    for i in range(room_count):
        room = rooms.nth(i)
        try:
            url = await room.get_attribute("data-href") or ""
            if not url:
                # Try link inside the row
                link = room.locator("a[href*='/chintai/']")
                if await link.count() > 0:
                    url = await link.first.get_attribute("href") or ""
            if not url:
                continue

            if not url.startswith("http"):
                url = f"https://www.homes.co.jp{url}"

            floor = await _safe_text(
                room.locator("td.floar li.roomKaisuu, td.floor li, td:nth-child(1)")
            )

            # Rent
            rent_num = await _safe_text(
                room.locator("td.price span.priceLabel span.num, td.price span.num")
            )
            rent = _parse_rent_man(rent_num)

            # Management fee
            price_full = await _safe_text(room.locator("td.price"))
            mgmt_fee = 0
            mgmt_match = re.search(r"/([\d,]+)円", price_full)
            if mgmt_match:
                mgmt_fee = int(mgmt_match.group(1).replace(",", ""))

            # Layout and area
            layout_td = await _safe_text(room.locator("td.layout"))
            layout_parts = layout_td.split("\n") if layout_td else [""]
            layout = layout_parts[0].strip()
            area_text = layout_parts[1].strip() if len(layout_parts) > 1 else ""
            area = _parse_area(area_text)

            if not rent:
                continue

            properties.append(Property(
                source="homes",
                url=url,
                name=name,
                address=address,
                rent=rent,
                management_fee=mgmt_fee,
                layout=layout,
                area_sqm=area,
                floor=floor,
                year_built=year_built,
                station_access=station_access,
            ))
        except Exception:
            logger.debug("Failed to extract HOME'S room from: %s", name)

    return properties


async def _enrich_from_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit HOME'S detail page to get features, structure, direction.

    Returns enriched Property, or None if female-only.
    """
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

        if await _is_captcha_page(page):
            logger.debug("HOME'S: CAPTCHA on detail page, skipping enrichment")
            return prop

        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("HOME'S: skipped female-only: %s", prop.name)
            return None

        building_type = prop.building_type
        direction = prop.direction
        address = prop.address
        features: list[str] = list(prop.features)

        rows = page.locator("table tr, dl")
        for ri in range(min(60, await rows.count())):
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

        # Random delay to avoid detection
        await asyncio.sleep(delay + random.uniform(0.5, 1.5))

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
        logger.debug("Failed to enrich HOME'S detail: %s", prop.url)
        return prop


async def scrape_homes(config: AppConfig) -> list[Property]:
    """Scrape HOME'S listings matching search criteria.

    NOTE: HOME'S has aggressive bot detection. This scraper uses realistic
    browser fingerprints and random delays. If CAPTCHA is detected,
    it gracefully skips instead of crashing.
    """
    query_params = _build_search_params(config.search)
    properties: list[Property] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=config.scraping.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        # Mask webdriver detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = await context.new_page()

        # Phase 1: Collect from list pages
        captcha_hit = False
        for area_name, area_base_url in AREA_URLS.items():
            if captcha_hit:
                break

            for page_num in range(1, config.scraping.max_pages_per_site + 1):
                search_url = f"{area_base_url}?{query_params}&page={page_num}"
                logger.info("Scraping HOME'S %s page %d", area_name, page_num)

                try:
                    # Random delay before each page
                    await asyncio.sleep(random.uniform(1.0, 3.0))

                    await page.goto(
                        search_url,
                        timeout=config.scraping.timeout_sec * 1000,
                    )
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    logger.exception("Failed to load HOME'S page")
                    break

                # Check for CAPTCHA
                if await _is_captcha_page(page):
                    logger.warning(
                        "HOME'S: CAPTCHA detected on %s page %d. "
                        "Stopping HOME'S scraping. Results so far: %d properties.",
                        area_name, page_num, len(properties),
                    )
                    captcha_hit = True
                    break

                # Try multiple selectors for building cards
                buildings = page.locator(
                    "div[class*='mod-mergeBuilding'], "
                    "div.mod-mergeBuilding--rent--photo, "
                    "div.p-property"
                )
                building_count = await buildings.count()

                if building_count == 0:
                    logger.info(
                        "No buildings on HOME'S %s page %d", area_name, page_num,
                    )
                    break

                for i in range(building_count):
                    rooms = await _extract_rooms_from_building(buildings.nth(i))
                    properties.extend(rooms)

                # Check for next page
                next_btn = page.locator(
                    "li.nextPage > a, a[rel='next'], a:has-text('次へ')"
                )
                if await next_btn.count() == 0:
                    break

                await asyncio.sleep(config.scraping.request_delay_sec)

        if not properties:
            logger.warning(
                "HOME'S: No properties collected (likely blocked by CAPTCHA). "
                "Consider running with headless=False to solve CAPTCHA manually."
            )
            await browser.close()
            return []

        # Deduplicate
        seen: set[str] = set()
        unique: list[Property] = []
        for p in properties:
            key = p.url or f"{p.name}_{p.rent}_{p.layout}"
            if key not in seen:
                seen.add(key)
                unique.append(p)

        logger.info(
            "HOME'S: %d unique from %d total, enriching details...",
            len(unique), len(properties),
        )

        # Phase 2: Visit each detail page
        enriched: list[Property] = []
        for i, prop in enumerate(unique):
            if captcha_hit:
                # If we already hit CAPTCHA, skip enrichment
                enriched.append(prop)
                continue
            if (i + 1) % 20 == 0:
                logger.info("HOME'S: enriching %d/%d...", i + 1, len(unique))
            result = await _enrich_from_detail(
                page, prop, config.scraping.request_delay_sec,
            )
            if result is not None:
                enriched.append(result)
            # Check if detail page triggered CAPTCHA
            if await _is_captcha_page(page):
                logger.warning("HOME'S: CAPTCHA during enrichment, stopping.")
                captcha_hit = True
                # Add remaining properties without enrichment
                enriched.extend(unique[i + 1:])
                break

        await browser.close()

    logger.info("HOME'S: Found %d properties (after detail check)", len(enriched))
    return enriched
