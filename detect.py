"""
Detection engine: finds English text on Japanese pages and broken (non-/ja/) links.
"""

import re
from urllib.parse import urlparse, urljoin

# ── JavaScript: extract visible text elements from a Playwright page ──────────

_ELEMENTS_JS = """
(function() {
  // Tags where we capture full innerText (incl. all descendants)
  const FULL = new Set([
    'button','a','h1','h2','h3','h4','h5','h6',
    'li','td','th','label','option','figcaption',
    'legend','caption','dt','dd','summary','cite'
  ]);
  // Tags where we capture only direct text nodes (avoids duplicating nested content)
  const CONTAINER = new Set([
    'div','nav','section','article','span','p',
    'header','footer','main','aside','form'
  ]);
  const SKIP = new Set([
    'script','style','noscript','head','iframe',
    'svg','path','g','use','circle','rect',
    'polyline','polygon','line','defs','clippath','mask'
  ]);
  // Cookie consent containers — skip entirely so their children are never visited
  const SKIP_IDS = new Set([
    'CybotCookiebotDialog',    // Cookiebot / Usercentrics
    'onetrust-consent-sdk',    // OneTrust
    'sp_message_container',    // Sourcepoint
    'cookie-law-info-bar',     // Cookie Law Info (WP plugin)
  ]);

  function vis(el) {
    const s = window.getComputedStyle(el);
    // display:contents removes the element's own box but renders children normally — treat as transparent
    if (s.display === 'contents') return true;
    if (s.display === 'none' || s.visibility === 'hidden'
        || parseFloat(s.opacity) <= 0.05 || s.fontSize === '0px') return false;
    // checkVisibility inspects the element AND its full ancestor chain (Chromium 105+)
    if (typeof el.checkVisibility === 'function') {
      if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    }
    // Element must have actual painted dimensions — zero-size elements are not visible to users
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function directText(el) {
    return [...el.childNodes]
      .filter(n => n.nodeType === 3)
      .map(n => n.textContent)
      .join('')
      .replace(/\\s+/g, ' ')
      .trim();
  }

  const out = [], seen = new Set();

  function add(tag, text) {
    text = text.replace(/\\s+/g, ' ').trim();
    if (text.length < 2 || seen.has(text)) return;
    seen.add(text);
    out.push({ tag, text });
  }

  function walk(el) {
    if (!el.tagName) return;
    const t = el.tagName.toLowerCase();
    if (SKIP.has(t) || SKIP_IDS.has(el.id) || !vis(el)) return;

    if (FULL.has(t))      add(t, el.innerText || el.textContent || '');
    else if (CONTAINER.has(t)) add(t, directText(el));

    for (const child of el.children) walk(child);
  }

  walk(document.body);
  return out;
})()
"""

# ── Character classification ──────────────────────────────────────────────────

def _is_japanese(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF or   # hiragana + katakana
        0x4E00 <= cp <= 0x9FFF or   # CJK unified ideographs
        0x3400 <= cp <= 0x4DBF or   # CJK extension A
        0xFF00 <= cp <= 0xFFEF       # fullwidth forms
    )


def _is_latin(ch: str) -> bool:
    cp = ord(ch)
    return (0x41 <= cp <= 0x5A or 0x61 <= cp <= 0x7A or 0xC0 <= cp <= 0x024F)


def _char_counts(text: str) -> tuple[int, int]:
    latin = sum(1 for c in text if _is_latin(c))
    jpn   = sum(1 for c in text if _is_japanese(c))
    return latin, jpn


# ── Product-code / acronym heuristic ─────────────────────────────────────────

_PRODUCT_CODE_RE = re.compile(r'\b[A-Z]{2,}[0-9]+[A-Z0-9]*\b')

_KNOWN_ACRONYMS = {
    "GPS", "LTE", "IOT", "IoT", "SIM", "API", "SDK", "USB", "IP",
    "LED", "CAN", "RS232", "RS485", "OBD", "GNSS", "GSM", "GPRS",
    "4G", "3G", "2G", "WiFi", "Wi-Fi", "MQTT", "TCP", "UDP",
    "HTTP", "HTTPS", "FTP", "JSON", "XML", "CSV", "CPU", "RAM",
    "ROM", "I2C", "SPI", "GPIO", "UART", "NFC", "BLE", "MCU",
    "ECU", "AVL", "VPN", "APN", "AES", "TLS", "SSL", "FW",
    "HW", "SW", "OK", "ID", "FOTA", "OTA", "FM", "FM",
    "DC", "AC", "EU", "CE", "RoHS", "IP67", "IP69", "CMS",
}


def is_product_code(text: str) -> bool:
    """True if the entire text looks like a product model or known technical acronym."""
    words = re.split(r"[\s/,;:·|]+", text.strip())
    if not words:
        return False
    # All words must be product codes or acronyms (or numbers/punctuation)
    for word in words:
        clean = re.sub(r"[^A-Za-z0-9]", "", word)
        if not clean:
            continue
        if clean.upper() in _KNOWN_ACRONYMS:
            continue
        if _PRODUCT_CODE_RE.match(clean):
            continue
        # Pure numeric or very short
        if re.fullmatch(r"[0-9]+[.,]?[0-9]*[A-Za-z%°]*", clean):
            continue
        return False
    return True


# ── English text detection ────────────────────────────────────────────────────

async def extract_elements(page) -> list[dict]:
    """Run the JS extractor on the Playwright page; returns [] on error."""
    try:
        return await page.evaluate(_ELEMENTS_JS) or []
    except Exception:
        return []


def detect_english(elements: list[dict]) -> list[dict]:
    """
    Given extracted DOM elements, return those that contain English text.

    Result keys:
        element_type: str   — HTML tag or attribute name
        text:         str   — the English text
        is_product_code: bool
    """
    violations: list[dict] = []
    for el in elements:
        tag  = el.get("tag", "")
        text = el.get("text", "")
        if not text:
            continue

        latin, jpn = _char_counts(text)

        if latin < 2:
            continue  # no meaningful Latin content

        # Skip if predominantly Japanese (occasional acronyms in Japanese text are OK)
        if jpn > 0 and (latin / (latin + jpn)) < 0.35:
            continue

        violations.append({
            "element_type":    tag,
            "text":            text,
            "is_product_code": is_product_code(text),
        })

    return violations


# ── URL violation detection ───────────────────────────────────────────────────

_STATIC_RE = re.compile(
    r"\.(pdf|zip|png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|woff2|ttf|eot)$",
    re.IGNORECASE,
)
_BASE_DOMAIN = "teltonika-gps.com"


def detect_url_violations(html: str, page_url: str) -> list[dict]:
    """
    Find internal links on a /ja/ page that don't include /ja/ in their path.

    Result keys:
        link_text:     str
        href:          str — the problematic full URL
        suggested_fix: str — URL with /ja/ inserted
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    violations: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        raw = a["href"].strip()
        if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        full = urljoin(page_url, raw).split("#")[0]
        parsed = urlparse(full)

        if _BASE_DOMAIN not in parsed.netloc:
            continue

        path = parsed.path or "/"

        # Already Japanese
        if path == "/ja" or path.startswith("/ja/") or "/ja/" in path:
            continue

        # Static asset
        if _STATIC_RE.search(path):
            continue

        if full in seen:
            continue
        seen.add(full)

        link_text = re.sub(r"\s+", " ", a.get_text()).strip()[:120]
        fixed_path = "/ja" + (path if path.startswith("/") else "/" + path)
        suggested = parsed._replace(path=fixed_path).geturl()

        violations.append({
            "link_text":     link_text or raw[:80],
            "href":          full,
            "suggested_fix": suggested,
        })

    return violations
