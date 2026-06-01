import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pages.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url         TEXT UNIQUE NOT NULL,
                title       TEXT,
                status      INTEGER,
                crawled_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS text_violations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id         INTEGER NOT NULL,
                url             TEXT NOT NULL,
                page_title      TEXT,
                element_type    TEXT NOT NULL,
                text            TEXT NOT NULL,
                context         TEXT,
                is_product_code INTEGER DEFAULT 0,
                FOREIGN KEY (page_id) REFERENCES pages(id)
            );

            CREATE TABLE IF NOT EXISTS url_violations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                page_id         INTEGER NOT NULL,
                url             TEXT NOT NULL,
                page_title      TEXT,
                link_text       TEXT,
                href            TEXT NOT NULL,
                suggested_fix   TEXT,
                FOREIGN KEY (page_id) REFERENCES pages(id)
            );
        """)


def upsert_page(url: str, title: str, status: int) -> int:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO pages (url, title, status, crawled_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                status=excluded.status,
                crawled_at=excluded.crawled_at
        """, (url, title, status, datetime.utcnow().isoformat()))
        row = conn.execute("SELECT id FROM pages WHERE url=?", (url,)).fetchone()
        return row["id"]


def seed_url(url: str) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO pages (url, title, status, crawled_at)
            VALUES (?, '', -1, ?)
        """, (url, datetime.utcnow().isoformat()))


def get_crawled_urls() -> set:
    with _conn() as conn:
        rows = conn.execute("SELECT url FROM pages WHERE status=200").fetchall()
        return {r["url"] for r in rows}


def get_pending_urls() -> list:
    with _conn() as conn:
        return conn.execute("SELECT url FROM pages WHERE status=-1").fetchall()


def get_failed_urls() -> list:
    with _conn() as conn:
        return conn.execute("SELECT url FROM pages WHERE status=0").fetchall()


def get_all_pages_with_status() -> list:
    with _conn() as conn:
        return conn.execute(
            "SELECT id, url, title, status FROM pages ORDER BY crawled_at"
        ).fetchall()


def clear_page_violations(page_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM text_violations WHERE page_id=?", (page_id,))
        conn.execute("DELETE FROM url_violations WHERE page_id=?", (page_id,))


def clear_all() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM text_violations")
        conn.execute("DELETE FROM url_violations")
        conn.execute("DELETE FROM pages")


def save_text_violation(page_id: int, url: str, page_title: str,
                        element_type: str, text: str, context: str,
                        is_product_code: bool) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO text_violations
                (page_id, url, page_title, element_type, text, context, is_product_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (page_id, url, page_title, element_type, text, context, int(is_product_code)))


def save_url_violation(page_id: int, url: str, page_title: str,
                       link_text: str, href: str, suggested_fix: str) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO url_violations
                (page_id, url, page_title, link_text, href, suggested_fix)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (page_id, url, page_title, link_text, href, suggested_fix))


def get_all_text_violations() -> list:
    with _conn() as conn:
        return conn.execute("""
            SELECT tv.*
            FROM text_violations tv
            ORDER BY tv.url, tv.element_type
        """).fetchall()


def get_all_url_violations() -> list:
    with _conn() as conn:
        return conn.execute("""
            SELECT uv.*
            FROM url_violations uv
            ORDER BY uv.url
        """).fetchall()


def get_violations_by_page() -> dict:
    """Return {url: {text_violations: [...], url_violations: [...]}} for all pages."""
    with _conn() as conn:
        text_rows = conn.execute(
            "SELECT * FROM text_violations ORDER BY url, element_type"
        ).fetchall()
        url_rows = conn.execute(
            "SELECT * FROM url_violations ORDER BY url"
        ).fetchall()

    result: dict = {}
    for r in text_rows:
        result.setdefault(r["url"], {"text_violations": [], "url_violations": []})
        result[r["url"]]["text_violations"].append(dict(r))
    for r in url_rows:
        result.setdefault(r["url"], {"text_violations": [], "url_violations": []})
        result[r["url"]]["url_violations"].append(dict(r))
    return result


def get_stats() -> dict:
    with _conn() as conn:
        total_pages       = conn.execute("SELECT count(*) FROM pages WHERE status=200").fetchone()[0]
        total_text        = conn.execute("SELECT count(*) FROM text_violations").fetchone()[0]
        total_url         = conn.execute("SELECT count(*) FROM url_violations").fetchone()[0]
        product_codes     = conn.execute(
            "SELECT count(*) FROM text_violations WHERE is_product_code=1"
        ).fetchone()[0]
        pages_with_any    = conn.execute("""
            SELECT count(DISTINCT url) FROM (
                SELECT url FROM text_violations
                UNION ALL
                SELECT url FROM url_violations
            )
        """).fetchone()[0]
    return {
        "total_pages":        total_pages,
        "total_text":         total_text,
        "total_url":          total_url,
        "product_code_count": product_codes,
        "pages_flagged":      pages_with_any,
    }
