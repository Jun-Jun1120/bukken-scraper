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
    """Remove duplicate properties by URL, keeping first occurrence."""
    seen: set[str] = set()
    unique: list[Property] = []
    for prop in properties:
        if prop.url and prop.url not in seen:
            seen.add(prop.url)
            unique.append(prop)
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

    # 1.5. Filter out female-only properties
    before = len(properties)
    properties = [p for p in properties if not p.is_female_only]
    if before != len(properties):
        logger.info("Filtered out %d female-only properties", before - len(properties))

    # 1.6. Filter by building age
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

    # 3. AI evaluation (or skip)
    if skip_ai:
        from ai.evaluator import Evaluation

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
        logger.info("Evaluating %d properties with Claude...", len(properties))
        evaluated = await evaluate_properties(properties, config)

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
