"""Flask web server for the bukken-scraper dashboard."""

import asyncio
import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, request, send_from_directory

from config import AppConfig, ScrapingConfig
from output.store import load_all, save_results

app = Flask(__name__, static_folder="static")
app.config["JSON_AS_ASCII"] = False

logger = logging.getLogger(__name__)

# Read API key at module level (guaranteed to have .env loaded)
_API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")


def _get_api_key() -> str:
    """Return Gemini API key, reading from .env file as fallback."""
    if _API_KEY:
        return _API_KEY
    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            for prefix in ("GOOGLE_API_KEY=", "GEMINI_API_KEY="):
                if line.startswith(prefix):
                    return line[len(prefix):].strip()
    return ""

# Scraping state
_scrape_lock = threading.Lock()
_scrape_status = {"running": False, "message": ""}


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/properties")
def api_properties():
    return jsonify(load_all())


@app.route("/api/properties/<path:prop_url>/like", methods=["POST"])
def api_like(prop_url):
    from output.store import toggle_like
    liked = toggle_like(prop_url)
    return jsonify({"url": prop_url, "liked": liked})


@app.route("/api/preferences")
def api_preferences():
    """Return learned preferences from liked properties."""
    from output.store import get_preferences
    return jsonify(get_preferences())


@app.route("/api/status")
def api_status():
    return jsonify(_scrape_status)


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if _scrape_status["running"]:
        return jsonify({"error": "Scraping already in progress"}), 409

    body = request.get_json(silent=True) or {}
    max_pages = body.get("max_pages", 5)
    skip_ai = body.get("skip_ai", False)

    thread = threading.Thread(
        target=_run_scrape,
        args=(max_pages, skip_ai),
        daemon=True,
    )
    thread.start()
    return jsonify({"message": "Scraping started"})


def _run_scrape(max_pages: int, skip_ai: bool):
    with _scrape_lock:
        _scrape_status["running"] = True
        _scrape_status["message"] = "スクレイピング中..."

        try:
            from scrapers.suumo import scrape_suumo
            from scrapers.chintai import scrape_chintai
            from scrapers.door import scrape_door
            from scrapers.yahoo import scrape_yahoo
            from scrapers.smocca import scrape_smocca
            from scrapers.homes import scrape_homes
            from scrapers.athome import scrape_athome
            from scrapers.athome_member import scrape_athome_member
            from geo import filter_by_distance
            from ai.evaluator import evaluate_properties, Evaluation

            config = AppConfig(
                scraping=ScrapingConfig(
                    headless=True,
                    max_pages_per_site=max_pages,
                ),
            )

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            scrapers = [
                ("SUUMO", scrape_suumo),
                ("CHINTAI", scrape_chintai),
                ("DOOR賃貸", scrape_door),
                ("Yahoo!不動産", scrape_yahoo),
                ("スモッカ", scrape_smocca),
                ("HOME'S", scrape_homes),
                ("athome", scrape_athome),
                ("athome会員", scrape_athome_member),
            ]

            all_props = []
            for site_name, scraper_fn in scrapers:
                _scrape_status["message"] = f"{site_name} スクレイピング中..."
                try:
                    result = loop.run_until_complete(scraper_fn(config))
                    all_props.extend(result)
                    logger.info("%s: %d properties", site_name, len(result))
                except Exception:
                    logger.warning("%s scraping failed", site_name)

            # Deduplicate
            seen = set()
            unique = []
            for p in all_props:
                if p.url and p.url not in seen:
                    seen.add(p.url)
                    unique.append(p)

            # Filter out female-only properties
            unique = [p for p in unique if not p.is_female_only]

            _scrape_status["message"] = f"距離フィルタ中... ({len(unique)}件)"
            unique = loop.run_until_complete(filter_by_distance(unique))

            if not unique:
                _scrape_status["message"] = "完了: 0件（範囲内の物件なし）"
                _scrape_status["running"] = False
                loop.close()
                return

            # AI evaluation
            if skip_ai:
                evaluated = [
                    (p, Evaluation(
                        property_url=p.url, score=0,
                        comment="AI評価スキップ", pros=(), cons=(),
                        recommendation="未評価",
                    ))
                    for p in unique
                ]
            else:
                _scrape_status["message"] = f"AI評価中... (0/{len(unique)})"
                results = []
                from ai.evaluator import _evaluate_property_sync
                from google import genai
                _key = _get_api_key()
                print(f"[DEBUG] API key in thread: {_key[:10]}... len={len(_key)}", flush=True)
                # Set env var explicitly for genai
                os.environ["GOOGLE_API_KEY"] = _key
                client = genai.Client(api_key=_key)
                for i, prop in enumerate(unique):
                    _scrape_status["message"] = f"AI評価中... ({i + 1}/{len(unique)})"
                    ev = _evaluate_property_sync(client, prop, config)
                    results.append((prop, ev))
                evaluated = sorted(results, key=lambda x: x[1].score, reverse=True)

            new_count = save_results(evaluated)
            total = len(load_all())
            _scrape_status["message"] = f"完了: {new_count}件追加（合計{total}件）"

            loop.close()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error("Scraping failed:\n%s", tb)
            _scrape_status["message"] = f"エラー: {e}"
            print(f"[SCRAPE ERROR]\n{tb}", flush=True)
        finally:
            _scrape_status["running"] = False


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Save existing CSV data to JSON store if store is empty
    if not load_all():
        logger.info("Migrating existing data to JSON store...")

    port = int(os.environ.get("PORT", 5000))
    key = _get_api_key()
    print(f"\n  Dashboard: http://localhost:{port}")
    print(f"  Gemini API key: {'OK (' + key[:8] + '...)' if key else 'NOT SET'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
