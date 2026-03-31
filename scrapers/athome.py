"""athome (公開) property scraper using Playwright.

Site: athome.co.jp
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

BASE_URL = "https://www.athome.co.jp/chintai/tokyo/"

# Area slugs for the target areas
# athome may use -city or -ku suffixes; try both patterns
AREA_SLUGS = [
    "shibuya-city",   # 渋谷区
    "meguro-city",    # 目黒区
    "shinjuku-city",  # 新宿区
    "minato-city",    # 港区
]

FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


def _build_search_url(area_slug: str, criteria: SearchCriteria) -> str:
    """Build athome search URL from criteria."""
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    layout_map = {"1R": "1", "1K": "2", "1DK": "3", "1LDK": "4", "2K": "5"}
    layout_codes = [layout_map[ly] for ly in criteria.layouts if ly in layout_map]

    params = [
        f"RENT_LOW={rent_min}",
        f"RENT_HIGH={rent_max}",
    ]
    for code in layout_codes:
        params.append(f"FLOOR_PLAN={code}")

    if criteria.bath_toilet_separate:
        params.append("EQUIPMENT=1")

    if criteria.city_gas:
        params.append("EQUIPMENT=61")

    query = "&".join(params)
    return f"{BASE_URL}{area_slug}/list/?{query}&page={{}}"


def _parse_rent_man(text: str) -> int:
    """Parse rent like '15.4' (万円) to yen integer."""
    match = re.search(r"([\d.]+)", text)
    if match:
        return int(float(match.group(1)) * 10000)
    return 0


def _parse_fee(text: str) -> int:
    """Parse fee string like '7,000円' or '1.5万円' to yen integer."""
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_area(text: str) -> float:
    """Parse area string like '49.53m²' to float."""
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
        body_snippet = ""
        try:
            body_snippet = (
                await page.evaluate("document.body?.innerText?.substring(0, 500)") or ""
            )
        except Exception:
            pass

        captcha_indicators = [
            "認証" in title,
            "verification" in title,
            "captcha" in title,
            "captcha" in url,
            "あなたがロボットでないことを確認" in body_snippet,
            "アクセスが制限" in body_snippet,
            "不正なアクセス" in body_snippet,
        ]
        return any(captcha_indicators)
    except Exception:
        return False


async def _extract_rooms_from_building(building: Locator) -> list[Property]:
    """Extract all room listings from a single building card."""
    properties: list[Property] = []

    # Building name
    name = await _safe_text(
        building.locator(
            "h2.p-property__title--building, "
            "h2[class*='property__title'], "
            "h2 a"
        )
    )

    # Address
    address = await _safe_text(
        building.locator(
            "dl.p-property__information-hint:has(i[class*='map']) dd strong, "
            "dl:has(dt:has-text('所在地')) dd, "
            ".p-property__address"
        )
    )
    if not address:
        # Fallback: first dl's dd
        first_dl = building.locator("dl.p-property__information-hint")
        if await first_dl.count() > 0:
            address = await _safe_text(first_dl.first.locator("dd strong, dd"))

    # Station access
    station_access = await _safe_text(
        building.locator(
            "dl.p-property__information-hint:has(i[class*='train']) dd, "
            "dl:has(dt:has-text('交通')) dd"
        )
    )
    if not station_access:
        info_hints = building.locator("dl.p-property__information-hint")
        if await info_hints.count() >= 2:
            station_access = await _safe_text(info_hints.nth(1).locator("dd"))

    # Building type and age
    type_age = await _safe_text(
        building.locator(
            "dl.p-property__information-hint:has(i[class*='home']) dd, "
            "dl:has(dt:has-text('築')) dd"
        )
    )
    if not type_age:
        info_hints = building.locator("dl.p-property__information-hint")
        if await info_hints.count() >= 3:
            type_age = await _safe_text(info_hints.nth(2).locator("dd"))

    # Room detail boxes
    rooms = building.locator(
        "div.p-property__room--detailbox, "
        "div[class*='property__room'], "
        "div.p-property__room"
    )
    room_count = await rooms.count()

    for i in range(room_count):
        room = rooms.nth(i)
        try:
            # Detail link
            link_el = room.locator(
                "a.p-property__room-more-inner, "
                "a[href*='/chintai/'], "
                "a[class*='room-more']"
            )
            href = ""
            if await link_el.count() > 0:
                href = await link_el.first.get_attribute("href") or ""
            url = (
                f"https://www.athome.co.jp{href}"
                if href and not href.startswith("http")
                else href
            )

            # Rent
            rent_text = await _safe_text(
                room.locator(
                    "b.p-property__information-rent, "
                    "[class*='information-rent'], "
                    ".rent"
                )
            )
            rent = _parse_rent_man(rent_text)

            # Management fee
            mgmt_text = await _safe_text(
                room.locator(
                    "li.p-property__room-rent p.p-property__information-price span, "
                    "[class*='information-price'] span"
                )
            )
            mgmt_fee = _parse_fee(mgmt_text)

            # Layout
            layout = await _safe_text(
                room.locator(
                    "li.p-property__room-floorplan div.p-property__floor, "
                    "[class*='room-floorplan'] [class*='floor'], "
                    ".madori"
                )
            )

            # Area
            area_text = await _safe_text(
                room.locator(
                    "li.p-property__room-floorplan > span, "
                    "[class*='room-floorplan'] > span"
                )
            )
            area = _parse_area(area_text)

            # Floor
            floor = await _safe_text(
                room.locator(
                    "li.p-property__room-number, "
                    "[class*='room-number']"
                )
            )

            # Image
            image_url = ""
            img_el = room.locator("img[src*='athome'], img[src*='http']")
            if await img_el.count() > 0:
                image_url = await img_el.first.get_attribute("src") or ""

            if not rent:
                continue

            properties.append(Property(
                source="athome",
                url=url,
                name=name,
                address=address,
                rent=rent,
                management_fee=mgmt_fee,
                layout=layout,
                area_sqm=area,
                floor=floor,
                year_built=type_age,
                station_access=station_access,
                image_url=image_url,
            ))
        except Exception:
            logger.debug("Failed to extract athome room from: %s", name)

    return properties


async def _enrich_from_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit athome detail page to get features, structure, direction.

    Returns enriched Property, or None if female-only.
    """
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

        if await _is_captcha_page(page):
            logger.debug("athome: CAPTCHA on detail page, skipping enrichment")
            return prop

        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("athome: skipped female-only: %s", prop.name)
            return None

        building_type = prop.building_type
        direction = prop.direction
        address = prop.address
        year_built = prop.year_built
        features: list[str] = list(prop.features)
        deposit = prop.deposit
        key_money = prop.key_money

        # Extract from table/dl rows
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
            elif "築年" in th and not year_built:
                year_built = td
            elif "敷金" in th and not deposit:
                deposit = _parse_fee(td)
            elif "礼金" in th and not key_money:
                key_money = _parse_fee(td)
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

        await asyncio.sleep(delay + random.uniform(0.5, 1.5))

        return Property(
            source=prop.source,
            url=prop.url,
            name=prop.name,
            address=address,
            rent=prop.rent,
            management_fee=prop.management_fee,
            deposit=deposit,
            key_money=key_money,
            layout=prop.layout,
            area_sqm=prop.area_sqm,
            floor=prop.floor,
            building_type=building_type,
            year_built=year_built,
            direction=direction,
            station_access=prop.station_access,
            features=tuple(features),
            image_url=prop.image_url,
        )
    except Exception:
        logger.warning("Failed to enrich athome detail: %s", prop.url)
        return prop


async def scrape_athome(config: AppConfig) -> list[Property]:
    """Scrape athome (public) listings matching search criteria.

    NOTE: athome has aggressive bot detection. This scraper uses realistic
    browser fingerprints and random delays. If CAPTCHA is detected,
    it gracefully skips instead of crashing.
    """
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

        captcha_hit = False

        # Phase 1: Collect from list pages
        for area_slug in AREA_SLUGS:
            if captcha_hit:
                break

            url_template = _build_search_url(area_slug, config.search)

            for page_num in range(1, config.scraping.max_pages_per_site + 1):
                search_url = url_template.format(page_num)
                logger.info("Scraping athome %s page %d", area_slug, page_num)

                try:
                    await asyncio.sleep(random.uniform(1.0, 3.0))
                    await page.goto(
                        search_url,
                        timeout=config.scraping.timeout_sec * 1000,
                    )
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    logger.exception(
                        "Failed to load athome page %d for %s",
                        page_num, area_slug,
                    )
                    break

                # Check for CAPTCHA
                if await _is_captcha_page(page):
                    logger.warning(
                        "athome: CAPTCHA detected on %s page %d. "
                        "Stopping athome scraping. Results so far: %d properties.",
                        area_slug, page_num, len(properties),
                    )
                    captcha_hit = True
                    break

                buildings = page.locator(
                    "div.p-property.p-property--building, "
                    "div.p-property--building, "
                    "div[class*='p-property--building']"
                )
                building_count = await buildings.count()

                if building_count == 0:
                    logger.info(
                        "No buildings on athome %s page %d",
                        area_slug, page_num,
                    )
                    break

                for i in range(building_count):
                    rooms = await _extract_rooms_from_building(buildings.nth(i))
                    properties.extend(rooms)

                # Next page
                next_link = page.locator(
                    "link[rel='next'], "
                    "a[rel='next'], "
                    "a:has-text('次へ'), "
                    "li.next a"
                )
                if await next_link.count() == 0:
                    break

                await asyncio.sleep(config.scraping.request_delay_sec)

        if not properties:
            logger.warning(
                "athome: No properties collected (likely blocked by CAPTCHA). "
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
            "athome: %d unique from %d total, enriching details...",
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
                            "athome: enriched %d/%d...",
                            enriched_count, len(unique),
                        )
                    return result
                finally:
                    await tab.close()

        results = await asyncio.gather(
            *(_enrich_one(i, p) for i, p in enumerate(unique)),
        )
        enriched = [r for r in results if r is not None]

        # Filter out female-only from name/features (quick check)
        filtered = [
            p for p in enriched
            if not any(kw in p.name for kw in FEMALE_KEYWORDS)
        ]
        if len(filtered) < len(enriched):
            logger.info("athome: filtered %d female-only from names", len(enriched) - len(filtered))

        await browser.close()

    logger.info("athome: Found %d properties (after detail check)", len(filtered))
    return filtered
