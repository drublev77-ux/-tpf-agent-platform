"""QA and Security: automatic gates between development and staging.

Gates: the build succeeds, every route exists, no high/critical vulnerabilities in dependencies,
and the reviewer confirms the code meets the spec's acceptance criteria.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from tpf.agents import visual
from tpf.agents.base import Ctx, Outcome
from tpf.agents.reviewer import review
from tpf.agents.devops import staging_project
from tpf.orchestrator.states import Ev
from tpf.tools.deploy import DeployError
from tpf.tools.sandbox import RunResult
from tpf.tools.template import SCHEMA_PATH, all_files, has_database, installable, kind_of, materialize, route_to_page
from tpf.tools.tenantdb import TenantDBError

BUILD_CMD = (
    "export NEXT_TELEMETRY_DISABLED=1; "
    "npm install --no-audit --no-fund --loglevel=error > /tmp/install.log 2>&1 "
    "|| { tail -c 4000 /tmp/install.log; exit 10; }; "
    "npx tsc --noEmit --pretty false || echo 'TYPE ERRORS ABOVE: fix all of them'; "
    "npm run build || exit 20; "
    "npm audit --omit=dev --audit-level=high || exit 30"
)
REVIEW_CODE_CHARS = 400_000

# The same gates, run by Vercel's own isolated build when the worker has no Docker (SANDBOX=vercel).
# The type check runs first and does not stop the build: it lists every type error at once, where the build
# alone would stop at the first and cost a whole round per error.
VERCEL_BUILD_CMD = ("(npx tsc --noEmit --pretty false || echo 'TYPE ERRORS ABOVE: fix all of them') "
                    "&& npm run build && npm audit --omit=dev --audit-level=high")


APP_CRITERIA = [
    "Every SQL query goes through the sql tagged template with ${} parameters; no query text is built by string "
    "concatenation and sql.unsafe is not used",
    "Every page under /admin except /admin/login, every admin route handler and every server action that reads or "
    "changes admin data calls `await requireAdmin()` before anything else; no public page or action exposes admin data",
    "Every input is validated and trimmed on the server (length limits, allowed values) before it reaches the database",
    "db/schema.sql creates every table and column the code queries, and is idempotent (IF NOT EXISTS)",
    "Every page, layout or route handler that queries the database exports dynamic = 'force-dynamic'",
    "No environment variable, password or raw database error is sent to the browser",
    "Lists the owner fills in start empty with an empty-state text; no invented sample records are seeded",
]
BOT_CRITERIA = [
    "lib/bot.ts implements every command and dialog in the architecture's bot list with the wording given, answers "
    "every callback query with answerCallback, and replies to unknown text with a hint and the main menu",
    "Each user's place in a dialog is stored in the database by chat id; nothing relies on memory between updates",
    "Telegram is reached only through @/tpf/telegram; the bot token is never read, logged or shown",
    "Notifications go only to owner chats linked with a one-time code; a message to many users sends at most 25 per call",
    "Text from users is trimmed and length-limited before it is stored or sent back",
]
SHOP_CRITERIA = [
    "Totals are computed on the server from database prices; nothing the browser sends (prices, totals, product names) is trusted",
    "An order is marked paid only in onPaid, with an update guarded by status = 'pending' so a repeated payment event changes nothing; "
    "stock changes and the owner's notification happen only when that update changed a row; a wrong amount is not accepted as paid",
    "The order status page is found by a random token, not a sequential id, and shows nothing about other orders",
    "onPaid reads the paid amount from payment.amountTotal and the currency from payment.currency (compared in "
    "lowercase), with no type casts on the payment object",
    "Checkout works without payments connected: the order is saved as 'new' and the customer is told the shop will contact them",
    "Money is stored as integers in minor units and formatted only for display; payment providers are reached only through @/tpf/payments and no provider is named in the code or texts",
]


def admin_cookie(session_secret: str, ttl_sec: int = 600) -> str:
    """A short-lived owner session, signed the way tpf/auth.ts signs it: lets the platform open admin pages."""
    exp = int(time.time()) + ttl_sec
    return f"{exp}.{hmac.new(session_secret.encode(), f'admin:{exp}'.encode(), hashlib.sha256).hexdigest()}"


async def prepare_database(ctx: Ctx, files: list[dict[str, str]], env: str, project: str) -> Any:
    """An application's database for this environment: created, brought to the code's schema, and handed
    to the Vercel project as environment variables. Raises TenantDBError when the schema is refused."""
    if ctx.tenant_db is None:
        raise TenantDBError("applications are not enabled: TENANT_DATABASE_URL is not set")
    db = await ctx.tenant_db.ensure(ctx.orch.pool, ctx.order_id, env)
    schema = next((f["content"] for f in files if f["path"] == SCHEMA_PATH), "")
    await ctx.tenant_db.apply_schema(db, schema)
    if ctx.deployer is not None:
        await ctx.deployer.set_env(project, db.env())
    return db


async def _build_on_vercel(ctx: Ctx, files: list[dict[str, str]], version: int, report: dict[str, Any],
                           kind: str = "site", routes: list[str] | None = None, cookie: str = "") -> RunResult:
    """Build the code as a Vercel preview. A green preview is kept for staging, so it is not built twice."""
    if ctx.deployer is None:
        return RunResult(1, "SANDBOX=vercel needs VERCEL_TOKEN")
    try:
        dep = await ctx.deployer.deploy(staging_project(ctx.order_id), all_files(files, ctx.form_endpoint, kind), production=False,
                                        build_command=VERCEL_BUILD_CMD)
    except DeployError as e:
        if getattr(e, "deployment_id", None) is None:
            raise  # Vercel refused the request itself (token, team, quota): not the code's fault,
            #        so escalate to the operator instead of sending the developer round in circles
        log = e.log or str(e)
        # npm audit prints "found N vulnerabilities" / "Severity:" only when it is the step that failed.
        code = 30 if ("npm audit" in log and "Severity:" in log) else 20
        return RunResult(code, log)
    if has_database(kind):
        problem = await ctx.deployer.health(dep.url)
        if problem:
            return RunResult(40, f"The application was built but does not answer with a working database: {problem}")
        broken = await ctx.deployer.smoke(dep.url, [r for r in (routes or []) if "[" not in r], cookie)
        if broken:
            return RunResult(40, "The application was built, but these pages fail when opened (signed in as the owner):\n"
                             + "\n".join(broken) + "\nFind what throws while each page renders and fix it.")
    if kind == "shop":
        failed = await _payment_check(ctx, dep.url)
        report["payment_check"] = {"ok": not failed, "failed": failed}
        if failed:
            return RunResult(40, "The shop was built, but lib/payments.ts fails the platform's payment check — with this code "
                             "a customer's payment would not be recorded correctly:\n" + "\n".join(f"- {f}" for f in failed)
                             + "\nFix onPaid, createCheckOrder, isPaid or removeCheckOrder in lib/payments.ts.")
    report["preview"] = {"url": dep.url, "deployment_id": dep.id, "code_version": version}
    report["visual"] = await _visual_gate(ctx, dep.url, routes or ["/"], cookie)
    return RunResult(0, f"Vercel build ok: {dep.url}")


async def _visual_gate(ctx: Ctx, url: str, routes: list[str], cookie: str) -> dict[str, Any]:
    """Look at the built pages. What is visibly broken sends the code back — but not forever: after a few
    rounds lost to looks alone the findings are kept for the engineer and stop blocking the order."""
    seen = await visual.look(ctx, url, routes, cookie)
    if seen["ok"] or "skipped" in seen:
        return seen
    earlier = await ctx.orch.pool.fetch(
        "SELECT content FROM artifacts WHERE order_id = $1 AND kind = 'qa_report' ORDER BY version", ctx.order_id)
    lost = sum(1 for r in earlier if (c := r["content"]) and not c.get("passed") and not (c.get("visual") or {}).get("ok", True)
               and all((c.get(g) or {}).get("ok") for g in ("routes", "build", "security", "acceptance")))
    if lost >= ctx.settings.visual_blocking_rounds:
        return seen | {"ok": True, "advisory": True}
    return seen


async def _payment_check(ctx: Ctx, url: str) -> list[str]:
    """Let the review build prove that its payment handling works, with a token only this check knows."""
    db = await ctx.tenant_db.ensure(ctx.orch.pool, ctx.order_id, "stg")
    token = secrets.token_hex(24)
    await ctx.tenant_db.set_secrets(db, {"check_token": token})
    try:
        return await ctx.deployer.check_payments(url, token)
    finally:
        await ctx.tenant_db.set_secrets(db, {"check_token": ""})     # the route answers to nobody afterwards


def _code_digest(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """The code the reviewer reads, trimmed to a budget; biggest files are cut first."""
    budget = REVIEW_CODE_CHARS
    out = []
    for f in sorted(files, key=lambda f: len(f["content"])):
        content = f["content"] if len(f["content"]) <= budget else f["content"][:max(budget, 0)] + "\n/* …trimmed… */"
        budget -= len(content)
        out.append({"path": f["path"], "content": content})
    return out


async def run(ctx: Ctx) -> Outcome:
    spec = await ctx.orch.latest_artifact(ctx.order_id, "spec")
    arch = await ctx.orch.latest_artifact(ctx.order_id, "architecture")
    code = await ctx.orch.latest_artifact(ctx.order_id, "code")
    if not (spec and arch and code):
        return Outcome(escalate="Нет ТЗ, архитектуры или кода для проверки")
    files: list[dict[str, str]] = code["files"]
    kind = kind_of(spec)

    report: dict[str, Any] = {}

    # Gate 1: every route has a page.
    paths = {f["path"] for f in files}
    missing = [r["path"] for r in arch["routes"] if route_to_page(r["path"]) not in paths]
    report["routes"] = {"ok": not missing, "missing": missing}

    # Gates 2-3: build and dependency audit, in the sandbox.
    version = await ctx.orch.pool.fetchval(
        "SELECT max(version) FROM artifacts WHERE order_id = $1 AND kind = 'code'", ctx.order_id
    )
    schema_error = None
    cookie = ""
    if has_database(kind):
        if ctx.tenant_db is None:
            return Outcome(escalate="Заказ — приложение с базой данных, но TENANT_DATABASE_URL не задан")
        try:
            db = await prepare_database(ctx, files, "stg", staging_project(ctx.order_id))
            cookie = admin_cookie(db.session_secret)
        except TenantDBError as e:
            schema_error = str(e)
    if schema_error:
        run_result = RunResult(50, schema_error)
    elif ctx.settings.sandbox == "vercel":
        run_result = await _build_on_vercel(ctx, files, version, report, kind, [r["path"] for r in arch["routes"]], cookie)
    else:
        workdir = materialize(Path(ctx.settings.workspace_root) / f"order-{ctx.order_id}" / f"v{version}", files, ctx.form_endpoint, kind)
        run_result = await ctx.sandbox.run(workdir, BUILD_CMD)
    stage = {0: "ok", 10: "install", 20: "build", 30: "audit", 40: "health", 50: "schema"}.get(run_result.exit_code, "sandbox")
    report["build"] = {
        "ok": run_result.exit_code in (0, 30) and not run_result.timed_out,
        "failed_at": None if run_result.ok else ("timeout" if run_result.timed_out else stage),
        "log_tail": run_result.output[-6000:],
    }
    report["security"] = {"ok": run_result.ok, "note": "npm audit: no high/critical" if run_result.ok else f"failed at {stage}"}
    await ctx.orch.log(ctx.order_id, "sandbox_run", "qa", {"exit_code": run_result.exit_code, "stage": stage}, kind="tool")

    # Gate 4: acceptance criteria, judged by the reviewer model (only if it builds: no point otherwise).
    if report["build"]["ok"] and report["routes"]["ok"]:
        verdict = await review(
            ctx, author="developer", artifact_name="site code",
            artifact=_code_digest(files),
            criteria=[
                *spec.get("acceptance_criteria", []),
                "No invented business facts: every phone, e-mail, address, opening hours, price, menu or product item "
                "and person's name on the site is copied from the spec's client_content; anything else is a visible "
                "placeholder in square brackets",
                "Implements every route and section in the architecture",
                "Accessible: semantic HTML, alt text on images, labels on form fields",
                *(APP_CRITERIA if has_database(kind) else []),
                *(BOT_CRITERIA if kind == "bot" else []),
                *(SHOP_CRITERIA if kind == "shop" else []),
                *(["Installable: lib/pwa.ts exports PWA with the design's colours, app/layout.tsx renders <Pwa /> and sets "
                   "appleWebApp, the icons and the theme colour; pages work at 360px wide with touch targets of at least 44px"]
                  if installable(kind) else []),
            ],
            context=(f"Architecture:\n{json.dumps(arch, ensure_ascii=False)}\n\n"
                     f"Spec client_content (the only facts the client gave):\n"
                     f"{json.dumps(spec.get('client_content', []), ensure_ascii=False)}\n"
                     f"Spec missing_content (must be placeholders):\n"
                     f"{json.dumps(spec.get('missing_content', []), ensure_ascii=False)}"),
        )
        report["acceptance"] = {"ok": verdict["approved"], "issues": verdict["issues"]}
    else:
        report["acceptance"] = {"ok": False, "issues": ["skipped: fix build and routes first"]}

    gates = ("routes", "build", "security", "acceptance") + (("visual",) if "visual" in report else ())
    passed = all(report[g]["ok"] for g in gates)
    report["passed"] = passed
    await ctx.orch.save_artifact(ctx.order_id, "qa_report", report, "qa")
    failed = [g for g in gates if not report[g]["ok"]]
    return Outcome(
        Ev.CHECKS_PASSED if passed else Ev.CHECKS_FAILED,
        data={"failed_gates": failed},
        details="" if passed else "не пройдены: " + ", ".join(failed),
    )
