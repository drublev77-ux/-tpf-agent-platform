"""Reviewer: replaces the human on internal checkpoints. Always a different model than the author."""
from __future__ import annotations

import json
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx

SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean", "description": "true only if every criterion is met"},
        "issues": {
            "type": "array", "items": {"type": "string"},
            "description": "Concrete, fixable problems. Empty when approved.",
        },
    },
    "required": ["approved", "issues"],
}

SYSTEM = (
    "You are the Reviewer at TPF Technology, a software studio run by AI agents. You check another "
    "agent's work product against explicit criteria before it moves on. Be strict about the criteria "
    "and nothing else: do not ask for polish the criteria do not require. Every issue must say what is "
    "wrong and what would fix it. " + SAFETY_NOTE
)


async def review(ctx: Ctx, *, author: str, artifact_name: str, artifact: Any, criteria: list[str], context: str = "") -> dict[str, Any]:
    prompt = (
        f"Review the {artifact_name} written by the {author} agent.\n\n"
        f"Criteria:\n" + "\n".join(f"- {c}" for c in criteria) + "\n\n"
        + (f"Context:\n{context}\n\n" if context else "")
        + f"{artifact_name}:\n```json\n{json.dumps(artifact, ensure_ascii=False, indent=2)}\n```"
    )
    result = await ctx.llm.structured(
        order_id=ctx.order_id, agent="reviewer", model_class="reviewer",
        system=SYSTEM, prompt=prompt, schema=SCHEMA, max_tokens=4000,
    )
    await ctx.orch.log(ctx.order_id, "review", "reviewer", {
        "artifact": artifact_name, "approved": result["approved"], "issues": result["issues"],
    })
    return result
