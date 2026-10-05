"""Architect: turns the approved spec into a site structure and a design direction."""
from __future__ import annotations

import json
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome
from tpf.agents.reviewer import review
from tpf.orchestrator.states import Ev
from tpf.tools.template import ACCOUNTS_RULES, has_database, kind_of, rules_for

SCHEMA = {
    "type": "object",
    "properties": {
        "site_name": {"type": "string"},
        "routes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "pattern": "^/((\\[[a-z]+\\]|[a-z0-9-]+)(/(\\[[a-z]+\\]|[a-z0-9-]+))*)?$",
                         "description": "A dynamic segment is written in brackets: /product/[id]"},
                "title": {"type": "string"},
                "purpose": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "string"}},
                "access": {"type": "string", "description": "Applications with user accounts only: who may open the page — "
                           "'public', 'owner' (the /admin area), 'users' (anyone signed in) or role ids separated by commas"},
            },
            "required": ["path", "title", "purpose", "sections"],
        }},
        "components": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["name", "purpose"],
        }},
        "design": {
            "type": "object",
            "properties": {
                "palette": {"type": "object", "properties": {
                    "background": {"type": "string"}, "surface": {"type": "string"},
                    "text": {"type": "string"}, "muted": {"type": "string"}, "accent": {"type": "string"},
                }, "required": ["background", "text", "accent"]},
                "fonts": {"type": "object", "properties": {
                    "heading": {"type": "string"}, "body": {"type": "string"},
                }, "required": ["heading", "body"]},
                "tone": {"type": "string"},
            },
            "required": ["palette", "fonts", "tone"],
        },
        "data_model": {"type": "array", "description": "Applications only: every database table.", "items": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "purpose": {"type": "string"},
                "columns": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "type": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["name", "type"],
                }},
            },
            "required": ["table", "purpose", "columns"],
        }},
        "bot": {"type": "array", "description": "Telegram bots only: every command and dialog.", "items": {
            "type": "object",
            "properties": {
                "trigger": {"type": "string", "description": "A command such as /start, a button's callback_data prefix, or 'free text'"},
                "purpose": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"},
                          "description": "What the bot says and which buttons it shows, step by step, with the exact wording"},
            },
            "required": ["trigger", "purpose", "steps"],
        }},
        "roles": {"type": "array", "description": "Only when the spec has people who sign in: every role.", "items": {
            "type": "object",
            "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,30}$"}, "name": {"type": "string"},
                           "sees": {"type": "string", "description": "Which records this role sees: all, or only its own (say by which column)"},
                           "can": {"type": "string", "description": "What this role may create, change and delete"}},
            "required": ["id", "name", "sees", "can"],
        }},
        "admin": {"type": "string", "description": "Applications only: what the owner sees and does under /admin, screen by screen."},
        "notes": {"type": "string"},
    },
    "required": ["site_name", "routes", "components", "design"],
}

SYSTEM = (
    "You are the Architect at TPF Technology. From an approved spec, define the site's routes, shared "
    "components and a design direction the Developer will implement exactly. Colors must give text a "
    "contrast ratio of at least 4.5:1 on its background. Prefer system fonts or Google Fonts. "
)
APP_NOTE = (
    "This order is a web application. Besides the public routes, list the admin routes (/admin, /admin/login and "
    "one per managed list), describe every database table in data_model (lowercase names, PostgreSQL column types, "
    "an id and created_at on each, a status column where records move through states) and describe the admin "
    "screens in admin. Keep it small: only the tables and screens the spec needs. "
)

ACCOUNTS_NOTE = (
    "The spec has people who sign in (user_roles). List every role in roles (a short lowercase id, a name in the "
    "site's language, which records it sees and what it may do). Give every route an access: 'public', 'owner' for "
    "the /admin area, 'users' for anyone signed in, or the role ids that may open it. Add /login (public), /account "
    "('users': the person's details and a change-password form) and /admin/users (the owner adds people, sets "
    "their role, blocks them and gives new passwords). The owner signs in to /admin with the admin password and is "
    "not one of the roles; if the client's manager should also work inside the application, the owner creates a "
    "manager account for them. Accounts live in the platform's own table: do not put users or passwords in "
    "data_model; refer to a person in your tables by an integer user_id column and say so in its note. There is "
    "no self-registration and no password reset by e-mail. "
)

BOT_NOTE = (
    "This order is a Telegram bot. Describe every command and dialog in bot (trigger, purpose, steps with the "
    "exact wording of messages and buttons, in the bot's language), including /start, /help and the reply to "
    "unknown text. data_model also holds the bot's users, the state of each user's current dialog and the owner "
    "chats that receive notifications. The web routes are only '/' (names the bot and links to it), /admin, "
    "/admin/login and one admin route per managed list. "
)

SHOP_NOTE = (
    "This order is an online shop. Routes: the catalogue, a product page (/product/[id]), /cart, /checkout, the "
    "order status page (/order/[token]), plus /admin, /admin/login, /admin/products and /admin/orders. data_model "
    "has products, orders (with a random token, status, customer details, total in minor units) and order_items "
    "(a snapshot of name, unit price and quantity). Say in notes which currency the shop uses and how the cart "
    "cookie is shaped. "
)

REVIEW_CRITERIA = [
    "Every page in the spec maps to a route, and every route's sections cover the spec's sections for it",
    "Route paths are lowercase, unique, and '/' exists",
    "Text and background colors meet a 4.5:1 contrast ratio",
]
SITE_CRITERIA = ["Nothing requires a backend, accounts, payments or packages beyond next/react"]
ACCOUNTS_CRITERIA = [
    "roles lists every role in the spec's user_roles with what it sees and may do; every route has an access value that is "
    "'public', 'owner', 'users' or ids from roles; /login, /account and /admin/users exist",
    "data_model has no table of users or passwords; tables that belong to a person carry an integer user_id column",
    "No role can reach records the spec says it must not see; nothing needs self-registration or e-mail password reset",
]
APP_CRITERIA = [
    "data_model has a table for every kind of record in the spec, with the fields the spec lists",
    "There is /admin, /admin/login and an admin route for every list the owner manages; public routes never expose admin data",
    "Nothing requires self-registration of visitors, file uploads, external services or packages beyond next, react and postgres; people sign in only when the spec has user_roles; payments appear only in an online shop",
]
BOT_CRITERIA = [
    "bot describes every command and dialog in the spec step by step with exact wording, including /start, /help and the reply to unknown text",
    "data_model stores each user's dialog state by Telegram chat id, the bot's users, and the owner chats for notifications if the spec has them",
    "Nothing requires artificial intelligence, payments, stored files or photos, group moderation or services other than Telegram",
]
SHOP_CRITERIA = [
    "There are routes for the catalogue, a product, the cart, the checkout and the order status page by token, and admin routes for products and orders",
    "Prices and totals are integers in minor units; orders carry a random token, a status and an order_items snapshot",
    "Card payment is the only online payment and goes through the platform's payment helper, with no provider named in the design; nothing needs customer accounts, discount codes or outside delivery services",
]


def access_problems(arch: dict[str, Any]) -> list[str]:
    """What is wrong with who-may-open-what in an architecture with user accounts; empty when it is consistent."""
    roles = [r.get("id", "") for r in arch.get("roles") or []]
    problems = [] if roles else ["roles is empty: list every role from the spec's user_roles"]
    if len(set(roles)) != len(roles) or any(r in ("public", "owner", "users") for r in roles):
        problems.append("role ids must be unique and may not be 'public', 'owner' or 'users'")
    paths = {r["path"] for r in arch["routes"]}
    problems += [f"route {p} is missing" for p in ("/login", "/account", "/admin", "/admin/login", "/admin/users") if p not in paths]
    for route in arch["routes"]:
        raw = [a.strip() for a in str(route.get("access") or "").split(",") if a.strip()]
        admin = route["path"] == "/admin" or route["path"].startswith("/admin/")
        if not raw:
            problems.append(f"route {route['path']} has no access value")
        elif admin and raw != (["public"] if route["path"] == "/admin/login" else ["owner"]):
            problems.append(f"route {route['path']}: pages under /admin are 'owner' (and /admin/login is 'public')")
        elif not admin and "owner" in raw:
            problems.append(f"route {route['path']}: 'owner' is only for pages under /admin")
        elif len(raw) > 1 and not set(raw) <= set(roles) or len(raw) == 1 and raw[0] not in ("public", "owner", "users", *roles):
            problems.append(f"route {route['path']}: access {route.get('access')!r} must be 'public', 'users' or ids from roles")
    return problems


async def run(ctx: Ctx) -> Outcome:
    spec = await ctx.orch.latest_artifact(ctx.order_id, "spec")
    if spec is None:
        return Outcome(escalate="Нет утверждённого ТЗ для проектирования")
    kind = kind_of(spec)
    accounts = kind == "app" and bool(spec.get("user_roles"))
    system = (SYSTEM + (APP_NOTE if has_database(kind) else "") + (ACCOUNTS_NOTE if accounts else "") + (BOT_NOTE if kind == "bot" else "")
              + (SHOP_NOTE if kind == "shop" else "") + rules_for(kind) + (ACCOUNTS_RULES if accounts else "") + " " + SAFETY_NOTE)
    criteria = (REVIEW_CRITERIA + (APP_CRITERIA if has_database(kind) else SITE_CRITERIA) + (ACCOUNTS_CRITERIA if accounts else [])
                + (BOT_CRITERIA if kind == "bot" else []) + (SHOP_CRITERIA if kind == "shop" else []))
    base = f"Approved spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\nDesign the site for this spec."
    issues: list[str] = []
    for _ in range(ctx.settings.max_review_rounds + 1):
        prompt = base + ("\n\nFix these reviewer issues:\n" + "\n".join(f"- {i}" for i in issues) if issues else "")
        arch: dict[str, Any] = await ctx.llm.structured(
            order_id=ctx.order_id, agent="architect", model_class="strong",
            system=system, prompt=prompt, schema=SCHEMA, max_tokens=24000 if has_database(kind) else 8000,
        )
        if not accounts:
            arch.pop("roles", None)          # nobody signs in unless the spec says who
        wrong = access_problems(arch) if accounts else []
        if wrong:
            issues = wrong
            continue
        verdict = await review(
            ctx, author="architect", artifact_name="architecture", artifact=arch,
            criteria=criteria, context=base,
        )
        if verdict["approved"]:
            await ctx.orch.save_artifact(ctx.order_id, "architecture", arch, "architect")
            return Outcome(Ev.DESIGN_READY, data={"routes": [r["path"] for r in arch["routes"]]})
        issues = verdict["issues"]
    return Outcome(escalate="Ревьюер не принял архитектуру: " + "; ".join(issues))
