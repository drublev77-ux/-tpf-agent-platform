"""Analyst: turns a client request into a spec and an estimate, or asks the client questions."""
from __future__ import annotations

import math
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome, conversation
from tpf.agents.reviewer import review
from tpf.orchestrator.states import Ev
from tpf.payments import stage_amounts

MAX_CLARIFICATION_ROUNDS = 2

SCHEMA = {
    "type": "object",
    "properties": {
        "in_pilot_scope": {
            "type": "boolean",
            "description": "true if the order fits what the studio builds, as described in your instructions",
        },
        "scope_note": {"type": "string", "description": "why it is or is not in scope"},
        "questions": {
            "type": "array", "items": {"type": "string"},
            "description": "Questions for the client, in the client's language. Empty if you can proceed.",
        },
        "spec": {
            "type": "object",
            "properties": {
                "product_type": {
                    "type": "string", "enum": ["site", "app", "bot", "shop"],
                    "description": "'site': pages with fixed content (optionally a contact form). 'app': a web application "
                                   "that stores data in a database and has an owner-only admin area. 'bot': a Telegram bot "
                                   "with a database and a web admin area for its owner. 'shop': an online shop "
                                   "with a catalogue, cart and card payments into the owner's own account with a payment provider.",
                },
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "audience": {"type": "string"},
                "pages": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "purpose": {"type": "string"},
                        "sections": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "purpose", "sections"],
                }},
                "features": {"type": "array", "items": {"type": "string"}},
                "user_roles": {
                    "type": "array",
                    "description": "Only for an 'app' in which people other than the owner sign in: every kind of user.",
                    "items": {"type": "object", "properties": {
                        "name": {"type": "string"},
                        "sees": {"type": "string", "description": "Which records this role sees: everything, or only its own"},
                        "can": {"type": "string", "description": "What this role may add, change and delete"}},
                        "required": ["name", "sees", "can"]},
                },
                "capabilities": {
                    "type": "array", "items": {"type": "string", "enum": ["uploads", "chat", "geolocation", "documents", "email",
                                                                      "schedule", "ai", "integrations"]},
                    "description": "Only for an 'app', and only what the client asked for. With user_roles: 'uploads' "
                                   "(people attach photos and PDF documents), 'chat' (messages between people), 'geolocation' "
                                   "(clocking in and out on site with the phone's location). For any application: "
                                   "'documents' (PDF documents, Excel export and import), 'email' (letters to staff and "
                                   "customers), 'schedule' (reminders and other work done at a set time), 'ai' (a "
                                   "language model reads documents or answers questions), 'integrations' (named "
                                   "outside services, with the client's own keys).",
                },
                "languages": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "client_content": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Every publishable fact the client actually gave, verbatim: business name, phone, e-mail, "
                                   "address, opening hours, prices, menu or product items, people's names, links. Nothing invented.",
                },
                "missing_content": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Facts the site needs but the client has not given (e.g. 'phone number', 'prices'), in the "
                                   "client's language. The site will show a bracketed placeholder for each.",
                },
                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Checkable statements, each verifiable by looking at the site",
                },
            },
            "required": ["product_type", "title", "summary", "pages", "features", "acceptance_criteria", "client_content", "missing_content"],
        },
        "estimate": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "hours": {"type": "number"}},
                    "required": ["name", "hours"],
                }},
                "duration_days": {"type": "integer", "minimum": 1},
            },
            "required": ["items", "duration_days"],
        },
    },
    "required": ["in_pilot_scope", "questions"],
}

SCOPE_SITE = (
    "WHAT THE STUDIO BUILDS. A website or landing page, one page or several (product_type 'site'): a static "
    "Next.js site with fixed content, no user accounts, no payments, no online booking and no admin panel. Its one "
    "dynamic feature is a contact or request form (name, contact, message) whose messages are e-mailed to the "
    "client; offer it when the client wants visitors to write, leave a request or ask for a callback. "
)
SCOPE_SEO = (
    "Every site, application and shop is built ready for search engines at no extra cost: each public page has its "
    "own title and description, the site has a sitemap and robots.txt, structured data with the business's real "
    "contact details, clean headings and fast static pages, and the review version is kept out of search. This is "
    "the technical base only. Not included, and to be said in assumptions when the client mentions search, "
    "promotion or 'SEO': a place in the results is never promised; no keyword research, link building, "
    "advertising, or registration in Google Search Console and Yandex Webmaster (the client does that with their "
    "own account, in a few minutes, once the site is on their domain). When the client names the searches they "
    "want to be found by, write them into the spec so that titles and texts use those words naturally. "
)
SCOPE_APP = (
    "A web application (product_type 'app'), when the client needs the site to store and manage data: public "
    "pages and forms that write to a database (bookings, requests, orders without payment, sign-ups, reviews "
    "awaiting moderation) or show data from it (a catalogue, a schedule, a price list, news), plus an admin area "
    "under /admin where the owner, signed in with one password, views and edits that data (lists, statuses, "
    "create, edit, delete). Choose 'app' only when the client needs data to be saved, managed or changed without "
    "a developer; otherwise choose 'site', which is cheaper and faster. "
    "An application can also have people who sign in — staff, members, clients — each with a role (for example "
    "worker, foreman, manager): a login page with e-mail and password, pages and actions that depend on the role, "
    "and records that belong to a person (my tasks, my hours, my requests). The owner adds these people in the "
    "admin area, sets their role, blocks them and gives them a password to pass on; a person can change their own "
    "password. When the client needs this, list every role in user_roles with what it sees and what it may do, "
    "and describe each page per role. There is no self-registration, no password reset by e-mail and no sign-in "
    "through Google or other services: say so in assumptions. Without user_roles an application has no accounts "
    "besides the owner. "
    "An application with user_roles can, when the client asks for it, also have (list each in capabilities): "
    "'uploads' — signed-in people attach photos and PDF documents to records (a receipt, an estimate, a document), "
    "up to 4 MB each after the phone shrinks the photo, 300 MB for the whole application, visible only to those who "
    "may see the record; no other file types, no video, no editing of documents. "
    "'chat' — messages between the people who sign in, in one common chat or one per project: text only, new "
    "messages appear within a few seconds while the page is open, with unread counters; no push notifications, "
    "calls, typing indicators or message search. "
    "'geolocation' — a worker clocks in and out on site from the phone: the application records the time and the "
    "phone's position at that moment, shows how far it was from the object and counts hours per person and "
    "period; a manager sets each object's position and can correct shifts. Location is read only when the button "
    "is pressed, never in the background; it can be switched off or faked on the phone, so it is a record, not "
    "proof — say so in assumptions. There are no maps inside the application (an address opens in the phone's "
    "maps) and no address search. "
    "Any application can, when the client asks for it, also have (list each in capabilities): "
    "'documents' — the application makes PDF documents from its records (an estimate, an invoice, an act, a "
    "report: a title, details, a table with totals, places to sign; A4, Latin and Cyrillic text only, no logo or "
    "custom layout) and exchanges tables with Excel: any list can be downloaded as .xlsx, and records can be "
    "loaded from an .xlsx or .csv file of up to 4 MB whose columns the spec names. No Word files, no editing of "
    "documents in the browser, no electronic signature. "
    "'email' — the application writes plain-text letters to the staff and to a customer about their own request, "
    "booking or document: up to 200 letters a day and 10 a day to one address, sent from the application's name "
    "through our mail service, with replies going to the client. No newsletters or advertising, no attachments "
    "(a letter links to the document), no letters from the client's own mailbox or domain — say so in assumptions. "
    "'schedule' — work done at a set time: reminders before a date, a daily or weekly summary, closing what is "
    "overdue. It runs about every 15 minutes, so 'at 9:00' means between 9:00 and 9:15. Reminders by e-mail need "
    "'email' as well. "
    "'ai' — a language model inside the application: it reads a photo or a PDF and fills in a form for the person "
    "to check (a receipt, an invoice, a business card), summarises or drafts text, or answers questions about the "
    "records the person may see. Its answers can be wrong, so a person always confirms them — say so in "
    "assumptions. Only signed-in people or the owner use it, each up to 30 requests a day; the application has a "
    "monthly allowance of requests that covers ordinary use by a small team, and when it is used up these features "
    "pause until the next month while everything else works. No image or voice generation, no training on the "
    "client's data. Name each AI feature in the spec with what goes in and what comes out. "
    "'integrations' — the application sends data to, or reads data from, outside services that have a public "
    "HTTPS API and that the client has an account with (a CRM, an accounting or messaging service, a calendar). "
    "The client enters the service's keys on the order page, never in messages. In the spec name every service, "
    "exactly what is sent or read and when; ask the client which service and which account plan they have if they "
    "did not say. We build from the service's public documentation and cannot try it without the client's keys, "
    "so the client checks every integration on the review version, and corrections found there are part of the "
    "work — say so in assumptions. Not available: services without a public API, banks, government systems, "
    "1C or other software installed on the client's own computers, and requests coming in from a service. "
    "For each of these capabilities add its work to the estimate as its own line. "
    "An application has no online payments, no file uploads by visitors who are not signed in (images are fixed files or links), no SMS "
    "sending, and no mobile app; e-mail beyond the contact form, scheduled work, AI and outside services appear only "
    "when listed in capabilities. In the spec of an application, list the admin area as a page, and state for every kind of record "
    "which fields it has, who creates it (visitor or owner) and what the owner can do with it. "
)
SCOPE_BOT = (
    "A Telegram bot (product_type 'bot'), when the client wants people to interact in Telegram: commands and a "
    "menu of buttons, answers to frequent questions, a catalogue or schedule shown in the chat, step-by-step "
    "dialogs that collect a request, booking or order without payment, notifications to the owner in Telegram "
    "about new records, and messages from the owner to the bot's users. It comes with a small web admin area "
    "where the owner, signed in with one password, sees users and records and edits the lists and texts the bot "
    "shows. The bot answers by fixed rules, not by artificial intelligence; it has no payments, does not store "
    "photos or files users send, does not moderate groups or channels and has no integrations with outside "
    "services. In the spec of a bot, describe in pages the bot's commands and dialogs step by step (one entry "
    "per command or dialog, its sections being the steps and the buttons shown) plus the admin area, and state "
    "for every kind of record its fields and what the owner can do with it. The client creates the bot in "
    "Telegram's @BotFather and enters its token on their order page later: never ask for the token in questions "
    "and do not list it as missing content. "
)
SCOPE_SHOP = (
    "An online shop (product_type 'shop'), when the client sells goods or services online: a catalogue of "
    "products with prices, a cart, a checkout that takes the customer's contact and delivery details, card "
    "payment into the client's own account with a payment provider (the money goes straight to the client): "
    "Stripe for international sales, YooKassa for a business in Russia, bePaid for a business in Belarus; and an admin area "
    "where the owner manages products and orders and gets an e-mail about each paid order. The shop sells in one "
    "currency. It has no customer accounts, discount codes, subscriptions, delivery-cost calculation by courier "
    "services, marketplace integrations or product photo uploads (the owner pastes image links). Name in "
    "assumptions the provider that fits where the client's business is and what currency it sells in (roubles: "
    "YooKassa; Belarusian roubles: bePaid; otherwise Stripe). If the client can use none of the three, or wants "
    "another provider, the shop takes orders without online payment and the owner arranges payment personally: "
    "say so in assumptions. A shop paid through YooKassa does not send fiscal receipts: say so in out_of_scope. In the spec state the currency, what a product has, what the checkout asks, the "
    "delivery or pickup options as fixed text or fixed prices the owner sets, and the order statuses. The client "
    "enters the provider's keys on their order page later: never ask for it in questions and do not list it as "
    "missing content. Products the owner will add through the admin area are not missing content. "
)
SCOPE_MOBILE = (
    "Every application and shop is also installable: on a phone it is added to the home screen with its own icon "
    "and opens full-screen like an app, with nothing to download from a store. When a client asks for a mobile "
    "app, offer exactly this (product_type 'app' or 'shop') and say in assumptions that it is installed from the "
    "browser and is not published in the App Store or Google Play; add an acceptance criterion that it can be "
    "installed on a phone. Only if the client insists on a store listing, push notifications or device features "
    "such as the camera, Bluetooth or background location is the order out of scope. "
)
SCOPE_END = (
    "Anything outside this — an app published in the App Store or Google Play, a marketplace with many sellers, a bot for another messenger, a "
    "system with user accounts, integrations — is out of scope: set in_pilot_scope to false and say why in scope_note. "
)
RULES = (
    "Read the client's request and conversation and produce a precise spec and an effort estimate in hours. Ask "
    "the client questions only when an answer would change the pages, features or price; otherwise make a "
    "reasonable assumption and list it. "
    "One thing you never assume is the client's own facts. Anything that will be published as a fact about "
    "the business — its name, phone, e-mail, address, opening hours, prices, menu or product items, people's "
    "names, social links, legal details — must come from the client. If the site needs such facts and the "
    "client has not given them, ask for all of them in one short list of questions before writing the spec. "
    "When you do write the spec, copy the facts the client gave into client_content verbatim and list every "
    "fact still missing in missing_content; the site will show a visible placeholder for those. This holds for "
    "every order, including ones the client calls a test, demo or draft: never plan to fill the site with "
    "plausible sample data. In an application, records the owner will enter later through the admin area are "
    "not missing content: the lists simply start empty. Design choices (palette, layout, wording of headings) "
    "are yours to assume. Write the spec and questions in the client's language. Estimate honestly: include "
    "design, content layout, the database and admin area if any, responsive checks and deployment. "
)

SCOPE_AUTOMATION = (
    "PROCESS AUTOMATION. The site offers 'process automation', and a request may begin with that label. It is not "
    "a product type of its own: it is an application (product_type 'app') whose capabilities do the routine work — "
    "'schedule', 'email', 'documents', 'ai', 'integrations' — within the limits stated above, or a Telegram bot "
    "when the process lives in Telegram. Every new request, booking or order of a delivered product can also be "
    "sent to the client's own n8n, Zapier, Make or CRM webhook, which the client sets on the order page; that needs "
    "no work in the estimate. Before writing the spec you must know: what is done by hand today and by whom, what "
    "starts each step (a new record, a date, a person's action), which outside services are involved and whether "
    "the client has accounts with them, and what the result of each step is. Ask about whatever of this the "
    "request does not say. If the process depends on something not available (a bank, a government system, "
    "software installed on the client's computers, a service without a public API), say so plainly and offer the "
    "part that can be built. "
)


def system_prompt(apps_enabled: bool) -> str:
    return (
        "You are the Analyst at TPF Technology, a studio where AI agents build client software end to end. "
        + SCOPE_SITE + SCOPE_SEO + (SCOPE_APP + SCOPE_BOT + SCOPE_SHOP + SCOPE_MOBILE + SCOPE_AUTOMATION if apps_enabled else "Applications with a database and Telegram bots are not available: product_type is always 'site'. ")
        + SCOPE_END + RULES + SAFETY_NOTE
    )


# Every delivered product can send its new records to a webhook the client sets on the order page (tpf.tools.hooks).
# The analyst is told it costs nothing; the reviewer has to know it too, or it reads a mention of it as an outside
# service missing from capabilities and the estimate, and rejects the spec until the order pauses (order 19).
ORDER_PAGE_WEBHOOK = (
    "The webhook the client sets on the order page, which sends each new request, booking or order to the client's "
    "own n8n, Zapier, Make or CRM, is built into every delivered product: a spec may mention it in features or "
    "assumptions without 'integrations' in capabilities and without estimate hours, and it is not an outside service "
    "for out_of_scope either; do not raise it as an issue")

REVIEW_CRITERIA = [
    "Every page has a purpose and concrete sections; nothing essential from the client's request is missing",
    "The spec fits what the studio builds: a static website (product_type 'site', its only dynamic feature a contact form), a web application (product_type 'app') with a database and an owner-only admin area, a rule-based Telegram bot (product_type 'bot') with a database and a web admin area, or an online shop (product_type 'shop') with card payments through the owner's own payment provider account; no self-registration of visitors, and no payments outside a shop; file uploads, chat and clocking in by location appear only in an 'app' with user_roles and are listed in capabilities; documents and Excel, e-mail from the application, scheduled work, AI features and outside services appear only in an 'app' and only when listed in capabilities, each within the limits the studio states (for 'integrations' every service is named with what is sent or read, and the assumptions say the client checks it on the review version; for 'ai' the assumptions say a person confirms the model's answers); people sign in only in an 'app' whose user_roles lists every role with what it sees and may do",
    "product_type is 'app' only if the client needs data stored or managed and 'bot' only if the client asked for a Telegram bot and 'shop' only if the client sells online; for an app or a bot, every kind of record has its fields and the owner's actions on it described; for a bot, every command and dialog is described step by step",
    "Acceptance criteria are specific and checkable by looking at the site or by talking to the bot",
    "Assumptions are stated where the client did not decide",
    ORDER_PAGE_WEBHOOK,
    "client_content contains only facts found in the client conversation; every other fact the site needs is in missing_content",
    "The estimate covers every page and feature and its hours are plausible for the scope",
]


def _format_details(language: str, hours: float, price: float, currency: str, days: int) -> str:
    return {
        "ru": f"{hours:g} ч работы, {price:,.0f} {currency}, срок около {days} дн.".replace(",", " "),
        "zh": f"工作量 {hours:g} 小时，{price:,.0f} {currency}，工期约 {days} 天",
    }.get(language, f"{hours:g} hours of work, {price:,.0f} {currency}, about {days} days")


async def run(ctx: Ctx) -> Outcome:
    s = ctx.settings
    history = await ctx.orch.messages(ctx.order_id)
    asked = await ctx.orch.pool.fetchval(
        "SELECT count(*) FROM artifacts WHERE order_id = $1 AND kind = 'questions'", ctx.order_id
    )
    base_prompt = (
        f"Client: {ctx.order['client_name']}\nClient language: {ctx.client_language}\n\n"
        f"Conversation so far:\n{conversation(history)}\n\n"
        + ("You have already asked the client questions twice. Do not ask again: decide with stated assumptions.\n\n"
           if asked >= MAX_CLARIFICATION_ROUNDS else "")
        + "Produce the spec and estimate, or questions if you truly cannot proceed."
    )

    issues: list[str] = []
    for round_no in range(s.max_review_rounds + 1):
        prompt = base_prompt + (
            "\n\nThe reviewer rejected your previous draft. Fix these issues:\n" + "\n".join(f"- {i}" for i in issues)
            if issues else ""
        )
        out: dict[str, Any] = await ctx.llm.structured(
            order_id=ctx.order_id, agent="analyst", model_class="strong",
            system=system_prompt(ctx.tenant_db is not None), prompt=prompt, schema=SCHEMA, max_tokens=16000,
        )

        if not out.get("in_pilot_scope", False):
            return Outcome(escalate=f"Заказ вне пилота: {out.get('scope_note', '')}")

        if out.get("questions") and asked < MAX_CLARIFICATION_ROUNDS:
            await ctx.orch.save_artifact(ctx.order_id, "questions", {"questions": out["questions"]}, "analyst")
            return Outcome(Ev.CLARIFICATION_NEEDED, details="\n".join(f"- {q}" for q in out["questions"]))

        if out.get("spec") and ctx.tenant_db is None:
            out["spec"]["product_type"] = "site"
        if "spec" not in out or "estimate" not in out:
            issues = ["Spec and estimate are required: questions are no longer allowed."]
            continue

        verdict = await review(
            ctx, author="analyst", artifact_name="spec and estimate",
            artifact={"spec": out["spec"], "estimate": out["estimate"]},
            criteria=REVIEW_CRITERIA, context=f"Client conversation:\n{conversation(history)}",
        )
        if verdict["approved"]:
            break
        issues = verdict["issues"]
    else:
        return Outcome(escalate="Ревьюер не принял ТЗ после всех попыток: " + "; ".join(issues))

    hours = math.ceil(sum(float(i["hours"]) for i in out["estimate"]["items"]))
    currency = ctx.order.get("currency") or s.currency
    rate = s.rate_in(currency, out["spec"].get("product_type", "site"), ctx.order["language"])
    price = round(hours * rate, 2)                # price is computed by code, never by the model
    estimate = {
        **out["estimate"], "total_hours": hours, "hourly_rate": rate,
        "price": price, "currency": currency,
    }
    if s.payment_provider != "off":
        split = stage_amounts(price, s.prepayment_share)
        estimate["payments"] = {"prepayment": float(split["prepayment"]), "final": float(split["final"])}
    await ctx.orch.save_artifact(ctx.order_id, "spec", out["spec"], "analyst")
    await ctx.orch.save_artifact(ctx.order_id, "estimate", estimate, "analyst")
    details = _format_details(ctx.order["language"], hours, price, currency, estimate["duration_days"])
    if "payments" in estimate:
        details += _payment_terms(ctx.order["language"], estimate["payments"], currency)
    return Outcome(
        Ev.SPEC_READY, data={"total_hours": hours, "price": price, "currency": currency}, details=details,
    )


def _payment_terms(language: str, parts: dict[str, float], currency: str) -> str:
    pre, fin = parts["prepayment"], parts["final"]
    money = lambda v: f"{v:,.2f} {currency}".replace(",", " ")  # noqa: E731
    if language == "ru":
        return "; оплата: " + ", ".join(filter(None, [
            f"{money(pre)} до старта" if pre else "", f"{money(fin)} перед запуском" if fin else ""]))
    if language == "zh":
        return "；付款：" + "，".join(filter(None, [
            f"开工前 {money(pre)}" if pre else "", f"上线前 {money(fin)}" if fin else ""]))
    return "; payment: " + ", ".join(filter(None, [
        f"{money(pre)} before we start" if pre else "", f"{money(fin)} before release" if fin else ""]))
