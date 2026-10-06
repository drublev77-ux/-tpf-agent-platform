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


@pytest.mark.parametrize("name,lang,address", [("root", "ru", "/"), ("ru", "ru", "/ru"), ("en", "en", "/en"), ("zh", "zh-CN", "/zh")])
def test_each_language_page_is_filled_in_and_fresh(name, lang, address):
    html = (SITE / f"index.{name}.html").read_text(encoding="utf-8")
    made_from = re.search(r'<meta name="tpf-source" content="([0-9a-f]+)"', html).group(1)
    assert made_from == prerender.source_hash(), "index.html changed: run `python scripts/prerender_site.py`"
    assert f'<html lang="{lang}"' in html
    assert f'<link rel="canonical" href="https://tpf-technology.com{address}">' in html
    for code in ("ru", "en", "zh-Hans", "x-default"):
        assert f'hreflang="{code}"' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    assert 20 <= len(title) <= 70 and 80 <= len(desc) <= 200 and "TPF Technology" in title
    assert len(re.findall(r"<h1[ >]", html)) == 1 and re.search(r"<h1>.*?\S.*?</h1>", html, re.S)
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S)
    assert len(text.split()) > 150 if name != "zh" else len(text) > 1500         # the page's text is in the HTML itself
    assert html.count('class="case"') == 8 and 'href="/en"' in html and 'href="/zh"' in html and 'href="/ru"' in html
    ld = json.loads(re.search(r'<script type="application/ld\+json" id="ld">(.*?)</script>', html, re.S).group(1))
    assert ld["@graph"][0]["@type"] == "Organization" and ld["@graph"][0]["description"] == desc.replace("&amp;", "&")
    assert f'<meta property="og:url" content="https://tpf-technology.com{address}">' in html
    assert '<meta property="og:image" content="https://tpf-technology.com/brand.jpg">' in html


def test_site_serves_language_pages_robots_and_sitemap(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as client:
        for address, lang in (("/", "ru"), ("/ru", "ru"), ("/en", "en"), ("/zh", "zh-CN")):
            page = client.get(address)
            assert page.status_code == 200 and f'<html lang="{lang}"' in page.text
        robots = client.get("/robots.txt").text
        assert "Disallow: /portal/" in robots and "Disallow: /admin" in robots and "Sitemap: https://tpf-technology.com/sitemap.xml" in robots
        sitemap = client.get("/sitemap.xml")
        assert sitemap.headers["content-type"].startswith("application/xml")
        for address in ("/</loc>", "/ru</loc>", "/en</loc>", "/zh</loc>", "/privacy</loc>"):
            assert "https://tpf-technology.com" + address in sitemap.text
        assert "portal" not in sitemap.text
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
