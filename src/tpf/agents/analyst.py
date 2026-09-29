"""Analyst: turns a client request into a spec and an estimate, or asks the client questions."""
from __future__ import annotations

import math
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome, conversation
from tpf.agents.reviewer import review
from tpf.orchestrator.states import Ev

MAX_CLARIFICATION_ROUNDS = 2

SCHEMA = {
    "type": "object",
    "properties": {
        "in_pilot_scope": {
            "type": "boolean",
            "description": "true if the order is a website or landing page (static marketing/content site)",
        },
        "scope_note": {"type": "string", "description": "why it is or is not in scope"},
        "questions": {
            "type": "array", "items": {"type": "string"},
            "description": "Questions for the client, in the client's language. Empty if you can proceed.",
        },
        "spec": {
            "type": "object",
            "properties": {
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
                "languages": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Checkable statements, each verifiable by looking at the site",
                },
            },
            "required": ["title", "summary", "pages", "features", "acceptance_criteria"],
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

SYSTEM = (
    "You are the Analyst at TPF Technology, a studio where AI agents build client software end to end. "
    "The pilot builds websites and landing pages only: a static Next.js site with no backend, no user "
    "accounts and no payments. Read the client's request and conversation and produce a precise spec and "
    "an effort estimate in hours. Ask the client questions only when an answer would change the pages, "
    "features or price; otherwise make a reasonable assumption and list it. Write the spec and questions "
    "in the client's language. Estimate honestly: include design, content layout, responsive checks and "
    "deployment. " + SAFETY_NOTE
)

REVIEW_CRITERIA = [
    "Every page has a purpose and concrete sections; nothing essential from the client's request is missing",
    "The spec fits the pilot: static website, no backend, no accounts, no payments",
    "Acceptance criteria are specific and checkable by looking at the site",
    "Assumptions are stated where the client did not decide",
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
            system=SYSTEM, prompt=prompt, schema=SCHEMA, max_tokens=8000,
        )

        if not out.get("in_pilot_scope", False):
            return Outcome(escalate=f"Заказ вне пилота: {out.get('scope_note', '')}")

        if out.get("questions") and asked < MAX_CLARIFICATION_ROUNDS:
            await ctx.orch.save_artifact(ctx.order_id, "questions", {"questions": out["questions"]}, "analyst")
            return Outcome(Ev.CLARIFICATION_NEEDED, details="\n".join(f"- {q}" for q in out["questions"]))

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
    price = round(hours * s.hourly_rate, 2)       # price is computed by code, never by the model
    estimate = {
        **out["estimate"], "total_hours": hours, "hourly_rate": s.hourly_rate,
        "price": price, "currency": s.currency,
        "prepayment": round(price * s.prepayment_share, 2) if s.payment_required else 0,
    }
    await ctx.orch.save_artifact(ctx.order_id, "spec", out["spec"], "analyst")
    await ctx.orch.save_artifact(ctx.order_id, "estimate", estimate, "analyst")
    return Outcome(
        Ev.SPEC_READY, data={"total_hours": hours, "price": price, "currency": s.currency},
        details=_format_details(ctx.order["language"], hours, price, s.currency, estimate["duration_days"]),
    )
