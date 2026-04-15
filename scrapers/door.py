"""DOOR賃貸 property scraper using Playwright.

Site: door.ac (formerly chintai.door.ac)
Structure: building-centric. Each building card (div.building-box) contains
building info in dl.description-item elements + room rows in table.table-secondary.
Detail pages use table.building-table / table.room-table / table.contract-table.
"""

import asyncio
import logging
import re

from playwright.async_api import Locator, async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property, goto_with_retry, needs_ai_fallback
from stations import WARD_CODES

logger = logging.getLogger(__name__)

BASE_URL = "https://door.ac"
# DOOR uses "city-XXXXX" prefix. Source of truth: WARD_CODES from stations.py.
AREA_CODES = [f"city-{code}" for code in WARD_CODES]

# DOOR now uses path-based layout filtering
LAYOUT_PATH_MAP = {
    "1R": "layout11",
    "1K": "layout12",
    "1DK": "layout13",
    "1LDK": "layout15",
    "2K": "layout22",
}

FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


def _build_search_urls(area_slug: str, criteria: SearchCriteria) -> list[str]:
    """Build DOOR search URL templates for each layout.

    DOOR requires path-based layout filtering (one layout per URL).
    Returns a list of URL templates with {{}} placeholder for page number.
    """
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    params = [
        f"rent_from={rent_min}",
        f"rent_to={rent_max}",
    ]

    if criteria.max_age_years > 0:
        params.append(f"chikunen={criteria.max_age_years}")

    query = "&".join(params)

    urls = []
    for layout in criteria.layouts:
        layout_path = LAYOUT_PATH_MAP.get(layout)
        if layout_path:
            urls.append(
                f"{BASE_URL}/specials/{layout_path}/tokyo/{area_slug}/list?{query}&page={{}}"
            )

    # Fallback: no layout filter if none matched
    if not urls:
        urls.append(f"{BASE_URL}/tokyo/{area_slug}/list?{query}&page={{}}")

    return urls


def _parse_rent(text: str) -> int:
    """Parse rent string like '13.5万円', '135,000円', or bare '13.5' to yen."""
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    # Bare number (from em.emphasis-primary): treat as 万円
    match = re.match(r"^\s*([\d.]+)\s*$", text)
    if match:
        val = float(match.group(1))
        if val < 1000:  # clearly 万円 unit
            return int(val * 10000)
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
    name = await _safe_text(
        building.locator(".building-box__head h2.heading a, h3 a, h2 a")
    )
    # Strip common suffix
    name = re.sub(r"の賃貸物件情報$", "", name).strip()

    # Building info from dl.description-item elements
    address = ""
    station = ""
    year_built = ""

    # Primary: new dl-based selectors
    station_el = building.locator("dl.description-item--station dd")
    if await station_el.count() > 0:
        try:
            raw_html = await station_el.first.inner_html()
            # Replace <br> tags with " / " for multi-station separation
            raw = re.sub(r"<br\s*/?>", " / ", raw_html, flags=re.IGNORECASE)
            raw = re.sub(r"<[^>]+>", "", raw)  # strip remaining tags
            station = raw.strip()
        except Exception:
            station = await _safe_text(station_el)

    # Address and year_built from generic dl items
    dl_items = building.locator("dl.description-item")
    for idx in range(min(10, await dl_items.count())):
        dl = dl_items.nth(idx)
        dt_text = await _safe_text(dl.locator("dt"))
        dd_text = await _safe_text(dl.locator("dd"))
        if not dd_text:
            continue
        if "所在地" in dt_text and not address:
            address = dd_text
        elif "築年" in dt_text and not year_built:
            year_built = dd_text

    # Legacy fallback: p-based or div-based info
    if not address or not station:
        info_els = building.locator(
            "p.location, p.stations, p.built, "
            ".building-info p, .building-info div, "
            ".building-box__summary-primary p, .building-box__summary-primary div"
        )
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
    img_el = building.locator(
        ".building-box__summary-image img, img[src*='door'], img[src*='http']"
    )
    if await img_el.count() > 0:
        image_url = await img_el.first.get_attribute("src") or ""
        if image_url.startswith("//"):
            image_url = "https:" + image_url

    # Room rows from table
    rows = building.locator("table.table-secondary tbody tr, table tbody tr, table tr")
    row_count = await rows.count()

    for i in range(row_count):
        row = rows.nth(i)
        try:
            cells = row.locator("td")
            cell_count = await cells.count()
            if cell_count < 4:
                continue

            # Try em.emphasis-primary for rent first (new DOOR format)
            rent = 0
            rent_el = row.locator("em.emphasis-primary")
            if await rent_el.count() > 0:
                rent_text = await _safe_text(rent_el)
                rent = _parse_rent(rent_text)

            # Extract all cell texts
            cell_texts = []
            for ci in range(cell_count):
                cell_texts.append((await cells.nth(ci).text_content() or "").strip())

            # Fallback rent from cell text
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

            # Management fee: secondary fee value smaller than rent
            for ct in cell_texts:
                if not mgmt_fee and ("万円" in ct or "円" in ct):
                    val = _parse_rent(ct)
                    if 0 < val < rent and val != rent:
                        mgmt_fee = val

            # Deposit/key money from "敷/礼" format
            for ct in cell_texts:
                if "/" in ct and ("万" in ct or "なし" in ct):
                    parts = ct.split("/")
                    if len(parts) == 2:
                        deposit = _parse_rent(parts[0])
                        key_money = _parse_rent(parts[1])

            # Detail link
            link_el = row.locator(
                "a[href*='/buildings/'][href*='/properties/'], "
                "a[href*='/buildings/'], a[href*='/properties/']"
            )
            href = ""
            if await link_el.count() > 0:
                href = await link_el.first.get_attribute("href") or ""
            if not href:
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
    """Visit DOOR detail page to get features, structure, direction, station.

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
        station = prop.station_access
        features: list[str] = list(prop.features)
        # 2026-04-09: 詳細ページから rent/管理費を再取得して list ページ抽出のブレを補正
        rent = prop.rent
        mgmt_fee = prop.management_fee
        rent_updated = False

        # Extract from building-table, room-table, contract-table
        rows = page.locator(
            "table.building-table tr, table.room-table tr, "
            "table.contract-table tr, table.table-primary tr, table tr, dl"
        )
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
                address = td.replace("\n", " ").strip()
            elif ("交通" in th or "最寄" in th) and not station:
                station = re.sub(r"\s*\n\s*", " / ", td).strip()
            elif "構造" in th and not building_type:
                building_type = td
            elif ("向き" in th or "方位" in th or "所在階" in th) and not direction:
                # "4階 / 北東" → extract direction part
                if "/" in td:
                    parts = td.split("/")
                    direction = parts[-1].strip()
                else:
                    direction = td
            elif "築年" in th and not year_built:
                year_built = td
            elif ("賃料" in th or th.strip() == "家賃") and not rent_updated:
                # 詳細ページの賃料を正として採用 (list ページの em.emphasis-primary は building 範囲値の
                # 可能性がある)
                new_rent = _parse_rent(td)
                if new_rent > 0:
                    rent = new_rent
                    rent_updated = True
            elif "管理費" in th or "共益費" in th:
                new_mgmt = _parse_rent(td)
                if new_mgmt > 0:
                    mgmt_fee = new_mgmt
            elif "設備" in th or "条件" in th:
                for item in re.split(r"[/／・、,\n]", td):
                    item = item.strip()
                    if item and item not in features:
                        features.append(item)

        # Fallback: text-based equipment extraction
        if not features and "設備" in body_text:
            equip_match = re.search(
                r"設備[/／条件]*[：:\s]*(.*?)(?:\n\n|\Z)", body_text, re.DOTALL
            )
            if equip_match:
                for item in re.split(r"[/／・、,\n]", equip_match.group(1)):
                    item = item.strip()
                    if item and item not in features and len(item) < 30:
                        features.append(item)

        await asyncio.sleep(delay)

        if rent_updated and rent != prop.rent:
            logger.info(
                "DOOR: rent corrected from list (%d → %d) for %s",
                prop.rent, rent, prop.name[:30],
            )

        return Property(
            source=prop.source,
            url=prop.url,
            name=prop.name,
            address=address,
            rent=rent,
            management_fee=mgmt_fee,
            deposit=prop.deposit,
            key_money=prop.key_money,
            layout=prop.layout,
            area_sqm=prop.area_sqm,
            floor=prop.floor,
            building_type=building_type,
            year_built=year_built,
            direction=direction,
            station_access=station,
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

        # Phase 1: Collect from list pages (one URL per layout × area)
        for area_slug in AREA_CODES:
            url_templates = _build_search_urls(area_slug, config.search)

            for url_template in url_templates:
                for page_num in range(1, config.scraping.max_pages_per_site + 1):
                    search_url = url_template.format(page_num)
                    logger.info("Scraping DOOR %s page %d", area_slug, page_num)

                    try:
                        await goto_with_retry(
                            page,
                            search_url,
                            timeout_ms=config.scraping.timeout_sec * 1000,
                            logger=logger,
                        )
                    except Exception:
                        logger.exception("Failed to load DOOR page after retries")
                        break

                    # Primary: div.building-box (current DOOR structure)
                    buildings = page.locator("div.building-box")
                    count = await buildings.count()

                    # Fallback: older selectors
                    if count == 0:
                        buildings = page.locator(
                            "div.building-card, "
                            "div[class*='building']:has(table)"
                        )
                        count = await buildings.count()

                    # Last resort: extract from whole page
                    if count == 0:
                        body = await page.text_content("body") or ""
                        if "buildings" in body or "building" in body:
                            logger.info(
                                "DOOR: selectors don't match; "
                                "trying broad extraction on page %d",
                                page_num,
                            )
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
                        "a[rel='next'], a.btn-pagination-next, "
                        "a:has-text('次'), a:has-text('次へ')"
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
            "DOOR: %d unique from %d total",
            len(unique), len(properties),
        )

        # Phase 2: Detail enrichment (skip if too many)
        enrichment_cap = config.scraping.detail_enrichment_cap
        if len(unique) > enrichment_cap:
            logger.info(
                "DOOR: skipping detail enrichment (%d > cap %d)",
                len(unique), enrichment_cap,
            )
            enriched = unique
        else:
            logger.info("DOOR: enriching %d details...", len(unique))
            enriched = []
            for i, prop in enumerate(unique):
                if (i + 1) % 20 == 0:
                    logger.info("DOOR: enriching %d/%d...", i + 1, len(unique))
                result = await _enrich_from_detail(
                    page, prop, config.scraping.request_delay_sec,
                )
                if result is not None:
                    enriched.append(result)

        # Phase 3: AI fallback for properties missing critical fields
        ai_needed = [p for p in enriched if needs_ai_fallback(p) and p.url]
        if ai_needed:
            logger.info(
                "DOOR: %d properties need AI fallback (missing station/address)",
                len(ai_needed),
            )
            try:
                from ai.extractor import extract_property_fields, reset_extraction_count

                reset_extraction_count()
                ai_fixed: list[Property] = []
                ai_count = 0
                for prop in enriched:
                    if needs_ai_fallback(prop) and prop.url:
                        try:
                            await page.goto(prop.url, timeout=15000)
                            await page.wait_for_load_state("domcontentloaded")
                            html = await page.content()
                            extracted = await extract_property_fields(
                                html, config.gemini_model
                            )
                            if extracted:
                                ai_count += 1
                                prop = Property(
                                    source=prop.source,
                                    url=prop.url,
                                    name=prop.name or extracted.name,
                                    address=prop.address or extracted.address,
                                    rent=prop.rent or extracted.rent,
                                    management_fee=prop.management_fee or extracted.management_fee,
                                    deposit=prop.deposit or extracted.deposit,
                                    key_money=prop.key_money or extracted.key_money,
                                    layout=prop.layout or extracted.layout,
                                    area_sqm=prop.area_sqm or extracted.area_sqm,
                                    floor=prop.floor or extracted.floor,
                                    building_type=prop.building_type or extracted.building_type,
                                    year_built=prop.year_built or extracted.year_built,
                                    direction=prop.direction or extracted.direction,
                                    station_access=prop.station_access or extracted.station_access,
                                    features=prop.features or tuple(extracted.features),
                                    image_url=prop.image_url,
                                )
                            await asyncio.sleep(config.scraping.request_delay_sec)
                        except Exception:
                            logger.debug("AI fallback failed for %s", prop.url)
                    ai_fixed.append(prop)
                enriched = ai_fixed
                logger.info("DOOR: AI fallback enriched %d properties", ai_count)
            except ImportError:
                logger.warning("AI extractor not available, skipping fallback")

        await browser.close()

    logger.info("DOOR: Found %d properties (final)", len(enriched))
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
                "xpath=ancestor::div[contains(@class,'building-box')] "
                "| ancestor::*[.//h3 or .//h2][1]"
            )
            name = ""
            address = ""
            station = ""
            if await building_parent.count() > 0:
                bp = building_parent.first
                name_el = bp.locator(
                    ".building-box__head h2.heading a, h3 a, h2 a"
                )
                name = await _safe_text(name_el)
                name = re.sub(r"の賃貸物件情報$", "", name).strip()

                # Try dl-based extraction first
                station_dd = bp.locator("dl.description-item--station dd")
                if await station_dd.count() > 0:
                    raw = await _safe_text(station_dd)
                    station = re.sub(r"\s*\n\s*", " / ", raw).strip()

                bp_text = (await bp.text_content() or "")
                addr_match = re.search(r"東京都[^\n]+", bp_text)
                if addr_match:
                    address = addr_match.group(0).strip()
                if not station:
                    station_match = re.search(
                        r"[^\n]*(?:駅|徒歩)[^\n]*", bp_text
                    )
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
