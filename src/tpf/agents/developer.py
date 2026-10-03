"""Developer: writes the site's code from the spec and architecture, and fixes it after failed checks
or client change requests."""
from __future__ import annotations

import json
import re
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome
from tpf.llm.gateway import LLMError
from tpf.orchestrator.states import Ev
from tpf.tools.tenantdb import check_schema_sql
from tpf.tools.template import BOT_HANDLER_PATH, MAX_FILE_BYTES, SCHEMA_PATH, has_database, kind_of, route_to_page, rules_for, validate_path

SCHEMA = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        }, "description": "Files you are writing or changing in this step, each with its COMPLETE content. "
                          "Files that already exist and are not listed stay as they are."},
        "delete": {"type": "array", "items": {"type": "string"}, "description": "Paths of existing files to remove"},
        "remaining": {"type": "array", "items": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["path", "purpose"],
        }, "description": "Files still to be written in later steps. Empty when the project is complete."},
        "notes": {"type": "string", "description": "What you wrote or changed and why, for the log"},
    },
    "required": ["files", "remaining", "notes"],
}

MAX_STEPS = 24        # calls for one version of the code, static-check repairs included
STEP_TOKENS = 64000   # a ceiling, not a target: steps are kept small by STEP_RULE
STEP_RULE = (
    "You work in steps, because one answer cannot hold a whole project. In each step write at most four files and "
    "about 600 lines of code in total, and keep every file under about 350 lines: split long logic into several "
    "modules (for a bot, lib/bot.ts only routes updates to handlers in lib/bot/*.ts). Start with what other files depend on (db/schema.sql, lib/, shared components, "
    "app/layout.tsx, app/globals.css), then the pages. List every file still to be written in `remaining`; you "
    "will be called again with the files written so far until `remaining` is empty. Every file you return is "
    "complete, never a fragment or a diff. Files written in earlier steps stay unless you return them again or "
    "list them in `delete`. When you fix or change existing code, return only the files you change. "
)

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
BOT_NOTE = (
    "This order is a Telegram bot: implement every command and dialog the architecture lists in lib/bot.ts, with "
    "the exact wording given, and keep each user's place in a dialog in the database. "
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
    for required in ["app/layout.tsx", *(route_to_page(r) for r in routes), *([SCHEMA_PATH] if has_database(kind) else []), *([BOT_HANDLER_PATH] if kind == "bot" else [])]:
        if required not in paths:
            problems.append(f"missing required file: {required}")
    if has_database(kind):
        for f in files:
            if "api.telegram.org" in f["content"]:
                problems.append(f"Telegram is called directly instead of through @/tpf/telegram: {f['path']}")
            if kind == "bot" and f["path"] == BOT_HANDLER_PATH and not re.search(r"export\s+async\s+function\s+handleUpdate\b", f["content"]):
                problems.append(f"{BOT_HANDLER_PATH} must export async function handleUpdate(update: TgUpdate)")
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
    system = (SYSTEM + (APP_NOTE if has_database(kind) else "") + (BOT_NOTE if kind == "bot" else "")
              + rules_for(kind) + " " + STEP_RULE + SAFETY_NOTE)

    base = (
        f"Spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\n"
        f"Architecture:\n{json.dumps(arch, ensure_ascii=False, indent=2)}\n\n"
    )
    files: dict[str, str] = {f["path"]: f["content"] for f in (previous or {}).get("files", [])}
    changing = bool(files)                    # a fix or a client change request, not a first version
    remaining: list[dict[str, str]] = []
    # Work in progress survives a retry or a worker restart: steps already written are not paid for twice.
    base_version = await ctx.orch.pool.fetchval(
        "SELECT coalesce(max(version), 0) FROM artifacts WHERE order_id = $1 AND kind = 'code'", ctx.order_id)
    draft = await ctx.orch.latest_artifact(ctx.order_id, "code_draft")
    resumed = bool(draft and draft.get("base") == base_version and draft.get("feedback") == feedback)
    if resumed:
        files = {f["path"]: f["content"] for f in draft["files"]}
        remaining = draft["remaining"]
    problems: list[str] = []
    too_long = False
    notes: list[str] = []
    repairs = 0
    for step in range(MAX_STEPS):
        current = [{"path": p, "content": c} for p, c in files.items()]
        if problems:
            task = "The code was rejected by static checks. Fix exactly these problems:\n" + "\n".join(problems)
        elif step == 0 and not resumed:
            if not changing:
                task = (f"{feedback}\n\n" if feedback else "") + "Write the first step of the project."
            elif feedback:
                task = f"{feedback}\n\nChange the code accordingly."
            else:
                task = "Review the code against the spec and architecture and complete it."
        elif remaining:
            task = ("Continue: write the next files from this list, as many as fit in one step.\n"
                    + json.dumps(remaining, ensure_ascii=False))
        else:
            task = "Check that the project is complete; write anything still missing."
        if too_long:
            task += "\n\nYour previous answer was too long and was lost. Write exactly one file in this step."
        prompt = (base + (f"Files written so far:\n{json.dumps(current, ensure_ascii=False)}\n\n" if current else "") + task)
        try:
            out: dict[str, Any] = await ctx.llm.structured(
                order_id=ctx.order_id, agent="developer", model_class="code",
                system=system, prompt=prompt, schema=SCHEMA, max_tokens=STEP_TOKENS,
            )
        except LLMError as e:
            if "max_tokens" not in str(e) or too_long:
                raise
            too_long = True
            continue
        too_long = False
        for f in out.get("files", []):
            files[f["path"]] = f["content"]
        for path in out.get("delete") or []:
            files.pop(path, None)
        notes.append(str(out.get("notes", "")))
        remaining = [r for r in out.get("remaining") or [] if r.get("path") not in files]
        if remaining:
            problems = []
            await ctx.orch.save_artifact(ctx.order_id, "code_draft", {
                "base": base_version, "feedback": feedback, "remaining": remaining,
                "files": [{"path": p, "content": c} for p, c in files.items()]}, "developer")
            continue
        result = [{"path": p, "content": c} for p, c in files.items()]
        problems = check_files(result, routes, kind)
        if not problems:
            await ctx.orch.save_artifact(ctx.order_id, "code", {"files": result}, "developer")
            await ctx.orch.log(ctx.order_id, "code_written", "developer",
                               {"files": len(result), "steps": step + 1, "notes": "\n".join(n for n in notes if n)[:4000]})
            return Outcome(Ev.CODE_READY, data={"files": len(result)})
        repairs += 1
        if repairs > 2:
            break
    if problems:
        return Outcome(escalate="Код не прошёл статические проверки: " + "; ".join(problems[:10]))
    return Outcome(escalate=f"Разработчик не закончил проект за {MAX_STEPS} шагов; осталось файлов: {len(remaining)}")
