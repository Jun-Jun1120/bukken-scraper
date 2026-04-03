"""AI-powered fallback extractor for property data using Google Gemini."""

import logging
import os
import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Safety cap to prevent runaway API costs per scrape run
MAX_AI_EXTRACTIONS_PER_RUN = 100
_extraction_count = 0


class ExtractedPropertyFields(BaseModel):
    """Structured output schema for AI-based property field extraction."""

    name: str = Field(default="", description="物件名・建物名")
    address: str = Field(default="", description="住所・所在地（例: 東京都渋谷区...）")
    station_access: str = Field(
        default="",
        description="最寄り駅と徒歩分数（例: 京王線 初台駅 徒歩5分）。複数ある場合は ' / ' で区切る",
    )
    year_built: str = Field(default="", description="築年数・築年月（例: 2024年3月, 築12年）")
    building_type: str = Field(
        default="",
        description="建物構造（例: RC, SRC, 鉄骨, 鉄筋コンクリート）",
    )
    direction: str = Field(default="", description="向き（例: 南, 北東）")
    rent: int = Field(default=0, description="家賃（円単位。11.3万円なら113000）")
    management_fee: int = Field(default=0, description="管理費・共益費（円単位）")
    deposit: int = Field(default=0, description="敷金（円単位）")
    key_money: int = Field(default=0, description="礼金（円単位）")
    layout: str = Field(default="", description="間取り（例: 1K, ワンルーム, 1LDK）")
    area_sqm: float = Field(default=0.0, description="専有面積（㎡）")
    floor: str = Field(default="", description="所在階（例: 4階）")
    features: list[str] = Field(
        default_factory=list,
        description="設備・条件のリスト（例: ['バス・トイレ別', 'オートロック', '宅配ボックス']）",
    )


def _html_to_minimal_text(html: str, max_chars: int = 6000) -> str:
    """Strip HTML to essential text content for AI processing."""
    text = html
    # Remove script, style, nav, footer, header blocks
    for tag in ("script", "style", "nav", "footer", "header", "noscript", "svg"):
        text = re.sub(
            rf"<{tag}[^>]*>.*?</{tag}>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # Replace br tags with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()
    # Truncate
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


_EXTRACTION_PROMPT = """以下のテキストは日本の賃貸物件ページから抽出したものです。
物件情報を正確に読み取り、JSON形式で返してください。

ルール:
- テキストに含まれない情報はデフォルト値のままにしてください
- 家賃・管理費・敷金・礼金は円単位の整数で返してください（11.3万円→113000）
- 最寄り駅が複数ある場合は「路線名 駅名 徒歩X分 / 路線名 駅名 徒歩Y分」の形式で返してください
- 設備は個別の項目に分割してリストにしてください

テキスト:
{text}"""


def reset_extraction_count() -> None:
    """Reset the per-run extraction counter (call at start of each scrape run)."""
    global _extraction_count
    _extraction_count = 0


def extract_property_fields_sync(
    html: str,
    gemini_model: str = "gemini-3-flash-preview",
) -> ExtractedPropertyFields | None:
    """Extract property fields from HTML using Gemini (synchronous).

    Returns None if extraction fails or the per-run cap is exceeded.
    """
    global _extraction_count

    if _extraction_count >= MAX_AI_EXTRACTIONS_PER_RUN:
        logger.warning(
            "AI extraction cap reached (%d). Skipping.", MAX_AI_EXTRACTIONS_PER_RUN
        )
        return None

    minimal_text = _html_to_minimal_text(html)
    if len(minimal_text) < 50:
        logger.debug("HTML too short for AI extraction (%d chars)", len(minimal_text))
        return None

    prompt = _EXTRACTION_PROMPT.format(text=minimal_text)

    try:
        from google import genai
        from google.genai import types

        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get(
            "GEMINI_API_KEY", ""
        )
        client = genai.Client(api_key=api_key)

        response = client.models.generate_content(
            model=gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractedPropertyFields,
            ),
        )

        _extraction_count += 1

        if response.parsed:
            return response.parsed

        # Fallback: parse JSON manually if .parsed not available
        import json

        result = json.loads(response.text)
        return ExtractedPropertyFields(**result)
    except Exception:
        logger.exception("AI extraction failed")
        _extraction_count += 1
        return None


async def extract_property_fields(
    html: str,
    gemini_model: str = "gemini-3-flash-preview",
) -> ExtractedPropertyFields | None:
    """Extract property fields from HTML using Gemini (async wrapper)."""
    import asyncio

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, extract_property_fields_sync, html, gemini_model
    )
