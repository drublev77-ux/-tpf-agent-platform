"""Tell search engines that the studio site changed, without anyone pressing a button.

IndexNow (Yandex, Bing and the engines that share with them) takes a list of changed addresses and crawls them
soon after, instead of whenever its robot next comes by. The worker reports the site's pages once per version of
the site, and only after the public site really serves that version: the worker and the site are published
separately, and a report sent ahead of the site would make the robot fetch the old pages."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import asyncpg
import httpx

from tpf.config import Settings

log = logging.getLogger("tpf.indexnow")

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
SITE_URL = "https://tpf-technology.com"
SERVICE_SLUGS = ("websites", "web-apps", "online-shops", "telegram-bots", "crm-integration")   # a page per kind of work in each language
PAGES = ("", "ru", "en", "nl", "zh", "privacy",     # the addresses in /sitemap.xml (tests keep the two in step)
         *(f"{lang}/{slug}" for lang in ("ru", "en", "nl", "zh") for slug in SERVICE_SLUGS))
# The shared endpoint passes every report on to all IndexNow engines, Yandex included. Yandex's own endpoint was
# dropped on 2026-10-14: since 2026-10-13 it closes the connection from the worker without answering.
ENDPOINTS = ("https://api.indexnow.org/indexnow",)
EVERY_SEC = 900


def key() -> str:
    path = SITE_DIR / "indexnow-key.txt"
    return path.read_text().strip() if path.is_file() else ""


def blog_pages() -> list[str]:
    """The blog index and articles of each language, from the manifest scripts/build_blog_pages.py writes
    (the same list the sitemap is built from)."""
    path = SITE_DIR / "blog" / "manifest.json"
    blog = json.loads(path.read_text()) if path.is_file() else {}
    return [p for lang, arts in blog.items() for p in (f"{lang}/blog", *(f"{lang}/blog/{a['slug']}" for a in arts))]


def urls() -> list[str]:
    return [f"{SITE_URL}/{page}" for page in (*PAGES, *blog_pages())]


def source_hash() -> str:
    """The mark scripts/prerender_site.py puts into every page it writes (<meta name="tpf-source">)."""
    return hashlib.sha256((SITE_DIR / "index.html").read_bytes()).hexdigest()[:16]


def version() -> str:
    """What the site's pages are right now: changes whenever a page a search engine indexes does."""
    privacy = hashlib.sha256((SITE_DIR / "privacy.html").read_bytes()).hexdigest()[:8]
    blog = SITE_DIR / "blog" / "manifest.json"
    if not blog.is_file():
        return f"{source_hash()}-{privacy}"
    return f"{source_hash()}-{privacy}-{hashlib.sha256(blog.read_bytes()).hexdigest()[:8]}"   # a new article is a change too


async def is_live(client: httpx.AsyncClient) -> bool:
    """Does the public site already serve the pages this worker was built with?"""
    try:
        page = await client.get(f"{SITE_URL}/")
    except httpx.HTTPError:
        return False
    return page.status_code == 200 and f'name="tpf-source" content="{source_hash()}"' in page.text


async def submit(pool: asyncpg.Pool, client: httpx.AsyncClient | None = None) -> list[str]:
    """Report the site's pages to every engine that has not heard of this version yet. Returns the endpoints
    that accepted the report now; an engine that refused or did not answer is asked again on the next round."""
    k, v = key(), version()
    if not k:
        return []
    names = {endpoint: f"indexnow:{endpoint}" for endpoint in ENDPOINTS}
    told = {r["key"]: r["value"] for r in await pool.fetch(
        "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])", list(names.values()))}
    pending = [e for e in ENDPOINTS if told.get(names[e]) != v]
    if not pending:
        return []
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
    accepted: list[str] = []
    try:
        if not await is_live(client):
            return []
        body = {"host": SITE_URL.split("//")[1], "key": k, "keyLocation": f"{SITE_URL}/indexnow-{k}.txt", "urlList": urls()}
        for endpoint in pending:
            failure = ""
            try:                            # a redirect is the engine's answer (a captcha, a moved address), not a page to follow
                reply = await client.post(endpoint, json=body, headers={"content-type": "application/json; charset=utf-8"},
                                          follow_redirects=False)
                status = reply.status_code
            except httpx.HTTPError as exc:  # say what broke: "answered 0" alone does not tell a timeout from a reset
                status, failure = 0, f" ({type(exc).__name__}: {exc})"[:300]
            if status in (200, 202):        # 202: accepted, the key file is still being checked
                await pool.execute(
                    "INSERT INTO app_settings (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()", names[endpoint], v)
                accepted.append(endpoint)
                log.info("indexnow: %s accepted %s pages (site version %s)", endpoint, len(body["urlList"]), v)
            else:
                where = reply.headers.get("location", "") if status and 300 <= status < 400 else ""
                log.warning("indexnow: %s answered %s%s%s", endpoint, status, failure, f" -> {where}" if where else "")
    finally:
        if own:
            await client.aclose()
    return accepted


async def run_forever(pool: asyncpg.Pool, settings: Settings, stopping: asyncio.Event) -> None:
    while settings.indexnow and not stopping.is_set():
        try:
            await submit(pool)
        except Exception:  # noqa: BLE001 — a passing database or network error; the next round tries again
            log.exception("indexnow round failed")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=EVERY_SEC)
        except asyncio.TimeoutError:
            pass
