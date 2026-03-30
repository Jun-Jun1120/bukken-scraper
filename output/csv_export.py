"""CSV export as a fallback/alternative to Google Sheets."""

import csv
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ai.evaluator import Evaluation
from scrapers import Property

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

HEADERS = [
    "取得日時",
    "サイト",
    "物件名",
    "住所",
    "家賃",
    "管理費",
    "合計",
    "間取り",
    "面積(㎡)",
    "階数",
    "構造",
    "築年",
    "向き",
    "最寄り駅",
    "設備",
    "AIスコア",
    "おすすめ度",
    "良い点",
    "悪い点",
    "AIコメント",
    "URL",
]


def write_to_csv(
    results: list[tuple[Property, Evaluation]],
    output_dir: str = "output",
) -> Path:
    """Write evaluated properties to a CSV file.

    Returns the path to the written file.
    """
    out_path = Path(output_dir)
    out_path.mkdir(exist_ok=True)

    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M")
    file_path = out_path / f"bukken_{timestamp}.csv"

    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)

        now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

        for prop, evaluation in results:
            writer.writerow([
                now_str,
                prop.source,
                prop.name,
                prop.address,
                f"{prop.rent:,}",
                f"{prop.management_fee:,}",
                f"{prop.total_rent:,}",
                prop.layout,
                str(prop.area_sqm),
                prop.floor,
                prop.building_type,
                prop.year_built,
                prop.direction,
                prop.station_access,
                " / ".join(prop.features),
                str(evaluation.score),
                evaluation.recommendation,
                " / ".join(evaluation.pros),
                " / ".join(evaluation.cons),
                evaluation.comment,
                prop.url,
            ])

    logger.info("Wrote %d properties to %s", len(results), file_path)
    return file_path
