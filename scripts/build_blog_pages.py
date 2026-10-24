#!/usr/bin/env python3
"""A blog for the studio site: a page of its own for every finished article, in the language it was written.

The articles are the markdown files in content/articles (written from the studio's content plan; the same
text also lives in the Notion knowledge base). This writes, per language: a blog index at
src/tpf/site/blog/<lang>.html and one page per article at src/tpf/site/blog/<lang>/<slug>.html, plus
src/tpf/site/blog/manifest.json — the list of pages the FastAPI app then serves from /<lang>/blog and
/<lang>/blog/<slug> and adds to the sitemap (src/tpf/api/main.py). Each page says only what the article
says; the prices, terms and honest limits come from the articles themselves, which were fact-checked.

    python scripts/build_blog_pages.py
"""
from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTICLES = ROOT / "content" / "articles"
SITE = ROOT / "src" / "tpf" / "site"
OUT = SITE / "blog"
BASE = "https://tpf-technology.com"
OG = {"ru": "ru_RU", "en": "en_US", "nl": "nl_NL"}
UI = {  # the few interface words a blog page needs beyond the article itself
    "ru": dict(blog="Блог", home="Главная", read="Читать →", other_h="Другие статьи"),
    "en": dict(blog="Blog", home="Home", read="Read →", other_h="More articles"),
    "nl": dict(blog="Blog", home="Home", read="Lees →", other_h="Meer artikelen"),
}

TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
            "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
            "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
            "э": "e", "ю": "yu", "я": "ya"}


def slugify(text: str) -> str:
    """A short, lowercase, ASCII address part; Russian is transliterated the usual way."""
    s = "".join(TRANSLIT.get(c, c) for c in text.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    words = [w for w in s.split("-") if w not in ("i", "v", "na", "po", "za", "kak", "ili", "the", "a", "of", "for", "to", "in", "de", "het", "een", "dat", "wat", "en")]
    s = "-".join(words) or s
    return s[:64].rstrip("-")


# ---------- inline markdown ----------
def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s in ("---", "***"):
            i += 1
            continue
        if s.startswith("|") and s.endswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append([inline(c) for c in cells])
                i += 1
            if rows:
                body = "".join(f"<tr><td>{c}</td></tr>" for r in rows[1:] for c in r)
                out.append("<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in rows[0]) + f"</tr></thead><tbody>{body}</tbody></table>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", s)
        if m:
            level = min(len(m.group(1)), 4)  # the article's H1 is the page's own; its ## becomes h2
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^[-*]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        m = re.match(r"^(\d+)[.)]\s+(.*)", s)
        if m:
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*\d+[.)]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        if s.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(inline(lines[i].strip().lstrip("> ")))
                i += 1
            out.append("<blockquote><p>" + " ".join(quote) + "</p></blockquote>")
            continue
        para = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,4}\s|[-*]\s|\d+[.)]\s|\||>\s|---$)", lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(para)) + "</p>")
    return "\n".join(out)


# ---------- reading one article ----------
def tail_cut(md: str) -> str:
    """Drop the writing notes (meta descriptions, URL slug) from the end; they are not for the visitor."""
    lines = md.splitlines()
    cut = len(lines)
    for k in range(max(0, len(lines) - 30), len(lines)):
        if re.match(r"^#{2,3}\s", lines[k]) and re.search(r"meta|seo|url|slug|мета|описан", lines[k], re.I):
            cut = k
            break
    while cut > 0 and not lines[cut - 1].strip():
        cut -= 1
    if cut > 0 and lines[cut - 1].strip() in ("---", "***"):
        cut -= 1
    return "\n".join(lines[:cut]).rstrip()


def meta_desc(md: str, title: str) -> str:
    """The author's first meta description; if the article has none, the opening sentences."""
    lines = md.splitlines()
    for k in range(max(0, len(lines) - 30), len(lines)):
        if re.match(r"^#{2,3}\s", lines[k]) and re.search(r"meta|seo|мета", lines[k], re.I):
            for j in range(k + 1, min(len(lines), k + 8)):
                m = re.match(r"^\s*(?:\d+[.)]|[-*])\s*(.+)$", lines[j])
                if m:
                    d = re.sub(r"\*\*|\*|`", "", m.group(1))
                    d = re.sub(r"^(?:Variant|Optie|Option)\s*\d+\s*(\(.*?\))?\s*:?\s*", "", d, flags=re.I)
                    d = d.strip()
                    if len(d) >= 80:
                        return d[:200].rstrip()
    intro = re.sub(r"[#*`]", "", title + ". " + " ".join(tail_cut(md).splitlines()[1:3]))
    intro = re.sub(r"\s+", " ", intro).strip()
    return (intro[:157] + "…") if len(intro) > 158 else intro


def slug_from(md: str, title: str) -> str:
    m = re.search(r"^#{2,3}\s*(?:URL[- ]?s|S)lug\s*$", md, re.I | re.M)
    if m:
        tail = md[m.end():m.end() + 200]
        s = re.search(r"`?([a-z0-9][a-z0-9-]{6,})`?", tail)
        if s and not re.match(r"(meta|seo)", s.group(1)):
            return s.group(1)
    return slugify(title.split(":")[0].split("?")[0])


# ---------- the pages ----------
CSS = """
:root{--bg:#07090C;--line:#16202A;--line2:#2A3844;--ink:#E6EDF2;--soft:#B7C3CC;--muted:#8D9AA5;--accent:#5CE1F0;--pad:clamp(20px,5.5vw,80px)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.65 Manrope,'Noto Sans SC',system-ui,sans-serif}
a{color:var(--accent)}header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:14px var(--pad);border-bottom:1px solid var(--line)}
.logo{font-family:Unbounded,sans-serif;font-weight:700;font-size:18px;color:var(--ink);text-decoration:none}.logo span{font-weight:400;color:var(--accent)}
nav.top{display:flex;gap:18px;font-size:15px}nav.top a{color:var(--soft);text-decoration:none}
main{max-width:820px;margin:0 auto;padding:36px var(--pad) 80px}.crumbs{font-size:14px;color:var(--muted)}.crumbs a{color:var(--muted)}
h1{font-family:Unbounded,'Noto Sans SC',sans-serif;font-weight:500;font-size:clamp(28px,4.5vw,42px);line-height:1.15;margin:16px 0 12px}
h2{font-family:Unbounded,'Noto Sans SC',sans-serif;font-weight:500;font-size:clamp(20px,3vw,26px);margin:48px 0 14px}
h3,h4,h5{font-size:19px;margin:32px 0 8px}p,li{color:var(--soft)}p{margin:0 0 14px}
ul,ol{padding-left:22px;margin:0 0 14px}li{margin:6px 0}
code{font:0.9em ui-monospace,Menlo,monospace;color:var(--ink)}
blockquote{margin:0 0 14px;padding:10px 18px;border-left:3px solid var(--accent);color:var(--soft)}
table{width:100%;border-collapse:collapse;margin:12px 0 20px;font-size:15px}th,td{border:1px solid var(--line2);padding:8px 10px;text-align:left;vertical-align:top}
th{color:var(--ink);background:#0C1319}td{color:var(--soft)}
.meta{font-size:14px;color:var(--muted);margin:0 0 8px}
hr{border:0;border-top:1px solid var(--line);margin:40px 0}
.back{display:inline-block;margin-top:24px;color:var(--accent)}
.index{list-style:none;padding:0;margin:0}.index li{border:1px solid var(--line2);border-radius:16px;padding:20px 22px;margin:14px 0}
.index a{color:var(--ink);text-decoration:none;font-family:Unbounded,sans-serif;font-size:19px;display:block}
.index p{color:var(--muted);font-size:15px;margin:8px 0 0}
footer{padding:28px var(--pad);border-top:1px solid var(--line);color:var(--muted);font-size:14px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}footer a{color:var(--muted)}
@media(max-width:640px){table{display:block;overflow-x:auto}}
"""


def head(lang: str, title: str, desc: str, url: str, ld: str) -> str:
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(desc, quote=True)}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="TPF Technology">
<meta property="og:url" content="{url}">
<meta property="og:locale" content="{OG[lang]}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc, quote=True)}">
<meta property="og:image" content="https://tpf-technology.com/brand.jpg">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">{ld}</script>
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=2">
<meta name="theme-color" content="#07090C">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;500;700&amp;family=Manrope:wght@400;600;700&amp;family=Noto+Sans+SC:wght@400;500&amp;display=swap" rel="stylesheet">
<style>{CSS}</style>"""


def shell(lang: str, title: str, desc: str, url: str, ld: str, body: str, t: dict) -> str:
    return f"""<!doctype html>
<html lang="{lang}">
<head>
{head(lang, title, desc, url, ld)}
</head>
<body>
<script src="/consent.js?v=2" data-tpf-analytics async></script>
<header>
<a class="logo" href="/{lang}">TPF<span>&nbsp;Technology</span></a>
<nav class="top"><a href="/{lang}">{t["home"]}</a><a href="/{lang}/blog" aria-current="page">{t["blog"]}</a></nav>
</header>
<main>
{body}
</main>
<footer><span>© 2026 TPF Technology</span><a href="/{lang}">{t["home"]}</a></footer>
</body>
</html>"""


def article_page(a: dict, lang: str, others: list[dict], t: dict) -> str:
    url = f"{BASE}/{lang}/blog/{a['slug']}"
    title = a["title"] if len(a["title"]) <= 74 else a["title"][:71].rstrip(" ,") + "…"
    ld = json.dumps({"@context": "schema.org", "@graph": [
        {"@type": "Article", "headline": a["title"][:110], "description": a["desc"],
         "inLanguage": lang, "datePublished": a["date"], "dateModified": a["date"],
         "mainEntityOfPage": url, "image": f"{BASE}/brand.jpg",
         "author": {"@type": "Organization", "name": "TPF Technology", "url": f"{BASE}/"},
         "publisher": {"@type": "Organization", "name": "TPF Technology", "url": f"{BASE}/"}},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": t["home"], "item": f"{BASE}/{lang}"},
            {"@type": "ListItem", "position": 2, "name": t["blog"], "item": f"{BASE}/{lang}/blog"},
            {"@type": "ListItem", "position": 3, "name": a["title"][:60], "item": url}]}]},
        ensure_ascii=False, separators=(",", ":"))
    crumbs = f'<p class="crumbs"><a href="/{lang}">{t["home"]}</a> / <a href="/{lang}/blog">{t["blog"]}</a></p>'
    h1 = f"<h1>{inline(a['title'])}</h1>"
    stamp = f'<p class="meta">{a["date"]}</p>'
    body = f"{crumbs}\n{h1}\n{stamp}\n{a['html']}"
    if others:
        more = "".join(f'<li><a href="/{lang}/blog/{o["slug"]}">{inline(o["title"])}</a><p>{html.escape(o["desc"][:140], quote=False)}</p></li>'
                       for o in others[:3])
        body += f'\n<hr>\n<h2 id="other">{t["other_h"]}</h2>\n<ul class="index">{more}</ul>'
    body += f'\n<a class="back" href="/{lang}/blog">← {t["blog"]}</a>'
    return shell(lang, title, a["desc"], url, ld, body, t)


def index_page(lang: str, arts: list[dict], t: dict) -> str:
    url = f"{BASE}/{lang}/blog"
    intro = {"ru": "Статьи студии TPF Technology о разработке сайтов, веб-приложений, интернет-магазинов и Telegram-ботов: честные цены, процесс и практика.",
             "en": "Articles by TPF Technology on websites, web apps, online shops and Telegram bots: honest prices, the process and practice.",
             "nl": "Artikelen van TPF Technology over websites, webapps, webshops en Telegram-bots: eerlijke prijzen, het proces en praktijk."}[lang]
    desc = {"ru": "Блог TPF Technology: сколько стоит сайт или Telegram-бот, как составить ТЗ, автоматизация бизнеса с ИИ. Цены и процесс без сюрпризов.",
            "en": "The TPF Technology blog: what a website, web app or Telegram bot costs, how to write a spec, and automating a business with AI agents.",
            "nl": "De blog van TPF Technology: wat kosten een website, webapp of Telegram-bot, en hoe automatiseert u uw bedrijf met AI-agents?"}[lang]
    ld = json.dumps({"@context": "schema.org", "@graph": [
        {"@type": "Blog", "name": t["blog"] + " — TPF Technology", "description": desc, "inLanguage": lang,
         "url": url, "publisher": {"@type": "Organization", "name": "TPF Technology", "url": f"{BASE}/"}},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": t["home"], "item": f"{BASE}/{lang}"},
            {"@type": "ListItem", "position": 2, "name": t["blog"], "item": url}]}]},
        ensure_ascii=False, separators=(",", ":"))
    crumbs = f'<p class="crumbs"><a href="/{lang}">{t["home"]}</a> / {t["blog"]}</p>'
    items = "".join(f'<li><a href="/{lang}/blog/{a["slug"]}">{inline(a["title"])}</a><p>{html.escape(a["desc"][:160], quote=False)}</p></li>'
                    for a in arts)
    body = f"{crumbs}\n<h1>{t['blog']} TPF Technology</h1>\n<p>{intro}</p>\n<ul class=\"index\">{items}</ul>"
    return shell(lang, f"{t['blog']} — TPF Technology", desc, url, ld, body, t)


def main() -> None:
    today = date.today().isoformat()
    by_lang: dict[str, list[dict]] = {}
    for f in sorted(ARTICLES.glob("article_*.md")):
        num = int(re.match(r"article_(\d+)_", f.name).group(1))
        lang = f.name.split("_")[2]
        if lang not in OG:
            continue
        md = f.read_text(encoding="utf-8")
        title = md.splitlines()[0].lstrip("# ").strip()
        body = tail_cut(md)
        if body.splitlines()[0].startswith("# "):
            body = "\n".join(body.splitlines()[1:])          # the H1 is the page's own, above the text
        a = dict(num=num, lang=lang, title=title, desc=meta_desc(md, title),
                 slug=slug_from(md, title), date=today, html=md_to_html(body))
        assert 80 <= len(a["desc"]) <= 260, (f.name, len(a["desc"]))
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{4,64}", a["slug"]), (f.name, a["slug"])
        assert a["html"].count("<h2>") >= 2, f.name
        by_lang.setdefault(lang, []).append(a)
    slugs: dict[str, set] = {}
    for lang, arts in by_lang.items():
        arts.sort(key=lambda a: a["num"])
        seen = set()
        for a in arts:
            s, n = a["slug"], 2
            while s in seen:                      # two articles may ask for the same address
                s, n = f"{a['slug']}-{n}", n + 1
            a["slug"] = s
            seen.add(s)
        slugs[lang] = seen
        (OUT / lang).mkdir(parents=True, exist_ok=True)
        for a in arts:
            others = [o for o in arts if o["num"] != a["num"]]
            (OUT / lang / f"{a['slug']}.html").write_text(article_page(a, lang, others, UI[lang]), encoding="utf-8")
        (OUT / f"{lang}.html").write_text(index_page(lang, arts, UI[lang]), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(
        {lang: [{k: a[k] for k in ("num", "lang", "slug", "title", "desc", "date")} for a in arts]
         for lang, arts in by_lang.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(*(f"{lang}: {len(arts)} articles" for lang, arts in by_lang.items()))


if __name__ == "__main__":
    main()
