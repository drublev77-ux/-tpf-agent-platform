"""DevOps: deploys the checked code to staging for client review, then to production."""
from __future__ import annotations

from tpf.agents.base import Ctx, Outcome
from tpf.orchestrator.states import Ev, State
from tpf.tools.deploy import Deployment, DeployError
from tpf.tools.telegram import TelegramError, webhook_secret
from tpf.tools.shop_pay import NAMES as PAY_NAMES, NOTIFY_PATH, effective_mode
from tpf.tools.shop_stripe import StripeKeyError
from tpf.tools.template import BOT_WEBHOOK_PATH, SHOP_WEBHOOK_PATH, all_files, has_database, kind_of
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


async def connect_shop_to(ctx: Ctx, env: str, site_url: str) -> str | None:
    """Give the shop at this deployment its owner's payment keys (and, for Stripe, a webhook). Returns the mode
    payments now run in ('test' or 'live'), or None when nothing was connected: the owner has not entered a
    key, or the key is a live one and this is the review build. Raises StripeKeyError when Stripe refuses the key."""
    row = await ctx.orch.pool.fetchrow(
        "SELECT secret_key, mode, provider, account_id FROM payment_connections WHERE order_id = $1", ctx.order_id)
    if row is None or ctx.tenant_db is None:
        return None
    provider = row["provider"]
    if env == "stg" and effective_mode(provider, row["mode"], env) == "live":
        # The review build is for trying things out and its orders are thrown away: a live key there would
        # charge real cards for them. It waits for the release; the review build keeps whatever it had.
        await ctx.orch.log(ctx.order_id, "payments_held", "devops", {"env": env, "mode": "live"}, kind="tool")
        return None
    key = row["secret_key"]
    db = await ctx.tenant_db.ensure(ctx.orch.pool, ctx.order_id, env)
    if provider != "stripe":
        # YooKassa and bePaid: nothing to register with the provider. The shop checks every payment with the
        # provider itself, and notes its payments in a table the platform makes for it.
        mode = effective_mode(provider, row["mode"], env)
        await ctx.tenant_db.ensure_payments_table(db)
        await ctx.tenant_db.set_secrets(db, {
            "pay_provider": provider, f"{provider}_shop_id": row["account_id"], f"{provider}_secret_key": key,
            **({"bepaid_test": "1" if mode == "test" else "0"} if provider == "bepaid" else {})})
        await ctx.orch.pool.execute(
            "UPDATE payment_connections SET connected_env = $2, updated_at = now() WHERE order_id = $1", ctx.order_id, env)
        await ctx.orch.log(ctx.order_id, "payments_connected", "devops", {"env": env, "mode": mode, "provider": provider}, kind="tool")
        return mode
    known = await ctx.tenant_db.get_secrets(db)
    same_key = known.get("stripe_secret_key") == key
    hook = site_url.rstrip("/") + "/" + SHOP_WEBHOOK_PATH.removeprefix("app/").removesuffix("/route.ts")
    endpoint_id, secret = await ctx.shop_stripe.ensure_webhook(key, hook, known.get("stripe_webhook_id", "") if same_key else "")
    values = {"pay_provider": "stripe", "stripe_secret_key": key, "stripe_webhook_id": endpoint_id}
    if secret:
        values["stripe_webhook_secret"] = secret
    await ctx.tenant_db.set_secrets(db, values)
    await ctx.orch.pool.execute(
        "UPDATE payment_connections SET connected_env = $2, updated_at = now() WHERE order_id = $1", ctx.order_id, env)
    await ctx.orch.log(ctx.order_id, "payments_connected", "devops", {"env": env, "mode": row["mode"], "webhook": hook}, kind="tool")
    return row["mode"]


async def _connect_after_deploy(ctx: Ctx, env: str, url: str) -> None:
    """A bot is useless until Telegram points at the deployment, and a shop cannot take payments until Stripe
    does; a refused token or key must not stop the release."""
    kind = await _kind(ctx)
    connect = {"bot": connect_bot_to, "shop": connect_shop_to}.get(kind)
    if connect is None:
        return
    try:
        await connect(ctx, env, url)
    except Exception as e:  # noqa: BLE001
        what = "Telegram-бота" if kind == "bot" else "приём оплаты магазина"
        await ctx.orch.log(ctx.order_id, f"{kind}_connect_failed", "devops", {"env": env, "error": str(e)[:300]}, kind="tool")
        await ctx.orch.notify_operator(ctx.order, f"Заказ #{ctx.order_id}: не удалось подключить {what} ({env}): {e}")


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


async def redeploy_production(ctx: Ctx) -> Outcome:
    """Side task (does not move the order): deploy a delivered order again from its latest code and the
    platform's current files — after a platform fix, or after an engineer corrected the code. The address
    and the data stay; the client is not notified."""
    if ctx.order["state"] != State.DELIVERED or not ctx.order["production_url"] or ctx.deployer is None:
        return Outcome(data={"result": "not_delivered"})
    files = await _files(ctx)
    if files is None:
        return Outcome(data={"result": "no_code"})
    app = has_database(await _kind(ctx))
    problem = await _database(ctx, files, "prod", project_name(ctx.order_id)) if app else None
    dep = None
    if not problem:
        try:
            dep = await ctx.deployer.deploy(project_name(ctx.order_id), files, production=True)
        except DeployError as e:      # a failed build leaves the version that is live untouched
            problem = f"{e}: {(e.log or '')[-1500:]}"
    if dep is not None and app:
        problem = await ctx.deployer.health(dep.url)
    if problem:
        await ctx.orch.log(ctx.order_id, "redeploy_failed", "devops", {"error": str(problem)[:2000]}, kind="tool")
        await ctx.orch.notify_operator(ctx.order, f"Заказ #{ctx.order_id}: повторная выкладка не удалась: {str(problem)[:500]}")
        return Outcome(data={"result": "failed"})
    await _connect_after_deploy(ctx, "prod", ctx.order["production_url"])
    await ctx.orch.save_artifact(ctx.order_id, "deployment", {"target": "production", "id": dep.id, "url": dep.url, "redeploy": True}, "devops")
    if ctx.github is not None:
        try:
            await ctx.github.publish(project_name(ctx.order_id), files, f"Redeploy of order #{ctx.order_id}")
        except Exception as e:  # noqa: BLE001 — the shop is live; the repository copy can wait
            await ctx.orch.log(ctx.order_id, "code_publish_failed", "devops", {"error": str(e)[:500]}, kind="tool")
    await ctx.orch.log(ctx.order_id, "redeployed", "devops", {"url": dep.url, "note": str(ctx.payload.get("note", ""))[:500]}, kind="tool")
    return Outcome(data={"result": "redeployed", "url": dep.url})


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
    target = _live_target(ctx)
    if target is None:
        await ctx.orch.notify_client(ctx.order, BOT_TEXT["saved"][lang].format(id=ctx.order_id))
        return Outcome(data={"result": "saved"})
    env, url = target
    try:
        username = await connect_bot_to(ctx, env, url)
    except TelegramError as e:
        await ctx.orch.log(ctx.order_id, "bot_connect_failed", "devops", {"env": env, "error": str(e)[:300]}, kind="tool")
        await ctx.orch.notify_client(ctx.order, BOT_TEXT["failed"][lang].format(id=ctx.order_id))
        return Outcome(data={"result": "failed"})
    await ctx.orch.notify_client(ctx.order, BOT_TEXT["connected"][lang].format(id=ctx.order_id, user=username))
    return Outcome(data={"result": "connected", "env": env, "username": username})


PAY_TEXT = {
    "connected_test": {
        "ru": "Stripe подключён к магазину заказа №{id} в тестовом режиме: оплату можно проверить картой 4242 4242 4242 4242, деньги не списываются. Перед приёмом настоящих платежей замените ключ на рабочий (sk_live…) на странице заказа.",
        "en": "Stripe is connected to the shop of order #{id} in test mode: try a payment with card 4242 4242 4242 4242, no money moves. Before taking real payments, replace the key with the live one (sk_live…) on your order page.",
        "zh": "Stripe 已以测试模式连接到订单 #{id} 的商店：可用卡号 4242 4242 4242 4242 测试付款，不会实际扣款。正式收款前，请在订单页面将密钥更换为正式密钥（sk_live…）。",
    },
    "connected_live": {
        "ru": "Stripe подключён к магазину заказа №{id} в рабочем режиме: покупатели платят настоящими картами, деньги поступают на ваш счёт Stripe.",
        "en": "Stripe is connected to the shop of order #{id} in live mode: customers pay with real cards and the money goes to your Stripe account.",
        "zh": "Stripe 已以正式模式连接到订单 #{id} 的商店：顾客使用真实银行卡付款，款项进入您的 Stripe 账户。",
    },
    "saved": {
        "ru": "Ключ Stripe для заказа №{id} сохранён. Мы подключим оплату, как только будет готова версия для проверки.",
        "en": "The Stripe key for order #{id} is saved. We will connect payments as soon as the review version is ready.",
        "zh": "订单 #{id} 的 Stripe 密钥已保存。审核版本准备好后我们会立即接通付款。",
    },
    "held": {
        "ru": "Рабочий ключ Stripe для заказа №{id} сохранён. К версии для проверки мы его не подключаем, чтобы при проверке не списывались настоящие деньги: оплата заработает сразу после запуска магазина. Чтобы попробовать оплату уже сейчас, введите на странице заказа тестовый ключ (sk_test…), а перед запуском снова замените его на рабочий.",
        "en": "The live Stripe key for order #{id} is saved. We do not connect it to the review version, so that reviewing the shop cannot charge real money: payments will start working as soon as the shop is released. To try a payment now, enter the test key (sk_test…) on your order page and switch back to the live one before release.",
        "zh": "订单 #{id} 的 Stripe 正式密钥已保存。为避免审核时产生真实扣款，我们不会把它连接到审核版本：商店上线后付款将立即生效。如需现在测试付款，请在订单页面输入测试密钥（sk_test…），上线前再换回正式密钥。",
    },
    "failed": {
        "ru": "Не удалось подключить Stripe к заказу №{id}: Stripe не принял ключ. Скопируйте секретный ключ в панели Stripe ещё раз и введите его на странице заказа.",
        "en": "We could not connect Stripe to order #{id}: Stripe refused the key. Copy the secret key from the Stripe dashboard again and enter it on your order page.",
        "zh": "无法将 Stripe 连接到订单 #{id}：Stripe 拒绝了该密钥。请在 Stripe 后台重新复制密钥并在订单页面输入。",
    },
}


OTHER_PAY_TEXT = {
    "connected_test": {
        "ru": "{name}: приём оплаты подключён к магазину заказа №{id} в тестовом режиме — деньги не списываются. Оплату можно проверить тестовой картой из документации {name}.",
        "en": "{name}: payments are connected to the shop of order #{id} in test mode — no money moves. Try a payment with a test card from the {name} documentation.",
        "zh": "{name}：订单 #{id} 的商店已以测试模式接通付款——不会实际扣款。可使用 {name} 文档中的测试卡测试付款。",
    },
    "connected_live": {
        "ru": "{name}: приём оплаты подключён к магазину заказа №{id} в рабочем режиме — покупатели платят настоящими картами, деньги поступают на ваш счёт в {name}.",
        "en": "{name}: payments are connected to the shop of order #{id} in live mode — customers pay with real cards and the money goes to your {name} account.",
        "zh": "{name}：订单 #{id} 的商店已以正式模式接通付款——顾客使用真实银行卡付款，款项进入您的 {name} 账户。",
    },
    "saved": {
        "ru": "Ключи {name} для заказа №{id} сохранены. Мы подключим оплату, как только будет готова версия для проверки.",
        "en": "The {name} keys for order #{id} are saved. We will connect payments as soon as the review version is ready.",
        "zh": "订单 #{id} 的 {name} 密钥已保存。审核版本准备好后我们会立即接通付款。",
    },
    "held": {
        "ru": "Рабочий ключ {name} для заказа №{id} сохранён. К версии для проверки мы его не подключаем, чтобы при проверке не списывались настоящие деньги: оплата заработает сразу после запуска магазина. Чтобы попробовать оплату уже сейчас, введите на странице заказа ключ тестового магазина (test_…), а перед запуском снова замените его на рабочий.",
        "en": "The live {name} key for order #{id} is saved. We do not connect it to the review version, so that reviewing the shop cannot charge real money: payments will start working as soon as the shop is released. To try a payment now, enter the key of your test shop (test_…) on your order page and switch back to the live one before release.",
        "zh": "订单 #{id} 的 {name} 正式密钥已保存。为避免审核时产生真实扣款，我们不会把它连接到审核版本：商店上线后付款将立即生效。如需现在测试付款，请在订单页面输入测试商店的密钥（test_…），上线前再换回正式密钥。",
    },
    "notify": {
        "ru": " Чтобы оплата засчитывалась, даже если покупатель закрыл страницу, не вернувшись в магазин, добавьте в кабинете ЮKassa («Интеграция → HTTP-уведомления») адрес {hook} и событие payment.succeeded.",
        "en": " So that a payment counts even when the customer closes the page without returning to the shop, add the address {hook} with the event payment.succeeded in your YooKassa dashboard (Integration → HTTP notifications).",
        "zh": " 为确保顾客未返回商店就关闭页面时付款仍能记入，请在 YooKassa 后台（集成 → HTTP 通知）添加地址 {hook} 并选择事件 payment.succeeded。",
    },
}


def _pay_text(kind: str, provider: str, lang: str, order_id: int, site_url: str = "") -> str:
    """What to tell the owner about their payment keys, in the words of the provider they chose."""
    if provider == "stripe":
        return PAY_TEXT[kind][lang].format(id=order_id)
    text = OTHER_PAY_TEXT[kind][lang].format(id=order_id, name=PAY_NAMES[provider])
    if provider == "yookassa" and kind == "connected_live" and site_url:
        text += OTHER_PAY_TEXT["notify"][lang].format(hook=site_url.rstrip("/") + NOTIFY_PATH)
    return text


def _live_target(ctx: Ctx) -> tuple[str, str] | None:
    """Which deployment a newly entered token or key should be connected to right now, if any."""
    state = ctx.order["state"]
    if ctx.order["production_url"] and state == State.DELIVERED:
        return "prod", ctx.order["production_url"]
    if ctx.order["staging_url"] and state in (State.AWAITING_ACCEPTANCE, State.AWAITING_FINAL_PAYMENT):
        return "stg", ctx.order["staging_url"]
    return None


async def connect_payments(ctx: Ctx) -> Outcome:
    """Side task (does not move the order): the owner entered or replaced the shop's Stripe key."""
    lang = ctx.order["language"] if ctx.order["language"] in ("ru", "en", "zh") else "en"
    target = _live_target(ctx)
    row = await ctx.orch.pool.fetchrow("SELECT mode, provider FROM payment_connections WHERE order_id = $1", ctx.order_id)
    if row is None:
        return Outcome(data={"result": "no_key"})
    provider = row["provider"]
    live = effective_mode(provider, row["mode"], "stg") == "live"    # a key that must not reach the review build
    if target is None or (live and target[0] == "stg"):
        await ctx.orch.notify_client(ctx.order, _pay_text("held" if live else "saved", provider, lang, ctx.order_id))
        return Outcome(data={"result": "held" if live else "saved"})
    try:
        mode = await connect_shop_to(ctx, *target)
    except StripeKeyError as e:
        await ctx.orch.log(ctx.order_id, "shop_connect_failed", "devops", {"env": target[0], "error": str(e)[:300]}, kind="tool")
        await ctx.orch.notify_client(ctx.order, PAY_TEXT["failed"][lang].format(id=ctx.order_id))
        return Outcome(data={"result": "failed"})
    await ctx.orch.notify_client(ctx.order, _pay_text(f"connected_{mode}", provider, lang, ctx.order_id, target[1]))
    return Outcome(data={"result": "connected", "env": target[0], "mode": mode, "provider": provider})
