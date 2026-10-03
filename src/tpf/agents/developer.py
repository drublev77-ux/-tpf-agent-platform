"""Developer: writes the site's code from the spec and architecture, and fixes it after failed checks
or client change requests."""
from __future__ import annotations

import json
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome
from tpf.orchestrator.states import Ev
from tpf.tools.tenantdb import check_schema_sql
from tpf.tools.template import MAX_FILE_BYTES, SCHEMA_PATH, kind_of, route_to_page, rules_for, validate_path

SCHEMA = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }, "description": "The COMPLETE set of files you own; files not listed are deleted"},
        "notes": {"type": "string", "description": "What changed and why, for the log"},
    },
    "required": ["files", "notes"],
}

SYSTEM = (
    "You are the Developer at TPF Technology. Implement the site exactly as the architecture describes: "
    "every route, every section, the palette and fonts. Write production-quality, accessible, responsive "
    "code: semantic HTML, alt text, visible focus states, readable at 375px wide. Write all visible text "
    "in the site's language from the spec. Facts about the client's business come only from the spec's "
    "client_content, copied exactly. Never invent or 'fill in' a phone, e-mail, address, opening hours, price, "
    "menu or product item, person's name, review or statistic, and never use sample-looking data (555 numbers, "
    "'Example street', round made-up prices): for anything not in client_content show a visible placeholder in "
    "square brackets in the site's language, such as [телефон] or [PHONE]. A tel: or mailto: link without a "
    "real value points to '#'. "
)
APP_NOTE = (
    "This order is a web application: implement the data_model as db/schema.sql and build the admin area the "
    "architecture describes, with empty-state texts for lists that have no records yet. Records the owner will "
    "add through the admin area are not seeded with invented examples. "
)


def check_files(files: list[dict[str, str]], routes: list[str], kind: str = "site") -> list[str]:
    """Static checks the code must pass before it goes to QA."""
    problems = []
    paths = set()
    for f in files:
        err = validate_path(f["path"], kind)
        if err:
            problems.append(err)
        if len(f["content"].encode("utf-8")) > MAX_FILE_BYTES:
            problems.append(f"file too large: {f['path']}")
        if f["path"] in paths:
            problems.append(f"duplicate file: {f['path']}")
        paths.add(f["path"])
    for required in ["app/layout.tsx", *(route_to_page(r) for r in routes), *([SCHEMA_PATH] if kind == "app" else [])]:
        if required not in paths:
            problems.append(f"missing required file: {required}")
    if kind == "app":
        for f in files:
            if f["path"] == SCHEMA_PATH:
                bad = check_schema_sql(f["content"])
                if bad:
                    problems.append(bad)
            elif "sql.unsafe" in f["content"]:
                problems.append(f"sql.unsafe is not allowed: {f['path']}")
            elif "@/tpf/db" in f["content"] and f["content"].lstrip().startswith(("'use client'", '"use client"')):
                problems.append(f"the database client is imported in a client component: {f['path']}")
    return problems


async def _feedback(ctx: Ctx) -> str:
    """Why we are (re)developing: last failed checks or the client's change request."""
    last = await ctx.orch.pool.fetchrow(
        "SELECT name, data FROM events WHERE order_id = $1 AND kind = 'transition' AND to_state = 'development' "
        "ORDER BY id DESC LIMIT 1",
        ctx.order_id,
    )
    if last is None:
        return ""
    if last["name"] == "checks_failed":
        report = await ctx.orch.latest_artifact(ctx.order_id, "qa_report")
        return "The last version FAILED checks. Fix every problem in this QA report:\n" + json.dumps(report, ensure_ascii=False, indent=2)
    if last["name"] == "changes_requested":
        return "The client reviewed the staging site and requested changes:\n" + str(last["data"].get("comment", ""))
    if last["name"] == "resume":
        return "An engineer resumed this order after an escalation. Their note:\n" + str(last["data"].get("note", ""))
    return ""


async def run(ctx: Ctx) -> Outcome:
    spec = await ctx.orch.latest_artifact(ctx.order_id, "spec")
    arch = await ctx.orch.latest_artifact(ctx.order_id, "architecture")
    if spec is None or arch is None:
        return Outcome(escalate="Нет ТЗ или архитектуры для разработки")
    previous = await ctx.orch.latest_artifact(ctx.order_id, "code")
    feedback = await _feedback(ctx)
    routes = [r["path"] for r in arch["routes"]]
    kind = kind_of(spec)
    system = SYSTEM + (APP_NOTE if kind == "app" else "") + rules_for(kind) + " " + SAFETY_NOTE

    prompt = (
        f"Spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\n"
        f"Architecture:\n{json.dumps(arch, ensure_ascii=False, indent=2)}\n\n"
        + (f"Current code (modify it; return the complete updated file set):\n{json.dumps(previous, ensure_ascii=False)}\n\n" if previous else "")
        + (f"{feedback}\n\n" if feedback else "")
        + "Write the site."
    )
    problems: list[str] = []
    for _ in range(2):
        out: dict[str, Any] = await ctx.llm.structured(
            order_id=ctx.order_id, agent="developer", model_class="code",
            system=system,
            prompt=prompt + ("\n\nYour previous output was rejected by static checks:\n" + "\n".join(problems) if problems else ""),
            schema=SCHEMA, max_tokens=48000 if kind == "app" else 32000,
        )
        problems = check_files(out["files"], routes, kind)
        if not problems:
            await ctx.orch.save_artifact(ctx.order_id, "code", {"files": out["files"]}, "developer")
            await ctx.orch.log(ctx.order_id, "code_written", "developer", {"files": len(out["files"]), "notes": out["notes"]})
            return Outcome(Ev.CODE_READY, data={"files": len(out["files"])})
    return Outcome(escalate="Код не прошёл статические проверки: " + "; ".join(problems[:10]))
