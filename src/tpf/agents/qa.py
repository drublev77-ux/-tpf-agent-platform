"""QA and Security: automatic gates between development and staging.

Gates: the build succeeds, every route exists, no high/critical vulnerabilities in dependencies,
and the reviewer confirms the code meets the spec's acceptance criteria.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tpf.agents.base import Ctx, Outcome
from tpf.agents.reviewer import review
from tpf.agents.devops import project_name
from tpf.orchestrator.states import Ev
from tpf.tools.deploy import DeployError
from tpf.tools.sandbox import RunResult
from tpf.tools.template import all_files, materialize, route_to_page

BUILD_CMD = (
    "export NEXT_TELEMETRY_DISABLED=1; "
    "npm install --no-audit --no-fund --loglevel=error > /tmp/install.log 2>&1 "
    "|| { tail -c 4000 /tmp/install.log; exit 10; }; "
    "npm run build || exit 20; "
    "npm audit --omit=dev --audit-level=high || exit 30"
)
REVIEW_CODE_CHARS = 120_000

# The same gates, run by Vercel's own isolated build when the worker has no Docker (SANDBOX=vercel).
VERCEL_BUILD_CMD = "npm run build && npm audit --omit=dev --audit-level=high"


async def _build_on_vercel(ctx: Ctx, files: list[dict[str, str]], version: int, report: dict[str, Any]) -> RunResult:
    """Build the code as a Vercel preview. A green preview is kept for staging, so it is not built twice."""
    if ctx.deployer is None:
        return RunResult(1, "SANDBOX=vercel needs VERCEL_TOKEN")
    try:
        dep = await ctx.deployer.deploy(project_name(ctx.order_id), all_files(files), production=False,
                                        build_command=VERCEL_BUILD_CMD)
    except DeployError as e:
        log = e.log or str(e)
        # npm audit prints "found N vulnerabilities" / "Severity:" only when it is the step that failed.
        code = 30 if ("npm audit" in log and "Severity:" in log) else 20
        return RunResult(code, log)
    report["preview"] = {"url": dep.url, "deployment_id": dep.id, "code_version": version}
    return RunResult(0, f"Vercel build ok: {dep.url}")


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

    report: dict[str, Any] = {}

    # Gate 1: every route has a page.
    paths = {f["path"] for f in files}
    missing = [r["path"] for r in arch["routes"] if route_to_page(r["path"]) not in paths]
    report["routes"] = {"ok": not missing, "missing": missing}

    # Gates 2-3: build and dependency audit, in the sandbox.
    version = await ctx.orch.pool.fetchval(
        "SELECT max(version) FROM artifacts WHERE order_id = $1 AND kind = 'code'", ctx.order_id
    )
    if ctx.settings.sandbox == "vercel":
        run_result = await _build_on_vercel(ctx, files, version, report)
    else:
        workdir = materialize(Path(ctx.settings.workspace_root) / f"order-{ctx.order_id}" / f"v{version}", files)
        run_result = await ctx.sandbox.run(workdir, BUILD_CMD)
    stage = {0: "ok", 10: "install", 20: "build", 30: "audit"}.get(run_result.exit_code, "sandbox")
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
                "Implements every route and section in the architecture",
                "No invented client facts (prices, contacts, reviews); placeholders are used instead",
                "Accessible: semantic HTML, alt text on images, labels on form fields",
            ],
            context=f"Architecture:\n{json.dumps(arch, ensure_ascii=False)}",
        )
        report["acceptance"] = {"ok": verdict["approved"], "issues": verdict["issues"]}
    else:
        report["acceptance"] = {"ok": False, "issues": ["skipped: fix build and routes first"]}

    passed = all(report[g]["ok"] for g in ("routes", "build", "security", "acceptance"))
    report["passed"] = passed
    await ctx.orch.save_artifact(ctx.order_id, "qa_report", report, "qa")
    failed = [g for g in ("routes", "build", "security", "acceptance") if not report[g]["ok"]]
    return Outcome(
        Ev.CHECKS_PASSED if passed else Ev.CHECKS_FAILED,
        data={"failed_gates": failed},
        details="" if passed else "не пройдены: " + ", ".join(failed),
    )
