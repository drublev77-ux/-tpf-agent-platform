"""DevOps: deploys the checked code to staging for client review, then to production."""
from __future__ import annotations

from tpf.agents.base import Ctx, Outcome
from tpf.orchestrator.states import Ev
from tpf.tools.deploy import Deployment, DeployError
from tpf.tools.template import all_files


def project_name(order_id: int) -> str:
    return f"tpf-order-{order_id}"


async def _files(ctx: Ctx) -> list[dict[str, str]] | None:
    code = await ctx.orch.latest_artifact(ctx.order_id, "code")
    return all_files(code["files"]) if code else None


async def _reuse_qa_preview(ctx: Ctx) -> Deployment | None:
    """With SANDBOX=vercel, QA already built the latest code as a preview: use it as staging."""
    report = await ctx.orch.latest_artifact(ctx.order_id, "qa_report")
    preview = (report or {}).get("preview")
    if not (report and report.get("passed") and preview):
        return None
    latest = await ctx.orch.pool.fetchval(
        "SELECT max(version) FROM artifacts WHERE order_id = $1 AND kind = 'code'", ctx.order_id
    )
    if preview.get("code_version") != latest:
        return None
    return Deployment(preview["deployment_id"], preview["url"], "READY")


async def deploy_staging(ctx: Ctx) -> Outcome:
    if ctx.deployer is None:
        return Outcome(escalate="Деплой не настроен: задайте VERCEL_TOKEN")
    files = await _files(ctx)
    if files is None:
        return Outcome(escalate="Нет кода для деплоя")
    dep = await _reuse_qa_preview(ctx)
    if dep is None:
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


REPO_TEXT = {
    "invited": {
        "ru": "Мы пригласили пользователя GitHub {user} в репозиторий с исходным кодом заказа №{id}: {url}. Примите приглашение — GitHub отправил его письмом и показывает на странице репозитория.",
        "en": "We invited GitHub user {user} to the source code repository of order #{id}: {url}. Accept the invitation — GitHub emailed it and shows it on the repository page.",
        "zh": "我们已邀请 GitHub 用户 {user} 访问订单 #{id} 的源代码仓库：{url}。请接受邀请——GitHub 已发送邮件，仓库页面上也会显示。",
    },
    "already": {
        "ru": "У пользователя GitHub {user} уже есть доступ к репозиторию заказа №{id}: {url}.",
        "en": "GitHub user {user} already has access to the repository of order #{id}: {url}.",
        "zh": "GitHub 用户 {user} 已拥有订单 #{id} 仓库的访问权限：{url}。",
    },
    "not_found": {
        "ru": "Не нашли пользователя GitHub «{user}». Проверьте имя и отправьте его ещё раз на странице заказа.",
        "en": "We could not find the GitHub user “{user}”. Please check the name and send it again on your order page.",
        "zh": "未找到 GitHub 用户“{user}”。请核对用户名后在订单页面重新提交。",
    },
}


async def grant_repo_access(ctx: Ctx) -> Outcome:
    """Side task (does not move the order): invite the client's GitHub account to the order's repository."""
    user = str(ctx.payload.get("username", ""))
    lang = ctx.order["language"] if ctx.order["language"] in ("ru", "en", "zh") else "en"
    if ctx.github is None:
        await ctx.orch.notify_operator(ctx.order, f"Заказ #{ctx.order_id}: клиент просит доступ к коду для GitHub-пользователя {user}, но GITHUB_TOKEN не задан")
        return Outcome(data={"result": "not_configured"})
    repo = project_name(ctx.order_id)
    url = f"https://github.com/{ctx.github.owner}/{repo}"
    try:
        result = await ctx.github.invite(repo, user)
    except LookupError:
        result = "not_found"
    except Exception as e:  # noqa: BLE001 — a delivered order must not be paused by a failed invitation
        await ctx.orch.log(ctx.order_id, "repo_access", "devops", {"username": user, "error": str(e)[:300]}, kind="tool")
        await ctx.orch.notify_operator(ctx.order, f"Заказ #{ctx.order_id}: не удалось пригласить {user} в репозиторий: {e}")
        return Outcome(data={"result": "failed", "username": user})
    await ctx.orch.log(ctx.order_id, "repo_access", "devops", {"username": user, "result": result}, kind="tool")
    await ctx.orch.notify_client(ctx.order, REPO_TEXT[result][lang].format(user=user, id=ctx.order_id, url=url))
    return Outcome(data={"result": result, "username": user})
