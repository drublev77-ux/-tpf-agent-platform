#!/usr/bin/env python3
"""A page of its own for each kind of work the studio does, in every language of the site.

The home page answers "what does TPF Technology do"; these answer the narrower questions people search for
("order a Telegram bot", "webshop laten maken"). They say only what the home page says: the "from" prices and
the demo projects are read from the filled-in home pages (index.<lang>.html), so a price change on the home page
reaches these pages the next time scripts/prerender_site.py runs (it calls this script at the end).

    python scripts/build_service_pages.py
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent / "src" / "tpf" / "site"
OUT = SITE / "services"
BASE = "https://tpf-technology.com"
LANGS = {"ru": ("ru", "ru_RU"), "en": ("en", "en_US"), "nl": ("nl", "nl_NL"), "zh": ("zh-Hans", "zh_CN")}
SLUGS = ("websites", "web-apps", "online-shops", "telegram-bots")

# Positions in the home page's price list and project list (index.html: prices[] and CASES[]).
PRICES = {"websites": (0, 1), "web-apps": (2, 3, 5), "online-shops": (4,), "telegram-bots": (6, 7)}
CASES = {"websites": (7, 6), "web-apps": (3, 0, 4), "online-shops": (2,), "telegram-bots": (5,)}

UI = {
    "ru": dict(home="Главная", price_h="Цены", price_note="Точная цена — в смете: бесплатно и до оплаты.",
               includes_h="Что входит", how_h="Как мы работаем", cases_h="Примеры наших работ", other_h="Другие услуги",
               cta="Оставить заявку", cta_h="Расскажите о задаче",
               cta_lead="Опишите задачу — ТЗ, смету и сроки пришлём на почту, обычно в течение часа. Бесплатно.",
               privacy="Политика обработки данных", footer="© 2026 TPF Technology. Все права защищены.",
               steps=["Вы описываете задачу в форме на сайте или письмом на orders@tpf-technology.com — можно приложить видео.",
                      "Мы присылаем ТЗ, смету и сроки, обычно в течение часа. Это бесплатно и до оплаты.",
                      "Вы оплачиваете половину суммы — команда ИИ‑агентов под контролем инженеров разрабатывает и проверяет проект.",
                      "Вы смотрите версию для проверки на странице заказа и при необходимости запрашиваете правки.",
                      "Вы принимаете работу и оплачиваете вторую половину. Мы запускаем проект, исходный код и все права — ваши."]),
    "en": dict(home="Home", price_h="Prices", price_note="The exact price is in the estimate: free and before you pay.",
               includes_h="What you get", how_h="How we work", cases_h="Examples of our work", other_h="Other services",
               cta="Send a request", cta_h="Tell us about your task",
               cta_lead="Describe the task — we will e‑mail the spec, estimate and timeline, usually within an hour. Free of charge.",
               privacy="Privacy policy", footer="© 2026 TPF Technology. All rights reserved.",
               steps=["You describe the task in the form on the site or by e‑mail to orders@tpf-technology.com — you can attach a video.",
                      "We send the spec, estimate and timeline, usually within an hour. It is free and comes before any payment.",
                      "You pay half — a team of AI agents overseen by engineers builds and checks the project.",
                      "You review the version on your order page and request changes if needed.",
                      "You accept the work and pay the second half. We launch the project; the source code and all rights are yours."]),
    "nl": dict(home="Home", price_h="Prijzen", price_note="De exacte prijs staat in de offerte: gratis en vóór betaling.",
               includes_h="Wat u krijgt", how_h="Zo werken wij", cases_h="Voorbeelden van ons werk", other_h="Andere diensten",
               cta="Aanvraag versturen", cta_h="Vertel ons over uw opdracht",
               cta_lead="Beschrijf de opdracht — wij mailen de specificatie, offerte en planning, meestal binnen een uur. Gratis. "
                        "U mag in het Nederlands schrijven; wij antwoorden in het Engels.",
               privacy="Privacybeleid", footer="© 2026 TPF Technology. Alle rechten voorbehouden.",
               steps=["U beschrijft de opdracht in het formulier op de site of per e‑mail aan orders@tpf-technology.com — u kunt een video bijvoegen.",
                      "Wij sturen de specificatie, offerte en planning, meestal binnen een uur. Gratis en vóór elke betaling.",
                      "U betaalt de helft — een team van AI‑agents onder toezicht van engineers bouwt en test het project.",
                      "U bekijkt de testversie op uw opdrachtpagina en vraagt zo nodig wijzigingen aan.",
                      "U keurt het werk goed en betaalt de tweede helft. Wij zetten het project live; de broncode en alle rechten zijn van u."]),
    "zh": dict(home="首页", price_h="价格", price_note="准确价格见报价：免费，并在付款前提供。",
               includes_h="服务内容", how_h="合作流程", cases_h="作品示例", other_h="其他服务",
               cta="提交需求", cta_h="告诉我们您的需求",
               cta_lead="描述您的需求——我们会把规格书、报价和工期发送到您的邮箱，通常在一小时内。完全免费。",
               privacy="隐私政策", footer="© 2026 TPF Technology。保留所有权利。",
               steps=["您在网站表单中描述需求，或发送邮件至 orders@tpf-technology.com——可以附上视频。",
                      "我们发送规格书、报价和工期，通常在一小时内。免费，并在付款前提供。",
                      "您支付一半费用——由工程师监督的 AI 智能体团队开发并检查项目。",
                      "您在订单页面查看审核版本，如有需要可申请修改。",
                      "您验收成果并支付另一半。我们上线项目，源代码及全部权利归您所有。"]),
}

PAGES = {
    "websites": {
        "ru": dict(name="Сайты и лендинги", title="Создание сайтов и лендингов под ключ — TPF Technology",
                   desc="Лендинги и сайты из нескольких страниц под ключ: дизайн, тексты по вашим фактам, мобильная вёрстка, форма заявки на почту. Смета бесплатно, код ваш.",
                   h1="Создание сайтов и лендингов под ключ",
                   lead="Делаем лендинги и сайты из нескольких страниц силами команды ИИ‑агентов под контролем инженеров — от заявки до опубликованного сайта.",
                   includes=["Одна страница или несколько: услуги, цены, контакты", "Дизайн и тексты по вашим фактам",
                             "Вёрстка под телефон", "Форма заявки, которая приходит вам на почту", "Публикация сайта; исходный код и права — у вас"]),
        "en": dict(name="Websites and landing pages", title="Website and landing page development — TPF Technology",
                   desc="Landing pages and multi-page websites built for you: design, copy from your facts, mobile layout and a request form sent to your e-mail. Free estimate.",
                   h1="Website and landing page development",
                   lead="We build landing pages and multi‑page websites with a team of AI agents overseen by engineers — from your request to a published site.",
                   includes=["One page or several: services, prices, contacts", "Design and copy written from your facts",
                             "Mobile layout", "A request form delivered to your e‑mail", "Publishing; the source code and all rights are yours"]),
        "nl": dict(name="Websites en landingspagina’s", title="Website of landingspagina laten maken — TPF Technology",
                   desc="Landingspagina’s en websites met meerdere pagina’s: ontwerp, teksten op basis van uw gegevens, mobiele weergave en een aanvraagformulier. Gratis offerte.",
                   h1="Website of landingspagina laten maken",
                   lead="Wij bouwen landingspagina’s en websites met meerdere pagina’s met een team van AI‑agents onder toezicht van engineers — van aanvraag tot livegang.",
                   includes=["Eén pagina of meerdere: diensten, prijzen, contact", "Ontwerp en teksten op basis van uw gegevens",
                             "Mobiele weergave", "Een aanvraagformulier dat in uw mailbox binnenkomt", "Livegang; de broncode en alle rechten zijn van u"]),
        "zh": dict(name="网站与落地页", title="网站与落地页开发 — TPF Technology",
                   desc="落地页与多页网站开发：设计、根据您提供的信息撰写文案、手机端适配，以及发送到您邮箱的咨询表单。报价免费，源代码归您所有。",
                   h1="网站与落地页开发",
                   lead="由工程师监督的 AI 智能体团队为您开发落地页和多页网站——从提交需求到网站上线。",
                   includes=["单页或多页：服务、价格、联系方式", "根据您提供的信息进行设计和撰写文案",
                             "手机端适配", "发送到您邮箱的咨询表单", "网站上线；源代码及全部权利归您所有"]),
    },
    "web-apps": {
        "ru": dict(name="Веб‑приложения", title="Разработка веб‑приложений под ключ — TPF Technology",
                   desc="Веб‑приложения с базой данных и админкой: запись, заявки, каталог, расписание, вход с ролями, установка на телефон. Смета бесплатно, код ваш.",
                   h1="Разработка веб‑приложений",
                   lead="Сайт с базой данных и админкой, где вы сами ведёте данные без программиста: запись, заявки, каталог, расписание. Сотрудники или клиенты могут входить со своими ролями.",
                   includes=["База данных и админка для ваших данных", "Вход по e‑mail и паролю: сотрудник видит свои задачи, руководитель — всё",
                             "Установка на телефон: своя иконка и полный экран, без магазинов приложений",
                             "Каждая заявка или запись — вам на почту и через вебхук в ваш n8n, Zapier, Make или CRM",
                             "Проверка на тестовой версии до окончательной оплаты"]),
        "en": dict(name="Web applications", title="Web application development — TPF Technology",
                   desc="Web applications with a database and an admin area: bookings, requests, a catalogue, a schedule, sign-in with roles, installs on the phone. Free estimate.",
                   h1="Web application development",
                   lead="A site with a database and an admin area where you manage the data yourself, without a developer: bookings, requests, a catalogue, a schedule. Staff or clients can sign in, each with their own role.",
                   includes=["A database and an admin area for your data", "Sign‑in with e‑mail and password: a worker sees their own tasks, a manager sees everything",
                             "Installs on the phone: its own icon, full screen, no app stores",
                             "Every request or booking is e‑mailed to you and sent by webhook to your n8n, Zapier, Make or CRM",
                             "You check the review version before the final payment"]),
        "nl": dict(name="Webapplicaties", title="Webapplicatie laten maken — TPF Technology",
                   desc="Webapplicaties met een database en beheeromgeving: afspraken, aanvragen, catalogus, rooster, inloggen met rollen, installeren op de telefoon. Gratis offerte.",
                   h1="Webapplicatie laten maken",
                   lead="Een site met een database en een beheeromgeving waarin u de gegevens zelf beheert, zonder ontwikkelaar: afspraken, aanvragen, een catalogus, een rooster. Medewerkers of klanten kunnen inloggen met hun eigen rol.",
                   includes=["Een database en een beheeromgeving voor uw gegevens", "Inloggen met e‑mail en wachtwoord: een medewerker ziet zijn eigen taken, een manager alles",
                             "Installeert op de telefoon: eigen icoon, schermvullend, zonder appstores",
                             "Elke aanvraag of reservering wordt naar u gemaild en via een webhook naar uw n8n, Zapier, Make of CRM gestuurd",
                             "U controleert de testversie vóór de laatste betaling"]),
        "zh": dict(name="网络应用", title="网络应用开发 — TPF Technology",
                   desc="带数据库和后台的网络应用：预约、咨询、目录、课程表、按角色登录，可安装到手机。报价免费，源代码归您所有。",
                   h1="网络应用开发",
                   lead="带数据库和后台的网站，您无需程序员即可自行管理数据：预约、咨询、目录、课程表。员工或客户可按各自角色登录。",
                   includes=["数据库和管理后台", "邮箱和密码登录：员工只看自己的任务，经理可查看全部",
                             "可安装到手机：独立图标、全屏显示，无需应用商店",
                             "每条咨询或预约都会发送到您的邮箱，并通过 webhook 推送到您的 n8n、Zapier、Make 或 CRM",
                             "支付尾款前先在审核版本上验收"]),
    },
    "online-shops": {
        "ru": dict(name="Интернет‑магазины", title="Создание интернет‑магазина под ключ — TPF Technology",
                   desc="Интернет‑магазин под ключ: каталог, корзина, оформление заказа, учёт остатков и оплата картой — деньги покупателей идут сразу вам. Смета бесплатно.",
                   h1="Создание интернет‑магазина",
                   lead="Каталог, корзина, оформление заказа, учёт остатков и оплата картой. Товары и заказы — в вашей админке, а деньги покупателей идут сразу вам, а не через нас.",
                   includes=["Каталог с категориями", "Корзина и оформление заказа", "Учёт остатков",
                             "Оплата картой через ваш аккаунт Stripe, ЮKassa или bePaid", "Админка для товаров и заказов"]),
        "en": dict(name="Online shops", title="Online shop development — TPF Technology",
                   desc="An online shop built for you: catalogue, cart, checkout, stock tracking and card payments — your customers' money goes straight to you. Free estimate.",
                   h1="Online shop development",
                   lead="Catalogue, cart, checkout, stock tracking and card payment. Products and orders live in your admin area, and your customers’ money goes straight to you, not through us.",
                   includes=["A catalogue with categories", "Cart and checkout", "Stock tracking",
                             "Card payments into your own Stripe, YooKassa or bePaid account", "An admin area for products and orders"]),
        "nl": dict(name="Webshops", title="Webshop laten maken — TPF Technology",
                   desc="Een webshop op maat: catalogus, winkelwagen, afrekenen, voorraadbeheer en kaartbetaling — het geld van uw klanten gaat rechtstreeks naar u. Gratis offerte.",
                   h1="Webshop laten maken",
                   lead="Catalogus, winkelwagen, afrekenen, voorraadbeheer en kaartbetaling. Producten en bestellingen staan in uw beheeromgeving, en het geld van uw klanten gaat rechtstreeks naar u, niet via ons.",
                   includes=["Een catalogus met categorieën", "Winkelwagen en afrekenen", "Voorraadbeheer",
                             "Kaartbetaling via uw eigen Stripe‑account", "Een beheeromgeving voor producten en bestellingen"]),
        "zh": dict(name="网上商店", title="网上商店开发 — TPF Technology",
                   desc="网上商店开发：商品目录、购物车、下单、库存管理和刷卡支付——顾客的付款直接进入您的账户。报价免费，源代码归您所有。",
                   h1="网上商店开发",
                   lead="商品目录、购物车、下单、库存管理和刷卡支付。商品和订单在您的后台管理，顾客的付款直接进入您的账户，而不经过我们。",
                   includes=["带分类的商品目录", "购物车与下单", "库存管理",
                             "通过您自己的 Stripe、YooKassa 或 bePaid 账户刷卡收款", "管理商品和订单的后台"]),
    },
    "telegram-bots": {
        "ru": dict(name="Telegram‑боты", title="Разработка Telegram‑ботов под ключ — TPF Technology",
                   desc="Telegram‑боты под ключ: меню из кнопок, ответы на частые вопросы, запись и заказы по шагам, уведомления владельцу и веб‑админка. Смета бесплатно.",
                   h1="Разработка Telegram‑ботов",
                   lead="Бот с меню из кнопок отвечает на частые вопросы, собирает заявки и записывает клиентов по шагам, а вам приходят уведомления. Записи и тексты вы ведёте в веб‑админке.",
                   includes=["Меню из кнопок и ответы на частые вопросы", "Контакты и сбор заявок", "Запись или заказ по шагам",
                             "Уведомления владельцу", "Веб‑админка для данных и текстов бота"]),
        "en": dict(name="Telegram bots", title="Telegram bot development — TPF Technology",
                   desc="Telegram bots built for you: a button menu, answers to frequent questions, step-by-step booking or ordering, owner notifications and a web admin area.",
                   h1="Telegram bot development",
                   lead="A bot with a button menu answers frequent questions, collects requests and books clients step by step, and you get notified. You manage bookings and texts in a web admin area.",
                   includes=["A button menu and answers to frequent questions", "Contacts and collecting requests", "Step‑by‑step booking or ordering",
                             "Notifications to the owner", "A web admin area for the bot’s data and texts"]),
        "nl": dict(name="Telegram‑bots", title="Telegram‑bot laten maken — TPF Technology",
                   desc="Telegram-bots op maat: een knoppenmenu, antwoorden op veelgestelde vragen, stap voor stap reserveren of bestellen, meldingen en een webbeheeromgeving.",
                   h1="Telegram‑bot laten maken",
                   lead="Een bot met een knoppenmenu beantwoordt veelgestelde vragen, verzamelt aanvragen en reserveert stap voor stap, en u krijgt een melding. Reserveringen en teksten beheert u in een webbeheeromgeving.",
                   includes=["Een knoppenmenu en antwoorden op veelgestelde vragen", "Contactgegevens en aanvragen verzamelen", "Stap voor stap reserveren of bestellen",
                             "Meldingen aan de eigenaar", "Een webbeheeromgeving voor de gegevens en teksten van de bot"]),
        "zh": dict(name="Telegram 机器人", title="Telegram 机器人开发 — TPF Technology",
                   desc="Telegram 机器人开发：按钮菜单、常见问题解答、分步预约或下单、店主通知以及网页后台。报价免费，源代码归您所有。",
                   h1="Telegram 机器人开发",
                   lead="带按钮菜单的机器人回答常见问题、收集咨询并分步为客户预约，同时通知您。预约和文本在网页后台管理。",
                   includes=["按钮菜单与常见问题解答", "联系方式与收集咨询", "分步预约或下单",
                             "通知店主", "管理机器人数据和文本的网页后台"]),
    },
}

STYLE = """:root{--bg:#07090C;--line:#16202A;--line2:#2A3844;--ink:#E6EDF2;--soft:#B7C3CC;--muted:#8D9AA5;--accent:#5CE1F0;--pad:clamp(20px,5.5vw,80px)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.65 Manrope,'Noto Sans SC',system-ui,sans-serif}
a{color:var(--accent)}header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:14px var(--pad);border-bottom:1px solid var(--line)}
.logo{font-family:Unbounded,sans-serif;font-weight:700;font-size:18px;color:var(--ink);text-decoration:none}.logo span{font-weight:400;color:var(--accent)}
.langs{display:flex;gap:2px;padding:3px;border:1px solid var(--line2);border-radius:999px}
.langs a{display:inline-flex;align-items:center;height:36px;padding:0 12px;border-radius:999px;color:var(--soft);text-decoration:none;font:500 13px ui-monospace,Menlo,monospace}
.langs a[aria-current=true]{background:var(--accent);color:#04161A}
main{max-width:980px;margin:0 auto;padding:40px var(--pad) 80px}.crumbs{font-size:14px;color:var(--muted)}.crumbs a{color:var(--muted)}
h1{font-family:Unbounded,'Noto Sans SC',sans-serif;font-weight:500;font-size:clamp(30px,5vw,48px);line-height:1.12;margin:18px 0 16px}
h2{font-family:Unbounded,'Noto Sans SC',sans-serif;font-weight:500;font-size:clamp(22px,3vw,28px);margin:56px 0 16px}
h3{font-size:19px;margin:0 0 6px}.lead{font-size:clamp(18px,2vw,21px);color:var(--soft);max-width:760px}p,li{color:var(--soft)}
.btn{display:inline-flex;align-items:center;height:54px;padding:0 28px;border-radius:999px;background:var(--accent);color:#04161A;font-weight:700;text-decoration:none;margin-top:12px}
.prices{border-top:1px solid var(--line2)}.price{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:6px 32px;padding:20px 0;border-bottom:1px solid var(--line2)}
.price p{margin:0}.sum{font-family:Unbounded,sans-serif;font-size:22px;color:var(--ink)}.alt{color:var(--muted);font-size:15px}.note{color:var(--muted);font-size:15px}
ul.inc{padding-left:22px}ul.inc li{margin:6px 0}ol.steps{padding-left:24px}ol.steps li{margin:10px 0}
.cases{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}.case{border:1px solid var(--line2);border-radius:16px;overflow:hidden;text-decoration:none;color:inherit;display:block}
.case img{width:100%;height:auto;display:block}.case div{padding:16px}.case span{color:var(--accent);font-size:15px}
.other{display:flex;flex-wrap:wrap;gap:10px}.other a{border:1px solid var(--line2);border-radius:999px;padding:8px 16px;text-decoration:none;color:var(--ink)}
.cta{margin-top:56px;padding:32px;border:1px solid var(--line2);border-radius:20px}
footer{padding:28px var(--pad);border-top:1px solid var(--line);color:var(--muted);font-size:14px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}footer a{color:var(--muted)}
@media(max-width:640px){.price{grid-template-columns:1fr}}"""


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def home_facts(lang: str) -> tuple[list[dict], list[dict]]:
    """The price list and the demo projects as the filled-in home page of this language shows them."""
    page = (SITE / f"index.{lang}.html").read_text(encoding="utf-8")
    prices = [dict(title=html.unescape(t), text=html.unescape(p), sum=html.unescape(s), alt=html.unescape(a or ""))
              for t, p, s, a in re.findall(r'<div class="prow"><h3>(.*?)</h3><p>(.*?)</p><div class="cost"><div class="sum">(.*?)</div>'
                                           r'(?:<div class="alt">(.*?)</div>)?', page)]
    cases = [dict(href=href or "", img=img, title=html.unescape(t), text=html.unescape(p))
             for href, img, t, p in re.findall(r'<(?:a|div) class="case"(?: href="([^"]*)")?[^>]*><img src="([^"]+)"[^>]*>'
                                               r'<div><div class="mono casetag">.*?</div><h3>(.*?)</h3><p>(.*?)</p>', page)]
    assert len(prices) >= 8 and len(cases) == 8, (lang, len(prices), len(cases))
    return prices, cases


def url(lang: str, slug: str) -> str:
    return f"{BASE}/{lang}/{slug}"


def build(lang: str, slug: str, prices: list[dict], cases: list[dict]) -> str:
    ui, c = UI[lang], PAGES[slug][lang]
    hreflang, locale = LANGS[lang]
    alternates = "".join(f'<link rel="alternate" hreflang="{LANGS[l][0]}" href="{url(l, slug)}">\n' for l in LANGS) \
        + f'<link rel="alternate" hreflang="x-default" href="{url("en", slug)}">\n'
    langs = "".join(f'<a href="/{l}/{slug}" hreflang="{LANGS[l][0]}" aria-current="{str(l == lang).lower()}">{"中文" if l == "zh" else l.upper()}</a>'
                    for l in LANGS)
    rows = [prices[i] for i in PRICES[slug]]
    price_html = "".join(f'<div class="price"><div><h3>{esc(p["title"])}</h3><p>{esc(p["text"])}</p></div>'
                         f'<div><div class="sum">{esc(p["sum"])}</div>' + (f'<div class="alt">{esc(p["alt"])}</div>' if p["alt"] else "")
                         + "</div></div>" for p in rows)
    case_html = "".join(
        (f'<a class="case" href="{esc(x["href"])}" target="_blank" rel="noopener">' if x["href"] else '<div class="case">')
        + f'<img src="{esc(x["img"])}" alt="{esc(x["title"])}" width="960" height="600" loading="lazy"><div><h3>{esc(x["title"])}</h3>'
        f'<p>{esc(x["text"])}</p></div>' + ("</a>" if x["href"] else "</div>") for x in (cases[i] for i in CASES[slug]))
    other = "".join(f'<a href="/{lang}/{s}">{esc(PAGES[s][lang]["name"])}</a>' for s in SLUGS if s != slug)
    first = int(re.sub(r"\D", "", rows[0]["sum"]) or 0)
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "Service", "name": c["h1"], "description": c["desc"], "url": url(lang, slug), "inLanguage": hreflang,
         "serviceType": c["name"], "provider": {"@type": "Organization", "@id": f"{BASE}/#org", "name": "TPF Technology", "url": f"{BASE}/"},
         "offers": {"@type": "Offer", "priceSpecification": {"@type": "PriceSpecification", "minPrice": first, "priceCurrency": "EUR"}}},
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": ui["home"], "item": f"{BASE}/{lang}"},
            {"@type": "ListItem", "position": 2, "name": c["name"], "item": url(lang, slug)}]}]}
    return f"""<!doctype html>
<html lang="{'zh-CN' if lang == 'zh' else lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(c["title"])}</title>
<meta name="description" content="{esc(c["desc"])}">
<link rel="canonical" href="{url(lang, slug)}">
{alternates}<meta property="og:type" content="website">
<meta property="og:site_name" content="TPF Technology">
<meta property="og:url" content="{url(lang, slug)}">
<meta property="og:locale" content="{locale}">
<meta property="og:title" content="{esc(c["title"])}">
<meta property="og:description" content="{esc(c["desc"])}">
<meta property="og:image" content="{BASE}/brand.jpg">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False, separators=(",", ":"))}</script>
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=2">
<meta name="theme-color" content="#07090C">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;500;700&amp;family=Manrope:wght@400;600;700&amp;family=Noto+Sans+SC:wght@400;500&amp;display=swap" rel="stylesheet">
<style>
{STYLE}
</style>
<script>window.va=window.va||function(){{(window.vaq=window.vaq||[]).push(arguments)}};</script>
<script defer src="/_vercel/insights/script.js"></script>
<script defer src="/consent.js?v=1"></script>
</head>
<body>
<header><a class="logo" href="/{lang}">TPF<span>technology</span></a><nav class="langs" aria-label="Language">{langs}</nav></header>
<main>
<div class="crumbs"><a href="/{lang}">{esc(ui["home"])}</a> / {esc(c["name"])}</div>
<h1>{esc(c["h1"])}</h1>
<p class="lead">{esc(c["lead"])}</p>
<a class="btn" href="/{lang}#contact">{esc(ui["cta"])}</a>
<h2>{esc(ui["price_h"])}</h2>
<div class="prices">{price_html}</div>
<p class="note">{esc(ui["price_note"])}</p>
<h2>{esc(ui["includes_h"])}</h2>
<ul class="inc">{"".join(f"<li>{esc(x)}</li>" for x in c["includes"])}</ul>
<h2>{esc(ui["how_h"])}</h2>
<ol class="steps">{"".join(f"<li>{esc(x)}</li>" for x in ui["steps"])}</ol>
<h2>{esc(ui["cases_h"])}</h2>
<div class="cases">{case_html}</div>
<h2>{esc(ui["other_h"])}</h2>
<div class="other">{other}</div>
<div class="cta"><h2 style="margin-top:0">{esc(ui["cta_h"])}</h2><p>{esc(ui["cta_lead"])}</p>
<a class="btn" href="/{lang}#contact">{esc(ui["cta"])}</a> &nbsp; <a href="mailto:orders@tpf-technology.com">orders@tpf-technology.com</a></div>
</main>
<footer><span>{esc(ui["footer"])}</span><a href="/privacy">{esc(ui["privacy"])}</a></footer>
</body>
</html>
"""


def main() -> None:
    for lang in LANGS:
        prices, cases = home_facts(lang)
        (OUT / lang).mkdir(parents=True, exist_ok=True)
        for slug in SLUGS:
            (OUT / lang / f"{slug}.html").write_text(build(lang, slug, prices, cases), encoding="utf-8")
    print(f"service pages: {len(LANGS) * len(SLUGS)}")


if __name__ == "__main__":
    main()
