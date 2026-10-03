"""Architect: turns the approved spec into a site structure and a design direction."""
from __future__ import annotations

import json
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome
from tpf.agents.reviewer import review
from tpf.orchestrator.states import Ev
from tpf.tools.template import has_database, kind_of, rules_for

SCHEMA = {
    "type": "object",
    "properties": {
        "site_name": {"type": "string"},
        "routes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "pattern": "^/([a-z0-9-]+(/[a-z0-9-]+)*)?$"},
                "title": {"type": "string"},
                "purpose": {"type": "string"},
                "sections": {"type": "array", "items": {"type": "string"}},
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

BOT_NOTE = (
    "This order is a Telegram bot. Describe every command and dialog in bot (trigger, purpose, steps with the "
    "exact wording of messages and buttons, in the bot's language), including /start, /help and the reply to "
    "unknown text. data_model also holds the bot's users, the state of each user's current dialog and the owner "
    "chats that receive notifications. The web routes are only '/' (names the bot and links to it), /admin, "
    "/admin/login and one admin route per managed list. "
)

REVIEW_CRITERIA = [
    "Every page in the spec maps to a route, and every route's sections cover the spec's sections for it",
    "Route paths are lowercase, unique, and '/' exists",
    "Text and background colors meet a 4.5:1 contrast ratio",
]
SITE_CRITERIA = ["Nothing requires a backend, accounts, payments or packages beyond next/react"]
APP_CRITERIA = [
    "data_model has a table for every kind of record in the spec, with the fields the spec lists",
    "There is /admin, /admin/login and an admin route for every list the owner manages; public routes never expose admin data",
    "Nothing requires visitor accounts, payments, file uploads, e-mail sending, external services or packages beyond next, react and postgres",
]
BOT_CRITERIA = [
    "bot describes every command and dialog in the spec step by step with exact wording, including /start, /help and the reply to unknown text",
    "data_model stores each user's dialog state by Telegram chat id, the bot's users, and the owner chats for notifications if the spec has them",
    "Nothing requires artificial intelligence, payments, stored files or photos, group moderation or services other than Telegram",
]


async def run(ctx: Ctx) -> Outcome:
    spec = await ctx.orch.latest_artifact(ctx.order_id, "spec")
    if spec is None:
        return Outcome(escalate="Нет утверждённого ТЗ для проектирования")
    kind = kind_of(spec)
    system = (SYSTEM + (APP_NOTE if has_database(kind) else "") + (BOT_NOTE if kind == "bot" else "")
              + rules_for(kind) + " " + SAFETY_NOTE)
    criteria = REVIEW_CRITERIA + (APP_CRITERIA if has_database(kind) else SITE_CRITERIA) + (BOT_CRITERIA if kind == "bot" else [])
    base = f"Approved spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\nDesign the site for this spec."
    issues: list[str] = []
    for _ in range(ctx.settings.max_review_rounds + 1):
        prompt = base + ("\n\nFix these reviewer issues:\n" + "\n".join(f"- {i}" for i in issues) if issues else "")
        arch: dict[str, Any] = await ctx.llm.structured(
            order_id=ctx.order_id, agent="architect", model_class="strong",
            system=system, prompt=prompt, schema=SCHEMA, max_tokens=8000,
        )
        verdict = await review(
            ctx, author="architect", artifact_name="architecture", artifact=arch,
            criteria=criteria, context=base,
        )
        if verdict["approved"]:
            await ctx.orch.save_artifact(ctx.order_id, "architecture", arch, "architect")
            return Outcome(Ev.DESIGN_READY, data={"routes": [r["path"] for r in arch["routes"]]})
        issues = verdict["issues"]
    return Outcome(escalate="Ревьюер не принял архитектуру: " + "; ".join(issues))
