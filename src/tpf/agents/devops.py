"""DevOps: deploys the checked code to staging for client review, then to production."""
from __future__ import annotations

from tpf.agents.base import Ctx, Outcome
from tpf.orchestrator.states import Ev
from tpf.tools.deploy import DeployError
from tpf.tools.template import all_files


def project_name(order_id: int) -> str:
    return f"tpf-order-{order_id}"


async def _files(ctx: Ctx) -> list[dict[str, str]] | None:
    code = await ctx.orch.latest_artifact(ctx.order_id, "code")
    return all_files(code["files"]) if code else None


async def deploy_staging(ctx: Ctx) -> Outcome:
    if ctx.deployer is None:
        return Outcome(escalate="Деплой не настроен: задайте VERCEL_TOKEN")
    files = await _files(ctx)
    if files is None:
        return Outcome(escalate="Нет кода для деплоя")
    try:
        dep = await ctx.deployer.deploy(project_name(ctx.order_id), files, production=False)
    except DeployError as e:
        return Outcome(escalate=f"Деплой на стейджинг не удался: {e}")
    await ctx.orch.pool.execute("UPDATE orders SET staging_url = $2 WHERE id = $1", ctx.order_id, dep.url)
    await ctx.orch.save_artifact(ctx.order_id, "deployment", {"target": "staging", "id": dep.id, "url": dep.url}, "devops")
    return Outcome(Ev.STAGING_DEPLOYED, data={"url": dep.url}, details=dep.url)


async def deploy_production(ctx: Ctx) -> Outcome:
    if ctx.deployer is None:
        return Outcome(escalate="Деплой не настроен: задайте VERCEL_TOKEN")
    files = await _files(ctx)
    if files is None:
        return Outcome(escalate="Нет кода для релиза")
    try:
        dep = await ctx.deployer.deploy(project_name(ctx.order_id), files, production=True)
    except DeployError as e:
        return Outcome(escalate=f"Релиз не удался: {e}")
    await ctx.orch.pool.execute("UPDATE orders SET production_url = $2 WHERE id = $1", ctx.order_id, dep.url)
    await ctx.orch.save_artifact(ctx.order_id, "deployment", {"target": "production", "id": dep.id, "url": dep.url}, "devops")

    repo_url = None
    if ctx.github is not None:
        try:
            repo_url = await ctx.github.publish(project_name(ctx.order_id), files, f"Release of order #{ctx.order_id}")
            await ctx.orch.log(ctx.order_id, "code_published", "devops", {"repo": repo_url}, kind="tool")
        except Exception as e:  # the site is live; a failed code handover is logged, not fatal
            await ctx.orch.log(ctx.order_id, "code_publish_failed", "devops", {"error": str(e)[:500]}, kind="tool")
    return Outcome(Ev.RELEASED, data={"url": dep.url, "repo": repo_url}, details=dep.url)
