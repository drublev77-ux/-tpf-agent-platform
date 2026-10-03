"""DevOps: deploys the checked code to staging for client review, then to production."""
from __future__ import annotations

from tpf.agents.base import Ctx, Outcome
from tpf.orchestrator.states import Ev, State
from tpf.tools.deploy import Deployment, DeployError
from tpf.tools.telegram import TelegramError, webhook_secret
from tpf.tools.template import BOT_WEBHOOK_PATH, all_files, has_database, kind_of
from tpf.tools.tenantdb import TenantDBError


def project_name(order_id: int) -> str:
    return f"tpf-order-{order_id}"


def staging_project(order_id: int) -> str:
    """Review builds live in their own Vercel project: the first deployment of a project takes its
    public address, and the final address must stay empty until the client has accepted and paid."""
    return f"tpf-order-{order_id}-preview"


async def _kind(ctx: Ctx) -> str:
    return kind_of(await ctx.orch.latest_artifact(ctx.order_id, "spec"))


async def _files(ctx: Ctx) -> list[dict[str, str]] | None:
    code = await ctx.orch.latest_artifact(ctx.order_id, "code")
    return all_files(code["files"], ctx.form_endpoint, await _kind(ctx)) if code else None


async def _database(ctx: Ctx, files: list[dict[str, str]], env: str, project: str) -> str | None:
    """For an application, get its database ready before the deployment. Returns why it failed, or None."""
    from tpf.agents.qa import prepare_database  # qa imports this module
    try:
        await prepare_database(ctx, files, env, project)
    except (TenantDBError, DeployError) as e:
        return str(e)
    return None


async def connect_bot_to(ctx: Ctx, env: str, site_url: str) -> str | None:
    """Point the client's Telegram bot at this deployment: hand the token to the application's database
    and register the webhook. Returns the bot's username, or None when the client has not entered a token.
    Raises TelegramError when Telegram refuses the token or the webhook."""
    row = await ctx.orch.pool.fetchrow("SELECT token FROM bot_connections WHERE order_id = $1", ctx.order_id)
    if row is None or ctx.tenant_db is None:
        return None
    token = row["token"]
    username = await ctx.telegram.get_me(token)
    db = await ctx.tenant_db.ensure(ctx.orch.pool, ctx.order_id, env)
    await ctx.tenant_db.set_secrets(db, {"bot_token": token, "bot_username": username})
    hook = site_url.rstrip("/") + "/" + BOT_WEBHOOK_PATH.removeprefix("app/").removesuffix("/route.ts")
    await ctx.telegram.set_webhook(token, hook, webhook_secret(db.session_secret))
    await ctx.orch.pool.execute(
        "UPDATE bot_connections SET username = $2, connected_env = $3, updated_at = now() WHERE order_id = $1",
        ctx.order_id, username, env)
    await ctx.orch.log(ctx.order_id, "bot_connected", "devops", {"env": env, "username": username, "webhook": hook}, kind="tool")
    return username


async def _connect_after_deploy(ctx: Ctx, env: str, url: str) -> None:
    """A bot order's deployment is useless until Telegram points at it; a refused token must not stop the release."""
    if await _kind(ctx) != "bot":
        return
    try:
        await connect_bot_to(ctx, env, url)
    except Exception as e:  # noqa: BLE001
        await ctx.orch.log(ctx.order_id, "bot_connect_failed", "devops", {"env": env, "error": str(e)[:300]}, kind="tool")
        await ctx.orch.notify_operator(ctx.order, f"Заказ #{ctx.order_id}: не удалось подключить Telegram-бота ({env}): {e}")


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
    app = has_database(await _kind(ctx))
    dep = await _reuse_qa_preview(ctx)
    if dep is None:
        if app and (problem := await _database(ctx, files, "stg", staging_project(ctx.order_id))):
            return Outcome(escalate=f"База данных для проверки не готова: {problem}")
        try:
            dep = await ctx.deployer.deploy(staging_project(ctx.order_id), files, production=False)
        except DeployError as e:
            return Outcome(escalate=f"Деплой на стейджинг не удался: {e}")
        if app and (problem := await ctx.deployer.health(dep.url)):
            return Outcome(escalate=f"Приложение на стейджинге не работает с базой данных: {problem}")
    await ctx.orch.pool.execute("UPDATE orders SET staging_url = $2 WHERE id = $1", ctx.order_id, dep.url)
    await _connect_after_deploy(ctx, "stg", dep.url)
    await ctx.orch.save_artifact(ctx.order_id, "deployment", {"target": "staging", "id": dep.id, "url": dep.url}, "devops")
    return Outcome(Ev.STAGING_DEPLOYED, data={"url": dep.url}, details=dep.url)


async def deploy_production(ctx: Ctx) -> Outcome:
    if ctx.deployer is None:
        return Outcome(escalate="Деплой не настроен: задайте VERCEL_TOKEN")
    files = await _files(ctx)
    if files is None:
        return Outcome(escalate="Нет кода для релиза")
    app = has_database(await _kind(ctx))
    if app and (problem := await _database(ctx, files, "prod", project_name(ctx.order_id))):
        return Outcome(escalate=f"Рабочая база данных не готова: {problem}")
    try:
        dep = await ctx.deployer.deploy(project_name(ctx.order_id), files, production=True)
    except DeployError as e:
        return Outcome(escalate=f"Релиз не удался: {e}")
    if app and (problem := await ctx.deployer.health(dep.url)):
        return Outcome(escalate=f"Приложение выпущено, но не работает с базой данных: {problem}")
    await ctx.orch.pool.execute("UPDATE orders SET production_url = $2 WHERE id = $1", ctx.order_id, dep.url)
    await _connect_after_deploy(ctx, "prod", dep.url)
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


BOT_TEXT = {
    "connected": {
        "ru": "Бот @{user} подключён к заказу №{id}. Откройте https://t.me/{user} и отправьте /start.",
        "en": "The bot @{user} is connected to order #{id}. Open https://t.me/{user} and send /start.",
        "zh": "机器人 @{user} 已连接到订单 #{id}。请打开 https://t.me/{user} 并发送 /start。",
    },
    "saved": {
        "ru": "Токен бота для заказа №{id} сохранён. Мы подключим бота, как только будет готова версия для проверки.",
        "en": "The bot token for order #{id} is saved. We will connect the bot as soon as the review version is ready.",
        "zh": "订单 #{id} 的机器人令牌已保存。审核版本准备好后我们会立即连接机器人。",
    },
    "failed": {
        "ru": "Не удалось подключить бота к заказу №{id}: Telegram не принял токен. Скопируйте токен из @BotFather ещё раз и введите его на странице заказа.",
        "en": "We could not connect the bot to order #{id}: Telegram refused the token. Copy the token from @BotFather again and enter it on your order page.",
        "zh": "无法将机器人连接到订单 #{id}：Telegram 拒绝了令牌。请从 @BotFather 重新复制令牌并在订单页面输入。",
    },
}


async def connect_bot(ctx: Ctx) -> Outcome:
    """Side task (does not move the order): the client entered or replaced the bot token on their order page."""
    lang = ctx.order["language"] if ctx.order["language"] in ("ru", "en", "zh") else "en"
    state = ctx.order["state"]
    if ctx.order["production_url"] and state == State.DELIVERED:
        env, url = "prod", ctx.order["production_url"]
    elif ctx.order["staging_url"] and state in (State.AWAITING_ACCEPTANCE, State.AWAITING_FINAL_PAYMENT):
        env, url = "stg", ctx.order["staging_url"]
    else:
        await ctx.orch.notify_client(ctx.order, BOT_TEXT["saved"][lang].format(id=ctx.order_id))
        return Outcome(data={"result": "saved"})
    try:
        username = await connect_bot_to(ctx, env, url)
    except TelegramError as e:
        await ctx.orch.log(ctx.order_id, "bot_connect_failed", "devops", {"env": env, "error": str(e)[:300]}, kind="tool")
        await ctx.orch.notify_client(ctx.order, BOT_TEXT["failed"][lang].format(id=ctx.order_id))
        return Outcome(data={"result": "failed"})
    await ctx.orch.notify_client(ctx.order, BOT_TEXT["connected"][lang].format(id=ctx.order_id, user=username))
    return Outcome(data={"result": "connected", "env": env, "username": username})
