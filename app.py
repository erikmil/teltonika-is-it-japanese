"""
Japanese audit scanner — real-time UI.
Crawls https://www.teltonika-gps.com/ja/ and finds:
  • English text on Japanese pages (text violations)
  • Internal links missing the /ja/ prefix (URL violations)

Usage:
    .venv/bin/python3.12 app.py  →  http://localhost:8001
"""

import asyncio
import json
import subprocess
import sys
from collections import deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse

from crawl import (
    BASE_URL, ALLOWED_HOST,
    normalise, is_internal, is_ja_url, should_skip,
    render_page, extract_title, extract_ja_links, fetch_all_sitemap_urls,
)
from db import (
    init_db, upsert_page, seed_url, clear_page_violations, clear_all,
    save_text_violation, save_url_violation,
    get_crawled_urls, get_pending_urls, get_failed_urls, get_all_pages_with_status,
    get_violations_by_page,
)
from detect import extract_elements, detect_english, detect_url_violations

# ── Constants ─────────────────────────────────────────────────────────────────

CONCURRENCY = 1
CRAWL_DELAY = 0.5

# Hardcoded seeds — BFS discovers everything else from here.
# /ja/products/ returns 404; product sub-pages are reached via BFS from root.
JA_SEEDS = [
    "https://www.teltonika-gps.com/ja/",
    "https://www.teltonika-gps.com/ja/solutions/",
    "https://www.teltonika-gps.com/ja/support/",
    "https://www.teltonika-gps.com/ja/about/",
    "https://www.teltonika-gps.com/ja/products/sensors-beacons",
    "https://www.teltonika-gps.com/ja/support/product-support",
]

# ── Globals ───────────────────────────────────────────────────────────────────

app = FastAPI()

_state: dict = {"running": False, "stop": False, "browser_ctx": None}
_history: list[dict] = []
_clients: list[asyncio.Queue] = []
_task: asyncio.Task | None = None


# ── SSE helpers ───────────────────────────────────────────────────────────────

async def _broadcast(event: dict) -> None:
    _history.append(event)
    for q in list(_clients):
        await q.put(event)


# ── Core scan task ────────────────────────────────────────────────────────────

async def _run_scan() -> None:
    _state["running"] = True
    _state["stop"]    = False
    _history.clear()

    try:
        init_db()
        await _broadcast({"type": "started"})
        await _broadcast({"type": "log", "msg": "Initialising…"})

        stats = {"pages": 0, "text_total": 0, "url_total": 0, "pages_flagged": 0}

        # ── Reload cached pages (no network) ─────────────────────────────────
        already_crawled = get_crawled_urls()
        viol_map = get_violations_by_page()

        cached_pages = get_all_pages_with_status()
        cached_200   = [p for p in cached_pages if p["status"] == 200]

        if cached_200:
            await _broadcast({"type": "log",
                               "msg": f"Restoring {len(cached_200)} cached pages…"})

        for p in cached_200:
            if _state["stop"]:
                break
            pv = viol_map.get(p["url"], {"text_violations": [], "url_violations": []})
            tv = pv["text_violations"]
            uv = pv["url_violations"]
            stats["pages"] += 1
            if tv or uv:
                stats["pages_flagged"] += 1
                stats["text_total"] += len(tv)
                stats["url_total"]  += len(uv)
            await _broadcast({
                "type":             "page_analyzed",
                "url":              p["url"],
                "title":            p["title"] or p["url"],
                "text_violations":  tv,
                "url_violations":   uv,
                "stats":            dict(stats),
            })

        # ── Seed queue ────────────────────────────────────────────────────────
        await _broadcast({"type": "log", "msg": "Checking sitemaps for /ja/ URLs…"})
        ja_from_sitemap = await fetch_all_sitemap_urls()
        await _broadcast({"type": "log",
                           "msg": f"  → {len(ja_from_sitemap)} Japanese URLs found in sitemaps"})

        queue_set: set[str] = set()
        queue: deque[str] = deque()

        # Hardcoded seeds — cover main /ja/ sections so BFS can discover the rest
        for raw in JA_SEEDS:
            u = normalise(raw, raw)
            if u not in already_crawled and u not in queue_set:
                queue.append(u); queue_set.add(u)

        # Re-queue anything previously discovered but never crawled
        for p in get_pending_urls():
            u = p["url"]
            if u not in already_crawled and u not in queue_set:
                queue.append(u); queue_set.add(u)

        # Re-queue pages that failed on last run (status=0)
        for p in get_failed_urls():
            u = p["url"]
            if u not in already_crawled and u not in queue_set:
                queue.append(u); queue_set.add(u)

        # Any /ja/ URLs the sitemap happened to list
        for u in ja_from_sitemap:
            if u not in already_crawled and u not in queue_set:
                queue.append(u); queue_set.add(u)

        seen: set[str] = queue_set | already_crawled
        stats["pages"] = len(cached_200)  # already counted above

        total_est = len(cached_200) + len(queue)
        await _broadcast({"type": "log",
                           "msg": f"Queue: {len(queue)} new pages to crawl "
                                  f"(+{len(cached_200)} cached)"})
        await _broadcast({"type": "phase", "queued": len(queue),
                           "cached": len(cached_200), "total": total_est})

        if not queue or _state["stop"]:
            await _broadcast({"type": "done",
                               "total_pages": stats["pages"],
                               "total_text":  stats["text_total"],
                               "total_url":   stats["url_total"]})
            return

        # ── Crawl new pages ───────────────────────────────────────────────────
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
            _state["browser_ctx"] = ctx
            pg_pool = [await ctx.new_page() for _ in range(CONCURRENCY)]

            async def crawl_one(idx: int, url: str) -> tuple:
                pg = pg_pool[idx]
                try:
                    html, status = await render_page(pg, url)
                    if not html:
                        return url, "", [], [], [], 0, f"Empty response (HTTP {status})"
                    title    = extract_title(html)
                    elements = await extract_elements(pg)
                    text_v   = detect_english(elements)
                    url_v    = detect_url_violations(html, url)
                    links    = await extract_ja_links(pg)
                    return url, title, text_v, url_v, links, status, None
                except Exception as exc:
                    return url, "", [], [], [], 0, str(exc)[:140]

            failed_count = 0

            while queue and not _state["stop"]:
                batch: list[str] = []
                while queue and len(batch) < CONCURRENCY:
                    batch.append(queue.popleft())

                results = await asyncio.gather(*[
                    crawl_one(i, url) for i, url in enumerate(batch)
                ])

                for res in results:
                    url, title, text_v, url_v, links, status, err = res

                    if err or not status:
                        failed_count += 1
                        upsert_page(url, "", 0)
                        msg = err or "No response / redirect"
                        await _broadcast({"type": "page_failed",
                                          "url": url, "error": msg})
                        await _broadcast({"type": "log",
                                          "msg": f"FAILED {url[:72]} — {msg}"})
                        continue

                    page_id = upsert_page(url, title, status)
                    clear_page_violations(page_id)

                    for v in text_v:
                        save_text_violation(
                            page_id, url, title,
                            v["element_type"], v["text"], v["text"],
                            v["is_product_code"],
                        )
                    for v in url_v:
                        save_url_violation(
                            page_id, url, title,
                            v["link_text"], v["href"], v["suggested_fix"],
                        )

                    stats["pages"] += 1
                    if text_v or url_v:
                        stats["pages_flagged"] += 1
                    stats["text_total"] += len(text_v)
                    stats["url_total"]  += len(url_v)

                    verdict = (
                        f"⚠ {len(text_v)} text + {len(url_v)} url"
                        if (text_v or url_v) else "✓ clean"
                    )
                    await _broadcast({"type": "log",
                                      "msg": f"{verdict} — {title or url[:60]}"})
                    await _broadcast({
                        "type":            "page_analyzed",
                        "url":             url,
                        "title":           title or url,
                        "text_violations": [
                            {"element_type": v["element_type"],
                             "text":         v["text"],
                             "is_product_code": v["is_product_code"]}
                            for v in text_v
                        ],
                        "url_violations":  [
                            {"link_text":     v["link_text"],
                             "href":          v["href"],
                             "suggested_fix": v["suggested_fix"]}
                            for v in url_v
                        ],
                        "stats": dict(stats),
                    })

                    # Discover new /ja/ links
                    new_found = 0
                    for link in links:
                        if link not in seen:
                            seen.add(link)
                            queue.append(link)
                            seed_url(link)
                            new_found += 1
                    if new_found:
                        await _broadcast({"type": "log",
                                          "msg": f"  ↳ +{new_found} new /ja/ URLs"})

                await asyncio.sleep(CRAWL_DELAY)

            await browser.close()
            _state["browser_ctx"] = None

        await _broadcast({"type": "log",
                           "msg": f"Done — {stats['pages_flagged']} pages flagged, "
                                  f"{stats['text_total']} text + {stats['url_total']} URL violations "
                                  f"({failed_count} failed)"})
        await _broadcast({
            "type":        "done",
            "total_pages": stats["pages"],
            "total_text":  stats["text_total"],
            "total_url":   stats["url_total"],
        })

    except Exception as exc:
        import traceback
        traceback.print_exc()
        await _broadcast({"type": "fatal", "message": str(exc)})
    finally:
        _state["running"] = False


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/start")
async def api_start():
    global _task
    if _state["running"]:
        return {"error": "Scan already running"}
    _task = asyncio.create_task(_run_scan())
    return {"status": "started"}


@app.post("/api/stop")
async def api_stop():
    _state["stop"] = True
    ctx = _state.get("browser_ctx")
    if ctx:
        try:
            await ctx.close()
        except Exception:
            pass
        _state["browser_ctx"] = None
    return {"status": "stopping"}


@app.get("/api/status")
async def api_status():
    return {"running": _state["running"]}


@app.post("/api/reset")
async def api_reset():
    if _state["running"]:
        return {"error": "Cannot reset while scan is running"}
    init_db()
    clear_all()
    return {"ok": True}


@app.get("/api/previous")
async def api_previous():
    init_db()
    pages = get_all_pages_with_status()
    if not pages:
        return {"pages": []}
    viol_map = get_violations_by_page()
    result = []
    for p in pages:
        pv = viol_map.get(p["url"], {"text_violations": [], "url_violations": []})
        entry = {
            "url":    p["url"],
            "title":  p["title"] or p["url"],
            "status": p["status"],
            "text_violations": pv["text_violations"] if p["status"] == 200 else [],
            "url_violations":  pv["url_violations"]  if p["status"] == 200 else [],
        }
        result.append(entry)
    return {"pages": result}


@app.get("/api/stream")
async def api_stream():
    q: asyncio.Queue = asyncio.Queue()
    _clients.append(q)

    async def generator():
        for event in list(_history):
            yield f"data: {json.dumps(event)}\n\n"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("done", "fatal"):
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"type":"ping"}\n\n'
        finally:
            if q in _clients:
                _clients.remove(q)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/download-csv")
async def download_csv(vtype: str = "all", hide_codes: str = "false"):
    import csv, io
    from datetime import datetime, timezone
    from db import get_all_text_violations, get_all_url_violations

    scan_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buf = io.StringIO()
    writer = csv.writer(buf)
    hide = hide_codes.lower() == "true"

    if vtype == "url":
        writer.writerow(["page_url", "page_title", "link_text", "href", "suggested_fix", "scan_date"])
        for v in get_all_url_violations():
            writer.writerow([v["url"], v["page_title"] or "", v["link_text"] or "",
                             v["href"], v["suggested_fix"] or "", scan_date])
        filename = "url-violations.csv"

    elif vtype == "text":
        writer.writerow(["page_url", "page_title", "element_type", "text",
                         "is_product_code", "scan_date"])
        for v in get_all_text_violations():
            if hide and v["is_product_code"]:
                continue
            writer.writerow([v["url"], v["page_title"] or "", v["element_type"],
                             v["text"], bool(v["is_product_code"]), scan_date])
        filename = "text-violations.csv"

    else:
        writer.writerow(["violation_type", "page_url", "page_title",
                         "element_type", "english_text",
                         "link_text", "href", "suggested_fix",
                         "is_product_code", "scan_date"])
        for v in get_all_text_violations():
            if hide and v["is_product_code"]:
                continue
            writer.writerow(["text", v["url"], v["page_title"] or "",
                             v["element_type"], v["text"],
                             "", "", "",
                             bool(v["is_product_code"]), scan_date])
        for v in get_all_url_violations():
            writer.writerow(["url", v["url"], v["page_title"] or "",
                             "", "",
                             v["link_text"] or "", v["href"], v["suggested_fix"] or "",
                             "", scan_date])
        filename = "all-violations.csv"

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/report")
async def api_report():
    result = subprocess.run(
        [sys.executable, "report.py"],
        capture_output=True, text=True, cwd="."
    )
    if result.returncode == 0:
        return {"ok": True}
    return {"ok": False, "error": result.stderr}


@app.get("/report")
async def serve_report():
    path = "data/reports/report.html"
    try:
        return FileResponse(path, media_type="text/html")
    except FileNotFoundError:
        return HTMLResponse("<p>Report not generated yet.</p>", status_code=404)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


# ── HTML UI ───────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Japanese Audit · teltonika-gps.com/ja/</title>
<style>
:root {
  --bg:      #0d1117;
  --surface: #161b22;
  --surf2:   #21262d;
  --border:  #30363d;
  --text:    #e6edf3;
  --muted:   #7d8590;
  --red:     #f85149;
  --yellow:  #d29922;
  --blue:    #388bfd;
  --green:   #3fb950;
  --orange:  #f0883e;
  --purple:  #bc8cff;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh;font-size:14px}

/* Header */
.hdr{background:var(--surface);border-bottom:1px solid var(--border);
     padding:13px 20px;display:flex;align-items:center;
     justify-content:space-between;position:sticky;top:0;z-index:100;gap:12px}
.hdr-left{display:flex;align-items:center;gap:10px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:background .3s}
.dot.on{background:var(--green);animation:blink 1.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.hdr h1{font-size:.95rem;font-weight:600}
.hdr p{font-size:.7rem;color:var(--muted);margin-top:1px}
.hdr-right{display:flex;align-items:center;gap:8px}
.btn{padding:6px 16px;border-radius:6px;border:none;font-size:.8rem;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn:hover{opacity:.85}.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-start{background:var(--green);color:#fff}
.btn-stop{background:var(--red);color:#fff}
.btn-reset{padding:5px 11px;border-radius:5px;font-size:.75rem;font-weight:500;
           border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer}
.btn-reset:hover{border-color:var(--red);color:var(--red)}

/* Stats */
.stats{display:flex;gap:1px;background:var(--border);border-bottom:1px solid var(--border)}
.stat{flex:1;background:var(--surface);padding:13px 14px;text-align:center}
.stat .n{font-size:1.7rem;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.stat .l{font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-top:4px}
.stat.s-text .n{color:var(--red)}
.stat.s-url  .n{color:var(--yellow)}
.stat.s-flag .n{color:var(--orange)}

/* Scan bar */
.scan-bar{background:var(--surface);border-bottom:1px solid var(--border);
          padding:7px 20px;font-size:.72rem;display:none;align-items:center;gap:10px}
.scan-bar.on{display:flex}
.scan-label{color:var(--blue);font-weight:700;flex-shrink:0;letter-spacing:.04em}
.scan-url{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
          font-family:monospace;flex:1}
.scan-q{color:var(--muted);flex-shrink:0}

/* Log */
.log{background:#0a0e15;border-bottom:1px solid var(--border);padding:4px 20px;
     display:none;font-family:monospace;font-size:.66rem;color:#4a5568;
     max-height:150px;overflow-y:auto}
.log.on{display:block}
.log-line{line-height:1.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.log-err{color:var(--red)}

/* Filter bar */
.fbar{padding:9px 20px;display:flex;gap:6px;align-items:center;
      border-bottom:1px solid var(--border);flex-wrap:wrap}
.fbtn{padding:3px 11px;border-radius:20px;border:1px solid var(--border);
      background:transparent;color:var(--muted);font-size:.73rem;cursor:pointer;transition:all .15s}
.fbtn:hover{border-color:var(--text);color:var(--text)}
.fbtn.on{background:var(--surf2);border-color:var(--blue);color:var(--text)}
.fsep{width:1px;height:20px;background:var(--border);margin:0 6px;opacity:.6;flex-shrink:0}

/* Smart filter toggle */
.smart-filter{display:flex;align-items:center;gap:6px;font-size:.75rem;
              color:var(--muted);cursor:pointer;user-select:none;
              padding:3px 11px;border-radius:20px;border:1px solid var(--border);
              background:transparent;transition:all .15s;white-space:nowrap}
.smart-filter:hover{border-color:var(--purple);color:var(--purple)}
.smart-filter.on{border-color:var(--purple);color:var(--purple);background:rgba(188,140,255,.08)}
.smart-filter input{accent-color:var(--purple);width:12px;height:12px}
#feed.filter-url .vrow-text{display:none!important}
#feed.filter-text .vrow-url{display:none!important}

.fcount{margin-left:auto;font-size:.73rem;color:var(--muted);white-space:nowrap}

.csv-btn{padding:4px 12px;border-radius:5px;border:1px solid var(--border);
         background:var(--surf2);color:var(--text);font-size:.74rem;font-weight:600;
         cursor:pointer;text-decoration:none;display:none;white-space:nowrap}
.csv-btn:hover{border-color:var(--text)}
.csv-btn.show{display:inline-block}

/* Feed */
.feed{padding:14px 20px;max-width:980px;margin:0 auto}
.empty{text-align:center;padding:70px 0;color:var(--muted)}
.empty .icon{font-size:2.2rem;margin-bottom:10px}
.empty p{font-size:.875rem;line-height:1.8}

/* Cards */
.card{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--border);
      border-radius:6px;margin-bottom:9px;animation:drop .2s ease-out}
@keyframes drop{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
@keyframes flash{0%{background:var(--surf2)}100%{background:var(--surface)}}
.card.c-text{border-left-color:var(--red)}
.card.c-url{border-left-color:var(--yellow)}
.card.c-both{border-left-color:var(--orange)}
.card.c-clean{border-left-color:var(--green)}
.card.c-pending{border-left-color:var(--border)}
.card.c-failed{border-left-color:var(--orange)}
.card.hidden{display:none}
.card.updated{animation:flash .6s ease-out}

.card-top{padding:10px 13px 6px;display:flex;align-items:flex-start;
          justify-content:space-between;gap:8px}
.card-badges{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.65rem;font-weight:600}
.b-ja{background:#1c2a3a;color:#a5f3fc}
.verdict{flex-shrink:0;padding:2px 9px;border-radius:20px;font-size:.66rem;font-weight:700;letter-spacing:.03em}
.v-clean{background:rgba(63,185,80,.1);color:var(--green);border:1px solid rgba(63,185,80,.3)}
.v-flag{background:rgba(240,136,62,.1);color:var(--orange);border:1px solid rgba(240,136,62,.3)}
.v-pend{background:rgba(125,133,144,.08);color:var(--muted);border:1px solid rgba(125,133,144,.2);animation:blink 2s ease infinite}
.v-fail{background:rgba(248,81,73,.1);color:var(--red);border:1px solid rgba(248,81,73,.3)}

.card-title{padding:0 13px 2px;font-size:.85rem;font-weight:600}
.card-url a{display:block;padding:0 13px 9px;font-size:.7rem;color:var(--blue);
            text-decoration:none;word-break:break-all;font-family:monospace}
.card-url a:hover{text-decoration:underline}
.card-err{padding:3px 13px 9px;font-size:.7rem;color:var(--orange);font-family:monospace;opacity:.8}

/* Violation rows */
.viols{border-top:1px solid var(--border);padding:9px 13px;display:flex;flex-direction:column;gap:6px}
.vrow{display:flex;align-items:flex-start;gap:8px;font-size:.75rem}
.vtype-text{padding:1px 6px;border-radius:4px;font-size:.62rem;font-weight:700;
            background:rgba(248,81,73,.12);color:var(--red);border:1px solid rgba(248,81,73,.2);flex-shrink:0}
.vtype-url{padding:1px 6px;border-radius:4px;font-size:.62rem;font-weight:700;
           background:rgba(210,153,34,.12);color:var(--yellow);border:1px solid rgba(210,153,34,.2);flex-shrink:0}
.vtag{padding:1px 6px;border-radius:4px;font-size:.62rem;background:var(--surf2);color:var(--muted);
      font-family:monospace;flex-shrink:0}
.vtext{color:var(--text);font-family:monospace;word-break:break-word}
.vcode-badge{padding:1px 5px;border-radius:3px;font-size:.6rem;
             background:rgba(188,140,255,.12);color:var(--purple);
             border:1px solid rgba(188,140,255,.25);flex-shrink:0}
.vfix{display:block;color:var(--muted);font-size:.68rem;margin-top:1px;word-break:break-all}

/* Banners */
.restored-banner,.done-banner{border-radius:6px;padding:11px 15px;margin-bottom:12px;
                              display:flex;align-items:center;justify-content:space-between;gap:14px}
.restored-banner{background:rgba(56,139,253,.07);border:1px solid rgba(56,139,253,.2);
                 font-size:.8rem;color:var(--muted)}
.done-banner{background:rgba(63,185,80,.07);border:1px solid rgba(63,185,80,.22)}
.done-text{font-size:.875rem}
.done-text strong{color:var(--green)}
.banner-actions{display:flex;gap:7px;flex-shrink:0}
.btn-sm{padding:4px 12px;border-radius:5px;border:1px solid var(--border);
        background:var(--surf2);color:var(--text);font-size:.76rem;
        font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
.btn-sm:hover{border-color:var(--text)}
.btn-sm.primary{background:var(--green);border-color:var(--green);color:#fff}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-left">
    <div class="dot" id="dot"></div>
    <div>
      <h1>Japanese Audit Scanner <span style="font-size:.45em;font-weight:400;opacity:.4;letter-spacing:.05em;vertical-align:middle">v2.5.4</span></h1>
      <p>teltonika-gps.com/ja/ &nbsp;·&nbsp; English text &amp; broken locale links</p>
    </div>
  </div>
  <div class="hdr-right">
    <button class="btn-reset" onclick="resetDB()">&#8635; Reset</button>
    <button class="btn btn-start" id="mainBtn" onclick="toggleScan()">&#9654; Start Scan</button>
  </div>
</div>

<div class="stats">
  <div class="stat">         <div class="n" id="s-pages">0</div><div class="l">Pages</div></div>
  <div class="stat s-flag">  <div class="n" id="s-flag">0</div> <div class="l">Flagged</div></div>
  <div class="stat s-text">  <div class="n" id="s-text">0</div> <div class="l">Text violations</div></div>
  <div class="stat s-url">   <div class="n" id="s-url">0</div>  <div class="l">URL violations</div></div>
</div>

<div class="scan-bar" id="scanBar">
  <span class="scan-label" id="scanLabel">CRAWLING</span>
  <span class="scan-url"   id="scanUrl">—</span>
  <span class="scan-q"     id="scanQ"></span>
</div>

<div class="log" id="logPanel"><div id="logLines"></div></div>

<div class="fbar">
  <button class="fbtn on" onclick="setFilter('all',this)">All</button>
  <button class="fbtn"    onclick="setFilter('text',this)">Text violations</button>
  <button class="fbtn"    onclick="setFilter('url',this)">URL violations</button>
  <button class="fbtn"    onclick="setFilter('clean',this)">Clean</button>
  <button class="fbtn"    onclick="setFilter('failed',this)" style="border-color:var(--orange);color:var(--orange)">Failed</button>
  <span class="fsep"></span>
  <label class="smart-filter" id="smartFilter" onclick="toggleSmartFilter()">
    <input type="checkbox" id="hideCodesChk" onclick="event.stopPropagation()"> Hide product codes
  </label>
  <label class="smart-filter" id="visibleFilter" onclick="toggleVisibleFilter()">
    <input type="checkbox" id="hideAttrsChk" onclick="event.stopPropagation()"> Visible text only
  </label>
  <label class="smart-filter" id="nameFilter" onclick="toggleNameFilter()">
    <input type="checkbox" id="hideNamesChk" onclick="event.stopPropagation()"> Hide names
  </label>
  <label class="smart-filter" id="uniqueFilter" onclick="toggleUniqueFilter()">
    <input type="checkbox" id="uniqueChk" onclick="event.stopPropagation()"> Unique only
  </label>
  <span class="fcount" id="fCount"></span>
  <a class="csv-btn" id="csvAll"  href="#" onclick="dlCsv('all')">&#11015; All CSV</a>
  <a class="csv-btn" id="csvText" href="#" onclick="dlCsv('text')">&#11015; Text CSV</a>
  <a class="csv-btn" id="csvUrl"  href="#" onclick="dlCsv('url')">&#11015; URL CSV</a>
</div>

<div class="feed" id="feed">
  <div class="empty" id="emptyMsg">
    <div class="icon">&#127758;</div>
    <p>Click <strong>Start Scan</strong> to crawl<br>
    <code style="font-size:.85em;background:var(--surf2);padding:2px 8px;border-radius:4px">
    teltonika-gps.com/ja/</code><br>
    and find untranslated English content.</p>
  </div>
</div>

<script>
let es = null, running = false, filter = 'all', hideCodes = false, hideAttrs = true, hideNames = false, uniqueOnly = false;

// Non-name words that appear title-cased or all-caps in content
const NAME_EXCLUSIONS = new Set([
  // navigation / UI
  'search','page','pages','home','about','all','new','free','more','read',
  'visit','contact','contacts','go','get','see','view','find','register',
  'compare','default','filters','filter','sort','language','english','spanish',
  'french','german','japanese','ukrainian','selector','region','country',
  // business / product words
  'web','access','easy','real','device','update','standard','basic','advanced',
  'professional','management','platform','solution','solutions','service',
  'services','system','network','networks','monitor','control','smart','global',
  'premium','enterprise','plus','pro','news','blog','list','fleet','telematics',
  'supported','information','configuration','configurator','iridium','connected',
  'practical','cost','effective','smooth','transition','robust','durable',
  'hardware','personalised','usage','scenarios','firmware','uploads','desktop',
  'versions','setup','wizard','automatic','seamless','software','categories',
  'partners','custom','security','tracking','analytics','cloud','data','portal',
  'dashboard','technology','integration','deployment','implementation','features',
  'products','providers','devices','types','options','results','terms','updates',
  'downloads','topics','resources','images','media','details','reports',
  'conditions','sections','section','error','loading','sorting','category',
  // job-title words
  'chief','executive','officer','director','manager','coordinator','engineer',
  'specialist','analyst','developer','designer','architect','consultant',
  'advisor','president','chairman','founder','associate','principal','senior',
  'junior','lead','head','deputy','representative','owner','operator',
  // webinar / event words
  'recording','webinar','webinars','speakers','agenda','session','live',
  'introduction','conclusions','overview','challenges','benefits','concept',
  'opportunities','practices','market','tracking','vehicle','insurance',
  'future','urban','signal','precise','blockage','industries','business',
  // brand / product names that look like names
  'teltonika','wirepas','bluetooth','configurator','tachograph','dualcam',
  'fota','atex','gnss','mesh','wiki','iridium','gpsgate','wialon','escort',
  'gurtam','argus','academy',
  // common non-name words
  'fast','top','use','not','lead','back','touch','start','privacy','policy',
  'policies','cookies','copyright','from','this','our','your','public',
  'modern','using','help','direct','explore','logistics','cases','found',
  'sorry','beyond','creative','powers','homepage','features','items',
]);

// Word endings that never appear in personal names
const NOT_NAME_SUFFIX = /(?:TION|NESS|MENT|OUND|CESS|WARE|WORK|SHIP|LESS|WISE|OLOG|ICAL|IBLE|ABLE|IOUS|ISM|INGS|IES)$/i;

function looksLikeName(text) {
  const stripped = text.replace(/[,.\s]+$/, '').trim();
  const words = stripped.split(/\s+/);
  if (words.length < 2 || words.length > 4) return false;
  if (/\d/.test(stripped)) return false;   // any digit anywhere → not a name

  const cleanWords = words.map(w => w.replace(/[.,;:]+$/, ''));

  // ── All-caps path: ANDY PATRICK, AIRIDAS STAŠENKA, GABRIELA MARIA RODRIGUEZ CALIX
  // Allow Lithuanian/accented uppercase: Š Ū Č Ė etc.
  const isAllCaps = cleanWords.every(w => w.length > 0 && /^[\p{Lu}]+$/u.test(w));
  if (isAllCaps) {
    for (const w of cleanWords) {
      if (w.length < 3 || w.length > 20) return false;
      if (!/^[\p{L}]+$/u.test(w)) return false;
      if (NAME_EXCLUSIONS.has(w.toLowerCase())) return false;
      if (NOT_NAME_SUFFIX.test(w)) return false;
    }
    return true;
  }

  // ── Title-case / mixed path: Gintarė N., Francisco Q., John Smith, Arūnas Kuginys
  for (const w of cleanWords) {
    if (!w) return false;
    // Single uppercase letter = surname initial (e.g. "N." in "Gintarė N.")
    if (/^[\p{Lu}]$/u.test(w)) continue;
    if (w.length < 2) return false;
    if (!/^[\p{Lu}]/u.test(w)) return false;        // must start uppercase
    if (/^[\p{Lu}]+$/u.test(w)) return false;       // all-caps word in mixed context = acronym
    if (!/^[\p{L}'\-]+$/u.test(w)) return false;   // letters, hyphens, apostrophes only
    if (NAME_EXCLUSIONS.has(w.toLowerCase())) return false;
    if (NOT_NAME_SUFFIX.test(w)) return false;
  }
  return true;
}
let hasViolations = false;
const ATTR_TYPES = new Set(['placeholder','alt','aria-label','data-tooltip','data-title']);
const cardMap = new Map();
const MAX_LOG = 400;

function setStat(id, val) {
  const el = document.getElementById('s-' + id);
  if (el && el.textContent !== String(val)) el.textContent = val;
}
function updateCsvButtons() {
  document.getElementById('csvAll') .classList.toggle('show', hasViolations && filter === 'all');
  document.getElementById('csvText').classList.toggle('show', hasViolations && (filter === 'all' || filter === 'text'));
  document.getElementById('csvUrl') .classList.toggle('show', hasViolations && (filter === 'all' || filter === 'url'));
}
function updateStats(s) {
  if (!s) return;
  setStat('pages', s.pages  || 0);
  setStat('flag',  s.pages_flagged || 0);
  setStat('text',  s.text_total || 0);
  setStat('url',   s.url_total  || 0);
  hasViolations = (s.text_total || 0) + (s.url_total || 0) > 0;
  updateCsvButtons();
}

function addLog(msg, isErr) {
  const panel = document.getElementById('logPanel');
  const lines = document.getElementById('logLines');
  panel.classList.add('on');
  const div = document.createElement('div');
  div.className = 'log-line' + (isErr ? ' log-err' : '');
  div.textContent = new Date().toTimeString().slice(0,8) + '  ' + msg;
  lines.appendChild(div);
  while (lines.children.length > MAX_LOG) lines.removeChild(lines.firstChild);
  panel.scrollTop = panel.scrollHeight;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function dlCsv(vtype) {
  const params = new URLSearchParams({ vtype, hide_codes: hideCodes ? 'true' : 'false' });
  const a = document.createElement('a');
  a.href = '/api/download-csv?' + params;
  a.download = (vtype === 'all' ? 'all' : vtype) + '-violations.csv';
  a.click();
  return false;
}

/* ── Filter logic ── */
function setFilter(f, btn) {
  filter = f;
  document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  const feed = document.getElementById('feed');
  feed.classList.toggle('filter-text', f === 'text');
  feed.classList.toggle('filter-url',  f === 'url');
  updateCsvButtons();
  recount();
}

function syncRowDisplay(row) {
  const hide = (row.classList.contains('hidden-code') && hideCodes)
             || (row.classList.contains('hidden-attr') && hideAttrs)
             || (row.classList.contains('hidden-name') && hideNames);
  row.style.display = hide ? 'none' : '';
}

function applyUniqueFilter() {
  if (!uniqueOnly) {
    // Restore every analyzed card to its full violation set
    cardMap.forEach((data) => {
      if (data.type !== 'analyzed') return;
      const { el, textV, urlV } = data;
      const newViols = violsHtml(textV, urlV);
      const viols = el.querySelector('.viols');
      if (viols) { if (newViols) viols.outerHTML = newViols; else viols.remove(); }
      else if (newViols) el.insertAdjacentHTML('beforeend', newViols);
      applySmartFilter(el);
      el.dataset.v = cardV(textV, urlV);
      const vd = el.querySelector('.verdict');
      if (vd) vd.outerHTML = verdictHtml(textV, urlV);
    });
    return;
  }

  // Walk cards in DOM order so the first occurrence of each text is the one kept
  const seenTexts = new Set(), seenHrefs = new Set();
  document.querySelectorAll('.card').forEach(card => {
    const data = cardMap.get(card.dataset.url);
    if (!data || data.type !== 'analyzed') return;

    const uTextV = data.textV.filter(v => {
      if (seenTexts.has(v.text)) return false;
      seenTexts.add(v.text); return true;
    });
    const uUrlV = data.urlV.filter(v => {
      if (seenHrefs.has(v.href)) return false;
      seenHrefs.add(v.href); return true;
    });

    const newViols = violsHtml(uTextV, uUrlV);
    const viols = card.querySelector('.viols');
    if (viols) { if (newViols) viols.outerHTML = newViols; else viols.remove(); }
    else if (newViols) card.insertAdjacentHTML('beforeend', newViols);

    applySmartFilter(card);
    card.dataset.v = cardV(uTextV, uUrlV);
    const vd = card.querySelector('.verdict');
    if (vd) vd.outerHTML = verdictHtml(uTextV, uUrlV);
  });
}

function toggleSmartFilter() {
  hideCodes = !hideCodes;
  document.getElementById('hideCodesChk').checked = hideCodes;
  document.getElementById('smartFilter').classList.toggle('on', hideCodes);
  document.querySelectorAll('.vrow.hidden-code').forEach(syncRowDisplay);
  recount();
}

function toggleVisibleFilter() {
  hideAttrs = !hideAttrs;
  document.getElementById('hideAttrsChk').checked = hideAttrs;
  document.getElementById('visibleFilter').classList.toggle('on', hideAttrs);
  document.querySelectorAll('.vrow.hidden-attr').forEach(syncRowDisplay);
  recount();
}

function toggleNameFilter() {
  hideNames = !hideNames;
  document.getElementById('hideNamesChk').checked = hideNames;
  document.getElementById('nameFilter').classList.toggle('on', hideNames);
  document.querySelectorAll('.vrow.hidden-name').forEach(syncRowDisplay);
  recount();
}

function toggleUniqueFilter() {
  uniqueOnly = !uniqueOnly;
  document.getElementById('uniqueChk').checked = uniqueOnly;
  document.getElementById('uniqueFilter').classList.toggle('on', uniqueOnly);
  applyUniqueFilter();
  recount();
}

function applySmartFilter(card) {
  card.querySelectorAll('.vrow').forEach(syncRowDisplay);
}

function recount() {
  let visible = 0;
  document.querySelectorAll('.card').forEach(c => {
    const v = c.dataset.v;
    let show;
    if      (filter === 'all')    show = true;
    else if (filter === 'clean')  show = v === '0';
    else if (filter === 'failed') show = v === 'f';
    else if (filter === 'text')   show = v === 'text' || v === 'both';
    else if (filter === 'url')    show = v === 'url'  || v === 'both';
    else show = false;
    c.classList.toggle('hidden', !show);
    if (show) visible++;
  });
  const total = document.querySelectorAll('.card').length;
  document.getElementById('fCount').textContent = total ? visible + ' of ' + total : '';
}

/* ── Violation HTML builders ── */
function textViolRow(v) {
  const isCode = v.is_product_code;
  const isAttr = ATTR_TYPES.has(v.element_type);
  const isName = looksLikeName(v.text);
  const classes = ['vrow', 'vrow-text'];
  if (isCode) classes.push('hidden-code');
  if (isAttr) classes.push('hidden-attr');
  if (isName) classes.push('hidden-name');
  const hide = (isCode && hideCodes) || (isAttr && hideAttrs) || (isName && hideNames);
  const style = hide ? ' style="display:none"' : '';
  return `<div class="${classes.join(' ')}"${style}>` +
    `<span class="vtype-text">TEXT</span>` +
    `<span class="vtag">&lt;${esc(v.element_type)}&gt;</span>` +
    `<span class="vtext">&ldquo;${esc(v.text)}&rdquo;</span>` +
    (isCode ? `<span class="vcode-badge">product code</span>` : '') +
    (isAttr ? `<span class="vcode-badge" style="background:rgba(56,139,253,.12);color:var(--blue);border-color:rgba(56,139,253,.3)">attr</span>` : '') +
    (isName ? `<span class="vcode-badge" style="background:rgba(120,80,200,.12);color:#a78bfa;border-color:rgba(120,80,200,.3)">name</span>` : '') +
    `</div>`;
}

function urlViolRow(v) {
  return `<div class="vrow vrow-url">` +
    `<span class="vtype-url">URL</span>` +
    `<span class="vtext">&ldquo;${esc(v.link_text)}&rdquo; &rarr; ` +
      `<code style="font-size:.68rem;color:var(--red)">${esc(v.href)}</code>` +
    `</span>` +
    (v.suggested_fix ? `<span class="vfix">&#8627; ${esc(v.suggested_fix)}</span>` : '') +
    `</div>`;
}

function violsHtml(textV, urlV) {
  if (!textV.length && !urlV.length) return '';
  return '<div class="viols">' +
    textV.map(textViolRow).join('') +
    urlV.map(urlViolRow).join('') +
    '</div>';
}

function cardClass(textV, urlV) {
  if (textV.length && urlV.length) return 'c-both';
  if (textV.length)                return 'c-text';
  if (urlV.length)                 return 'c-url';
  return 'c-clean';
}

function cardV(textV, urlV) {
  if (textV.length && urlV.length) return 'both';
  if (textV.length)                return 'text';
  if (urlV.length)                 return 'url';
  return '0';
}

function verdictHtml(textV, urlV) {
  if (!textV.length && !urlV.length)
    return '<span class="verdict v-clean">&#10003; Clean</span>';
  const parts = [];
  if (textV.length) parts.push(textV.length + ' text');
  if (urlV.length)  parts.push(urlV.length + ' url');
  return `<span class="verdict v-flag">&#10007; ${parts.join(' &middot; ')}</span>`;
}

function analyzedCardHtml(url, title, textV, urlV) {
  return `<div class="card-top">` +
    `<div class="card-badges"><span class="badge b-ja">ja</span></div>` +
    verdictHtml(textV, urlV) +
    `</div>` +
    `<div class="card-title">${esc(title)}</div>` +
    `<div class="card-url"><a href="${esc(url)}" target="_blank">${esc(url)}</a></div>` +
    violsHtml(textV, urlV);
}

/* ── Card operations ── */
function _insertCard(card) {
  document.getElementById('emptyMsg')?.remove();
  const feed = document.getElementById('feed');
  feed.insertBefore(card, feed.firstChild);
}

function addPendingCard(d) {
  if (cardMap.has(d.url)) return;
  const card = document.createElement('div');
  card.className   = 'card c-pending';
  card.dataset.url = d.url;
  card.dataset.v   = 'p';
  card.innerHTML   =
    `<div class="card-top"><div class="card-badges"><span class="badge b-ja">ja</span></div>` +
    `<span class="verdict v-pend">&#9203; Pending</span></div>` +
    `<div class="card-url"><a href="${esc(d.url)}" target="_blank">${esc(d.url)}</a></div>`;
  _insertCard(card);
  cardMap.set(d.url, { el: card, textV: [], urlV: [], type: 'pending' });
}

function addFailedCard(d) {
  if (cardMap.has(d.url)) return;
  const card = document.createElement('div');
  card.className   = 'card c-failed';
  card.dataset.url = d.url;
  card.dataset.v   = 'f';
  card.innerHTML   =
    `<div class="card-top"><div class="card-badges"><span class="badge b-ja">ja</span></div>` +
    `<span class="verdict v-fail">&#10007; Failed</span></div>` +
    `<div class="card-url"><a href="${esc(d.url)}" target="_blank">${esc(d.url)}</a></div>` +
    `<div class="card-err">${esc(d.error || 'Failed')}</div>`;
  _insertCard(card);
  cardMap.set(d.url, { el: card, textV: [], urlV: [], type: 'failed' });
}

function updateCard(url, title, textV, urlV) {
  const data = cardMap.get(url);
  if (!data) return;
  const card = data.el;
  const t = title || card.dataset.title || url;
  card.className  = 'card ' + cardClass(textV, urlV) + ' updated';
  card.dataset.v  = cardV(textV, urlV);
  card.dataset.title = t;
  card.innerHTML  = analyzedCardHtml(url, t, textV, urlV);
  data.textV = textV; data.urlV = urlV; data.type = 'analyzed';
  applySmartFilter(card);
  if (uniqueOnly) applyUniqueFilter();
  setTimeout(() => card.classList.remove('updated'), 700);
}

function addAnalyzedCard(d) {
  if (cardMap.has(d.url)) return;
  const textV = d.text_violations || [];
  const urlV  = d.url_violations  || [];
  const card = document.createElement('div');
  card.className     = 'card ' + cardClass(textV, urlV);
  card.dataset.url   = d.url;
  card.dataset.title = d.title;
  card.dataset.v     = cardV(textV, urlV);
  card.innerHTML     = analyzedCardHtml(d.url, d.title, textV, urlV);
  applySmartFilter(card);
  _insertCard(card);
  cardMap.set(d.url, { el: card, textV, urlV, type: 'analyzed' });
  if (uniqueOnly) applyUniqueFilter();
}

/* ── SSE event handler ── */
function handle(d) {
  if (d.type === 'ping') return;

  if (d.type === 'started') {
    running = true;
    document.getElementById('dot').classList.add('on');
    const btn = document.getElementById('mainBtn');
    btn.innerHTML = '&#9632; Stop'; btn.className = 'btn btn-stop';
    document.getElementById('scanBar').classList.add('on');
    document.getElementById('logPanel').classList.remove('on');
    document.getElementById('logLines').innerHTML = '';
    document.getElementById('emptyMsg')?.remove();
    document.querySelectorAll('.done-banner,.restored-banner').forEach(el => el.remove());
    updateStats({pages:0,pages_flagged:0,text_total:0,url_total:0});
    return;
  }

  if (d.type === 'log') { addLog(d.msg, !!d.err); return; }

  if (d.type === 'phase') {
    document.getElementById('scanLabel').textContent = 'CRAWLING';
    document.getElementById('scanUrl').textContent   = 'Discovering /ja/ pages…';
    document.getElementById('scanQ').textContent     = d.queued ? d.queued + ' queued' : '';
    return;
  }

  if (d.type === 'page_failed') {
    addFailedCard(d); recount(); return;
  }

  if (d.type === 'page_analyzed') {
    updateStats(d.stats);
    document.getElementById('scanUrl').textContent = (d.title || d.url).substring(0, 72);
    document.getElementById('scanQ').textContent   = (d.stats?.pages || '') + ' pages';
    if (!cardMap.has(d.url)) addAnalyzedCard(d);
    else updateCard(d.url, d.title, d.text_violations || [], d.url_violations || []);
    recount(); return;
  }

  if (d.type === 'done') {
    running = false;
    document.getElementById('dot').classList.remove('on');
    document.getElementById('mainBtn').innerHTML   = '&#9654; Start Scan';
    document.getElementById('mainBtn').className   = 'btn btn-start';
    document.getElementById('scanBar').classList.remove('on');
    showDone(d); return;
  }

  if (d.type === 'fatal') {
    running = false;
    document.getElementById('dot').classList.remove('on');
    document.getElementById('mainBtn').innerHTML = '&#9654; Start Scan';
    document.getElementById('mainBtn').className = 'btn btn-start';
    document.getElementById('scanBar').classList.remove('on');
    alert('Scan error: ' + d.message);
  }
}

/* ── Scan control ── */
async function toggleScan() {
  if (running) {
    await fetch('/api/stop', { method: 'POST' });
  } else {
    const r = await fetch('/api/start', { method: 'POST' });
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    connectStream();
  }
}
function connectStream() {
  if (es) { es.close(); es = null; }
  es = new EventSource('/api/stream');
  es.onmessage = e => handle(JSON.parse(e.data));
}
async function resetDB() {
  if (running) { alert('Stop the scan first.'); return; }
  if (!confirm('Wipe all scanned data and start fresh?')) return;
  const r = await fetch('/api/reset', { method: 'POST' });
  const d = await r.json();
  if (d.ok) {
    cardMap.clear();
    document.querySelectorAll('.card,.done-banner,.restored-banner').forEach(el => el.remove());
    updateStats({pages:0,pages_flagged:0,text_total:0,url_total:0});
    if (!document.getElementById('emptyMsg')) {
      const msg = document.createElement('div');
      msg.className = 'empty'; msg.id = 'emptyMsg';
      msg.innerHTML = '<div class="icon">&#127758;</div><p>Click <strong>Start Scan</strong> to begin.</p>';
      document.getElementById('feed').appendChild(msg);
    }
  } else { alert('Reset failed: ' + d.error); }
}

/* ── Done banner ── */
function showDone(d) {
  const banner = document.createElement('div');
  banner.className = 'done-banner';
  banner.innerHTML =
    `<div class="done-text"><strong>Scan complete.</strong> ` +
      `${d.total_pages} pages &nbsp;&middot;&nbsp; ` +
      `${d.total_text} text violations &nbsp;&middot;&nbsp; ` +
      `${d.total_url} URL violations</div>` +
    `<div class="banner-actions">` +
      `<button class="btn-sm primary" onclick="genReport(this)">Generate Report</button>` +
    `</div>`;
  document.getElementById('feed').insertBefore(banner, document.getElementById('feed').firstChild);
}
async function genReport(btn) {
  btn.textContent = 'Generating…'; btn.disabled = true;
  const r = await fetch('/api/report', { method: 'POST' });
  const d = await r.json();
  btn.disabled = false;
  if (d.ok) {
    btn.innerHTML = '&#10003; Open Report';
    btn.onclick = () => window.open('/report', '_blank');
  } else { btn.textContent = 'Error — retry'; console.error(d.error); }
}

/* ── Load previous results ── */
async function loadPrevious() {
  const res  = await fetch('/api/previous');
  const data = await res.json();
  if (!data.pages || !data.pages.length) return;

  let totalPages = 0, flagged = 0, textTotal = 0, urlTotal = 0;
  data.pages.forEach(p => {
    totalPages++;
    const tv = p.text_violations || [], uv = p.url_violations || [];
    if (tv.length || uv.length) flagged++;
    textTotal += tv.length;
    urlTotal  += uv.length;
  });
  updateStats({pages: totalPages, pages_flagged: flagged,
               text_total: textTotal, url_total: urlTotal});

  const banner = document.createElement('div');
  banner.className = 'restored-banner';
  banner.innerHTML =
    `<span><strong style="color:var(--blue)">&#9679; Previous scan restored</strong> &nbsp;&middot;&nbsp; ` +
    `${totalPages} pages &nbsp;&middot;&nbsp; ${flagged} flagged &nbsp;&middot;&nbsp; ` +
    `${textTotal} text &nbsp;&middot;&nbsp; ${urlTotal} URL &nbsp;&middot;&nbsp; ` +
    `<em>Click Start Scan to re-crawl.</em></span>`;
  document.getElementById('emptyMsg')?.remove();
  document.getElementById('feed').insertBefore(banner, document.getElementById('feed').firstChild);

  const pages = [...data.pages].reverse();
  const CHUNK = 50;
  let i = 0;
  function renderChunk() {
    const end = Math.min(i + CHUNK, pages.length);
    for (; i < end; i++) {
      const p = pages[i];
      if (p.status === 200)  addAnalyzedCard(p);
      else if (p.status === 0)  addFailedCard({ url: p.url, error: 'Previously failed' });
      else if (p.status === -1) addPendingCard({ url: p.url });
    }
    recount();
    if (i < pages.length) setTimeout(renderChunk, 0);
  }
  renderChunk();
}

/* ── Init ── */
window.addEventListener('load', async () => {
  document.getElementById('hideAttrsChk').checked = hideAttrs;
  document.getElementById('visibleFilter').classList.toggle('on', hideAttrs);
  const r = await fetch('/api/status');
  const d = await r.json();
  if (d.running) {
    running = true;
    document.getElementById('dot').classList.add('on');
    document.getElementById('mainBtn').innerHTML = '&#9632; Stop';
    document.getElementById('mainBtn').className = 'btn btn-stop';
    document.getElementById('scanBar').classList.add('on');
    document.getElementById('emptyMsg')?.remove();
    connectStream();
  } else {
    await loadPrevious();
  }
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    import atexit, os, signal, time

    PID_FILE = "/tmp/teltonika-ja-scanner.pid"

    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
        except (ProcessLookupError, ValueError, OSError):
            pass

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))

    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=False)
