"""AI-powered property evaluation using Google Gemini API."""

import logging
from dataclasses import dataclass

from google import genai

from config import AppConfig, SearchCriteria
from scrapers import Property, normalized_features

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Evaluation:
    """Immutable AI evaluation result for a property."""

    property_url: str
    score: int  # 1-100
    comment: str
    pros: tuple[str, ...]
    cons: tuple[str, ...]
    recommendation: str  # "強くおすすめ", "おすすめ", "普通", "微妙", "おすすめしない"


def _build_liked_context() -> str:
    """Build rich context from liked/disliked properties for personalized evaluation."""
    try:
        from output.store import get_preferences
        prefs = get_preferences()
        if prefs["count"] == 0 and prefs.get("dislike_count", 0) == 0:
            return ""

        lines = ["\n## ユーザーの学習済み好み（いいね/興味なしデータから分析）"]
        lines.append(f"- いいね: {prefs['count']}件 / 興味なし: {prefs.get('dislike_count', 0)}件")

        patterns = prefs.get("patterns", {})

        # Liked patterns - detailed
        if patterns.get("rent_range"):
            avg = patterns.get("avg_rent", 0)
            lines.append(f"- 好みの家賃帯: {patterns['rent_range'][0]//10000}〜{patterns['rent_range'][1]//10000}万円 (平均{avg//10000}万円)")
        if patterns.get("area_range"):
            lines.append(f"- 好みの面積: {patterns['area_range'][0]}〜{patterns['area_range'][1]}㎡ (平均{patterns.get('avg_area', 0)}㎡)")
        if patterns.get("preferred_layouts"):
            layouts = ", ".join(f"{l}({c}件)" for l, c in patterns["preferred_layouts"])
            lines.append(f"- 好みの間取り: {layouts}")
        if patterns.get("preferred_stations"):
            stations = ", ".join(f"{s}({c}件)" for s, c in patterns["preferred_stations"])
            lines.append(f"- 好みのエリア: {stations}")
        if patterns.get("preferred_directions"):
            dirs = ", ".join(f"{d}({c}件)" for d, c in patterns["preferred_directions"])
            lines.append(f"- 好みの向き: {dirs}")
        if patterns.get("preferred_building_types"):
            types = ", ".join(f"{t}({c}件)" for t, c in patterns["preferred_building_types"])
            lines.append(f"- 好みの構造: {types}")
        if patterns.get("year_range"):
            lines.append(f"- 好みの築年: {patterns['year_range'][0]}〜{patterns['year_range'][1]}年築 (平均{patterns.get('avg_year', 0)}年)")
        if patterns.get("preferred_features"):
            feats = ", ".join(f"{f}({c})" for f, c in patterns["preferred_features"][:10])
            lines.append(f"- よく選ぶ設備: {feats}")

        # Liked property examples with full detail
        liked_props = prefs.get("liked_properties", [])
        if liked_props:
            lines.append("\n### いいねした物件の例（これらに似た物件を高評価すること）")
            for p in liked_props[:8]:
                parts = [f"{p['name']}"]
                if p.get("total_rent"):
                    parts.append(f"{p['total_rent']//10000}万円")
                if p.get("layout"):
                    parts.append(p["layout"])
                if p.get("area_sqm"):
                    parts.append(f"{p['area_sqm']}㎡")
                if p.get("year_built"):
                    parts.append(p["year_built"])
                if p.get("building_type"):
                    parts.append(p["building_type"])
                if p.get("direction"):
                    parts.append(f"{p['direction']}向き")
                detail = ", ".join(parts)
                pros = "、".join(p.get("pros", [])[:3]) or "不明"
                feats = "、".join(p.get("features", [])[:5])
                lines.append(f"- {detail}")
                lines.append(f"  良い点: {pros}")
                if feats:
                    lines.append(f"  設備: {feats}")

        # Disliked patterns - what to avoid
        dislike_patterns = prefs.get("dislike_patterns", {})
        disliked_props = prefs.get("disliked_properties", [])
        if disliked_props or dislike_patterns.get("avoided_features"):
            lines.append("\n### 興味なしと判断した物件の傾向（これらを避けること）")
            if dislike_patterns.get("avoided_features"):
                feats = ", ".join(f"{f}({c})" for f, c in dislike_patterns["avoided_features"][:8])
                lines.append(f"- 避ける設備/特徴: {feats}")
            if dislike_patterns.get("avoided_building_types"):
                types = ", ".join(f"{t}({c})" for t, c in dislike_patterns["avoided_building_types"])
                lines.append(f"- 避ける構造: {types}")
            if dislike_patterns.get("avoided_stations"):
                stations = ", ".join(f"{s}({c})" for s, c in dislike_patterns["avoided_stations"])
                lines.append(f"- 興味の薄いエリア: {stations}")
            for p in disliked_props[:3]:
                cons = "、".join(p.get("cons", [])[:2]) or "不明"
                lines.append(f"- NG例: {p['name']} ({p.get('total_rent', 0)//10000}万円) → {cons}")

        # Maybe (微妙) properties
        maybe_props = prefs.get("maybe_properties", [])
        if maybe_props:
            lines.append("\n### △ 微妙と判断した物件（惜しいが決め手に欠ける）")
            for p in maybe_props[:5]:
                pros = "、".join(p.get("pros", [])[:2]) or "不明"
                cons = "、".join(p.get("cons", [])[:2]) or "不明"
                lines.append(f"- {p['name']} ({p.get('total_rent', 0)//10000}万円) → 良い点: {pros} / 悪い点: {cons}")

        # User notes (direct feedback)
        user_notes = prefs.get("user_notes", [])
        if user_notes:
            lines.append("\n### ユーザーの直接コメント（最も重要なフィードバック）")
            for n in user_notes[:10]:
                lines.append(f"- {n['name']}: 「{n['note']}」")
            lines.append("**上記コメントの内容を最優先で考慮し、同様の問題がある物件は減点、コメントで評価された特徴がある物件は加点すること。**")

        lines.append("\n**重要: いいね物件の共通パターンに近い物件は+10〜20点。△微妙物件に似た物件は±0〜-5点。興味なし物件のパターンは-10〜20点。ユーザーコメントの指摘は最優先で反映。**")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_prompt(prop: Property, criteria: SearchCriteria) -> str:
    """Build evaluation prompt with learned preferences."""
    features_str = "\n".join(f"  - {f}" for f in prop.features) if prop.features else "  (情報なし)"
    liked_context = _build_liked_context()

    # Normalized capabilities — AI reads these instead of parsing features strings.
    caps = normalized_features(prop.features)
    def _mark(tag: str) -> str:
        return "○" if caps.get(tag, False) else "✕"
    caps_block = (
        f"  - 室内物干し: {_mark('indoor_drying')}\n"
        f"  - 暖房便座: {_mark('heated_seat')} (ウォシュレット/温水洗浄便座があればON)\n"
        f"  - ウォシュレット(洗浄): {_mark('washlet')}\n"
        f"  - 都市ガス: {_mark('city_gas')}\n"
        f"  - 2口コンロ以上: {_mark('two_burner')}\n"
        f"  - 宅配BOX: {_mark('delivery_box')}\n"
        f"  - 24時間ゴミ出し: {_mark('anytime_trash')}\n"
        f"  - オートロック: {_mark('auto_lock')}\n"
        f"  - エレベーター: {_mark('elevator')}\n"
        f"  - 礼金なし: {_mark('no_key_money')}\n"
        f"  - 仲介手数料不要: {_mark('delivery_free')}"
    )

    # Nearest target station (pre-computed by geo.py)
    if prop.nearest_station_name:
        station_block = (
            f"- **最寄りターゲット駅**: {prop.nearest_station_name} "
            f"(直線{prop.nearest_station_distance_km}km)"
        )
    else:
        station_block = "- 最寄りターゲット駅: (未計算 — `station_access` から判定してください)"

    return f"""あなたは東京の賃貸物件の評価者です。下の採点ルールに従って物件を1-100点で採点してください。

## ユーザー
- 男性。勤務先は渋谷DTビル(道玄坂1-16-10)。通勤は電車/LUUP/バス全部OK。
- パートナーが東新宿在住で副都心線/大江戸線ユーザー。東新宿ドアドア30分以内が理想。
- **女性限定/女性専用物件はスコア0**（入居不可）。

## 【絶対条件】1つでも外れたらスコア0
- DTビルから直線3km以内
- 管理費込み家賃 13.5万円以下
- BT別

## 【採点の軸】（合計100点に収まるよう配分）
1. 家賃妥当性 (25点) — 管理費込みで判定:
   - ≤11.0万: 満点 / 11.0-12.5万: -1〜0 / 12.5-13.5万: -3 / 13.5超: スコア0（絶対上限）
2. 最寄り駅ランク (20点) — 下の表参照
3. 構造・防音 (15点) — RC満点 / SRC -1 / 重量鉄骨 -5 / 鉄骨 -12 / 軽量鉄骨 -18
4. 築年数 (10点) — 築5年以内 満点 / ~10年 -1 / ~15年 -4 / ~20年 -8 / 20年超 -10（リノベ済は半分戻す）
5. 面積・間取り (10点) — 20-25㎡が満点、25㎡超は軽く加点、20㎡未満は面積に比例して減点
6. 向き・日当たり (8点) — 南満点 / 東南・南西 -1 / 東・西 -3 / 北 -6
7. 必須設備 (7点) — 室内物干し/暖房便座の2点が有るか
8. Nice-to-have (5点) — 24時間ゴミ出し/宅配BOX/都市ガス/2口コンロ/ウォシュレット(洗浄)の加点項目、各+1

## 駅ランク（今回の4ターゲット駅）
- **北参道** S (副都心線、東新宿直通13分) → 満点20
- **代々木** A (大江戸線/山手、渋谷2駅) → 18
- **国立競技場** A (大江戸線直通、東新宿20分) → 18
- **青山一丁目** B (大江戸線直通だが距離あり) → 13
- 上記以外が駅名に出たら (SUUMO 検索で拾った隣接駅等) → 原則10以下

## 好みの具体像（参考物件：アバンティア初台）
- ワンルーム22㎡/築浅/RC/管理費込12.7万/敷礼なし
- 白基調ミニマル内装、ライトオーク床、ダクトレール照明を好む
- モダンデザイナーズ系外観を好む (ブランド例: LEGALAND, ASTILE, ウェルスクエア, アバンティア)
- 階数のこだわりは弱い (高層でも EV あればOK。階数単独で減点しない)
- 敷礼なし・仲介手数料不要は加点
{liked_context}

## 物件情報
- 名前: {prop.name} / 住所: {prop.address}
- 家賃 {prop.rent:,}円 + 管理費 {prop.management_fee:,}円 = **合計 {prop.total_rent:,}円**
- 間取り: {prop.layout} / 面積: {prop.area_sqm}㎡ / 階: {prop.floor}
- 構造: {prop.building_type} / 築年: {prop.year_built} / 向き: {prop.direction}
{station_block}
- 元の駅情報(参考): {prop.station_access}

## 設備チェック（自動判定済み、○=あり ✕=なし）
{caps_block}

## 原文設備リスト（参考）
{features_str}

## 回答フォーマット（厳密に）
SCORE: [1-100]
RECOMMENDATION: [強くおすすめ/おすすめ/普通/微妙/おすすめしない]
PROS:
- [良い点1 — 採点軸と紐付ける。例: 「駅ランクS(+20)」「家賃12.3万でスイートスポット(+23)」]
- [良い点2]
- [良い点3]
CONS:
- [悪い点1 — 減点理由と点数を明示。例: 「重量鉄骨で防音-5」「築18年で-8」]
- [悪い点2]
COMMENT: [**2文以内**で総合評価。形式: 「{{駅ランク}}+{{家賃帯}}で{{推奨度}}。ただし{{主な減点要因}}。」]"""


def _parse_evaluation(response_text: str, property_url: str) -> Evaluation:
    """Parse AI response into an Evaluation object."""
    lines = response_text.strip().split("\n")

    score = 50
    recommendation = "普通"
    pros: list[str] = []
    cons: list[str] = []
    comment = ""

    section = ""
    for line in lines:
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(line.split(":")[1].strip())
                score = max(1, min(100, score))
            except ValueError:
                pass
        elif line.startswith("RECOMMENDATION:"):
            recommendation = line.split(":")[1].strip()
        elif line.startswith("PROS:"):
            section = "pros"
        elif line.startswith("CONS:"):
            section = "cons"
        elif line.startswith("COMMENT:"):
            comment = line.split(":", 1)[1].strip()
            section = "comment"
        elif line.startswith("- "):
            item = line[2:].strip()
            if section == "pros":
                pros.append(item)
            elif section == "cons":
                cons.append(item)
        elif section == "comment" and line:
            comment += " " + line

    return Evaluation(
        property_url=property_url,
        score=score,
        comment=comment.strip(),
        pros=tuple(pros),
        cons=tuple(cons),
        recommendation=recommendation,
    )


def _evaluate_property_sync(
    client: genai.Client,
    prop: Property,
    config: AppConfig,
) -> Evaluation:
    """Evaluate a single property using Gemini API."""
    prompt = _build_prompt(prop, config.search)

    try:
        response = client.models.generate_content(
            model=config.gemini_model,
            contents=prompt,
        )
        response_text = response.text
        return _parse_evaluation(response_text, prop.url)
    except Exception:
        logger.exception("Failed to evaluate property: %s", prop.url)
        return Evaluation(
            property_url=prop.url,
            score=0,
            comment="評価に失敗しました",
            pros=(),
            cons=(),
            recommendation="評価不可",
        )


async def evaluate_properties(
    properties: list[Property],
    config: AppConfig,
) -> list[tuple[Property, Evaluation]]:
    """Evaluate all properties and return sorted by score (descending)."""
    import asyncio
    import os
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    client = genai.Client(api_key=api_key)

    concurrency = 5
    semaphore = asyncio.Semaphore(concurrency)
    evaluated_count = 0

    async def _eval_one(prop: Property) -> tuple[Property, Evaluation]:
        nonlocal evaluated_count
        async with semaphore:
            loop = asyncio.get_event_loop()
            evaluation = await loop.run_in_executor(
                None, _evaluate_property_sync, client, prop, config,
            )
            evaluated_count += 1
            if evaluated_count % 20 == 0:
                logger.info(
                    "Evaluated %d/%d properties...",
                    evaluated_count, len(properties),
                )
            logger.info(
                "Evaluated %s: score=%d, rec=%s",
                prop.name,
                evaluation.score,
                evaluation.recommendation,
            )
            return (prop, evaluation)

    results = await asyncio.gather(*(_eval_one(p) for p in properties))

    # Sort by score descending (immutable - returns new list)
    return sorted(results, key=lambda x: x[1].score, reverse=True)
