"""CHINTAI property scraper using Playwright.

Site structure: building-centric with hidden input fields.
Each section.cassette_item.build has inputs like .bkName, .chinRyo, .madori etc.
Multiple rooms per building share the same section, each set of inputs repeated.
"""

import asyncio
import logging
import re

from playwright.async_api import Locator, async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property

logger = logging.getLogger(__name__)

BASE_URL = "https://www.chintai.net/tokyo/area/"
AREA_CODES = ["13113", "13110", "13104", "13103"]


def _build_search_url(area_code: str, criteria: SearchCriteria) -> str:
    rent_min = criteria.rent_min // 10000
    rent_max = criteria.rent_max // 10000
    age_param = f"&built={criteria.max_age_years}" if criteria.max_age_years > 0 else ""
    return f"{BASE_URL}{area_code}/list/?rent_low={rent_min}&rent_high={rent_max}{age_param}&page={{}}"


async def _safe_val(locator: Locator) -> str:
    """Get value attribute from first matching input."""
    try:
        if await locator.count() > 0:
            return (await locator.first.get_attribute("value") or "").strip()
    except Exception:
        pass
    return ""


async def _safe_text(locator: Locator) -> str:
    try:
        if await locator.count() > 0:
            return (await locator.first.text_content() or "").strip()
    except Exception:
        pass
    return ""


async def _extract_from_page(page) -> list[Property]:
    """Extract all properties from CHINTAI search results page.

    CHINTAI uses repeating hidden inputs for each room within the page.
    We collect all .bkName, .chinRyo, .madori, etc. inputs and zip them.
    """
    properties: list[Property] = []

    # Get all hidden input arrays
    names = page.locator("input.bkName")
    rents = page.locator("input.chinRyo")
    layouts = page.locator("input.madori")
    areas = page.locator("input.senMenseki")
    stations = page.locator("input.ekiName")
    walks = page.locator("input.ekiToho")
    img_urls = page.locator("input.imgUrl")

    count = await names.count()
    rent_count = await rents.count()

    # Use the smaller count to avoid index errors
    n = min(count, rent_count)

    # Get detail links
    links = page.locator("a.bukkenlisting_link[href*='/detail/']")
    link_count = await links.count()

    for i in range(n):
        try:
            name = await names.nth(i).get_attribute("value") or ""
            rent_str = await rents.nth(i).get_attribute("value") or "0"
            layout = await layouts.nth(i).get_attribute("value") or "" if i < await layouts.count() else ""
            area_str = await areas.nth(i).get_attribute("value") or "0" if i < await areas.count() else "0"
            station = await stations.nth(i).get_attribute("value") or "" if i < await stations.count() else ""
            walk = await walks.nth(i).get_attribute("value") or "" if i < await walks.count() else ""

            rent = int(rent_str) if rent_str.isdigit() else 0
            if not rent:
                continue

            area = float(area_str) if area_str.replace(".", "").isdigit() else 0.0
            station_access = f"{station} 徒歩{walk}分" if station else ""

            url = ""
            if i < link_count:
                href = await links.nth(i).get_attribute("href") or ""
                url = f"https://www.chintai.net{href}" if href and not href.startswith("http") else href

            image_url = ""
            if i < await img_urls.count():
                img_val = await img_urls.nth(i).get_attribute("value") or ""
                if img_val:
                    image_url = f"https:{img_val}" if img_val.startswith("//") else img_val

            properties.append(Property(
                source="chintai",
                url=url,
                name=name.strip(),
                address="",
                rent=rent,
                layout=layout.strip(),
                area_sqm=area,
                station_access=station_access,
                image_url=image_url,
            ))
        except Exception:
            logger.debug("Failed to extract CHINTAI property %d", i)

    return properties


FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


def _parse_fee(text: str) -> int:
    """Parse fee string like '7,000円' to yen integer."""
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    return 0


async def _enrich_chintai_detail(page, prop: Property, delay: float) -> Property | None:
    """Visit CHINTAI detail page to get address, features, structure, direction."""
    if not prop.url:
        return prop

    try:
        await page.goto(prop.url, timeout=15000)
        await page.wait_for_load_state("domcontentloaded")

        body_text = await page.text_content("body") or ""
        if any(kw in body_text for kw in FEMALE_KEYWORDS):
            logger.info("CHINTAI: skipped female-only: %s", prop.name)
            return None

        address = prop.address
        building_type = prop.building_type
        direction = prop.direction
        year_built = prop.year_built
        floor = prop.floor
        features: list[str] = list(prop.features)
        deposit = prop.deposit
        key_money = prop.key_money
        management_fee = prop.management_fee

        # Helper to process a label-value pair into the extracted fields
        def _process_label_value(
            th: str, td: str,
            address_: str, building_type_: str, direction_: str,
            year_built_: str, floor_: str, management_fee_: int,
            deposit_: int, key_money_: int, features_: list[str],
        ) -> tuple[str, str, str, str, str, int, int, int, list[str]]:
            if not th or not td:
                return (address_, building_type_, direction_, year_built_,
                        floor_, management_fee_, deposit_, key_money_, features_)
            if ("所在地" in th or "住所" in th) and not address_:
                address_ = td.replace("\n", "").strip()
            elif "構造" in th and not building_type_:
                building_type_ = td
            elif "向き" in th and not direction_:
                direction_ = td
            elif "築年" in th and not year_built_:
                year_built_ = td
            elif "階" in th and "建" not in th and not floor_:
                floor_ = td
            elif "管理費" in th and not management_fee_:
                management_fee_ = _parse_fee(td)
            elif "敷金" in th and not deposit_:
                deposit_ = _parse_fee(td)
            elif "礼金" in th and not key_money_:
                key_money_ = _parse_fee(td)
            elif "設備" in th or "条件" in th:
                for item in re.split(r"[/／・、,\n]", td):
                    item = item.strip()
                    if item and item not in features_:
                        features_.append(item)
            return (address_, building_type_, direction_, year_built_,
                    floor_, management_fee_, deposit_, key_money_, features_)

        # Extract from table rows (th/td pairs)
        table_rows = page.locator("table tr")
        for ri in range(await table_rows.count()):
            th_el = table_rows.nth(ri).locator("th")
            td_el = table_rows.nth(ri).locator("td")
            if await th_el.count() == 0 or await td_el.count() == 0:
                continue
            th = (await th_el.first.text_content() or "").strip()
            td = (await td_el.first.text_content() or "").strip()
            (address, building_type, direction, year_built, floor,
             management_fee, deposit, key_money, features) = _process_label_value(
                th, td, address, building_type, direction, year_built,
                floor, management_fee, deposit, key_money, features,
            )

        # Extract from dl elements (dt/dd pairs)
        dl_elements = page.locator("dl")
        for ri in range(await dl_elements.count()):
            dt_el = dl_elements.nth(ri).locator("dt")
            dd_el = dl_elements.nth(ri).locator("dd")
            if await dt_el.count() == 0 or await dd_el.count() == 0:
                continue
            th = (await dt_el.first.text_content() or "").strip()
            td = (await dd_el.first.text_content() or "").strip()
            (address, building_type, direction, year_built, floor,
             management_fee, deposit, key_money, features) = _process_label_value(
                th, td, address, building_type, direction, year_built,
                floor, management_fee, deposit, key_money, features,
            )

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
            address=address,
            rent=prop.rent,
            management_fee=management_fee,
            deposit=deposit,
            key_money=key_money,
            layout=prop.layout,
            area_sqm=prop.area_sqm,
            floor=floor,
            building_type=building_type,
            year_built=year_built,
            direction=direction,
            station_access=prop.station_access,
            features=tuple(features),
            image_url=prop.image_url,
        )
    except Exception:
        logger.warning("Failed to enrich CHINTAI detail: %s", prop.url)
        return prop


async def scrape_chintai(config: AppConfig) -> list[Property]:
    properties: list[Property] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=config.scraping.headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Phase 1: Collect from list pages
        for area_code in AREA_CODES:
            url_template = _build_search_url(area_code, config.search)

            for page_num in range(1, config.scraping.max_pages_per_site + 1):
                search_url = url_template.format(page_num)
                logger.info("Scraping CHINTAI %s page %d", area_code, page_num)

                try:
                    await page.goto(search_url, timeout=config.scraping.timeout_sec * 1000)
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:
                    logger.exception("Failed to load CHINTAI page")
                    break

                buildings = page.locator("section.cassette_item.build")
                if await buildings.count() == 0:
                    break

                rooms = await _extract_from_page(page)
                properties.extend(rooms)

                next_btn = page.locator("a[rel='next'], li.next a")
                if await next_btn.count() == 0:
                    break

                await asyncio.sleep(config.scraping.request_delay_sec)

        # Deduplicate by URL or name+rent (CHINTAI has many dupes across areas)
        seen: set[str] = set()
        unique: list[Property] = []
        for p in properties:
            key = p.url if p.url else f"{p.name}_{p.rent}_{p.layout}"
            if key not in seen:
                seen.add(key)
                unique.append(p)
        logger.info("CHINTAI: %d unique from %d total, enriching details...", len(unique), len(properties))

        # Phase 2: Visit each detail page
        enriched: list[Property] = []
        for i, prop in enumerate(unique):
            if (i + 1) % 20 == 0:
                logger.info("CHINTAI: enriching %d/%d...", i + 1, len(unique))
            result = await _enrich_chintai_detail(page, prop, config.scraping.request_delay_sec)
            if result is not None:
                enriched.append(result)

        await browser.close()

    logger.info("CHINTAI: Found %d properties (after detail check)", len(enriched))
    return enriched
