"""DOOR賃貸 property scraper using Playwright.

Site: door.ac (formerly chintai.door.ac)
Structure: building-centric. Each building card contains building info
+ room rows in a table. Detail pages use dl/dt/dd format.
"""

import asyncio
import logging
import re

from playwright.async_api import Locator, async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property

logger = logging.getLogger(__name__)

BASE_URL = "https://door.ac/tokyo/"
AREA_CODES = ["city-13113", "city-13110", "city-13104", "city-13103"]

FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


def _build_search_url(area_slug: str, criteria: SearchCriteria) -> str:
    """Build DOOR search URL from criteria."""
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    params = [
        f"rent_from={rent_min}",
        f"rent_to={rent_max}",
    ]

    layout_map = {"1R": "1r", "1K": "1k", "1DK": "1dk", "1LDK": "1ldk", "2K": "2k"}
    for layout in criteria.layouts:
        if code := layout_map.get(layout):
            params.append(f"layout[]={code}")

    if criteria.max_age_years > 0:
        params.append(f"chikunen={criteria.max_age_years}")

    query = "&".join(params)
    return f"{BASE_URL}{area_slug}/list?{query}&page={{}}"


def _parse_rent(text: str) -> int:
    """Parse rent string like '13.5万円' or '135,000円' to yen integer."""
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_area(text: str) -> float:
    """Parse area string like '20.84m2' to float."""
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


async def _extract_from_building(building: Locator) -> list[Property]:
    """Extract all room listings from a single building card."""
    properties: list[Property] = []

    # Building name from heading link
    name = await _safe_text(building.locator("h3 a, h2 a"))

    # Building info paragraphs: location, built year, station
    address = ""
    station = ""
    year_built = ""

    # Try .location, .built, .stations class-based selectors first
    loc_el = building.locator("p.location, .building-info .location")
    if await loc_el.count() > 0:
        address = await _safe_text(loc_el)

    built_el = building.locator("p.built, .building-info .built")
    if await built_el.count() > 0:
        year_built = await _safe_text(built_el)

    station_el = building.locator("p.stations, .building-info .stations")
    if await station_el.count() > 0:
        station = await _safe_text(station_el)

    # Fallback: look at all p/div elements within building info
    if not address or not station:
        info_els = building.locator(".building-info p, .building-info div")
        for i in range(min(10, await info_els.count())):
            text = (await info_els.nth(i).text_content() or "").strip()
            if not text:
                continue
            if "東京都" in text and not address:
                address = text
            elif ("駅" in text or "徒歩" in text) and not station:
                station = text
            elif "築" in text and not year_built:
                year_built = text

    # Image URL
    image_url = ""
    img_el = building.locator("img[src*='door'], img[src*='http']")
    if await img_el.count() > 0:
        image_url = await img_el.first.get_attribute("src") or ""

    # Room rows from table
    rows = building.locator("table tr")
    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)
        try:
            cells = row.locator("td")
            cell_count = await cells.count()
            if cell_count < 4:
                continue

            # Extract all cell texts
            cell_texts = []
            for ci in range(cell_count):
                cell_texts.append((await cells.nth(ci).text_content() or "").strip())

            # Find rent (contains 万円), layout (1K, 1R etc), area (m2)
            rent = 0
            mgmt_fee = 0
            layout = ""
            area = 0.0
            floor = ""
            deposit = 0
            key_money = 0

            for ct in cell_texts:
                if "万円" in ct and not rent:
                    rent = _parse_rent(ct)
                elif re.match(r"^\d+階", ct):
                    floor = ct
                elif re.match(r"^(ワンルーム|1[RKDL]|2[KDL])", ct):
                    layout = ct
                elif "m" in ct and re.search(r"[\d.]+", ct) and not area:
                    area = _parse_area(ct)

            # Management fee: look for secondary fee in cells
            for ct in cell_texts:
                if "万円" in ct and _parse_rent(ct) != rent and not mgmt_fee:
                    val = _parse_rent(ct)
                    if val < rent:
                        mgmt_fee = val

            # Deposit/key money from "敷/礼" format
            for ct in cell_texts:
                if "/" in ct and "万" in ct:
                    parts = ct.split("/")
                    if len(parts) == 2:
                        deposit = _parse_rent(parts[0])
                        key_money = _parse_rent(parts[1])

            # Detail link
            link_el = row.locator("a[href*='/buildings/'], a[href*='/properties/']")
            href = ""
            if await link_el.count() > 0:
                href = await link_el.first.get_attribute("href") or ""
            if not href:
                # Try any link in the row
                any_link = row.locator("a[href]")
                if await any_link.count() > 0:
                    href = await any_link.first.get_attribute("href") or ""

            url = f"https://door.ac{href}" if href and not href.startswith("http") else href

            if not rent:
                continue

            properties.append(Property(
                source="door",
                url=url,
                name=name,
                address=address,
                rent=rent,
                management_fee=mgmt_fee,
                deposit=deposit,
                key_money=key_money,
                layout=layout,
                area_sqm=area,
                floor=floor,
                year_built=year_built,
                station_access=station,
                image_url=image_url,
            ))
        except Exception:
            logger.debug("Failed to extract DOOR room from: %s", name)

    return properties


async def _enrich_from_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit DOOR detail page to get features, structure, direction.

    Returns enriched Property, or None if female-only.
    """
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("DOOR: skipped female-only: %s", prop.name)
            return None

        building_type = prop.building_type
        direction = prop.direction
        address = prop.address
        year_built = prop.year_built
        features: list[str] = list(prop.features)

        # DOOR detail uses dl/dt/dd or simple label-value pairs
        # Try table rows first
        rows = page.locator("table tr, dl")
        for ri in range(await rows.count()):
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
            elif "設備" in th or "条件" in th:
                for item in re.split(r"[/／・、,\n]", td):
                    item = item.strip()
                    if item and item not in features:
                        features.append(item)

        # Also check for text blocks with section headers
        all_text = body_text
        if "設備" in all_text and not features:
            equip_match = re.search(r"設備[/／条件]*[：:\s]*(.*?)(?:\n\n|\Z)", all_text, re.DOTALL)
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
            year_built=year_built,
            direction=direction,
            station_access=prop.station_access,
            features=tuple(features),
            image_url=prop.image_url,
        )
    except Exception:
        logger.debug("Failed to enrich DOOR detail: %s", prop.url)
        return prop


async def scrape_door(config: AppConfig) -> list[Property]:
    """Scrape DOOR listings matching search criteria."""
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

        # Phase 1: Collect from list pages
        for area_slug in AREA_CODES:
            url_template = _build_search_url(area_slug, config.search)

            for page_num in range(1, config.scraping.max_pages_per_site + 1):
                search_url = url_template.format(page_num)
                logger.info("Scraping DOOR %s page %d", area_slug, page_num)

                try:
                    await page.goto(
                        search_url,
                        timeout=config.scraping.timeout_sec * 1000,
                    )
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    logger.exception("Failed to load DOOR page")
                    break

                # Try multiple selectors for building cards
                buildings = page.locator(
                    "div.building-card, div.building-box, "
                    "div[class*='building'], section[class*='building']"
                )
                count = await buildings.count()

                # Fallback: look for any container that has a room table
                if count == 0:
                    buildings = page.locator(
                        ":has(> table):has(> h3 a[href*='/buildings/']), "
                        ":has(> table):has(> h2 a[href*='/buildings/'])"
                    )
                    count = await buildings.count()

                # Last resort: find tables with property links directly
                if count == 0:
                    # Check if page loaded at all
                    body = await page.text_content("body") or ""
                    if "buildings" in body:
                        logger.info(
                            "DOOR: page has content but selectors don't match; "
                            "trying broad extraction on page %d",
                            page_num,
                        )
                        # Extract properties from the whole page
                        rooms = await _extract_from_whole_page(page)
                        properties.extend(rooms)
                        if not rooms:
                            break
                    else:
                        logger.info("No buildings on DOOR page %d", page_num)
                        break
                else:
                    for i in range(count):
                        rooms = await _extract_from_building(buildings.nth(i))
                        properties.extend(rooms)

                # Check pagination
                next_btn = page.locator(
                    "a[rel='next'], a:has-text('次'), a:has-text('Next'), "
                    "a[href*='page=']:last-of-type"
                )
                if await next_btn.count() == 0:
                    break

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
            "DOOR: %d unique from %d total, enriching details...",
            len(unique), len(properties),
        )

        # Phase 2: Visit each detail page
        enriched: list[Property] = []
        for i, prop in enumerate(unique):
            if (i + 1) % 20 == 0:
                logger.info("DOOR: enriching %d/%d...", i + 1, len(unique))
            result = await _enrich_from_detail(
                page, prop, config.scraping.request_delay_sec,
            )
            if result is not None:
                enriched.append(result)

        await browser.close()

    logger.info("DOOR: Found %d properties (after detail check)", len(enriched))
    return enriched


async def _extract_from_whole_page(page) -> list[Property]:
    """Fallback: extract properties from the entire page when building cards
    can't be identified by CSS class."""
    properties: list[Property] = []

    # Find all detail links to properties
    links = page.locator("a[href*='/buildings/'][href*='/properties/']")
    link_count = await links.count()

    for i in range(link_count):
        try:
            link = links.nth(i)
            href = await link.get_attribute("href") or ""
            url = f"https://door.ac{href}" if href and not href.startswith("http") else href
            text = (await link.text_content() or "").strip()

            # Get surrounding context (parent row)
            parent = link.locator("xpath=ancestor::tr")
            if await parent.count() > 0:
                row_text = (await parent.first.text_content() or "").strip()
            else:
                row_text = text

            # Try to extract rent from row text
            rent = _parse_rent(row_text)
            if not rent:
                continue

            # Extract layout
            layout_match = re.search(r"(ワンルーム|[12][RKDL]+)", row_text)
            layout = layout_match.group(1) if layout_match else ""

            # Extract area
            area = _parse_area(row_text)

            # Extract floor
            floor_match = re.search(r"(\d+)階", row_text)
            floor = floor_match.group(0) if floor_match else ""

            # Get building context
            building_parent = link.locator(
                "xpath=ancestor::*[.//h3 or .//h2][1]"
            )
            name = ""
            address = ""
            station = ""
            if await building_parent.count() > 0:
                name_el = building_parent.first.locator("h3 a, h2 a")
                name = await _safe_text(name_el)
                bp_text = (await building_parent.first.text_content() or "")
                addr_match = re.search(r"東京都[^\n]+", bp_text)
                if addr_match:
                    address = addr_match.group(0).strip()
                station_match = re.search(r"[^\n]*駅[^\n]*徒歩[^\n]*", bp_text)
                if station_match:
                    station = station_match.group(0).strip()

            properties.append(Property(
                source="door",
                url=url,
                name=name,
                address=address,
                rent=rent,
                layout=layout,
                area_sqm=area,
                floor=floor,
                station_access=station,
            ))
        except Exception:
            logger.debug("Failed to extract DOOR property from link %d", i)

    return properties
