"""SUUMO property scraper using Playwright.

Site structure: building-centric. Each cassetteitem is a building
containing one or more room rows in table.cassetteitem_other tbody.
"""

import asyncio
import logging
import re

from playwright.async_api import Locator, async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property

logger = logging.getLogger(__name__)

BASE_URL = "https://suumo.jp/jj/chintai/ichiran/FR301FC001/"


def _build_search_url(criteria: SearchCriteria) -> str:
    """Build SUUMO search URL from criteria."""
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    layout_map = {"1R": "01", "1K": "02", "1DK": "03", "1LDK": "04"}
    layout_codes = [layout_map[l] for l in criteria.layouts if l in layout_map]

    params = [
        ("ar", "030"),
        ("bs", "040"),
        ("ta", "13"),
        ("sc", "13113"),  # 渋谷区
        ("sc", "13110"),  # 目黒区
        ("sc", "13104"),  # 新宿区
        ("sc", "13103"),  # 港区
        ("cb", f"{rent_min}.0"),
        ("ct", f"{rent_max}.0"),
    ]
    for code in layout_codes:
        params.append(("md", code))

    if criteria.bath_toilet_separate:
        params.append(("tc", "0400301"))

    if criteria.city_gas:
        params.append(("tc", "0400501"))

    if criteria.max_age_years > 0:
        params.append(("cn", str(criteria.max_age_years)))

    query = "&".join(f"{k}={v}" for k, v in params)
    return f"{BASE_URL}?{query}&page={{}}"


def _parse_rent(text: str) -> int:
    """Parse rent string like '11.7万円' or '5000円' to yen integer."""
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_area(text: str) -> float:
    """Parse area string like '18.24m²' to float."""
    match = re.search(r"([\d.]+)\s*m", text)
    return float(match.group(1)) if match else 0.0


async def _safe_text(locator: Locator) -> str:
    """Safely get text content, returning empty string on failure."""
    try:
        if await locator.count() > 0:
            return (await locator.first.text_content() or "").strip()
    except Exception:
        pass
    return ""


async def _extract_rooms_from_building(building: Locator) -> list[Property]:
    """Extract all room listings from a single building card."""
    properties: list[Property] = []

    # Building-level info
    name = await _safe_text(building.locator(".cassetteitem_content-title"))
    address = await _safe_text(building.locator(".cassetteitem_detail-col1"))
    station_els = building.locator(".cassetteitem_detail-col2 .cassetteitem_detail-text")
    stations = []
    for i in range(await station_els.count()):
        text = await station_els.nth(i).text_content()
        if text:
            stations.append(text.strip())
    station_access = " / ".join(stations)

    col3_divs = building.locator(".cassetteitem_detail-col3 div")
    year_built = await _safe_text(col3_divs.first)
    building_type_from_list = ""
    if await col3_divs.count() >= 2:
        building_type_from_list = await _safe_text(col3_divs.nth(1))

    # Building image - SUUMO uses rel attribute for lazy-loaded images
    image_url = ""
    all_imgs = building.locator("img")
    for idx in range(min(5, await all_imgs.count())):
        # Try rel attribute first (lazy load), then src
        rel = await all_imgs.nth(idx).get_attribute("rel") or ""
        src = await all_imgs.nth(idx).get_attribute("src") or ""
        real_src = rel if rel.startswith("http") else src
        if real_src and real_src.startswith("http") and "suumo" in real_src:
            image_url = real_src
            break

    # Room rows (each tbody in table.cassetteitem_other)
    rows = building.locator("table.cassetteitem_other tbody")
    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)
        try:
            # Detail link
            link_el = row.locator("a.cassetteitem_other-linktext")
            href = ""
            if await link_el.count() > 0:
                href = await link_el.first.get_attribute("href") or ""
            url = f"https://suumo.jp{href}" if href and not href.startswith("http") else href

            # Room details
            floor = await _safe_text(row.locator("td").nth(2))
            rent_text = await _safe_text(
                row.locator(".cassetteitem_price--rent .cassetteitem_other-emphasis")
            )
            mgmt_text = await _safe_text(
                row.locator(".cassetteitem_price--administration")
            )
            deposit_text = await _safe_text(
                row.locator(".cassetteitem_price--deposit")
            )
            key_money_text = await _safe_text(
                row.locator(".cassetteitem_price--gratuity")
            )
            layout = await _safe_text(row.locator(".cassetteitem_madori"))
            area_text = await _safe_text(row.locator(".cassetteitem_menseki"))

            properties.append(Property(
                source="suumo",
                url=url,
                name=name,
                address=address,
                rent=_parse_rent(rent_text),
                management_fee=_parse_rent(mgmt_text),
                deposit=_parse_rent(deposit_text),
                key_money=_parse_rent(key_money_text),
                layout=layout,
                area_sqm=_parse_area(area_text),
                floor=floor,
                building_type=building_type_from_list,
                year_built=year_built,
                direction="",  # not shown on list page
                station_access=station_access,
                image_url=image_url,
            ))
        except Exception:
            logger.exception("Failed to extract room from building: %s", name)

    return properties


FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


async def _enrich_from_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit SUUMO detail page to get features, structure, direction, conditions.

    Returns enriched Property, or None if female-only / invalid.
    """
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

        # Check for female-only
        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("SUUMO: skipped female-only: %s", prop.name)
            return None

        # Extract detail table rows
        building_type = prop.building_type
        direction = prop.direction
        features: list[str] = list(prop.features)

        rows = page.locator("table tr")
        for ri in range(await rows.count()):
            th_el = rows.nth(ri).locator("th")
            td_el = rows.nth(ri).locator("td")
            if await th_el.count() == 0 or await td_el.count() == 0:
                continue
            th = (await th_el.first.text_content() or "").strip()
            td = (await td_el.first.text_content() or "").strip()

            if "構造" in th and not building_type:
                building_type = td
            elif "向き" in th and not direction:
                direction = td
            elif "設備" in th or "条件" in th:
                # Split by common delimiters
                for item in re.split(r"[/／・、,\n]", td):
                    item = item.strip()
                    if item and item not in features:
                        features.append(item)

        # Regex body-text fallback for fields still empty
        body_text = await page.text_content("body") or ""

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
            address=prop.address,
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
        logger.warning("Failed to enrich SUUMO detail: %s", prop.url)
        return prop


async def scrape_suumo(config: AppConfig) -> list[Property]:
    """Scrape SUUMO listings matching search criteria."""
    url_template = _build_search_url(config.search)
    properties: list[Property] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=config.scraping.headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()

        # Phase 1: Collect from list pages
        for page_num in range(1, config.scraping.max_pages_per_site + 1):
            search_url = url_template.format(page_num)
            logger.info("Scraping SUUMO page %d", page_num)

            try:
                await page.goto(search_url, timeout=config.scraping.timeout_sec * 1000)
                await page.wait_for_load_state("domcontentloaded")
            except Exception:
                logger.exception("Failed to load SUUMO page %d", page_num)
                break

            buildings = page.locator("div.cassetteitem")
            building_count = await buildings.count()

            if building_count == 0:
                logger.info("No more buildings on page %d", page_num)
                break

            for i in range(building_count):
                rooms = await _extract_rooms_from_building(buildings.nth(i))
                properties.extend(rooms)

            next_btn = page.locator("p.pagination-parts a:has-text('次へ')")
            if await next_btn.count() == 0:
                break

            await asyncio.sleep(config.scraping.request_delay_sec)

        logger.info("SUUMO: %d properties from list pages, enriching details...", len(properties))

        # Phase 2: Visit each detail page for full info
        enriched: list[Property] = []
        for i, prop in enumerate(properties):
            if (i + 1) % 10 == 0:
                logger.info("SUUMO: enriching %d/%d...", i + 1, len(properties))
            result = await _enrich_from_detail(page, prop, config.scraping.request_delay_sec)
            if result is not None:
                enriched.append(result)

        await browser.close()

    logger.info("SUUMO: Found %d properties (after detail check)", len(enriched))
    return enriched
