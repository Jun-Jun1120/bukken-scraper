"""Entry point for the bukken-scraper pipeline."""

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from config import AppConfig, ScrapingConfig, SheetsConfig
from scrapers import Property
from scrapers.suumo import scrape_suumo
from scrapers.homes import scrape_homes
from scrapers.athome import scrape_athome
from scrapers.athome_member import scrape_athome_member
from scrapers.chintai import scrape_chintai
from scrapers.door import scrape_door
from scrapers.yahoo import scrape_yahoo
from scrapers.smocca import scrape_smocca
from geo import filter_by_distance
from ai.evaluator import evaluate_properties
from output.csv_export import write_to_csv
from output.html_report import generate_html_report

logger = logging.getLogger(__name__)


def _deduplicate(properties: list[Property]) -> list[Property]:
    """Remove duplicates across sites, keeping first occurrence.

    Two passes:
      1. URL — exact same listing link (same site, same room).
      2. Fingerprint (name + normalized address + total_rent + area + layout) —
         catches the same building re-listed on SUUMO/CHINTAI/HOME'S etc. so
         AI evaluation doesn't run 3x for the same apartment.
    """
    import re

    def _normalize_address(addr: str) -> str:
        # Strip common noise: 丁目/番/号 separators and whitespace.
        s = re.sub(r"[\s　]+", "", addr or "")
        s = re.sub(r"[－\-‐]", "-", s)
        return s

    seen_urls: set[str] = set()
    seen_fp: set[tuple] = set()
    unique: list[Property] = []
    url_dupes = 0
    fp_dupes = 0
    for prop in properties:
        if prop.url:
            if prop.url in seen_urls:
                url_dupes += 1
                continue
            seen_urls.add(prop.url)

        fp = (
            (prop.name or "").strip(),
            _normalize_address(prop.address),
            prop.total_rent,
            round(prop.area_sqm, 1),
            (prop.layout or "").strip(),
        )
        # Only fingerprint-dedup when the key is substantive enough to be
        # unique — avoid over-dedup when multiple fields are empty.
        if all(fp[:3]):
            if fp in seen_fp:
                fp_dupes += 1
                continue
            seen_fp.add(fp)

        unique.append(prop)

    if url_dupes or fp_dupes:
        logger.info(
            "Dedup: %d URL dupes + %d cross-site dupes removed (%d → %d)",
            url_dupes, fp_dupes, len(properties), len(unique),
        )
    return unique


async def _scrape_all(
    config: AppConfig,
    suumo_only: bool = False,
    skip_scrapers: set[str] | None = None,
) -> list[Property]:
    """Run scrapers and collect results."""
    if suumo_only:
        return await scrape_suumo(config)

    skip = {s.lower() for s in (skip_scrapers or set())}

    scrapers = [
        ("SUUMO", scrape_suumo),
        ("CHINTAI", scrape_chintai),
        ("DOOR", scrape_door),
        ("Yahoo", scrape_yahoo),
        ("Smocca", scrape_smocca),
        ("HOME'S", scrape_homes),
        ("athome", scrape_athome),
        ("athome会員", scrape_athome_member),
    ]

    all_properties: list[Property] = []
    for name, scraper_fn in scrapers:
        if name.lower() in skip:
            logger.info("Skipping %s (--skip-scrapers)", name)
            continue
        try:
            result = await scraper_fn(config)
            logger.info("%s: collected %d properties", name, len(result))
            all_properties.extend(result)
        except Exception as e:
            logger.error("%s scraper failed: %s", name, e)

    return _deduplicate(all_properties)


async def _reenrich_door_rents(properties: list[Property]) -> list[Property]:
    """Re-fetch DOOR detail pages to get accurate rent after distance filter.

    DOOR list pages show building-level rent ranges in em.emphasis-primary,
    which often maps the cheapest room's rent to wrong room URLs.
    After the distance filter only ~100 properties remain, so re-fetching
    is fast (~2s each with httpx).
    """
    import re
    import httpx

    door_indices = [i for i, p in enumerate(properties) if p.source == "door" and p.url]
    if not door_indices:
        return properties

    logger.info("DOOR rent re-enrichment: %d properties to verify", len(door_indices))
    corrected = 0
    result = list(properties)

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0"},
    ) as client:
        for idx in door_indices:
            prop = result[idx]
            try:
                resp = await client.get(prop.url)
                if resp.status_code != 200:
                    continue
                html = resp.text

                # Extract rent: look for 賃料 th/dt followed by td/dd with price
                rent_match = re.search(
                    r'(?:賃料|家賃).*?(?:</(?:th|dt)>).*?(?:<(?:td|dd)[^>]*>)\s*(.*?)\s*(?:</(?:td|dd)>)',
                    html, re.DOTALL | re.IGNORECASE,
                )
                new_rent = 0
                if rent_match:
                    rent_text = re.sub(r'<[^>]+>', '', rent_match.group(1))
                    m = re.search(r'([\d,]+)\s*円', rent_text)
                    if m:
                        new_rent = int(m.group(1).replace(',', ''))
                    else:
                        m = re.search(r'([\d.]+)\s*万', rent_text)
                        if m:
                            new_rent = int(float(m.group(1)) * 10000)

                # Extract management fee
                mgmt_match = re.search(
                    r'(?:管理費|共益費).*?(?:</(?:th|dt)>).*?(?:<(?:td|dd)[^>]*>)\s*(.*?)\s*(?:</(?:td|dd)>)',
                    html, re.DOTALL | re.IGNORECASE,
                )
                new_mgmt = 0
                if mgmt_match:
                    mgmt_text = re.sub(r'<[^>]+>', '', mgmt_match.group(1))
                    m = re.search(r'([\d,]+)\s*円', mgmt_text)
                    if m:
                        new_mgmt = int(m.group(1).replace(',', ''))

                # Sanity check: mgmt fee should be much smaller than rent
                if new_mgmt >= new_rent or new_mgmt > 30000:
                    new_mgmt = 0

                if new_rent > 0 and new_rent != prop.rent:
                    logger.info(
                        "DOOR rent corrected: %s (%d → %d)",
                        prop.name[:25], prop.rent, new_rent,
                    )
                    result[idx] = Property(
                        source=prop.source, url=prop.url, name=prop.name,
                        address=prop.address, rent=new_rent,
                        management_fee=new_mgmt if new_mgmt else prop.management_fee,
                        deposit=prop.deposit, key_money=prop.key_money,
                        layout=prop.layout, area_sqm=prop.area_sqm,
                        floor=prop.floor, building_type=prop.building_type,
                        year_built=prop.year_built, direction=prop.direction,
                        station_access=prop.station_access,
                        features=prop.features, image_url=prop.image_url,
                    )
                    corrected += 1
            except Exception:
                logger.debug("DOOR re-enrich failed for %s", prop.url)

    logger.info("DOOR rent re-enrichment: %d/%d corrected", corrected, len(door_indices))
    return result


async def _run_pipeline_async(
    config: AppConfig,
    suumo_only: bool = False,
    skip_ai: bool = False,
    use_sheets: bool = False,
    skip_scrapers: set[str] | None = None,
) -> None:
    """Async pipeline: scrape → filter → evaluate → output."""
    logger.info("=== Starting bukken-scraper pipeline ===")

    # 1. Scrape
    properties = await _scrape_all(
        config, suumo_only=suumo_only, skip_scrapers=skip_scrapers,
    )
    logger.info("Total unique properties: %d", len(properties))

    if not properties:
        logger.info("No properties found. Exiting pipeline.")
        return

    # 1.1. AI fallback for properties missing station/address
    from scrapers import needs_ai_fallback
    missing = [p for p in properties if needs_ai_fallback(p) and p.url]
    if missing and not skip_ai:
        logger.info(
            "AI fallback: %d/%d properties missing station/address",
            len(missing), len(properties),
        )
        try:
            from ai.extractor import (
                extract_property_fields_sync,
                reset_extraction_count,
            )
            import httpx

            reset_extraction_count()
            ai_count = 0
            fixed: list[Property] = []
            for prop in properties:
                if needs_ai_fallback(prop) and prop.url:
                    try:
                        resp = httpx.get(
                            prop.url,
                            timeout=15,
                            follow_redirects=True,
                            headers={
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 Chrome/131.0.0.0"
                                ),
                            },
                        )
                        if resp.status_code == 200:
                            extracted = extract_property_fields_sync(
                                resp.text, config.gemini_model
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
                    except Exception:
                        logger.debug("AI fallback failed for %s", prop.url)
                fixed.append(prop)
            properties = fixed
            logger.info("AI fallback enriched %d properties", ai_count)
        except ImportError:
            logger.warning("AI extractor or httpx not available, skipping fallback")

    # 1.5. Filter out female-only properties
    before = len(properties)
    properties = [p for p in properties if not p.is_female_only]
    if before != len(properties):
        logger.info("Filtered out %d female-only properties", before - len(properties))

    # 1.6. Filter by total rent (管理費込み13.5万以下が上限。rent==0は不明扱いで残す)
    _max_total = 135000
    _before = len(properties)
    properties = [p for p in properties if p.total_rent <= _max_total or p.total_rent == 0]
    _filtered = _before - len(properties)
    if _filtered:
        logger.info("Filtered out %d properties over %d yen total rent", _filtered, _max_total)
    # Log sweet spot count
    _sweet = len([p for p in properties if 0 < p.total_rent <= 125000])
    logger.info("Sweet spot (12.5万以下): %d / %d properties", _sweet, len(properties))

    # 1.7. Filter by building structure (木造を除外)
    _before = len(properties)

    def _is_allowed_structure(p):
        bt = p.building_type or ""
        if not bt:
            return True  # 構造不明は残す
        # 木造・軽量鉄骨・ALCを除外
        if "木造" in bt or "ウッド" in bt:
            return False
        if "軽量鉄骨" in bt:
            return False
        if "ALC" in bt.upper():
            return False
        return True

    properties = [p for p in properties if _is_allowed_structure(p)]
    _filtered = _before - len(properties)
    if _filtered:
        logger.info("Filtered out %d wooden structure properties", _filtered)

    # 1.7. Filter by building age
    if config.search.max_age_years > 0:
        import re as _re
        _current_year = __import__("datetime").datetime.now().year
        _before = len(properties)

        def _within_age(p):
            yb = p.year_built or ""
            if "新築" in yb:
                return True
            m = _re.search(r"(\d{4})", yb)
            if not m:
                return True  # 築年数不明は残す
            return _current_year - int(m.group(1)) <= config.search.max_age_years

        properties = [p for p in properties if _within_age(p)]
        _filtered = _before - len(properties)
        if _filtered:
            logger.info(
                "Filtered out %d properties older than %d years",
                _filtered, config.search.max_age_years,
            )

    # 2. Distance filter (3km from Shibuya DT Building)
    properties = await filter_by_distance(properties)
    logger.info("After distance filter: %d properties", len(properties))

    if not properties:
        logger.info("No properties within range. Exiting pipeline.")
        return

    # 2.5. Re-enrich DOOR properties from detail pages (list page rent is unreliable)
    properties = await _reenrich_door_rents(properties)

    # 3. AI evaluation (only new properties, reuse existing evaluations)
    from ai.evaluator import Evaluation
    from output.store import load_all as _load_existing

    existing_data = {p["url"]: p for p in _load_existing()}

    if skip_ai:
        evaluated = [
            (prop, Evaluation(
                property_url=prop.url,
                score=0,
                comment="AI評価スキップ",
                pros=(),
                cons=(),
                recommendation="未評価",
            ))
            for prop in properties
        ]
    else:
        # Split into already-evaluated and new
        new_props: list[Property] = []
        reused: list[tuple[Property, Evaluation]] = []
        for prop in properties:
            ex = existing_data.get(prop.url)
            if ex and ex.get("score", 0) > 0:
                reused.append((prop, Evaluation(
                    property_url=prop.url,
                    score=ex["score"],
                    comment=ex.get("comment", ""),
                    pros=tuple(ex.get("pros", ())),
                    cons=tuple(ex.get("cons", ())),
                    recommendation=ex.get("recommendation", ""),
                )))
            else:
                new_props.append(prop)

        logger.info(
            "AI evaluation: %d new, %d reused (already evaluated)",
            len(new_props), len(reused),
        )

        if new_props:
            new_evaluated = await evaluate_properties(new_props, config)
        else:
            new_evaluated = []

        evaluated = sorted(
            reused + new_evaluated,
            key=lambda x: x[1].score,
            reverse=True,
        )

    # 4. Output
    csv_path = write_to_csv(evaluated)
    logger.info("CSV saved: %s", csv_path)

    html_path = generate_html_report(evaluated)
    logger.info("HTML report: %s", html_path)

    from output.store import save_results
    save_results(evaluated)

    if use_sheets:
        try:
            from output.sheets import write_to_sheets
            written = write_to_sheets(evaluated, config.sheets)
            logger.info("Google Sheets: wrote %d new properties.", written)
        except Exception:
            logger.exception("Failed to write to Google Sheets (CSV still saved)")

    # 5. Summary
    logger.info("=== Pipeline complete ===")
    logger.info("Total properties: %d", len(evaluated))
    if evaluated and not skip_ai:
        top3 = evaluated[:3]
        logger.info("Top 3 picks:")
        for prop, ev in top3:
            logger.info(
                "  [%d点] %s - %s (%s/月) %s",
                ev.score,
                ev.recommendation,
                prop.name,
                f"{prop.total_rent:,}円",
                prop.url,
            )


async def _evaluate_only_async(config: AppConfig, use_sheets: bool = False) -> None:
    """Re-evaluate existing properties without scraping."""
    from output.store import load_all, save_results as _save
    from scrapers import Property

    logger.info("=== Evaluate-only mode ===")
    existing = load_all()
    # Fallback: try docs/data.json (committed by CI)
    if not existing:
        import json
        from pathlib import Path
        docs_data = Path("docs/data.json")
        if docs_data.exists():
            existing = json.loads(docs_data.read_text(encoding="utf-8"))
            logger.info("Loaded %d properties from docs/data.json", len(existing))
    if not existing:
        logger.info("No existing data. Run scraping first.")
        return

    # Convert dicts back to Property objects
    properties = []
    for p in existing:
        properties.append(Property(
            source=p.get("source", ""),
            url=p.get("url", ""),
            name=p.get("name", ""),
            address=p.get("address", ""),
            rent=p.get("rent", 0),
            management_fee=p.get("management_fee", 0),
            deposit=p.get("deposit", 0),
            key_money=p.get("key_money", 0),
            layout=p.get("layout", ""),
            area_sqm=p.get("area_sqm", 0),
            floor=p.get("floor", ""),
            building_type=p.get("building_type", ""),
            year_built=p.get("year_built", ""),
            direction=p.get("direction", ""),
            station_access=p.get("station_access", ""),
            features=tuple(p.get("features", ())),
            image_url=p.get("image_url", ""),
        ))

    logger.info("Loaded %d existing properties, running AI evaluation...", len(properties))
    evaluated = await evaluate_properties(properties, config)

    # Output
    csv_path = write_to_csv(evaluated)
    logger.info("CSV saved: %s", csv_path)
    html_path = generate_html_report(evaluated)
    logger.info("HTML report: %s", html_path)
    _save(evaluated)

    if use_sheets:
        try:
            from output.sheets import write_to_sheets
            write_to_sheets(evaluated, config.sheets)
        except Exception:
            logger.exception("Failed to write to Google Sheets")

    logger.info("=== Evaluate-only complete: %d properties ===", len(evaluated))


def run_pipeline(config: AppConfig, **kwargs) -> None:
    """Execute the full pipeline (sync wrapper)."""
    asyncio.run(_run_pipeline_async(config, **kwargs))


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="物件スクレイパー - 渋谷DTビル周辺の賃貸物件収集")
    parser.add_argument("--schedule", action="store_true", help="定期実行モード (24時間周期)")
    parser.add_argument("--suumo-only", action="store_true", help="SUUMOのみスクレイピング")
    parser.add_argument("--visible", action="store_true", help="ブラウザを表示 (CAPTCHA対応)")
    parser.add_argument("--skip-ai", action="store_true", help="AI評価をスキップ")
    parser.add_argument("--evaluate-only", action="store_true", help="スクレイピングせずAI評価のみ再実行")
    parser.add_argument("--sheets", action="store_true", help="Google Sheetsにも出力")
    parser.add_argument("--email", type=str, default="", help="Google Sheets共有先メールアドレス")
    parser.add_argument("--max-pages", type=int, default=20, help="サイトあたりの最大ページ数")
    parser.add_argument(
        "--skip-scrapers", type=str, default="",
        help="スキップするスクレイパー (カンマ区切り, 例: HOME'S,athome会員)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    headless = not args.visible
    config = AppConfig(
        scraping=ScrapingConfig(
            headless=headless,
            max_pages_per_site=args.max_pages,
        ),
        sheets=SheetsConfig(share_with_email=args.email),
    )

    skip_set = (
        {s.strip() for s in args.skip_scrapers.split(",") if s.strip()}
        if args.skip_scrapers
        else None
    )

    if args.schedule:
        from scheduler import start_scheduler
        start_scheduler(config)
    elif args.evaluate_only:
        asyncio.run(_evaluate_only_async(config, use_sheets=args.sheets))
    else:
        run_pipeline(
            config,
            suumo_only=args.suumo_only,
            skip_ai=args.skip_ai,
            use_sheets=args.sheets,
            skip_scrapers=skip_set,
        )


if __name__ == "__main__":
    main()
