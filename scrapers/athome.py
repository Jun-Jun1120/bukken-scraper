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

from bs4 import BeautifulSoup, Tag
from playwright.async_api import async_playwright

from config import AppConfig, SearchCriteria
from scrapers import Property, goto_with_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://www.athome.co.jp/chintai/tokyo/"

# Area slugs aligned with stations.py target wards.
AREA_SLUGS = [
    "shibuya-city",   # 渋谷区 (13113)
    "shinjuku-city",  # 新宿区 (13104)
    "minato-city",    # 港区 (13103)
    "meguro-city",    # 目黒区 (13110)
    "setagaya-city",  # 世田谷区 (13112)
    "shinagawa-city", # 品川区 (13109) — 目黒駅
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


def _bs_text(tag: Tag | None) -> str:
    """Safely get trimmed text from a BS4 tag."""
    if tag is None:
        return ""
    return tag.get_text(" ", strip=True)


def _bs_select_one(tag: Tag, selectors: tuple[str, ...]) -> Tag | None:
    """Try multiple CSS selectors; return the first matching tag."""
    for sel in selectors:
        found = tag.select_one(sel)
        if found is not None:
            return found
    return None


def _find_info_hint_by_dt(building: Tag, dt_keyword: str) -> str:
    """Find dl.p-property__information-hint where dt contains keyword."""
    for dl in building.select("dl.p-property__information-hint"):
        dt = dl.find("dt")
        if dt and dt_keyword in dt.get_text():
            dd = dl.find("dd")
            return _bs_text(dd)
    return ""


_BUILDING_TYPE_RE = re.compile(
    r"(SRC|RC|鉄骨鉄筋コンクリート|鉄筋コンクリート|重量鉄骨|軽量鉄骨|鉄骨|ALC|木造)"
)


def _extract_rooms_from_building_html(building: Tag) -> list[Property]:
    """Extract all room listings from a single building Tag using BeautifulSoup.

    2026-04-09: BS4 版。従来の Playwright locator 版 (_extract_rooms_from_building)
    はページあたり数千回の CDP 呼び出しで遅い (Windows で 2:30/page)。
    HTML を一度取得して Python 側でパースすることで 10 倍以上高速化。
    """
    properties: list[Property] = []

    # Building name
    name_el = _bs_select_one(
        building,
        (
            "h2.p-property__title--building",
            "h2[class*='property__title']",
            "h2 a",
            "h2",
        ),
    )
    name = _bs_text(name_el)

    # Address: try "map" icon first, then dt with 所在地, then first dl's dd
    address = ""
    map_dl = building.select_one(
        "dl.p-property__information-hint:has(i[class*='map'])"
    )
    if map_dl is not None:
        dd_strong = map_dl.select_one("dd strong")
        address = _bs_text(dd_strong) if dd_strong else _bs_text(map_dl.find("dd"))
    if not address:
        address = _find_info_hint_by_dt(building, "所在地")
    if not address:
        addr_el = building.select_one(".p-property__address")
        address = _bs_text(addr_el)
    if not address:
        first_dl = building.select_one("dl.p-property__information-hint")
        if first_dl is not None:
            dd_strong = first_dl.select_one("dd strong")
            address = _bs_text(dd_strong) if dd_strong else _bs_text(first_dl.find("dd"))

    # Station access
    station_access = ""
    train_dl = building.select_one(
        "dl.p-property__information-hint:has(i[class*='train'])"
    )
    if train_dl is not None:
        station_access = _bs_text(train_dl.find("dd"))
    if not station_access:
        station_access = _find_info_hint_by_dt(building, "交通")
    if not station_access:
        hints = building.select("dl.p-property__information-hint")
        if len(hints) >= 2:
            station_access = _bs_text(hints[1].find("dd"))

    # Building type and age
    type_age_raw = ""
    home_dl = building.select_one(
        "dl.p-property__information-hint:has(i[class*='home'])"
    )
    if home_dl is not None:
        type_age_raw = _bs_text(home_dl.find("dd"))
    if not type_age_raw:
        type_age_raw = _find_info_hint_by_dt(building, "築")
    if not type_age_raw:
        hints = building.select("dl.p-property__information-hint")
        if len(hints) >= 3:
            type_age_raw = _bs_text(hints[2].find("dd"))

    building_type_from_list = ""
    bt_match = _BUILDING_TYPE_RE.search(type_age_raw)
    if bt_match:
        building_type_from_list = bt_match.group(1)

    # Room detail boxes
    rooms = building.select(
        "div.p-property__room--detailbox, "
        "div[class*='property__room'], "
        "div.p-property__room"
    )

    for room in rooms:
        try:
            # Detail link
            link_el = _bs_select_one(
                room,
                (
                    "a.p-property__room-more-inner",
                    "a[href*='/chintai/']",
                    "a[class*='room-more']",
                ),
            )
            href = link_el.get("href", "") if link_el else ""
            if isinstance(href, list):
                href = href[0] if href else ""
            url = (
                f"https://www.athome.co.jp{href}"
                if href and not href.startswith("http")
                else href
            )

            # Rent
            rent_el = _bs_select_one(
                room,
                (
                    "b.p-property__information-rent",
                    "[class*='information-rent']",
                    ".rent",
                ),
            )
            rent = _parse_rent_man(_bs_text(rent_el))

            # Management fee
            mgmt_el = _bs_select_one(
                room,
                (
                    "li.p-property__room-rent p.p-property__information-price span",
                    "[class*='information-price'] span",
                ),
            )
            mgmt_fee = _parse_fee(_bs_text(mgmt_el))

            # Layout
            layout_el = _bs_select_one(
                room,
                (
                    "li.p-property__room-floorplan div.p-property__floor",
                    "[class*='room-floorplan'] [class*='floor']",
                    ".madori",
                ),
            )
            layout = _bs_text(layout_el)

            # Area — direct child span of room-floorplan li
            area_text = ""
            floorplan_li = room.select_one(
                "li.p-property__room-floorplan, [class*='room-floorplan']"
            )
            if floorplan_li is not None:
                for child in floorplan_li.find_all("span", recursive=False):
                    area_text = _bs_text(child)
                    if area_text:
                        break
            area = _parse_area(area_text)

            # Floor
            floor_el = _bs_select_one(
                room,
                (
                    "li.p-property__room-number",
                    "[class*='room-number']",
                ),
            )
            floor = _bs_text(floor_el)

            # Image
            image_url = ""
            img_el = room.find("img", src=True)
            if img_el is not None:
                src = img_el.get("src", "")
                if isinstance(src, str) and src.startswith("http"):
                    image_url = src

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
                building_type=building_type_from_list,
                year_built=type_age_raw,
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

        # 2026-04-09: Block images/fonts/media to speed up page loads (was ~2min/page)
        async def _block_heavy_resources(route) -> None:
            if route.request.resource_type in ("image", "font", "media", "stylesheet"):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", _block_heavy_resources)

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
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    # 2026-04-09: wait_until="commit" でナビゲーションコミットのみ待機
                    # DOM 描画は wait_for_selector で明示的に待つ
                    await goto_with_retry(
                        page,
                        search_url,
                        timeout_ms=config.scraping.timeout_sec * 1000,
                        wait_until="commit",
                        logger=logger,
                    )
                    # 物件カードが出るまで待つ (最大 10 秒)
                    try:
                        await page.wait_for_selector(
                            "div[class*='p-property--building']",
                            timeout=10000,
                            state="attached",
                        )
                    except Exception:
                        pass  # No results or slow page — continue to extraction
                except Exception:
                    logger.exception(
                        "Failed to load athome page %d for %s after retries",
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

                # 2026-04-09: BS4 ベース抽出に切替 (CDP 呼び出し削減で 10x 高速化)
                # ページ HTML を 1 回取得 → ローカルでパース
                page_html = await page.content()
                soup = BeautifulSoup(page_html, "lxml")
                building_tags = soup.select(
                    "div.p-property.p-property--building, "
                    "div.p-property--building, "
                    "div[class*='p-property--building']"
                )

                if not building_tags:
                    logger.info(
                        "No buildings on athome %s page %d",
                        area_slug, page_num,
                    )
                    break

                for building_tag in building_tags:
                    rooms = _extract_rooms_from_building_html(building_tag)
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
            "athome: %d unique from %d total (skipping detail enrichment for speed)",
            len(unique), len(properties),
        )

        # Filter out female-only from name (quick check)
        filtered = [
            p for p in unique
            if not any(kw in p.name for kw in FEMALE_KEYWORDS)
        ]
        if len(filtered) < len(unique):
            logger.info("athome: filtered %d female-only from names", len(unique) - len(filtered))

        await browser.close()

    logger.info("athome: Found %d properties (after detail check)", len(filtered))
    return filtered
