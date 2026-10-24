"""The blog: every finished article is a page of its own, in the language it was written, and the search
engines can find it — served by the app, in the sitemap, one address per page, consent script on board."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tpf.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
BLOG = ROOT / "src" / "tpf" / "site" / "blog"
MANIFEST = json.loads((BLOG / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("article", [a for arts in MANIFEST.values() for a in arts], ids=lambda a: f"{a['lang']}-{a['num']}")
def test_each_article_has_a_page_of_its_own(article):
    page = (BLOG / article["lang"] / f"{article['slug']}.html").read_text(encoding="utf-8")
    url = f"https://tpf-technology.com/{article['lang']}/blog/{article['slug']}"
    assert f'<link rel="canonical" href="{url}">' in page           # one address per page
    assert f'<html lang="{article["lang"]}"' in page
    assert len(re.findall(r"<h1[ >]", page)) == 1 and article["title"] in page
    assert 'src="/consent.js?v=2"' in page                          # the cookie notice script every page carries
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
    assert ld["@graph"][0]["@type"] == "Article"
    assert ld["@graph"][0]["datePublished"] == article["date"] and ld["@graph"][0]["description"] == article["desc"]
    assert ld["@graph"][0]["author"]["name"] == "TPF Technology"
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S)
    assert len(text.split()) > 300                                  # the article is in the HTML itself
    assert "Variant" not in text and "мета-описан" not in text.lower()   # the writing notes stayed out


def test_the_blog_is_served_and_in_the_sitemap(pool, settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    from tpf.api.main import app
    with TestClient(app) as client:
        sitemap = client.get("/sitemap.xml").text
        for lang, arts in MANIFEST.items():
            index = client.get(f"/{lang}/blog")
            assert index.status_code == 200 and "<h1>" in index.text
            assert f"<loc>https://tpf-technology.com/{lang}/blog</loc>" in sitemap
            for a in arts:
                page = client.get(f"/{lang}/blog/{a['slug']}")
                assert page.status_code == 200 and "<h1>" in page.text
                assert f"<loc>https://tpf-technology.com/{lang}/blog/{a['slug']}</loc>" in sitemap
        moved = client.get("/en/blog/", follow_redirects=False)
        assert moved.status_code == 308 and moved.headers["location"] == "https://tpf-technology.com/en/blog"
    get_settings.cache_clear()


def test_every_language_index_links_only_to_its_own_articles():
    for lang, arts in MANIFEST.items():
        index = (BLOG / f"{lang}.html").read_text(encoding="utf-8")
        for a in arts:
            assert f'href="/{lang}/blog/{a["slug"]}"' in index
        other = [a["slug"] for l, arts2 in MANIFEST.items() if l != lang for a in arts2]
        assert not any(f"/blog/{s}" in index for s in other)
