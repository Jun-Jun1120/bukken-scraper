"""AI-powered property evaluation using Google Gemini API."""

import logging
from dataclasses import dataclass

from google import genai

from config import AppConfig, SearchCriteria
from scrapers import Property

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

    return f"""あなたは東京の賃貸物件の専門家です。以下の物件を評価してください。

## ユーザープロフィール
- 性別: 男性
- 勤務先: 渋谷DTビル（道玄坂1-16-10）
- 通勤手段: 電車だけでなくLUUP（電動自転車）やバスもOK
- 生活圏: 東新宿（副都心線/大江戸線）が拠点。東新宿からドアドア30分以内が理想
- 家賃補助条件: DTビルから3km圏内が必須（超えると補助が出ない）
- **重要: 女性限定・女性専用物件はスコア0にしてください（入居不可のため）**

## 希望条件
- エリア: 渋谷DTビルから3km圏内（絶対条件）
- 家賃: 手取り30万+補助2万。管理費込み11〜12万がベスト、12.5万以下がスイートスポット、13万超は少し減点、15万が上限
- 間取り: {', '.join(criteria.layouts)}（ワンルーム・1K中心）
- 構造: RC/SRCが最高評価（防音性能が高い）。鉄骨もOK。軽量鉄骨は減点。木造は除外済み
- **防音性能を重視**: RC > SRC > 重量鉄骨 > 鉄骨 > 軽量鉄骨。防音・遮音に関する設備があれば加点
- 築年数: {criteria.max_age_years}年以内。築浅志向だが、リノベーション済みの築古物件もOK
- **築古物件の評価ルール**: 築16年以上の場合、リノベ済み・設備交換済み（新しいキッチン/浴室/トイレ等）なら減点なし。設備が古いまま（3点ユニットバス、古い給湯器、旧式キッチン等）は-10〜-20点
- 駅徒歩: {criteria.max_walk_minutes}分以内
- 面積: 20〜25m2がベスト（広さ重視。面積広い順で検索するタイプ）
- 必須: BT別, 2口コンロ以上, 都市ガス, 室内洗濯機置場, 宅配ボックス
- 優先: 南向き, ウォシュレット, 室内物干し
- あると嬉しい: 24時間ゴミ出し

## ユーザーの好み（実際の検索傾向とお気に入り物件から厳密に分析済み）

【参考物件: アバンティア初台（最も気に入っている物件）】
初台駅歩5分 / 新築 / ワンルーム22.42m2 / 11.7万+管理費1万=12.7万
敷礼なし / 仲介手数料不要 / RC5階建 / 14戸の小規模マンション

【建物の好み】
- 3〜5階建ての低層RC/SRCマンション（小規模14戸前後が理想）
- モダンなデザイナーズ系外観（コンクリート打ちっぱなし、ダーク/グレー/ベージュ系）
- ブランド物件に好感（LEGALAND, ASTILE, ウェルスクエア, アバンティア等）

【内装の好み】
- 白基調でミニマルな内装（壁・床・建具すべて白〜ライトグレー）
- ライトオーク系フローリング
- ダクトレール照明（スポットライト式）を高評価
- 収納充実（ダブルクローゼット等）
- 清潔感・新しさ・シンプルさを最重視

【コスト面の好み】
- 敷礼なし or 少額を好む
- 仲介手数料不要は大きなプラス
- 管理費込み11〜12万がベスト。12.5万以下ならスイートスポット。13万超は少し減点。15万が絶対上限

## 駅のおすすめランク（DTビル通勤 × 東新宿ドアドア時間のバランス）
評価時、最寄り駅のランクを必ずスコアに反映してください。

【Sランク】+15点ボーナス（東新宿ドアドア15分以内）
  北参道(副都心線で東新宿2駅/DTビルLUUP8分/東新宿13分)

【Aランク】+10点ボーナス（東新宿ドアドア22分以内）
  明治神宮前(副都心線で東新宿3駅/16分),
  渋谷(副都心線で東新宿4駅/18分), 南新宿(小田急新宿→副都心線/18分),
  代々木(大江戸線で東新宿4駅/18分), 表参道(副都心線経由/20分),
  国立競技場(大江戸線で東新宿5駅/20分), 原宿(JR新宿→副都心線/22分),
  代々木公園(千代田線→副都心線/22分), 参宮橋(小田急新宿→副都心線/22分),
  初台(京王新線新宿→副都心線/22分), 千駄ヶ谷(JR新宿→副都心線/22分)

【Bランク】+5点ボーナス（東新宿ドアドア27分以内）
  信濃町(23分), 六本木(大江戸線で24分), 神泉(25分),
  代々木八幡(25分), 恵比寿(25分), 青山一丁目(大江戸線で25分),
  幡ヶ谷(25分), 代官山(27分), 代々木上原(27分)

【Cランク】±0点（東新宿ドアドア28〜30分、ギリギリ圏内）
  池尻大橋(28分), 東北沢(28分), 乃木坂(28分),
  駒場東大前(30分), 中目黒(30分), 池ノ上(30分),
  外苑前(30分), 下北沢(30分)

【圏外】スコア-10点（東新宿30分超で非推奨）
  祐天寺(32分), 広尾(33分)
{liked_context}

## 物件情報
- 物件名: {prop.name}
- 住所: {prop.address}
- 家賃: {prop.rent:,}円
- 管理費: {prop.management_fee:,}円
- 合計: {prop.total_rent:,}円/月
- 間取り: {prop.layout}
- 面積: {prop.area_sqm}㎡
- 階数: {prop.floor}
- 構造: {prop.building_type}
- 築年: {prop.year_built}
- 向き: {prop.direction}
- 最寄り駅: {prop.station_access}
- 設備・条件:
{features_str}

## 評価基準の重み
1. 最寄り駅ランク（上記S/A/B/Cを反映）: 25%
2. 家賃の妥当性（管理費込みで12.5万以下が理想）: 20%
3. 設備・条件の充実度（必須条件の充足率）: 20%
4. 面積・間取りの使いやすさ: 15%
5. 築年数・建物グレード: 10%
6. 周辺環境（買い物、飲食、治安）: 10%

## 回答形式（厳密に守ってください）
以下のフォーマットで回答してください。各項目は改行で区切ってください。

SCORE: [1-100の数値]
RECOMMENDATION: [強くおすすめ/おすすめ/普通/微妙/おすすめしない]
PROS:
- [良い点1]
- [良い点2]
- [良い点3]
CONS:
- [悪い点1]
- [悪い点2]
COMMENT: [総合コメント（2-3文）。必ず最寄り駅のランク（S/A/B/C）に言及すること]"""


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
