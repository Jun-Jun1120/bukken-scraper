"""Google Sheets output writer using gspread."""

import logging
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

from ai.evaluator import Evaluation
from config import SheetsConfig
from scrapers import Property

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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


def _get_client(config: SheetsConfig) -> gspread.Client:
    """Create authenticated gspread client."""
    credentials = Credentials.from_service_account_file(
        config.credentials_path,
        scopes=SCOPES,
    )
    return gspread.authorize(credentials)


def _ensure_worksheet(
    client: gspread.Client, config: SheetsConfig
) -> gspread.Worksheet:
    """Get or create the target worksheet with headers."""
    spreadsheet = client.open_by_key(config.spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(config.worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=config.worksheet_name, rows=1000, cols=len(HEADERS)
        )
        logger.info("Created new worksheet: %s", config.worksheet_name)

    # Ensure headers exist
    existing = worksheet.row_values(1) if worksheet.row_count > 0 else []
    if existing != HEADERS:
        worksheet.update("A1", [HEADERS])
        logger.info("Updated headers in worksheet")

    return worksheet


def _property_to_row(
    prop: Property, evaluation: Evaluation, timestamp: str
) -> list[str]:
    """Convert property and evaluation to a spreadsheet row."""
    return [
        timestamp,
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
    ]


def write_to_sheets(
    results: list[tuple[Property, Evaluation]],
    config: SheetsConfig,
) -> int:
    """Write evaluated properties to Google Sheets.

    Returns the number of rows written.
    """
    if not results:
        logger.info("No results to write")
        return 0

    client = _get_client(config)
    worksheet = _ensure_worksheet(client, config)

    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    # Build all rows (immutable approach - create new list)
    rows = [
        _property_to_row(prop, evaluation, timestamp)
        for prop, evaluation in results
    ]

    # Get existing URLs to avoid duplicates
    existing_urls: set[str] = set()
    try:
        url_col = worksheet.col_values(HEADERS.index("URL") + 1)
        existing_urls = set(url_col[1:])  # skip header
    except Exception:
        logger.warning("Could not read existing URLs for dedup")

    # Filter out duplicates (immutable - new list)
    new_rows = [row for row in rows if row[-1] not in existing_urls]

    if not new_rows:
        logger.info("All properties already exist in sheet")
        return 0

    # Append new rows
    next_row = len(worksheet.col_values(1)) + 1
    worksheet.update(f"A{next_row}", new_rows)

    logger.info("Wrote %d new properties to Google Sheets", len(new_rows))
    return len(new_rows)
