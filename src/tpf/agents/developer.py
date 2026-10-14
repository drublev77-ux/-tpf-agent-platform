"""Developer: writes the site's code from the spec and architecture, and fixes it after failed checks
or client change requests."""
from __future__ import annotations

import json
import re
from typing import Any

from tpf.agents.base import SAFETY_NOTE, Ctx, Outcome, reference_note
from tpf.llm.gateway import LLMError
from tpf.orchestrator.states import Ev
from tpf.tools.shop_payments_ts import PAYMENT_HOSTS
from tpf.tools.tenantdb import check_schema_sql
from tpf.tools.imagegen import check_manifest
from tpf.tools.template_caps import SEO_CONFIG_PATH
from tpf.tools.template import ACCOUNTS_RULES, ROLES_PATH, access_of, capability_rules, uses_accounts, PWA_CONFIG_PATH, SHOP_HANDLER_PATH, installable, BOT_HANDLER_PATH, MAX_FILE_BYTES, SCHEMA_PATH, has_database, kind_of, route_to_page, rules_for, validate_path

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
SHOP_NOTE = (
    "This order is an online shop: implement the catalogue, cart, checkout, order status page and admin area as "
    "the architecture describes, with payment through the platform's payment helper and onPaid in lib/payments.ts. "
)
BOT_NOTE = (
    "This order is a Telegram bot: implement every command and dialog the architecture lists in lib/bot.ts, with "
    "the exact wording given, and keep each user's place in a dialog in the database. "
)


def _imports_values(code: str, module: str) -> bool:
    """True when the file imports something other than types from the module (types vanish at build time)."""
    for kind, names in re.findall(r"import\s+(type\s+)?\{([^}]*)\}\s*from\s*[\"']" + re.escape(module) + "[\"']", code):
        if not kind and any(n.strip() and not n.strip().startswith("type ") for n in names.split(",")):
            return True
    return bool(re.search(r"import\s+(?!type\b)[\w*][^{}]*?from\s*[\"']" + re.escape(module) + "[\"']", code))


def check_accounts(files: list[dict[str, str]], arch: dict) -> list[str]:
    """Static checks for an application whose people sign in."""
    by_path = {f["path"]: f["content"] for f in files}
    problems = []
    roles_file = by_path.get(ROLES_PATH, "")
    if not re.search(r"export\s+const\s+ROLES\b", roles_file):
        problems.append(f"{ROLES_PATH} must export const ROLES: {{ id: string; name: string }}[]")
    for role in arch.get("roles") or []:
        if not re.search(r"""id:\s*["']""" + re.escape(role["id"]) + r"""["']""", roles_file):
            problems.append(f"{ROLES_PATH} does not list the role '{role['id']}' from the architecture")
    if re.search(r"\bbytea\b", by_path.get(SCHEMA_PATH, ""), re.I):
        problems.append("db/schema.sql must not store file contents (bytea): files live in the platform's own table")
    if re.search(r"\bpassword", by_path.get(SCHEMA_PATH, ""), re.I):
        problems.append("db/schema.sql must not store passwords: accounts live in the platform's own table")
    for route in arch.get("routes") or []:
        page = by_path.get(route_to_page(route["path"]), "")
        allowed = access_of(route)
        if not page or allowed == ["public"]:
            continue
        guard = "requireAdmin" if allowed == ["owner"] else "requireUser"
        if guard not in page:
            problems.append(f"{route_to_page(route['path'])} must call {guard}() first: the page is for {', '.join(allowed)}")
    for f in files:
        if f["path"].endswith((".ts", ".tsx")):
            if re.search(r"\btpf_users\b|\btpf_user\b", f["content"]):
                problems.append(f"accounts are reached only through @/tpf/users, not directly: {f['path']}")
            if re.search(r"""from\s+["'](node:)?crypto["']""", f["content"]) and re.search(r"scrypt|pbkdf2|createHash", f["content"]):
                problems.append(f"passwords are hashed by the platform, not in the application: {f['path']}")
            if "serveFile(" in f["content"] and not re.search(r"\b(requireUser|requireAdmin|currentUser|isAdmin)\s*\(", f["content"]):
                problems.append(f"a file is served without checking who asks: sign the visitor in before serveFile: {f['path']}")
            if "navigator.geolocation" in f["content"]:
                problems.append(f"the phone's location is read through GeoForm from @/tpf/GeoForm, not directly: {f['path']}")
            if _imports_values(f["content"], "@/tpf/files") and f["content"].lstrip().startswith(("'use client'", '"use client"')):
                problems.append(f"@/tpf/files is server-only and is imported in a client component: {f['path']}")
            if _imports_values(f["content"], "@/tpf/users") and f["content"].lstrip().startswith(("'use client'", '"use client"')):
                problems.append(f"@/tpf/users is server-only and is imported in a client component: {f['path']}")
    return problems


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
    for required in ["app/layout.tsx", *(route_to_page(r) for r in routes), *([SCHEMA_PATH] if has_database(kind) else []), *([BOT_HANDLER_PATH] if kind == "bot" else []), *([SHOP_HANDLER_PATH] if kind == "shop" else []), *([PWA_CONFIG_PATH] if installable(kind) else []), SEO_CONFIG_PATH]:
        if required not in paths:
            problems.append(f"missing required file: {required}")
    for f in files:
        # Next.js refuses a "use server" module that exports anything but async functions, and says so only
        # late in the build.
        if f["path"].endswith((".ts", ".tsx")) and re.match(r"""\s*(['"])use server\1""", f["content"]):
            bad = re.findall(r"^export\s+(?:(?:const|let|var|class|enum)\b|\{|\*|(?:default\s+)?function\b).*$", f["content"], re.M)
            if bad:
                problems.append(f"a 'use server' file may export only async functions: {f['path']}: {bad[0].strip()[:80]}")
    seo = next((f["content"] for f in files if f["path"] == SEO_CONFIG_PATH), None)
    if seo is not None and not re.search(r"export\s+const\s+SEO\b", seo):
        problems.append(f"{SEO_CONFIG_PATH} must export const SEO = {{ name, title, description, locale, pages }}")
    if "@/tpf/seo" not in next((f["content"] for f in files if f["path"] == "app/layout.tsx"), ""):
        problems.append("app/layout.tsx must export metadata = siteMetadata() from '@/tpf/seo'")
    if installable(kind):
        by_path = {f["path"]: f["content"] for f in files}
        if PWA_CONFIG_PATH in by_path and not re.search(r"export\s+const\s+PWA\b", by_path[PWA_CONFIG_PATH]):
            problems.append(f"{PWA_CONFIG_PATH} must export const PWA = {{ name, shortName, themeColor, backgroundColor, letter }}")
        if "@/tpf/Pwa" not in by_path.get("app/layout.tsx", ""):
            problems.append("app/layout.tsx must render <Pwa /> from '@/tpf/Pwa'")
    if has_database(kind):
        for f in files:
            if "api.telegram.org" in f["content"]:
                problems.append(f"Telegram is called directly instead of through @/tpf/telegram: {f['path']}")
            if any(host in f["content"] for host in PAYMENT_HOSTS):
                problems.append(f"a payment provider is called directly instead of through @/tpf/payments: {f['path']}")
            if kind == "shop" and f["path"] == SHOP_HANDLER_PATH and not re.search(r"export\s+async\s+function\s+onPaid\b", f["content"]):
                problems.append(f"{SHOP_HANDLER_PATH} must export async function onPaid(reference, payment)")
            if kind == "shop" and f["path"] == SHOP_HANDLER_PATH:
                # A real payment was once refused by a shop whose onPaid read a guessed field through a cast.
                for needed in ("createCheckOrder", "isPaid", "removeCheckOrder"):
                    if not re.search(r"export\s+async\s+function\s+" + needed + r"\b", f["content"]):
                        problems.append(f"{SHOP_HANDLER_PATH} must export async function {needed} for the platform's payment check")
                if not re.search(r"payment\s*\.\s*amountTotal|\{[^}]*\bamountTotal\b[^}]*\}\s*=\s*payment\b", f["content"]):
                    problems.append(f"{SHOP_HANDLER_PATH} must compare payment.amountTotal with the order's total")
                if re.search(r"\bas\s+(unknown|any)\b", f["content"]):
                    problems.append(f"{SHOP_HANDLER_PATH} must use the Payment fields as typed, without `as unknown` or `as any`")
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
        note = "An engineer resumed this order after an escalation. Their note:\n" + str(last["data"].get("note", ""))
        report = await ctx.orch.latest_artifact(ctx.order_id, "qa_report")
        if report and not report.get("passed"):      # what stopped the order is usually still in the last report
            note += "\n\nThe last QA report, still to be fixed:\n" + json.dumps(report, ensure_ascii=False, indent=2)
        return note
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
              + (SHOP_NOTE if kind == "shop" else "") + rules_for(kind) + (ACCOUNTS_RULES if uses_accounts(arch) else "") + capability_rules(spec) + " " + STEP_RULE + SAFETY_NOTE)

    base = (
        f"Spec:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\n"
        f"Architecture:\n{json.dumps(arch, ensure_ascii=False, indent=2)}\n\n"
        + reference_note(await ctx.orch.latest_artifact(ctx.order_id, "reference"))
    )
    files: dict[str, str] = {f["path"]: f["content"] for f in (previous or {}).get("files", [])}
    changing = bool(files)                    # a fix or a client change request, not a first version
    remaining: list[dict[str, str]] = []
    written: set[str] = set()                 # files this run has (re)written
    # Work in progress survives a retry or a worker restart: steps already written are not paid for twice.
    base_version = await ctx.orch.pool.fetchval(
        "SELECT coalesce(max(version), 0) FROM artifacts WHERE order_id = $1 AND kind = 'code'", ctx.order_id)
    draft = await ctx.orch.latest_artifact(ctx.order_id, "code_draft")
    resumed = bool(draft and draft.get("base") == base_version and draft.get("feedback") == feedback)
    if resumed:
        files = {f["path"]: f["content"] for f in draft["files"]}
        remaining = draft["remaining"]
        written = set(draft.get("written") or [])
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
            # a fix that takes several steps keeps the report in front of it: without it the later files were
            # rewritten blind and the same findings came back round after round
            task = ((f"{feedback}\n\n" if changing and feedback else "")
                    + "Continue: write the next files from this list, as many as fit in one step.\n"
                    + json.dumps(remaining, ensure_ascii=False))
        elif changing and feedback:
            task = (f"{feedback}\n\nCheck the files written so far against every point above and change whatever "
                    "is still not fixed. Return only the files you change.")
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
            written.add(f["path"])
        for path in out.get("delete") or []:
            files.pop(path, None)
        notes.append(str(out.get("notes", "")))
        # In a fix the files to change already exist, so "exists" cannot mean "done": only what this run wrote is.
        # (It did, and a fix that named its later files stopped after the first step with the rest untouched.)
        remaining = [r for r in out.get("remaining") or [] if r.get("path") not in (written if changing else files)]
        if remaining:
            problems = []
            await ctx.orch.save_artifact(ctx.order_id, "code_draft", {
                "base": base_version, "feedback": feedback, "remaining": remaining, "written": sorted(written),
                "files": [{"path": p, "content": c} for p, c in files.items()]}, "developer")
            continue
        result = [{"path": p, "content": c} for p, c in files.items()]
        problems = (check_files(result, routes, kind) + (check_accounts(result, arch) if uses_accounts(arch) else [])
                    + (check_manifest(result) if kind != "bot" else []))
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
