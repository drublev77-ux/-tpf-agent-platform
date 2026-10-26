"""The studio site for search engines: every language has an address with its text in the HTML, and the
copies are not older than the page they were made from."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tpf.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "src" / "tpf" / "site"
spec = importlib.util.spec_from_file_location("prerender_site", ROOT / "scripts" / "prerender_site.py")
prerender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prerender)


@pytest.mark.parametrize("name,lang,address", [("root", "ru", "/"), ("ru", "ru", "/ru"), ("en", "en", "/en"), ("nl", "nl", "/nl"), ("zh", "zh-CN", "/zh")])
def test_each_language_page_is_filled_in_and_fresh(name, lang, address):
    html = (SITE / f"index.{name}.html").read_text(encoding="utf-8")
    made_from = re.search(r'<meta name="tpf-source" content="([0-9a-f]+)"', html).group(1)
    assert made_from == prerender.source_hash(), "index.html changed: run `python scripts/prerender_site.py`"
    assert f'<html lang="{lang}"' in html
    assert f'<link rel="canonical" href="https://tpf-technology.com{address}">' in html
    for code in ("ru", "en", "nl", "zh-Hans", "x-default"):
        assert f'hreflang="{code}"' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    assert 20 <= len(title) <= 70 and 80 <= len(desc) <= 200 and "TPF Technology" in title
    assert len(re.findall(r"<h1[ >]", html)) == 1 and re.search(r"<h1>.*?\S.*?</h1>", html, re.S)
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
    assert len(text.split()) > 150 if name != "zh" else len(text) > 1500         # the page's text is in the HTML itself
    assert 'id="cases"' not in html and 'href="/en"' in html and 'href="/zh"' in html and 'href="/ru"' in html and 'href="/nl"' in html
    ld = json.loads(re.search(r'<script type="application/ld\+json" id="ld">(.*?)</script>', html, re.S).group(1))
    assert ld["@graph"][0]["@type"] == "Organization" and ld["@graph"][0]["description"] == desc.replace("&amp;", "&")
    assert f'<meta property="og:url" content="https://tpf-technology.com{address}">' in html
    assert '<meta property="og:image" content="https://tpf-technology.com/brand.jpg">' in html
    faq = next(g for g in ld["@graph"] if g["@type"] == "FAQPage")        # the questions block, readable by search engines
    assert len(faq["mainEntity"]) == 6 and html.count("<details>") == 6 and 'id="faq"' in html
    assert all(q["acceptedAnswer"]["text"] and "{" not in q["acceptedAnswer"]["text"] for q in faq["mainEntity"])
    assert re.sub(r"\D", "", ld["@graph"][0]["hasOfferCatalog"]["itemListElement"][0]["priceSpecification"]["minPrice"].__str__()) in re.sub(r"\D", "", faq["mainEntity"][0]["acceptedAnswer"]["text"])
    offers = ld["@graph"][0]["hasOfferCatalog"]["itemListElement"]      # the services and "from" prices the page shows
    assert len(offers) == 8 and [o["priceSpecification"]["minPrice"] for o in offers[:2]] == [700, 880]
    assert all(o["priceSpecification"]["priceCurrency"] == "EUR" and o["itemOffered"]["name"] for o in offers)
    assert 'rel="preload" as="image" href="/brand-hero.jpg"' in html and 'fetchpriority="high"' in html


def test_site_serves_language_pages_robots_and_sitemap(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as client:
        for address, lang in (("/", "ru"), ("/ru", "ru"), ("/en", "en"), ("/nl", "nl"), ("/zh", "zh-CN")):
            page = client.get(address)
            assert page.status_code == 200 and f'<html lang="{lang}"' in page.text
        robots = client.get("/robots.txt").text
        assert "Disallow: /portal/" in robots and "Disallow: /admin" in robots and "Sitemap: https://tpf-technology.com/sitemap.xml" in robots
        sitemap = client.get("/sitemap.xml")
        assert sitemap.headers["content-type"].startswith("application/xml")
        for address in ("/</loc>", "/ru</loc>", "/en</loc>", "/nl</loc>", "/zh</loc>", "/privacy</loc>"):
            assert "https://tpf-technology.com" + address in sitemap.text
        assert "portal" not in sitemap.text
        assert sitemap.text.count("<lastmod>") == 26                       # the privacy page is dated too
        for address in ("/", "/en", "/privacy", "/robots.txt", "/sitemap.xml", "/brand.jpg", "/llms.txt"):
            assert client.head(address).status_code == 200, address           # crawlers ask with HEAD first
        for address in ("/ru/", "/en/", "/nl/", "/zh/", "/privacy/"):
            moved = client.get(address, follow_redirects=False)
            assert moved.status_code == 308 and moved.headers["location"] == "https://tpf-technology.com" + address.rstrip("/")
        assert "orders@tpf-technology.com" in client.get("/llms.txt").text
        changed = set(re.findall(r"<lastmod>(.*?)</lastmod>", sitemap.text))
        assert changed == {(SITE / "lastmod.txt").read_text().strip()} and min(changed) >= "2026-10-06"
    get_settings.cache_clear()


def test_built_sites_ship_the_search_engine_plumbing_and_the_developer_is_held_to_it():
    from conftest import CODE
    from tpf.agents.developer import check_files
    from tpf.agents.qa import SEO_CRITERIA
    from tpf.tools.template import all_files, rules_for, validate_path

    for kind in ("site", "app", "shop", "bot"):
        assert "lib/seo.ts" in rules_for(kind) and "PRIVATE_PAGE" in rules_for(kind)
        paths = {f["path"] for f in all_files(CODE["files"], "", kind)}
        assert {"tpf/seo.ts", "tpf/JsonLd.tsx", "app/robots.ts", "app/sitemap.ts"} <= paths
        # a project written before this has no lib/seo.ts: the files that import it are left out, and it still builds
        assert not {"app/robots.ts", "tpf/seo.ts"} & {f["path"] for f in all_files(CODE["files"][:2], "", kind)}
    assert validate_path("app/robots.ts") and validate_path("app/sitemap.ts", "app") and validate_path("lib/seo.ts") is None
    assert check_files(CODE["files"], ["/"], "site") == []
    assert any("lib/seo.ts" in p for p in check_files(CODE["files"][:2], ["/"], "site"))
    bare = [{"path": "app/layout.tsx", "content": "export default function L(){return null}"}, *CODE["files"][1:]]
    assert any("siteMetadata" in p for p in check_files(bare, ["/"], "site"))
    assert len(SEO_CRITERIA) == 2


@pytest.mark.parametrize("name,choice", [("ru", "Автоматизация процессов"), ("en", "Process automation"), ("nl", "Procesautomatisering"), ("zh", "流程自动化")])
def test_the_site_offers_process_automation_and_the_form_can_ask_for_it(name, choice):
    from tpf.agents.analyst import system_prompt

    html = (SITE / f"index.{name}.html").read_text(encoding="utf-8")
    section = re.search(r'<section id="automation">(.*?)</section>', html, re.S).group(1)
    assert section.count('class="agent"') == 6 and 'id="auto-cta"' in section and 'href="#automation"' in html
    options = re.findall(r"<option>(.*?)</option>", re.search(r'<select name="type" id="types">(.*?)</select>', html, re.S).group(1))
    source = (SITE / "index.html").read_text(encoding="utf-8")
    assert options.index(choice) == int(re.search(r"var AUTO_TYPE = (\d+);", source).group(1))   # the button picks this one
    assert "PROCESS AUTOMATION" in system_prompt(True) and "PROCESS AUTOMATION" not in system_prompt(False)


SERVICE_SLUGS = ("websites", "web-apps", "online-shops", "telegram-bots", "crm-integration")


@pytest.mark.parametrize("lang", ["ru", "en", "nl", "zh"])
def test_each_service_has_a_page_in_each_language_with_the_home_page_prices(lang):
    home = (SITE / f"index.{lang}.html").read_text(encoding="utf-8")
    sums = re.findall(r'<div class="sum">(.*?)</div>', home)
    for slug in SERVICE_SLUGS:
        assert f'href="/{lang}/{slug}"' in home, slug                         # the home page links to it
        page = (SITE / "services" / lang / f"{slug}.html").read_text(encoding="utf-8")
        assert f'<link rel="canonical" href="https://tpf-technology.com/{lang}/{slug}">' in page
        for code in ("ru", "en", "nl", "zh-Hans", "x-default"):
            assert f'hreflang="{code}"' in page
        assert len(re.findall(r"<h1>", page)) == 1 and 'href="/' + lang + '#contact"' in page
        title = re.search(r"<title>(.*?)</title>", page).group(1)
        desc = re.search(r'<meta name="description" content="([^"]*)"', page).group(1)
        assert 15 <= len(title) <= 70 and (40 if lang == "zh" else 80) <= len(desc) <= 200, (slug, len(title), len(desc))  # Chinese says it in fewer characters
        shown = re.findall(r'<div class="sum">(.*?)</div>', page)
        assert shown and set(shown) <= set(sums), (slug, shown)                # the same prices as the home page
        ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
        assert ld["@graph"][0]["@type"] == "Service" and ld["@graph"][0]["offers"]["priceSpecification"]["minPrice"] > 0


def test_service_pages_are_served_and_in_the_sitemap(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as client:
        sitemap = client.get("/sitemap.xml").text
        for lang in ("ru", "en", "nl", "zh"):
            for slug in SERVICE_SLUGS:
                page = client.get(f"/{lang}/{slug}")
                assert page.status_code == 200 and "<h1>" in page.text
                assert f"<loc>https://tpf-technology.com/{lang}/{slug}</loc>" in sitemap
        moved = client.get("/en/websites/", follow_redirects=False)
        assert moved.status_code == 308 and moved.headers["location"] == "https://tpf-technology.com/en/websites"
    get_settings.cache_clear()


def test_google_analytics_waits_for_consent(pool, settings, monkeypatch):
    """GA sets cookies, so it is loaded only after "Accept" (GDPR); every page carries the notice script, the
    saved pages carry no banner, and the privacy policy describes it in every language."""
    script = (SITE / "consent.js").read_text(encoding="utf-8")
    assert "G-7JRVHTTDHW" in script and "navigator.webdriver" in script and "googletagmanager" not in (SITE / "index.html").read_text(encoding="utf-8")
    pages = [SITE / f"index.{n}.html" for n in ("root", "ru", "en", "nl", "zh")] + list((SITE / "services").glob("*/*.html")) + [SITE / "privacy.html"]
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert 'src="/consent.js?v=2"' in html and 'id="tpf-consent"' not in html, page.name
    privacy = (SITE / "privacy.html").read_text(encoding="utf-8")
    assert privacy.count("Google Analytics") >= 8 and privacy.count("tpfConsent.open()") == 4
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as client:
        served = client.get("/consent.js")
        assert served.status_code == 200 and served.headers["content-type"].startswith("text/javascript")
    get_settings.cache_clear()


@pytest.mark.parametrize("name,choose", [("ru", "Выбрать видео"), ("en", "Choose a video"), ("nl", "Video kiezen"), ("zh", "选择视频")])
def test_the_video_field_speaks_the_page_language(name, choose):
    """The browser's own file button is labelled in the browser's language (a Russian browser showed
    "Выберите файл" on the English page); the page labels its own button instead."""
    html = (SITE / f"index.{name}.html").read_text(encoding="utf-8")
    assert f'<span class="pickbtn" data-t="f_choose">{choose}</span>' in html and 'name="video"' in html
