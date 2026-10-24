#!/usr/bin/env python3
"""Fill the studio site in for search engines. The page builds its text in the browser from one source
(src/tpf/site/index.html); a crawler that does not run scripts would see an empty page, and no language would
have an address of its own. This writes index.ru.html, index.en.html, index.nl.html and index.zh.html next to it: the same page
with the text already in the HTML and the head tags of that language; and index.root.html, the Russian page
for the bare address, where the visitor's own language is then chosen by the script. Run it after every change to index.html
(tests/test_site_seo.py fails while the copies are stale):

    python scripts/prerender_site.py        # needs `playwright` with Chromium
"""
from __future__ import annotations

import hashlib
import http.server
import json
import re
import threading
from datetime import date
from functools import partial
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "src" / "tpf" / "site"
BASE = "https://tpf-technology.com"
LANGS = {"ru": ("ru", "ru_RU"), "en": ("en", "en_US"), "nl": ("nl", "nl_NL"), "zh": ("zh-Hans", "zh_CN")}     # hreflang, og:locale


def source_hash() -> str:
    return hashlib.sha256((SITE / "index.html").read_bytes()).hexdigest()[:16]


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        if path.split("?")[0].rstrip("/") in ("", "/ru", "/en", "/nl", "/zh"):
            return str(SITE / "index.html")
        return super().translate_path(path)

    def log_message(self, *args: object) -> None:
        pass


def finish(html: str, lang: str, root: bool = False) -> str:
    """Head tags that differ per language, taken from what the page itself rendered."""
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    url = f"{BASE}/" if root else f"{BASE}/{lang}"

    def attr(pattern: str, value: str) -> None:
        nonlocal html
        html, n = re.subn(pattern, lambda m: m.group(1) + value + m.group(2), html, count=1)
        assert n == 1, pattern
    attr(r'(<link rel="canonical" href=")[^"]*(")', url)
    attr(r'(<meta property="og:url" content=")[^"]*(")', url)
    attr(r'(<meta property="og:locale" content=")[^"]*(")', LANGS[lang][1])
    attr(r'(<meta property="og:title" content=")[^"]*(")', title)
    attr(r'(<meta property="og:description" content=")[^"]*(")', desc)
    ld = re.search(r'(<script type="application/ld\+json" id="ld">)(.*?)(</script>)', html, re.S)
    data = json.loads(ld.group(2))
    data["@graph"][0]["description"] = desc.replace("&amp;", "&")
    data["@graph"][1]["inLanguage"] = LANGS[lang][0]
    html = html.replace(ld.group(0), ld.group(1) + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ld.group(3), 1)
    return html.replace("<head>", f'<head>\n<meta name="tpf-source" content="{source_hash()}">', 1)


def main() -> None:
    from playwright.sync_api import sync_playwright
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for lang, name in [("ru", "root"), *((code, code) for code in LANGS)]:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="ru-RU")
            page.route("**/*", lambda route: route.continue_() if "127.0.0.1" in route.request.url else route.abort())  # no web fonts needed
            page.goto(f"http://127.0.0.1:{port}/" + ("" if name == "root" else lang), wait_until="domcontentloaded")
            page.wait_for_function("document.querySelectorAll('.prow').length > 0 && document.title.length > 0")
            html = "<!doctype html>\n" + page.evaluate("document.documentElement.outerHTML") + "\n"
            (SITE / f"index.{name}.html").write_text(finish(html, lang, name == "root"), encoding="utf-8")
            print(name, len(html))
            page.close()
        browser.close()
    server.shutdown()
    (SITE / "lastmod.txt").write_text(date.today().isoformat() + "\n")       # the sitemap's <lastmod>
    import importlib.util                     # the service pages read their prices from the pages just written
    spec = importlib.util.spec_from_file_location("build_service_pages", Path(__file__).with_name("build_service_pages.py"))
    services = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(services)
    services.main()


if __name__ == "__main__":
    main()
