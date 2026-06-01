"""
Page rendering utilities for the Japanese audit crawler.
Imported by app.py — not a standalone CLI.
"""

import asyncio
import re
import sys
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError as PWTimeout

BASE_URL    = "https://www.teltonika-gps.com/ja/"
ALLOWED_HOST = "www.teltonika-gps.com"
PAGE_TIMEOUT = 60_000  # ms

_STATIC_RE = re.compile(
    r"\.(pdf|zip|png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|woff2|ttf|eot)$"
    r"|\?lightbox=|/_partials",
    re.IGNORECASE,
)


# ── URL helpers ───────────────────────────────────────────────────────────────

def normalise(url: str, base: str) -> str:
    full = urljoin(base, url).split("#")[0].rstrip("/")
    parsed = urlparse(full)
    return parsed._replace(fragment="", query="").geturl()


def is_internal(url: str) -> bool:
    return urlparse(url).netloc == ALLOWED_HOST


def is_ja_url(url: str) -> bool:
    path = urlparse(url).path
    return path == "/ja" or path.startswith("/ja/")


def should_skip(url: str) -> bool:
    return bool(_STATIC_RE.search(url))


# ── Sitemap fetching ──────────────────────────────────────────────────────────

async def fetch_sitemap_urls(sitemap_url: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(sitemap_url)
            soup = BeautifulSoup(r.text, "xml")
            child_sitemaps = [
                tag.find("loc").text.strip()
                for tag in soup.find_all("sitemap")
                if tag.find("loc")
            ]
            if child_sitemaps:
                urls: list[str] = []
                for child in child_sitemaps:
                    urls.extend(await fetch_sitemap_urls(child))
                return urls
            return [tag.text.strip() for tag in soup.find_all("loc")]
    except Exception as e:
        print(f"[warn] sitemap {sitemap_url}: {e}", file=sys.stderr)
        return []


# ── Page rendering ────────────────────────────────────────────────────────────

async def dismiss_cookie_banner(page: Page) -> None:
    """Click the accept/allow-all button on common cookie consent overlays."""
    selectors = [
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",  # Cookiebot
        "#onetrust-accept-btn-handler",                             # OneTrust
        "button[id*='accept-all']",
        "button[id*='allow-all']",
        "button[data-cookiebanner='accept_all']",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await asyncio.sleep(0.8)
                return
        except Exception:
            pass


async def scroll_to_load(page: Page) -> None:
    for _ in range(4):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
        await asyncio.sleep(0.4)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await asyncio.sleep(0.5)


async def render_page(page: Page, url: str) -> tuple[str, int]:
    """Navigate to url, return (html, http_status).
    Uses wait_until='load' so background XHR/analytics don't block us.
    On timeout, grabs whatever HTML is already rendered rather than failing."""
    status = 0
    try:
        resp = await page.goto(url, wait_until="load", timeout=PAGE_TIMEOUT)
        status = resp.status if resp else 0
        await asyncio.sleep(5)   # let Wix SPA framework finish rendering
        await dismiss_cookie_banner(page)
        await scroll_to_load(page)
        html = await page.content()
        return html, status
    except PWTimeout:
        print(f"[timeout-partial] {url}", file=sys.stderr)
        # Page may have loaded content before timing out — grab it
        try:
            html = await page.content()
            if html and len(html) > 1000:
                return html, 200
        except Exception:
            pass
        return "", 408
    except Exception as e:
        print(f"[error] {url}: {e}", file=sys.stderr)
        return "", 0


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(strip=True) if soup.title else ""


async def extract_ja_links(page: Page) -> list[str]:
    """Extract /ja/ links from the live Playwright DOM.

    Uses page.evaluate so we capture JavaScript-rendered navigation
    (React/Vue menus, lazy-loaded link lists, etc.) that wouldn't
    appear reliably in a static HTML string.
    """
    try:
        raw_hrefs: list[str] = await page.evaluate("""
            () => [...document.querySelectorAll('a[href]')]
                    .map(a => a.href)
                    .filter(h => h && !h.startsWith('javascript:') && !h.startsWith('mailto:') && !h.startsWith('tel:'))
        """)
    except Exception:
        raw_hrefs = []

    seen: set[str] = set()
    links: list[str] = []
    for href in raw_hrefs:
        try:
            url = normalise(href, href)   # a.href is already absolute
            if is_internal(url) and is_ja_url(url) and not should_skip(url) and url not in seen:
                seen.add(url)
                links.append(url)
        except Exception:
            pass
    return links


async def fetch_all_sitemap_urls() -> list[str]:
    """Try several sitemap patterns and return every /ja/ URL found."""
    candidates = [
        "https://www.teltonika-gps.com/sitemap.xml",
        "https://www.teltonika-gps.com/sitemap_index.xml",
        "https://www.teltonika-gps.com/ja/sitemap.xml",
        "https://www.teltonika-gps.com/ja-sitemap.xml",
        "https://www.teltonika-gps.com/ja_jp-sitemap.xml",
        "https://www.teltonika-gps.com/page-sitemap.xml",
    ]
    found: list[str] = []
    for sm in candidates:
        urls = await fetch_sitemap_urls(sm)
        found.extend(u for u in urls if is_ja_url(u))
    return list(dict.fromkeys(found))  # deduplicate, preserve order
