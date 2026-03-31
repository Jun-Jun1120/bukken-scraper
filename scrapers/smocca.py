"""スモッカ (Smocca) property scraper using Playwright.

Site: smocca.jp
Structure: card-based listings. Each div.bukken contains a property card
with an anchor to the detail page. Detail pages have label-value info.
Pagination: /search/tokyo/city/{code}/page/{num}
"""

import asyncio
import logging
import re

from playwright.async_api import Locator, async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property

logger = logging.getLogger(__name__)

BASE_URL = "https://smocca.jp/search/tokyo/city/"
AREA_CODES = ["13113", "13110", "13104", "13103"]

FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


def _build_search_url(area_code: str, criteria: SearchCriteria) -> str:
    """Build Smocca search URL from criteria.

    Smocca uses path-based pagination: /page/{num}
    Query params for rent range and layout.
    """
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    params = [
        f"rent_from={rent_min}",
        f"rent_to={rent_max}",
    ]

    layout_map = {
        "1R": "1r", "1K": "1k", "1DK": "1dk",
        "1LDK": "1ldk", "2K": "2k",
    }
    for layout in criteria.layouts:
        if code := layout_map.get(layout):
            params.append(f"madori[]={code}")

    query = "&".join(params)
    # Page 1 has no /page/ prefix; page 2+ uses /page/{num}
    return f"{BASE_URL}{area_code}/page/{{}}" + f"?{query}"


def _build_first_page_url(area_code: str, criteria: SearchCriteria) -> str:
    """Build URL for the first page (no /page/ in path)."""
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000

    params = [
        f"rent_from={rent_min}",
        f"rent_to={rent_max}",
    ]

    layout_map = {
        "1R": "1r", "1K": "1k", "1DK": "1dk",
        "1LDK": "1ldk", "2K": "2k",
    }
    for layout in criteria.layouts:
        if code := layout_map.get(layout):
            params.append(f"madori[]={code}")

    query = "&".join(params)
    return f"{BASE_URL}{area_code}?{query}"


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
    """Parse area string like '20.84m²' to float."""
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


async def _extract_listings(page) -> list[Property]:
    """Extract all property listings from a Smocca search results page.

    Smocca cards are div.bukken elements, each containing an anchor
    to the detail page with property info as text content.
    """
    properties: list[Property] = []

    cards = page.locator("div.bukken")
    count = await cards.count()

    if count == 0:
        # Fallback: try other container selectors
        cards = page.locator(
            ".bukken_item, .item_list02.bukken, "
            "div[class*='bukken'], article[class*='bukken']"
        )
        count = await cards.count()

    for i in range(count):
        try:
            card = cards.nth(i)
            card_text = (await card.text_content() or "").strip()

            if not card_text:
                continue

            # Detail link
            link_el = card.locator("a[href*='/bukken/detail/']")
            href = ""
            if await link_el.count() > 0:
                href = await link_el.first.get_attribute("href") or ""
            else:
                # Try any link
                any_link = card.locator("a[href]")
                if await any_link.count() > 0:
                    href = await any_link.first.get_attribute("href") or ""

            url = (
                f"https://smocca.jp{href}"
                if href and not href.startswith("http")
                else href
            )

            # Building name: from the link text or heading
            name = ""
            name_el = card.locator("a[href*='/bukken/detail/']")
            if await name_el.count() > 0:
                # Get just the first line / heading text
                name = (await name_el.first.text_content() or "").strip()
                # Trim to just the building name (first line)
                name = name.split("\n")[0].strip() if name else ""

            # Extract from card text using patterns
            # Address: 東京都...
            address = ""
            addr_match = re.search(r"(東京都[^\n]+?)(?:\s|$)", card_text)
            if addr_match:
                address = addr_match.group(1).strip()

            # Rent: X.X万円 format
            rent = 0
            rent_match = re.search(r"([\d.]+)\s*万円", card_text)
            if rent_match:
                rent = int(float(rent_match.group(1)) * 10000)

            if not rent:
                continue

            # Management fee: often after rent like "/ 5,000円" or "管理費X円"
            mgmt_fee = 0
            mgmt_match = re.search(
                r"(?:管理費|共益費|/)\s*([\d,.]+)\s*(?:万?円)", card_text,
            )
            if mgmt_match:
                mgmt_text = mgmt_match.group(0)
                mgmt_fee = _parse_rent(mgmt_text)
                # Avoid misidentifying rent as mgmt fee
                if mgmt_fee == rent:
                    mgmt_fee = 0

            # Layout: 1R, 1K, 1DK, 1LDK, 2K, ワンルーム
            layout = ""
            layout_match = re.search(
                r"(ワンルーム|[12][RKDL]{1,3})", card_text,
            )
            if layout_match:
                layout = layout_match.group(1)

            # Area: XX.XXm²
            area = _parse_area(card_text)

            # Station: XX線/XX駅 徒歩X分
            station = ""
            station_match = re.search(
                r"([^\n]*(?:線|駅)[^\n]*徒歩\d+分)", card_text,
            )
            if station_match:
                station = station_match.group(1).strip()

            # Floor: X階
            floor = ""
            floor_match = re.search(r"(\d+)階", card_text)
            if floor_match:
                floor = floor_match.group(0)

            # Year built
            year_built = ""
            year_match = re.search(r"(築\d+年|\d{4}年\d{1,2}月)", card_text)
            if year_match:
                year_built = year_match.group(0)

            # Image
            image_url = ""
            img_el = card.locator("img[src*='smocca'], img[src*='http']")
            if await img_el.count() > 0:
                image_url = await img_el.first.get_attribute("src") or ""

            properties.append(Property(
                source="smocca",
                url=url,
                name=name,
                address=address,
                rent=rent,
                management_fee=mgmt_fee,
                layout=layout,
                area_sqm=area,
                floor=floor,
                year_built=year_built,
                station_access=station,
                image_url=image_url,
            ))
        except Exception:
            logger.debug("Failed to extract Smocca listing %d", i)

    return properties


async def _enrich_from_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit Smocca detail page to get features, structure, direction.

    Returns enriched Property, or None if female-only.
    """
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("Smocca: skipped female-only: %s", prop.name)
            return None

        building_type = prop.building_type
        direction = prop.direction
        address = prop.address
        year_built = prop.year_built
        features: list[str] = list(prop.features)
        deposit = prop.deposit
        key_money = prop.key_money
        management_fee = prop.management_fee

        # Smocca detail uses label-value pairs; try table/dl parsing
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
            elif ("構造" in th or "種別" in th) and not building_type:
                building_type = td
            elif ("向き" in th or "方位" in th) and not direction:
                direction = td
            elif "築年" in th and not year_built:
                year_built = td
            elif "敷金" in th and not deposit:
                deposit = _parse_rent(td)
            elif "礼金" in th and not key_money:
                key_money = _parse_rent(td)
            elif "管理" in th and not management_fee:
                management_fee = _parse_rent(td)
            elif "設備" in th or "条件" in th:
                for item in re.split(r"[/／・、,\n]", td):
                    item = item.strip()
                    if item and item not in features:
                        features.append(item)

        # Fallback: parse body text for equipment section
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

        # Extract direction from body text if still missing
        if not direction:
            dir_match = re.search(r"方位[：:\s]*(北|南|東|西|北東|北西|南東|南西)", body_text)
            if dir_match:
                direction = dir_match.group(1)

        await asyncio.sleep(delay)

        return Property(
            source=prop.source,
            url=prop.url,
            name=prop.name,
            address=address,
            rent=prop.rent,
            management_fee=management_fee,
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
        logger.warning("Failed to enrich Smocca detail: %s", prop.url)
        return prop


async def scrape_smocca(config: AppConfig) -> list[Property]:
    """Scrape Smocca listings matching search criteria."""
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
        for area_code in AREA_CODES:
            for page_num in range(1, config.scraping.max_pages_per_site + 1):
                if page_num == 1:
                    search_url = _build_first_page_url(area_code, config.search)
                else:
                    url_template = _build_search_url(area_code, config.search)
                    search_url = url_template.format(page_num)

                logger.info("Scraping Smocca %s page %d", area_code, page_num)

                try:
                    resp = await page.goto(
                        search_url,
                        timeout=config.scraping.timeout_sec * 1000,
                        wait_until="domcontentloaded",
                    )
                    # Handle redirect issues gracefully
                    if resp and resp.status >= 400:
                        logger.warning(
                            "Smocca returned status %d for %s",
                            resp.status, search_url,
                        )
                        break
                except Exception as exc:
                    err_str = str(exc).lower()
                    if "redirect" in err_str or "err_too_many_redirects" in err_str:
                        logger.warning(
                            "Smocca: redirect error for %s page %d. "
                            "Trying without query params...",
                            area_code, page_num,
                        )
                        # Try simplified URL without params
                        try:
                            simple_url = f"{BASE_URL}{area_code}"
                            if page_num > 1:
                                simple_url += f"/page/{page_num}"
                            await page.goto(
                                simple_url,
                                timeout=config.scraping.timeout_sec * 1000,
                            )
                            await page.wait_for_load_state("domcontentloaded")
                        except Exception:
                            logger.exception(
                                "Failed to load Smocca page (retry)",
                            )
                            break
                    else:
                        logger.exception("Failed to load Smocca page")
                        break

                listings = await _extract_listings(page)

                if not listings:
                    logger.info(
                        "No listings on Smocca %s page %d", area_code, page_num,
                    )
                    break

                properties.extend(listings)

                # Check pagination
                next_el = page.locator(
                    f"a[href*='/page/{page_num + 1}'], "
                    "a[rel='next'], a:has-text('次')"
                )
                if await next_el.count() == 0:
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
            "Smocca: %d unique from %d total, enriching details...",
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
                            "Smocca: enriched %d/%d...",
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

    logger.info("Smocca: Found %d properties (after detail check)", len(enriched))
    return enriched
