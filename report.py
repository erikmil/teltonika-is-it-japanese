"""
Generate HTML + CSV report from the violations DB.

Outputs:
  data/reports/report.html
  data/reports/violations.csv

Usage:
    python report.py
"""

import csv
import os
import sys
from datetime import datetime, timezone

from jinja2 import Environment, BaseLoader

from db import init_db, get_all_text_violations, get_all_url_violations, get_stats

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "data", "reports")

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Japanese Audit Report — teltonika-gps.com/ja/</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     margin:0;padding:24px;background:#f5f5f5;color:#1a1a1a}
h1{font-size:1.35rem;margin-bottom:4px}
.meta{color:#666;font-size:.84rem;margin-bottom:24px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:24px}
.card{background:#fff;border-radius:8px;padding:15px 22px;
      box-shadow:0 1px 3px rgba(0,0,0,.08);min-width:120px}
.card .n{font-size:1.9rem;font-weight:700;line-height:1}
.card .l{font-size:.78rem;color:#666;margin-top:4px}
.card.red   .n{color:#d32f2f}
.card.yellow.n{color:#f57f17}
.card.purple.n{color:#6a1b9a}
h2{font-size:1.05rem;margin:28px 0 10px;padding-bottom:6px;border-bottom:2px solid #e0e0e0}
.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
.filters label{font-size:.83rem;font-weight:600}
.filters select,.filters input{padding:5px 9px;border:1px solid #ccc;
     border-radius:4px;font-size:.83rem}
.filters input[type=checkbox]{width:auto;cursor:pointer}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;
      overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:.855rem;margin-bottom:32px}
th{background:#263238;color:#fff;text-align:left;padding:9px 13px;white-space:nowrap}
td{padding:9px 13px;border-bottom:1px solid #eee;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafafa}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.72rem;font-weight:700}
.tag-text{background:#ffebee;color:#c62828}
.tag-url{background:#fff3e0;color:#bf360c}
.tag-code{background:#ede7f6;color:#4527a0}
.url-cell a{color:#1565c0;text-decoration:none;word-break:break-all;font-size:.78rem}
.url-cell a:hover{text-decoration:underline}
.fix{font-size:.72rem;color:#2e7d32;word-break:break-all}
mark{background:#fff176;padding:1px 2px;border-radius:2px}
.hidden{display:none!important}
footer{margin-top:24px;font-size:.78rem;color:#999;text-align:center}
</style>
</head>
<body>
<h1>Japanese Audit Report</h1>
<div class="meta">
  Site: <a href="https://www.teltonika-gps.com/ja/" target="_blank">teltonika-gps.com/ja/</a>
  &nbsp;·&nbsp; Generated: {{ scan_date }}
</div>

<div class="cards">
  <div class="card"><div class="n">{{ stats.total_pages }}</div><div class="l">Pages crawled</div></div>
  <div class="card"><div class="n">{{ stats.pages_flagged }}</div><div class="l">Pages flagged</div></div>
  <div class="card red"><div class="n">{{ stats.total_text }}</div><div class="l">Text violations</div></div>
  <div class="card yellow"><div class="n">{{ stats.total_url }}</div><div class="l">URL violations</div></div>
  <div class="card purple"><div class="n">{{ stats.product_code_count }}</div><div class="l">Product codes</div></div>
</div>

<h2>Text violations — English found on Japanese pages</h2>
<div class="filters">
  <label>Filter:</label>
  <select id="elem-filter" onchange="applyText()">
    <option value="">All elements</option>
    <option value="button">button</option>
    <option value="a">a (link)</option>
    <option value="h1">h1</option>
    <option value="h2">h2</option>
    <option value="h3">h3</option>
    <option value="p">p</option>
    <option value="nav">nav</option>
    <option value="li">li</option>
    <option value="div">div</option>
    <option value="span">span</option>
  </select>
  <input id="text-search" type="text" placeholder="Search URL or text…"
         oninput="applyText()" style="width:240px">
  <label style="display:flex;align-items:center;gap:5px;cursor:pointer">
    <input type="checkbox" id="hide-codes" onchange="applyText()"> Hide product codes
  </label>
</div>
<table id="text-table">
  <thead><tr><th>#</th><th>Page</th><th>Element</th><th>English text found</th><th>Type</th></tr></thead>
  <tbody>
  {% for v in text_violations %}
  <tr data-elem="{{ v.element_type }}" data-code="{{ '1' if v.is_product_code else '0' }}"
      data-url="{{ v.url }}" data-text="{{ v.text | lower }}">
    <td>{{ loop.index }}</td>
    <td class="url-cell">
      <a href="{{ v.url }}" target="_blank">{{ v.page_title or v.url }}</a><br>
      <small style="color:#999">{{ v.url }}</small>
    </td>
    <td><span class="tag tag-text">&lt;{{ v.element_type }}&gt;</span></td>
    <td><mark>{{ v.text }}</mark></td>
    <td>{% if v.is_product_code %}<span class="tag tag-code">product code</span>{% endif %}</td>
  </tr>
  {% else %}
  <tr><td colspan="5" style="text-align:center;padding:28px;color:#888">No text violations found.</td></tr>
  {% endfor %}
  </tbody>
</table>

<h2>URL violations — internal links missing <code>/ja/</code></h2>
<div class="filters">
  <input id="url-search" type="text" placeholder="Search page or URL…"
         oninput="applyUrl()" style="width:300px">
</div>
<table id="url-table">
  <thead><tr><th>#</th><th>Page</th><th>Link text</th><th>Broken URL</th><th>Suggested fix</th></tr></thead>
  <tbody>
  {% for v in url_violations %}
  <tr data-url="{{ v.url }}" data-href="{{ v.href }}">
    <td>{{ loop.index }}</td>
    <td class="url-cell">
      <a href="{{ v.url }}" target="_blank">{{ v.page_title or v.url }}</a><br>
      <small style="color:#999">{{ v.url }}</small>
    </td>
    <td>{{ v.link_text or '—' }}</td>
    <td class="url-cell"><a href="{{ v.href }}" target="_blank">{{ v.href }}</a></td>
    <td class="fix">{{ v.suggested_fix }}</td>
  </tr>
  {% else %}
  <tr><td colspan="5" style="text-align:center;padding:28px;color:#888">No URL violations found.</td></tr>
  {% endfor %}
  </tbody>
</table>

<footer>Japanese Audit Scanner &nbsp;·&nbsp; {{ scan_date }}</footer>

<script>
function applyText() {
  const elem   = document.getElementById('elem-filter').value;
  const search = document.getElementById('text-search').value.toLowerCase();
  const hide   = document.getElementById('hide-codes').checked;
  document.querySelectorAll('#text-table tbody tr[data-elem]').forEach(row => {
    const matchElem   = !elem   || row.dataset.elem === elem;
    const matchSearch = !search || row.textContent.toLowerCase().includes(search);
    const matchCode   = !hide   || row.dataset.code !== '1';
    row.classList.toggle('hidden', !(matchElem && matchSearch && matchCode));
  });
}
function applyUrl() {
  const search = document.getElementById('url-search').value.toLowerCase();
  document.querySelectorAll('#url-table tbody tr[data-url]').forEach(row => {
    const match = !search || row.textContent.toLowerCase().includes(search);
    row.classList.toggle('hidden', !match);
  });
}
</script>
</body>
</html>"""


def build_report() -> None:
    init_db()
    os.makedirs(REPORTS_DIR, exist_ok=True)

    text_violations = get_all_text_violations()
    url_violations  = get_all_url_violations()
    stats           = get_stats()

    if not text_violations and not url_violations:
        print("No violations found — run a scan first.", file=sys.stderr)
        sys.exit(1)

    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    env      = Environment(loader=BaseLoader(), autoescape=False)
    template = env.from_string(TEMPLATE)
    html     = template.render(
        scan_date=scan_date,
        stats=stats,
        text_violations=text_violations,
        url_violations=url_violations,
    )

    html_path = os.path.join(REPORTS_DIR, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    csv_path = os.path.join(REPORTS_DIR, "violations.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["violation_type", "page_url", "page_title",
                         "detail_1", "detail_2", "detail_3", "is_product_code", "scan_date"])
        for v in text_violations:
            writer.writerow(["text", v["url"], v["page_title"] or "",
                             v["element_type"], v["text"], "",
                             bool(v["is_product_code"]), scan_date])
        for v in url_violations:
            writer.writerow(["url", v["url"], v["page_title"] or "",
                             v["link_text"] or "", v["href"], v["suggested_fix"] or "",
                             "", scan_date])

    print(f"Report written:")
    print(f"  HTML → {html_path}")
    print(f"  CSV  → {csv_path}")
    print(f"\n  {stats['total_pages']} pages crawled")
    print(f"  {len(text_violations)} text violations | {len(url_violations)} URL violations")


if __name__ == "__main__":
    build_report()
