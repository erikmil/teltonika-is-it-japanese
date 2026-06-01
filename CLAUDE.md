# teltonika-is-it-japanese · v2.5.2

Japanese language audit scanner for `teltonika-gps.com/ja/`.
Finds English text left untranslated on Japanese pages, and internal links that are missing the `/ja/` locale prefix.

Forked from `../teltonika-scraper`.

---

## Run

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python3.12 app.py
# Open http://localhost:8001
```

Click **Start Scan** → pages stream in live → click **Generate Report** when done.

---

## Architecture

```
app.py      FastAPI server at :8001 — crawls /ja/ and detects violations via SSE streaming
crawl.py    Page rendering utilities (Playwright) + link extraction (BS4)
detect.py   Detection engine: English text heuristic + URL violation checker
db.py       SQLite — pages + text_violations + url_violations tables
report.py   HTML + CSV report generator (called by /api/report)
data/       gitignored — pages.db + reports/
```

---

## Audience

This tool is for **copywriters**, not developers. Every violation reported must be text that a human can read on screen — if you can only find it by inspecting the HTML source, it must not appear in the report.

Consequences:
- HTML attributes (`aria-label`, `alt`, `placeholder`, `data-*`) are **never reported** — they live in the code, not on the page.
- Cookie consent overlays (`#CybotCookiebotDialog` etc.) are **skipped** — the copywriter cannot translate third-party consent widgets.
- Only rendered, visible text nodes and element inner text count.

---

## Detection logic

**Text violations** (`detect.py → detect_english()`):
- JS walks all visible DOM elements and extracts (tag, text) pairs — **no HTML attributes**
- Per element: count Latin chars vs Japanese chars (hiragana/katakana/kanji)
- Flag if: Latin ≥ 2 AND (no Japanese OR Latin/Japanese ratio ≥ 35%)
- Tag as `is_product_code=True` if text matches `[A-Z]{2,}[0-9]+` or is in the known acronym list (GPS, LTE, etc.)

**URL violations** (`detect.py → detect_url_violations()`):
- For every `<a href>` on a `/ja/` page: resolve to full URL
- If internal to `teltonika-gps.com` and path lacks `/ja/` → flag
- Suggests fix: prepend `/ja` to the existing path

**Smart filter**: UI toggle hides `is_product_code=True` rows client-side; no re-crawl needed.

---

## Gotchas

- Runs on port **8001** (not 8000) to coexist with the original scraper.
- Playwright context uses `locale="ja-JP"` so the server sees a Japanese browser.
- The JS extractor deduplicates by text string per page — same phrase appearing 10 times is reported once.
- Resume-safe: URLs at `status=200` are skipped on re-crawl; previous violations are shown instantly from DB.
- `data/` is gitignored — never commit `pages.db` or reports.

---

## Versioning rule

**Every code change must bump the version** — in two places:
1. `app.py` — the `<span>` in the `<h1>` header: `v2.3.0`
2. `CLAUDE.md` — the `# teltonika-is-it-japanese · vX.Y.Z` heading

Use semver: patch (+0.0.1) for bug fixes, minor (+0.1.0) for new features, major (+1.0.0) for breaking changes.
This rule applies to ALL future changes, no exceptions.
