"""Generate a static HTML report from evaluated properties."""

import html
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ai.evaluator import Evaluation
from scrapers import Property

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


def _escape(text: str) -> str:
    return html.escape(text)


def _rec_badge(rec: str) -> str:
    colors = {
        "強くおすすめ": ("#dcfce7", "#166534", "&#9733;&#9733;&#9733;"),
        "おすすめ": ("#dcfce7", "#166534", "&#9733;&#9733;"),
        "普通": ("#fef9c3", "#854d0e", "&#9733;"),
        "微妙": ("#fed7aa", "#9a3412", "&#9888;"),
        "おすすめしない": ("#fecaca", "#991b1b", "&#10007;"),
    }
    bg, color, icon = colors.get(rec, ("#e5e7eb", "#374151", "?"))
    return f'<span class="badge" style="background:{bg};color:{color}">{icon} {_escape(rec)}</span>'


def _score_color(score: int) -> str:
    if score >= 70:
        return "#22c55e"
    if score >= 50:
        return "#eab308"
    if score >= 30:
        return "#f97316"
    return "#ef4444"


def _score_bg(score: int) -> str:
    if score >= 70:
        return "#f0fdf4"
    if score >= 50:
        return "#fefce8"
    if score >= 30:
        return "#fff7ed"
    return "#fef2f2"


def _build_card(prop: Property, ev: Evaluation) -> str:
    pros_html = "".join(
        f'<li>&#10003; {_escape(p)}</li>' for p in ev.pros
    ) or '<li class="empty">情報なし</li>'

    cons_html = "".join(
        f'<li>&#10007; {_escape(c)}</li>' for c in ev.cons
    ) or '<li class="empty">情報なし</li>'

    features_html = ""
    if prop.features:
        tags = "".join(
            f'<span class="tag">{_escape(f)}</span>' for f in prop.features[:8]
        )
        features_html = f'<div class="features">{tags}</div>'

    return f"""<div class="card" data-score="{ev.score}" data-rent="{prop.total_rent}" data-area="{prop.area_sqm}" style="border-left-color:{_score_color(ev.score)}">
  <div class="card-header">
    <div class="card-title">
      <h3>{_escape(prop.name)}</h3>
      <p class="address">{_escape(prop.address)}</p>
    </div>
    <div class="card-score">
      <div class="score" style="color:{_score_color(ev.score)}">{ev.score}<span class="score-max">/100</span></div>
      {_rec_badge(ev.recommendation)}
    </div>
  </div>
  <div class="info-grid">
    <div class="info"><span class="info-label">家賃</span><span class="info-value">{prop.rent:,}円</span></div>
    <div class="info"><span class="info-label">管理費</span><span class="info-value">{prop.management_fee:,}円</span></div>
    <div class="info"><span class="info-label">合計</span><span class="info-value rent-total">{prop.total_rent:,}円</span></div>
    <div class="info"><span class="info-label">間取り</span><span class="info-value">{_escape(prop.layout) or '-'}</span></div>
    <div class="info"><span class="info-label">面積</span><span class="info-value">{prop.area_sqm}㎡</span></div>
    <div class="info"><span class="info-label">階数</span><span class="info-value">{_escape(prop.floor) or '-'}</span></div>
    <div class="info"><span class="info-label">築年</span><span class="info-value">{_escape(prop.year_built) or '-'}</span></div>
    <div class="info"><span class="info-label">向き</span><span class="info-value">{_escape(prop.direction) or '-'}</span></div>
  </div>
  <div class="station">
    <strong>最寄り駅:</strong> {_escape(prop.station_access)}
  </div>
  {features_html}
  <div class="pros-cons">
    <div class="pros"><div class="section-label">良い点</div><ul>{pros_html}</ul></div>
    <div class="cons"><div class="section-label">悪い点</div><ul>{cons_html}</ul></div>
  </div>
  <div class="comment" style="background:{_score_bg(ev.score)}">{_escape(ev.comment)}</div>
  <div class="card-footer">
    <a href="{_escape(prop.url)}" target="_blank">物件詳細を見る &rarr;</a>
  </div>
</div>"""


def generate_html_report(
    results: list[tuple[Property, Evaluation]],
    output_dir: str = "output",
) -> Path:
    out_path = Path(output_dir)
    out_path.mkdir(exist_ok=True)

    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    file_path = out_path / "report.html"

    cards_html = "\n".join(_build_card(p, e) for p, e in results)

    total = len(results)
    best_score = max((e.score for _, e in results), default=0)
    cheapest = min((p.total_rent for p, _ in results), default=0)
    avg_score = sum(e.score for _, e in results) // max(total, 1)

    report_html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>物件レポート - {timestamp}</title>
<style>
*{{box-sizing:border-box}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Hiragino Sans","Noto Sans JP",sans-serif;
  background:#f1f5f9;margin:0;padding:20px;color:#1e293b;
}}
.container{{max-width:820px;margin:0 auto}}
h1{{font-size:24px;margin:0}}
.subtitle{{color:#6b7280;font-size:14px;margin-top:4px}}
.stats{{display:flex;gap:12px;margin:20px 0;flex-wrap:wrap}}
.stat{{background:white;padding:14px 18px;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.1);flex:1;min-width:110px;text-align:center}}
.stat-value{{font-size:26px;font-weight:700}}
.stat-label{{font-size:11px;color:#9ca3af;margin-top:2px}}

.sort-bar{{
  display:flex;gap:8px;margin:16px 0;flex-wrap:wrap;align-items:center;
}}
.sort-bar span{{font-size:13px;color:#6b7280;font-weight:600}}
.sort-btn{{
  padding:6px 14px;border-radius:8px;border:1.5px solid #cbd5e1;
  background:white;color:#475569;font-size:13px;font-weight:500;
  cursor:pointer;transition:all .15s;
}}
.sort-btn:hover{{border-color:#3b82f6;color:#3b82f6}}
.sort-btn.active{{background:#3b82f6;color:white;border-color:#3b82f6}}

.cards{{display:flex;flex-direction:column;gap:16px}}
.card{{
  background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.1);
  padding:24px;border-left:4px solid #ccc;transition:transform .1s;
}}
.card:hover{{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.1)}}
.card-header{{display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap;gap:8px}}
.card-title{{flex:1;min-width:200px}}
.card-title h3{{margin:0 0 4px;font-size:18px}}
.address{{margin:0;color:#6b7280;font-size:14px}}
.card-score{{text-align:right}}
.score{{font-size:32px;font-weight:700}}
.score-max{{font-size:14px;color:#9ca3af}}
.badge{{padding:4px 10px;border-radius:12px;font-size:13px;font-weight:600;display:inline-block;margin-top:4px}}
.info-grid{{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;
  margin-top:16px;padding:14px;background:#f8fafc;border-radius:8px;
}}
.info-label{{font-size:11px;color:#9ca3af;text-transform:uppercase;display:block}}
.info-value{{font-size:17px;font-weight:600;color:#1e293b;display:block}}
.rent-total{{color:#dc2626;font-weight:700}}
.station{{margin-top:12px;font-size:14px;color:#4b5563}}
.features{{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}}
.tag{{background:#f1f5f9;padding:2px 8px;border-radius:8px;font-size:12px}}
.pros-cons{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}}
.section-label{{font-weight:600;margin-bottom:4px;font-size:14px}}
.pros .section-label{{color:#166534}}
.cons .section-label{{color:#991b1b}}
.pros ul,.cons ul{{margin:0;padding:0;list-style:none;font-size:13px}}
.pros li{{margin:2px 0;color:#166534}}
.cons li{{margin:2px 0;color:#991b1b}}
li.empty{{color:#9ca3af}}
.comment{{margin-top:12px;padding:12px;border-radius:8px;font-size:14px;color:#374151;line-height:1.6}}
.card-footer{{margin-top:12px;text-align:right}}
.card-footer a{{color:#3b82f6;text-decoration:none;font-size:14px;font-weight:500}}
.card-footer a:hover{{text-decoration:underline}}
</style>
</head>
<body>
<div class="container">
  <h1>物件レポート</h1>
  <p class="subtitle">渋谷DTビル 3km圏内 ｜ {timestamp} 更新 ｜ {total}件</p>

  <div class="stats">
    <div class="stat"><div class="stat-value">{total}</div><div class="stat-label">物件数</div></div>
    <div class="stat"><div class="stat-value" style="color:#22c55e">{best_score}</div><div class="stat-label">最高スコア</div></div>
    <div class="stat"><div class="stat-value">{cheapest:,}</div><div class="stat-label">最安合計(円)</div></div>
    <div class="stat"><div class="stat-value">{avg_score}</div><div class="stat-label">平均スコア</div></div>
  </div>

  <div class="sort-bar">
    <span>並び替え:</span>
    <button class="sort-btn active" data-sort="score" data-dir="desc">AIスコア高い順</button>
    <button class="sort-btn" data-sort="score" data-dir="asc">AIスコア低い順</button>
    <button class="sort-btn" data-sort="rent" data-dir="asc">家賃安い順</button>
    <button class="sort-btn" data-sort="rent" data-dir="desc">家賃高い順</button>
    <button class="sort-btn" data-sort="area" data-dir="desc">面積広い順</button>
    <button class="sort-btn" data-sort="area" data-dir="asc">面積狭い順</button>
  </div>

  <div class="cards" id="cards">
{cards_html}
  </div>
</div>
<script>
document.querySelectorAll('.sort-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const key = btn.dataset.sort;
    const dir = btn.dataset.dir === 'asc' ? 1 : -1;
    const container = document.getElementById('cards');
    const cards = [...container.querySelectorAll('.card')];
    cards.sort((a, b) => (parseFloat(a.dataset[key]) - parseFloat(b.dataset[key])) * dir);
    cards.forEach(c => container.appendChild(c));
  }});
}});
</script>
</body>
</html>"""

    file_path.write_text(report_html, encoding="utf-8")
    logger.info("HTML report saved: %s", file_path)
    return file_path
