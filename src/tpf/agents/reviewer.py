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

# A finished product is judged in two grades: a round of fixes costs as much as writing the product did, and
# one more missing aria attribute found each round kept a working application from its client (order 16).
GRADED_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {"type": "string"},
                   "description": "Problems that stop the product from being handed over. Empty when there are none."},
        "minor": {"type": "array", "items": {"type": "string"},
                  "description": "Everything else worth fixing: it does not stop the handover."},
    },
    "required": ["issues", "minor"],
}

GRADED_NOTE = (
    " Report EVERYTHING you find in this one pass: the work comes back to you only after a costly round of fixes, "
    "so a problem you hold back costs a whole round. Sort what you find. `issues` is only what stops the product "
    "from being handed over to the client: a required page, section or function missing or not working; a person "
    "seeing or changing what their role must not; a visitor's input able to damage data or reach what it should "
    "not; secrets or other people's data exposed; invented business facts. `minor` is what the client would not "
    "notice or what does no harm: a missing label or ARIA attribute, a tap target somewhat under size, wording, a "
    "small inconsistency with the architecture, a hand-crafted URL or form value that only gives its own author an "
    "error page. When unsure, it is minor."
)

SYSTEM = (
    "You are the Reviewer at TPF Technology, a software studio run by AI agents. You check another "
    "agent's work product against explicit criteria before it moves on. Be strict about the criteria "
    "and nothing else: do not ask for polish the criteria do not require. Every issue must say what is "
    "wrong and what would fix it. " + SAFETY_NOTE
)


async def review(ctx: Ctx, *, author: str, artifact_name: str, artifact: Any, criteria: list[str], context: str = "",
                 graded: bool = False) -> dict[str, Any]:
    prompt = (
        f"Review the {artifact_name} written by the {author} agent.\n\n"
        f"Criteria:\n" + "\n".join(f"- {c}" for c in criteria) + "\n\n"
        + (f"Context:\n{context}\n\n" if context else "")
        + f"{artifact_name}:\n```json\n{json.dumps(artifact, ensure_ascii=False, indent=2)}\n```"
    )
    result = await ctx.llm.structured(
        order_id=ctx.order_id, agent="reviewer", model_class="reviewer",
        system=SYSTEM + (GRADED_NOTE if graded else ""), prompt=prompt, schema=GRADED_SCHEMA if graded else SCHEMA,
        max_tokens=12000,
    )
    if graded:
        result = {"issues": list(result.get("issues") or []), "minor": list(result.get("minor") or [])}
        result["approved"] = not result["issues"]
    await ctx.orch.log(ctx.order_id, "review", "reviewer", {
        "artifact": artifact_name, "approved": result["approved"], "issues": result["issues"],
        **({"minor": result["minor"]} if graded else {}),
    })
    return result
