"""athome会員サイト (customer.athome.jp) マッチング物件スクレイパー.

Scrapes matching properties from the athome customer portal.
Requires ATHOME_USER and ATHOME_PASS environment variables.
"""

import asyncio
import logging
import os
import re

from playwright.async_api import Page, async_playwright

from config import AppConfig
from scrapers import Property

logger = logging.getLogger(__name__)

BASE_URL = "https://customer.athome.jp"
LOGIN_URL = f"{BASE_URL}/Account/LogOn"
SHOP_ID = "00263109"
MATCHING_URL = f"{BASE_URL}/{SHOP_ID}/MatchingReference/MatchingIndex"


def _parse_rent(text: str) -> int:
    """Parse rent like '7.35万円' to yen integer."""
    match = re.search(r"([\d.]+)\s*万", text)
    if match:
        return int(float(match.group(1)) * 10000)
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_fee(text: str) -> int:
    """Parse fee like '3,000円' or '1ヶ月' to yen integer (0 for month-based)."""
    match = re.search(r"([\d,]+)\s*円", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0


def _parse_area(text: str) -> float:
    """Parse area like '18.23m²' to float."""
    match = re.search(r"([\d.]+)\s*m", text)
    return float(match.group(1)) if match else 0.0


async def _login(page: Page) -> bool:
    """Login to athome customer portal. Returns True on success."""
    user = os.environ.get("ATHOME_USER", "")
    password = os.environ.get("ATHOME_PASS", "")

    if not user or not password:
        logger.error("ATHOME_USER or ATHOME_PASS not set")
        return False

    return_url = f"%2f{SHOP_ID}%2fMatchingReference%2fMatchingIndex"
    await page.goto(
        f"{LOGIN_URL}?ReturnUrl={return_url}",
        timeout=30000,
    )
    await page.wait_for_load_state("domcontentloaded")

    await page.fill("#MypageId", user)
    await page.fill("#MypagePassword", password)
    await page.locator('input[type="submit"]').click()
    await page.wait_for_load_state("networkidle", timeout=15000)

    # Handle forced logout (another session active)
    if "SelectForcedLogOn" in page.url or "ForcedLogOn" in page.url:
        logger.info("athome member: forced logout detected, re-logging in")
        forced_btn = page.locator("#forcedLogOn")
        if await forced_btn.count() > 0:
            await forced_btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)

    return "MatchingIndex" in page.url or SHOP_ID in page.url


FEMALE_KEYWORDS = ("女性限定", "女性専用", "女性のみ", "レディース")


async def _check_detail_page(page: Page, detail_idx: int) -> tuple[bool, str, str]:
    """Click detail link, check for female-only, extract extra info, then go back.

    Returns (is_female_only, building_name, conditions_text).
    """
    detail_links = page.locator("a.detailLink")
    if detail_idx >= await detail_links.count():
        return False, "", ""

    # Remember current URL to go back
    list_url = page.url

    try:
        # Click detail link (opens in same tab via JS)
        await detail_links.nth(detail_idx).click()
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        await asyncio.sleep(0.5)

        # Check full page text for female-only keywords
        body_text = await page.text_content("body") or ""
        is_female = any(kw in body_text for kw in FEMALE_KEYWORDS)

        # Extract building name and conditions
        building_name = ""
        conditions = ""
        rows = page.locator("table tr")
        for ri in range(await rows.count()):
            th_el = rows.nth(ri).locator("th")
            td_el = rows.nth(ri).locator("td")
            if await th_el.count() == 0 or await td_el.count() == 0:
                continue
            th = (await th_el.first.text_content() or "").strip()
            td = (await td_el.first.text_content() or "").strip()
            if th == "建物名・部屋番号":
                building_name = td
            elif th == "条件等":
                conditions = td

        return is_female, building_name, conditions
    except Exception:
        return False, "", ""
    finally:
        # Go back to list page
        try:
            await page.go_back(timeout=10000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass


async def _extract_properties_from_table(page: Page) -> list[Property]:
    """Extract property rows, checking each detail page for conditions."""
    properties: list[Property] = []

    # Each property occupies 2 rows: data row + button row
    data_rows = page.locator("form#checkedform tr:has(td.chkBox)")
    count = await data_rows.count()

    for i in range(count):
        # Re-locate rows after each navigation (DOM may refresh)
        data_rows = page.locator("form#checkedform tr:has(td.chkBox)")
        if i >= await data_rows.count():
            break

        row = data_rows.nth(i)
        try:
            # Traffic/Location: "駅名/路線<br>住所"
            traffic_loc = await row.locator("td.traficLocation").inner_html()
            parts = traffic_loc.split("<br>")
            station_line = parts[0].strip() if parts else ""
            address = parts[1].strip() if len(parts) > 1 else ""

            # Walk minutes
            walk_text = (await row.locator("td.stWalk").text_content() or "").strip()
            station_access = f"{station_line} 徒歩{walk_text}" if station_line else ""

            # Rent/Admin: "7.35万円<br>3,000円"
            rent_admin = await row.locator("td.rent-adminExp").inner_html()
            rent_parts = rent_admin.split("<br>")
            rent = _parse_rent(rent_parts[0]) if rent_parts else 0
            mgmt_fee = _parse_fee(rent_parts[1]) if len(rent_parts) > 1 else 0

            # Deposit/Reward: "1ヶ月 / なし<br>1ヶ月"
            deposit_reward = await row.locator("td.deposit-Reward").inner_html()
            dep_parts = deposit_reward.split("<br>")
            deposit_text = dep_parts[0].strip() if dep_parts else ""
            key_money_text = dep_parts[1].strip() if len(dep_parts) > 1 else ""

            deposit = 0
            if "/" in deposit_text:
                dep_val = deposit_text.split("/")[0].strip()
                deposit = _parse_fee(dep_val)

            key_money = _parse_fee(key_money_text)

            # Floor plan/Area: "ワンルーム<br>18.23m²"
            plan_area = await row.locator("td.floorplan-Area").inner_html()
            plan_parts = plan_area.split("<br>")
            layout = plan_parts[0].strip() if plan_parts else ""
            area = _parse_area(plan_parts[1]) if len(plan_parts) > 1 else 0.0

            # Property type/Build age: "貸マンション<br>2014年3月"
            type_age = await row.locator("td.propEvent-buildAge").inner_html()
            type_parts = type_age.split("<br>")
            building_type = type_parts[0].strip() if type_parts else ""
            year_built = type_parts[1].strip() if len(type_parts) > 1 else ""

            # Image URL
            image_url = ""
            img_el = row.locator("td.estImg img.estateImage")
            if await img_el.count() > 0:
                image_url = await img_el.get_attribute("src") or ""

            # Detail link key
            checkbox = row.locator('input[name="estatekey"]')
            estate_key = await checkbox.get_attribute("value") or ""
            detail_url = f"{BASE_URL}/{SHOP_ID}/EstateSearch/EstateDetail/{estate_key}?scd=01"

            if not rent:
                continue

            # Check detail page for female-only and extra info
            is_female, building_name, conditions = await _check_detail_page(page, i)

            if is_female:
                logger.info(
                    "athome member: skipped female-only: %s (%s)",
                    building_name or address, conditions,
                )
                continue

            prop_name = building_name if building_name else f"{layout} {address}"

            properties.append(Property(
                source="athome会員",
                url=detail_url,
                name=prop_name,
                address=address,
                rent=rent,
                management_fee=mgmt_fee,
                deposit=deposit,
                key_money=key_money,
                layout=layout,
                area_sqm=area,
                floor="",
                building_type=building_type,
                year_built=year_built,
                direction="",
                station_access=station_access,
                image_url=image_url,
            ))
        except Exception:
            logger.debug("Failed to extract athome member property row %d", i)

    return properties


async def scrape_athome_member(config: AppConfig) -> list[Property]:
    """Scrape matching properties from athome customer portal."""
    all_properties: list[Property] = []

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

        # Login
        if not await _login(page):
            logger.error("athome member: login failed")
            await browser.close()
            return []

        logger.info("athome member: logged in successfully")

        # Navigate to matching index if not already there
        if "MatchingIndex" not in page.url:
            await page.goto(MATCHING_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

        # Count matching conditions (must click, not goto - session-based nav)
        search_links = page.locator("a.selectMatching")
        link_count = await search_links.count()
        logger.info("athome member: found %d matching conditions", link_count)

        for idx in range(link_count):
            logger.info(
                "athome member: scraping matching condition %d/%d",
                idx + 1,
                link_count,
            )

            try:
                # Click the search link (must use click, not goto)
                search_links = page.locator("a.selectMatching")
                await search_links.nth(idx).click()
                await page.wait_for_load_state("networkidle", timeout=30000)

                props = await _extract_properties_from_table(page)
                all_properties.extend(props)
                logger.info(
                    "athome member: condition %d yielded %d properties",
                    idx + 1,
                    len(props),
                )

                # Go back to matching index for next condition
                if idx < link_count - 1:
                    await page.goto(MATCHING_URL, timeout=30000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                logger.exception(
                    "athome member: failed to scrape condition %d", idx + 1
                )
                # Try to recover by navigating back
                try:
                    await page.goto(MATCHING_URL, timeout=30000)
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    break

            await asyncio.sleep(config.scraping.request_delay_sec)

        await browser.close()

    logger.info("athome member: total %d properties", len(all_properties))
    return all_properties
