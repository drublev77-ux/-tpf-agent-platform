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
SLUGS = ("websites", "web-apps", "online-shops", "telegram-bots", "crm-integration")

# Positions in the home page's price list (index.html: prices[]).
PRICES = {"websites": (0, 1), "web-apps": (2, 3, 5), "online-shops": (4,), "telegram-bots": (6, 7), "crm-integration": (4,)}

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
    "crm-integration": {
        "ru": dict(name="Интеграция с CRM", title="Интеграция магазина с CRM и учётом — TPF Technology",
                   desc="Связываем магазин с CRM, учётом и n8n через открытые API: заказы попадают в CRM сами, статусы и остатки синхронны, письма уходят без ручной работы. Смета бесплатно.",
                   h1="Интеграция магазина с CRM и учётом",
                   lead="Заказы, клиенты и оплаты передаются между вашим магазином, CRM и учётом автоматически — через открытые API. Вам остаётся проверять результат.",
                   includes=["Заказы попадают в CRM сами: amoCRM, Bitrix24, HubSpot и другие сервисы с открытым API",
                             "Статусы синхронизируются: отправили заказ — статус обновился, клиенту ушло письмо",
                             "Остатки актуальны: товар закончился — кнопка покупки спрятана автоматически",
                             "Сценарии автоматизации n8n, Zapier, Make: заявки уходят по вебхуку",
                             "Выгрузки в Excel и загрузки из него, документы в PDF в одно нажатие",
                             "Без прямых банков, государственных систем и сервисов без открытого API"]),
        "en": dict(name="CRM integration", title="Online shop CRM integration — TPF Technology",
                   desc="We connect your shop to a CRM, accounting and n8n through open APIs: orders reach the CRM on their own, statuses and stock stay in sync, e-mails go out without manual work.",
                   h1="Shop and CRM integration",
                   lead="Orders, customers and payments flow between your shop, CRM and accounting automatically — through open APIs. All you do is check the result.",
                   includes=["Orders reach the CRM on their own: amoCRM, Bitrix24, HubSpot and other services with an open API",
                             "Statuses stay in sync: an order ships — the status updates and the customer gets an e-mail",
                             "Stock is accurate: an item runs out and the buy button hides itself",
                             "n8n, Zapier and Make scenarios: requests flow in by webhook",
                             "Excel exports and imports, PDF documents in one click",
                             "No direct bank connections, government systems or services without an open API"]),
        "nl": dict(name="CRM-integratie", title="Webshop en CRM-integratie — TPF Technology",
                   desc="Wij koppelen uw webshop aan een CRM, boekhouding en n8n via open API’s: bestellingen komen vanzelf in de CRM, statussen en voorraad blijven synchroon, e‑mails gaan automatisch.",
                   h1="Webshop en CRM-integratie",
                   lead="Bestellingen, klanten en betalingen lopen automatisch tussen uw webshop, CRM en boekhouding — via open API’s. U hoeft alleen het resultaat te controleren.",
                   includes=["Bestellingen komen vanzelf in de CRM: amoCRM, Bitrix24, HubSpot en andere diensten met een open API",
                             "Statussen blijven synchroon: een bestelling is verzonden — de status klopt en de klant krijgt een e‑mail",
                             "Voorraad is actueel: een product is op en de bestelknop verbergt zichzelf",
                             "n8n-, Zapier- en Make-scenario’s: aanvragen binnenkomen via een webhook",
                             "Excel-export en -import, pdf-documenten in één klik",
                             "Geen directe bankkoppelingen, overheidsystemen of diensten zonder open API"]),
        "zh": dict(name="CRM 集成", title="商店与 CRM 集成 — TPF Technology",
                   desc="通过开放 API 将您的商店与 CRM、记账系统和 n8n 打通：订单自动进入 CRM，状态与库存保持同步，邮件自动发送。报价免费。",
                   h1="商店与 CRM 集成",
                   lead="订单、客户与付款数据通过开放 API 在您的商店、CRM 与记账系统之间自动流转。您只需检查结果。",
                   includes=["订单自动进入 CRM：amoCRM、Bitrix24、HubSpot 等支持开放 API 的服务",
                             "状态同步：订单发货后状态更新，客户自动收到邮件",
                             "库存实时准确：商品售罄后购买按钮自动隐藏",
                             "n8n、Zapier、Make 自动化场景：咨询通过 webhook 进入流程",
                             "Excel 导入导出，一键生成 PDF 文档",
                             "不直连银行、政府系统或无开放 API 的服务"]),
    },
}


# The niche landing pages the outreach campaigns link to (sales_kit/offers_by_niche.md).
# Unlike the service pages, these carry their own price rows: the packages are niche-specific
# and are not in the home page's price list, so they are written here, not read from it.
NICHE_SLUGS = ("agri-digital", "mkb-website", "mvp-ai")

NICHE_PAGES = {
    "agri-digital": {
        "ru": dict(name="Сайты для агро и food",
                   title="Сайты и онлайн-продажи для агрокомпаний — TPF Technology",
                   desc="Многоязычные B2B-сайты, каталоги продукции и фермерские магазины для хозяйств и food-компаний: прямые продажи онлайн без посредников. Смета бесплатно, код ваш.",
                   h1="Сайты и онлайн-продажи для агро- и food-компаний",
                   lead="Строим цифровое присутствие для хозяйств и food-компаний: многоязычные B2B-сайты с каталогом продукции и канал прямой онлайн-продажи, чтобы покупатели приходили напрямую, без посредников. Мы также разрабатываем сельскохозяйственных роботов и знаем отрасль изнутри.",
                   includes=["Многоязычный B2B-сайт (NL/EN/DE) с каталогом продукции и формами заявок",
                             "Фермерский магазин: онлайн-заказы и оплата, все заказы в одной админке",
                             "Бот заказов для оптовиков в Telegram или WhatsApp",
                             "Автоматические рассылки «что созрело на этой неделе»",
                             "Аналитика: откуда приходят покупатели и что они смотрят"],
                   rows=[dict(title="Пакет B2B", text="Многоязычный сайт с каталогом и формами заявок. Запуск за 3–4 недели.", sum="от 2 900 €", alt=""),
                         dict(title="Пакет прямых продаж", text="То же плюс интернет-магазин: заказы, оплата, доставка или самовывоз, единая админка.", sum="от 4 900 €", alt=""),
                         dict(title="Бот заказов для оптовиков", text="Telegram- или WhatsApp-бот, который принимает заказы у ваших оптовых покупателей.", sum="от 1 500 €", alt="")]),
        "en": dict(name="Websites for agri & food",
                   title="Websites and online sales for agri companies — TPF Technology",
                   desc="Multilingual B2B websites, product catalogues and farm shops for growers and food companies: sell direct online, without middlemen. Free estimate, the code is yours.",
                   h1="Websites and online sales for agri and food companies",
                   lead="We build digital presence for growers and food companies: multilingual B2B websites with a product catalogue, and a direct online sales channel — so buyers reach you without middlemen. We also develop agricultural robots, so we know the sector from the inside.",
                   includes=["Multilingual B2B website (NL/EN/DE) with a product catalogue and request forms",
                             "Farm shop: online orders and payment, all orders in one dashboard",
                             "A wholesale order bot on Telegram or WhatsApp",
                             "Automatic 'in season this week' e-mail announcements",
                             "Analytics: where buyers come from and what they look at"],
                   rows=[dict(title="B2B package", text="A multilingual website with a catalogue and request forms. Live in 3–4 weeks.", sum="from €2,900", alt=""),
                         dict(title="Direct-sales package", text="The same, plus an online shop: orders, payment, delivery or pickup, one dashboard.", sum="from €4,900", alt=""),
                         dict(title="Wholesale order bot", text="A Telegram or WhatsApp bot that takes orders from your wholesale buyers.", sum="from €1,500", alt="")]),
        "nl": dict(name="Websites voor agrarisch & food",
                   title="Website en online verkoop voor agrarische bedrijven — TPF Technology",
                   desc="Meertalige B2B-websites, productcatalogi en verkoopsites voor telers en foodbedrijven: verkoop direct online, zonder tussenpersonen. Gratis offerte.",
                   h1="Website en online verkoop voor agrarische en foodbedrijven",
                   lead="Wij bouwen digitale zichtbaarheid voor telers en foodbedrijven: meertalige B2B-websites met productcatalogus en een direct online verkoopkanaal — zodat kopers u zonder tussenpersonen vinden. Wij ontwikkelen ook landbouwrobots en kennen de sector van binnenuit.",
                   includes=["Meertalige B2B-website (NL/EN/DE) met productcatalogus en aanvraagformulieren",
                             "Verkoopsite: online bestellingen en betaling, alle orders in één beheeromgeving",
                             "Bestelbot voor groothandel via Telegram of WhatsApp",
                             "Automatische e-mails: 'dit week vers geplukt'",
                             "Analytics: waar kopers vandaan komen en wat zij bekijken"],
                   rows=[dict(title="B2B-pakket", text="Meertalige website met catalogus en aanvraagformulieren. Live in 3–4 weken.", sum="vanaf €2.900", alt=""),
                         dict(title="Direct-verkooppakket", text="Zelfde, plus een webshop: bestellingen, betaling, levering of afhalen, één dashboard.", sum="vanaf €4.900", alt=""),
                         dict(title="Bestelbot groothandel", text="Een Telegram- of WhatsApp-bot die bestellingen van uw groothandelafnemers opneemt.", sum="vanaf €1.500", alt="")]),
        "zh": dict(name="农业与食品企业网站",
                   title="农业企业网站与在线销售 — TPF Technology",
                   desc="为种植企业和食品公司打造多语言B2B网站、产品目录和在线直营商店：无需中间商，直接在线销售。报价免费，源代码归您。",
                   h1="农业与食品企业网站与在线销售",
                   lead="我们为种植企业和食品公司构建数字化形象：带产品目录的多语言B2B网站，以及直接的在线销售渠道，让采购商无需中间商即可找到您。我们同时研发农业机器人，深知行业内部。",
                   includes=["多语言B2B网站（NL/EN/DE），含产品目录与询价表单",
                             "在线直营商店：在线下单与付款，所有订单集中管理",
                             "面向批发客户的Telegram或WhatsApp订购机器人",
                             "自动发送『本周新鲜上市』邮件",
                             "数据分析：采购商从哪里来、看什么页面"],
                   rows=[dict(title="B2B套餐", text="多语言网站，含目录与询价表单。3–4周上线。", sum="€2,900 起", alt=""),
                         dict(title="直营销售套餐", text="同上，另加在线商店：下单、付款、配送或自提，统一管理。", sum="€4,900 起", alt=""),
                         dict(title="批发订购机器人", text="通过Telegram或WhatsApp机器人接收批发客户订单。", sum="€1,500 起", alt="")]),
    },
    "mkb-website": {
        "ru": dict(name="Сайты для малого бизнеса",
                   title="Сайты для малого бизнеса под ключ — TPF Technology",
                   desc="Быстрый мобильный сайт, который превращает посетителей в заявки: дизайн, тексты, формы, аналитика. От 2 500 €, запуск за 3 недели. Бесплатный аудит вашего сайта.",
                   h1="Сайт, который превращает посетителей в заявки",
                   lead="Для малого и среднего бизнеса: современный быстрый сайт, который превращает посетителей в заявки. Мобильный, с формами заявок, аналитикой и понятными ценами — запуск за 3 недели.",
                   includes=["Мобильный дизайн: больше половины посетителей заходят с телефона",
                             "Формы заявок, которые сразу приходят на вашу почту",
                             "Готовность к поисковикам: страницы, sitemap, карточка в Google",
                             "Скорость и HTTPS, всё измеримо в аналитике",
                             "Пакет поддержки: хостинг, обновления, мелкие правки"],
                   rows=[dict(title="Новый сайт", text="Полностью новый сайт: дизайн, тексты, формы. Запуск за 3 недели.", sum="от 2 500 €", alt=""),
                         dict(title="Пакет поддержки", text="Хостинг, обновления, резервные копии и мелкие правки.", sum="99 € в месяц", alt=""),
                         dict(title="Бесплатный аудит сайта", text="Разбор вашего текущего сайта: скорость, мобильность, заявки. Одна страница с конкретными пунктами.", sum="бесплатно", alt="")]),
        "en": dict(name="SME websites",
                   title="Fast, lead-generating websites for SMEs — TPF Technology",
                   desc="A modern, mobile-first website that turns visitors into requests: design, copy, forms, analytics. From €2,500, live in 3 weeks. Free audit of your current website.",
                   h1="A website that turns visitors into requests",
                   lead="For SMEs: a modern, fast website that turns visitors into requests. Mobile-first, with request forms, analytics and clear pricing — live within 3 weeks.",
                   includes=["Mobile-first design: over half of your visitors come from a phone",
                             "Request forms delivered straight to your mailbox",
                             "Search-ready: pages, sitemap, your Google Business profile",
                             "Speed and HTTPS, measured in analytics",
                             "Care plan: hosting, updates, small changes"],
                   rows=[dict(title="New website", text="A complete new website: design, copy, forms. Live in 3 weeks.", sum="from €2,500", alt=""),
                         dict(title="Care plan", text="Hosting, updates, backups and small changes.", sum="€99 per month", alt=""),
                         dict(title="Free website audit", text="We review your current site: speed, mobile, lead capture. One page of concrete points.", sum="free", alt="")]),
        "nl": dict(name="MKB-websites",
                   title="Nieuwe website voor MKB-bedrijven — TPF Technology",
                   desc="Een moderne, mobielvriendelijke website die bezoekers omzet in aanvragen: ontwerp, teksten, formulieren, analytics. Vanaf €2.500, live in 3 weken. Gratis site-audit.",
                   h1="Nieuwe website die leads oplevert",
                   lead="Voor MKB-bedrijven in Nederland en België: een moderne, snelle website die bezoekers omzet in aanvragen. Mobiel-first, met aanvraagformulieren, analytics en heldere prijzen — live binnen 3 weken.",
                   includes=["Mobiel-first ontwerp: meer dan de helft van uw bezoekers komt via de telefoon",
                             "Aanvraagformulieren die direct in uw mailbox binnenkomen",
                             "Klaar voor Google: pagina's, sitemap en uw Google Business-profiel",
                             "Snelheid en HTTPS, meetbaar in analytics",
                             "Onderhoudspakket: hosting, updates en kleine wijzigingen"],
                   rows=[dict(title="Nieuwe website", text="Een compleet nieuwe website: ontwerp, teksten, formulieren. Live in 3 weken.", sum="vanaf €2.500", alt=""),
                         dict(title="Onderhoudspakket", text="Hosting, updates, back-ups en kleine wijzigingen.", sum="€99 per maand", alt=""),
                         dict(title="Gratis site-audit", text="Wij doorlichten uw huidige site: snelheid, mobiel, leads. Eén pagina met concrete punten.", sum="gratis", alt="")]),
        "zh": dict(name="中小企业网站",
                   title="为中小企业打造的高转化网站 — TPF Technology",
                   desc="现代化移动优先网站，把访客变成询盘：设计、文案、表单、数据分析。€2,500起，3周上线。现有网站可免费评估。",
                   h1="把访客变成询盘的网站",
                   lead="为中小企业打造：现代化、加载迅速的网站，把访客转化为询盘。移动优先，配询盘表单、数据分析和清晰报价——3周内上线。",
                   includes=["移动优先设计：超过一半访客来自手机",
                             "询盘表单直达您的邮箱",
                             "搜索引擎就绪：页面、sitemap、Google商家资料",
                             "速度与HTTPS，全部可用数据分析衡量",
                             "维护套餐：主机、更新与小修改"],
                   rows=[dict(title="全新网站", text="完整新网站：设计、文案、表单。3周上线。", sum="€2,500 起", alt=""),
                         dict(title="维护套餐", text="主机、更新、备份与小修改。", sum="€99 每月", alt=""),
                         dict(title="免费网站评估", text="分析您现有网站：速度、移动端、询盘。一页具体改进清单。", sum="免费", alt="")]),
    },
    "mvp-ai": {
        "ru": dict(name="MVP и AI-автоматизация",
                   title="Разработка MVP и AI-автоматизация — TPF Technology",
                   desc="MVP веб-приложения за 4–6 недель по фиксированной цене и AI-автоматизация: чат-боты, обработка заявок, документооборот. Смета бесплатно, код ваш.",
                   h1="Разработка MVP и AI-автоматизация процессов",
                   lead="Для основателей и растущих компаний: MVP веб-приложения за 4–6 недель по фиксированной цене и автоматизация рутины — чат-боты, обработка заявок, документооборот — на AI-агентах, которые мы сами эксплуатируем в продакшене.",
                   includes=["Фиксированная цена и точный срок: спека, разработка, запуск",
                             "AI-функции: чат-бот, обработка заявок, работа с документами",
                             "Тестовая версия до окончательной оплаты",
                             "Исходный код и все права — ваши",
                             "После запуска: поддержка или передача вашей команде"],
                   rows=[dict(title="MVP веб-приложение", text="Спека, дизайн, разработка и запуск за 4–6 недель. Фиксированная цена.", sum="от 6 000 €", alt=""),
                         dict(title="AI-автоматизация процессов", text="Чат-боты, обработка заявок, документооборот — AI-агенты берут рутину на себя.", sum="от 2 000 €", alt=""),
                         dict(title="AI-аудит процесса", text="Где AI окупится в вашем процессе. Сумма зачитывается при заказе разработки.", sum="500 €", alt="")]),
        "en": dict(name="MVP & AI automation",
                   title="MVP development and AI automation — TPF Technology",
                   desc="MVP web applications in 4–6 weeks at a fixed price, plus AI automation: chatbots, lead processing, document flow. Free estimate, the source code is yours.",
                   h1="MVP development and AI process automation",
                   lead="For founders and growing companies: we ship MVP web applications in 4–6 weeks at a fixed price, and automate the routine — chatbots, lead processing, document flow — with AI agents we run in production ourselves.",
                   includes=["A fixed price and a firm date: spec, build, launch",
                             "AI features: chatbot, lead processing, document handling",
                             "A review version you check before the final payment",
                             "The source code and all rights are yours",
                             "After launch: a care plan or handover to your team"],
                   rows=[dict(title="MVP web application", text="Spec, design, build and launch in 4–6 weeks. Fixed price.", sum="from €6,000", alt=""),
                         dict(title="AI process automation", text="Chatbots, lead processing, document flow — AI agents take over the routine.", sum="from €2,000", alt=""),
                         dict(title="AI audit of your process", text="Where AI pays off in your workflow. Credited in full if we build it.", sum="€500", alt="")]),
        "nl": dict(name="MVP & AI-automatisering",
                   title="MVP-ontwikkeling en AI-automatisering — TPF Technology",
                   desc="MVP-webapplicaties in 4–6 weken tegen een vaste prijs, plus AI-automatisering: chatbots, leadverwerking, documentstroom. Gratis offerte, de broncode is van u.",
                   h1="MVP-ontwikkeling en AI-automatisering van processen",
                   lead="Voor oprichters en groeiende bedrijven: wij leveren MVP-webapplicaties in 4–6 weken tegen een vaste prijs, en automatiseren het routinewerk — chatbots, leadverwerking, documentstroom — met AI-agents die wij zelf in productie draaien.",
                   includes=["Een vaste prijs en een duidelijke datum: specificatie, bouw, lancering",
                             "AI-functies: chatbot, leadverwerking, documentverwerking",
                             "Een testversie die u controleert vóór de laatste betaling",
                             "De broncode en alle rechten zijn van u",
                             "Na de lancering: onderhoudspakket of overdracht aan uw team"],
                   rows=[dict(title="MVP-webapplicatie", text="Specificatie, ontwerp, bouw en lancering in 4–6 weken. Vaste prijs.", sum="vanaf €6.000", alt=""),
                         dict(title="AI-procesautomatisering", text="Chatbots, leadverwerking, documentstroom — AI-agents nemen het routinewerk over.", sum="vanaf €2.000", alt=""),
                         dict(title="AI-audit van uw proces", text="Waar AI zich terugverdient in uw workflow. Volledig verrekenbaar bij opdracht.", sum="€500", alt="")]),
        "zh": dict(name="MVP与AI自动化",
                   title="MVP开发与AI流程自动化 — TPF Technology",
                   desc="4–6周内以固定价格交付MVP Web应用，并提供AI自动化：聊天机器人、线索处理、文档流程。报价免费，源代码归您。",
                   h1="MVP开发与AI流程自动化",
                   lead="面向创始人和成长期公司：我们以固定价格在4–6周内交付MVP Web应用，并自动完成重复工作——聊天机器人、线索处理、文档流程——使用我们自己在生产环境运行的AI智能体。",
                   includes=["固定价格与明确交付日：规格、开发、上线",
                             "AI功能：聊天机器人、线索处理、文档处理",
                             "尾款支付前可先验收测试版本",
                             "源代码与全部权利归您所有",
                             "上线后：维护套餐或交接给您的团队"],
                   rows=[dict(title="MVP Web应用", text="规格、设计、开发与上线，4–6周。固定价格。", sum="€6,000 起", alt=""),
                         dict(title="AI流程自动化", text="聊天机器人、线索处理、文档流程——AI智能体接管重复工作。", sum="€2,000 起", alt=""),
                         dict(title="AI流程评估", text="找出AI在您流程中的回报点。下单开发时全额抵扣。", sum="€500", alt="")]),
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
    assert len(prices) >= 8 and not cases, (lang, len(prices), len(cases))
    return prices, cases

def url(lang: str, slug: str) -> str:
    return f"{BASE}/{lang}/{slug}"

def build(lang: str, slug: str, prices: list[dict], cases: list[dict]) -> str:
    book = PAGES if slug in PAGES else NICHE_PAGES
    ui, c = UI[lang], book[slug][lang]
    hreflang, locale = LANGS[lang]
    alternates = "".join(f'<link rel="alternate" hreflang="{LANGS[l][0]}" href="{url(l, slug)}">\n' for l in LANGS) \
        + f'<link rel="alternate" hreflang="x-default" href="{url("en", slug)}">\n'
    langs = "".join(f'<a href="/{l}/{slug}" hreflang="{LANGS[l][0]}" aria-current="{str(l == lang).lower()}">{"中文" if l == "zh" else l.upper()}</a>'
                    for l in LANGS)
    rows = c["rows"] if "rows" in c else [prices[i] for i in PRICES[slug]]
    price_html = "".join(f'<div class="price"><div><h3>{esc(p["title"])}</h3><p>{esc(p["text"])}</p></div>'
                         f'<div><div class="sum">{esc(p["sum"])}</div>' + (f'<div class="alt">{esc(p["alt"])}</div>' if p["alt"] else "")
                         + "</div></div>" for p in rows)
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
<script defer src="/consent.js?v=2"></script>
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
        for slug in (*SLUGS, *NICHE_SLUGS):
            (OUT / lang / f"{slug}.html").write_text(build(lang, slug, prices, cases), encoding="utf-8")
    print(f"service pages: {len(LANGS) * (len(SLUGS) + len(NICHE_SLUGS))}")

if __name__ == "__main__":
    main()
