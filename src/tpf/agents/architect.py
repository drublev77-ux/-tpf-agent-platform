"""Architect: turns the approved spec into a site structure and a design direction."""
from __future__ import annotations

import json
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome
from tpf.agents.reviewer import review
from tpf.orchestrator.states import Ev
from tpf.tools.template import TEMPLATE_RULES

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
        "notes": {"type": "string"},
    },
    "required": ["site_name", "routes", "components", "design"],
}

SYSTEM = (
    "You are the Architect at TPF Technology. From an approved spec, define the site's routes, shared "
    "components and a design direction the Developer will implement exactly. Colors must give text a "
    "contrast ratio of at least 4.5:1 on its background. Prefer system fonts or Google Fonts. "
    + TEMPLATE_RULES + " " + SAFETY_NOTE
)

REVIEW_CRITERIA = [
    "Every page in the spec maps to a route, and every route's sections cover the spec's sections for it",
    "Route paths are lowercase, unique, and '/' exists",
    "Text and background colors meet a 4.5:1 contrast ratio",
    "Nothing requires a backend, accounts, payments or packages beyond next/react",
]


async def run(ctx: Ctx) -> Outcome:
    spec = await ctx.orch.latest_artifact(ctx.order_id, "spec")
    if spec is None:
        return Outcome(escalate="Нет утверждённого ТЗ для проектирования")
    base = f"Approved spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\nDesign the site for this spec."
    issues: list[str] = []
    for _ in range(ctx.settings.max_review_rounds + 1):
        prompt = base + ("\n\nFix these reviewer issues:\n" + "\n".join(f"- {i}" for i in issues) if issues else "")
        arch: dict[str, Any] = await ctx.llm.structured(
            order_id=ctx.order_id, agent="architect", model_class="strong",
            system=SYSTEM, prompt=prompt, schema=SCHEMA, max_tokens=8000,
        )
        verdict = await review(
            ctx, author="architect", artifact_name="architecture", artifact=arch,
            criteria=REVIEW_CRITERIA, context=base,
        )
        if verdict["approved"]:
            await ctx.orch.save_artifact(ctx.order_id, "architecture", arch, "architect")
            return Outcome(Ev.DESIGN_READY, data={"routes": [r["path"] for r in arch["routes"]]})
        issues = verdict["issues"]
    return Outcome(escalate="Ревьюер не принял архитектуру: " + "; ".join(issues))
