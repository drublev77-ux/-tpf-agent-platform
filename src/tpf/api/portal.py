"""Client portal: one page per order, opened by a private link (/portal/{id}?t=<client token>).

The client sees the status, the conversation, the spec and estimate, payment and staging links,
and acts on the order: answer questions, approve or reject the estimate, accept the work or ask
for changes. Server-rendered HTML, no JavaScript, in the client's language (ru / en / zh).
"""
from __future__ import annotations

import hmac
import io
import re
import zipfile
from html import escape
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tpf.notifications import portal_url  # noqa: F401  (re-exported)
import asyncpg

from tpf.orchestrator import queue
from tpf.orchestrator.states import Ev, State, TransitionError
from tpf.config import get_settings
from tpf.tools.telegram import TOKEN_RE, TelegramAPI, TelegramError
from tpf.tools.template import all_files, form_endpoint, kind_of

router = APIRouter(include_in_schema=False)

T: dict[str, dict[str, str]] = {
    "ru": {
        "bot": "Telegram-бот", "bot_how": "Создайте бота в Telegram: откройте @BotFather, отправьте /newbot, задайте имя — он пришлёт токен вида 123456789:AA… Вставьте его сюда. Токен хранится у нас в закрытом виде; не пересылайте его в переписке.", "bot_token": "Токен бота", "bot_save": "Подключить бота", "bot_on": "Подключён бот", "bot_wait": "Токен сохранён. Бот заработает, когда будет готова версия для проверки.", "bot_stg": "Сейчас бот работает на проверочной версии.", "bot_change": "Заменить токен", "admin": "Панель управления", "admin_note": "Здесь вы сами добавляете и меняете данные приложения. Пароль никому не передавайте.", "admin_stg_note": "Это проверочная версия: данные из неё в рабочую версию не переносятся, у рабочей будет свой пароль.", "password": "Пароль", "inbox": "Сообщения с формы вашего сайта", "inbox_note": "Каждое сообщение мы также пересылаем вам на почту. Здесь последние 50.", "missing": "Нужно от вас — пока на сайте заглушки", "source": "Исходный код", "zip": "Скачать ZIP", "gh_label": "Ваше имя пользователя на GitHub — пригласим в приватный репозиторий", "gh_btn": "Получить доступ", "title": "Заказ №{id}", "status": "Статус", "conversation": "Переписка", "you": "Вы", "studio": "TPF Technology",
        "spec": "Техническое задание", "pages": "Страницы", "criteria": "Критерии приёмки", "estimate": "Смета",
        "hours": "Трудоёмкость", "price": "Стоимость", "duration": "Срок", "days": "дн.", "h": "ч",
        "pay_pre": "Предоплата", "pay_final": "Остаток перед запуском", "paid": "оплачено", "pay_now": "Оплатить",
        "reply": "Ответить на вопросы", "send": "Отправить", "approve": "Утвердить смету",
        "reject": "Нужны изменения в смете", "comment": "Комментарий (необязательно)",
        "comment_req": "Что нужно изменить?", "staging": "Посмотреть результат", "accept": "Принять работу",
        "changes": "Запросить правки", "live": "Рабочая версия", "done": "Готово: {what}",
        "err": "Не получилось: {why}", "open_link": "Открыть",
    },
    "en": {
        "bot": "Telegram bot", "bot_how": "Create the bot in Telegram: open @BotFather, send /newbot, choose a name — it replies with a token like 123456789:AA… Paste it here. We store the token privately; do not send it in messages.", "bot_token": "Bot token", "bot_save": "Connect the bot", "bot_on": "Connected bot", "bot_wait": "The token is saved. The bot will start working when the review version is ready.", "bot_stg": "The bot is currently running on the review version.", "bot_change": "Replace the token", "admin": "Admin area", "admin_note": "This is where you add and edit the application's data yourself. Keep the password private.", "admin_stg_note": "This is the review version: its data is not carried over to the live version, which gets its own password.", "password": "Password", "inbox": "Messages from your site's form", "inbox_note": "Each message is also e-mailed to you. The latest 50 are shown here.", "missing": "Needed from you — the site shows placeholders for now", "source": "Source code", "zip": "Download ZIP", "gh_label": "Your GitHub username — we will invite you to the private repository", "gh_btn": "Get access", "title": "Order #{id}", "status": "Status", "conversation": "Conversation", "you": "You", "studio": "TPF Technology",
        "spec": "Specification", "pages": "Pages", "criteria": "Acceptance criteria", "estimate": "Estimate",
        "hours": "Effort", "price": "Price", "duration": "Timeline", "days": "days", "h": "h",
        "pay_pre": "Deposit", "pay_final": "Balance before release", "paid": "paid", "pay_now": "Pay now",
        "reply": "Answer our questions", "send": "Send", "approve": "Approve the estimate",
        "reject": "Request estimate changes", "comment": "Comment (optional)",
        "comment_req": "What should change?", "staging": "Review the result", "accept": "Accept the work",
        "changes": "Request changes", "live": "Live version", "done": "Done: {what}",
        "err": "Could not do that: {why}", "open_link": "Open",
    },
    "zh": {
        "bot": "Telegram 机器人", "bot_how": "在 Telegram 中创建机器人：打开 @BotFather，发送 /newbot，设置名称——它会回复形如 123456789:AA… 的令牌。请粘贴到此处。令牌由我们保密存储；请勿在消息中发送。", "bot_token": "机器人令牌", "bot_save": "连接机器人", "bot_on": "已连接机器人", "bot_wait": "令牌已保存。审核版本准备好后机器人将开始工作。", "bot_stg": "机器人目前运行在审核版本上。", "bot_change": "更换令牌", "admin": "管理后台", "admin_note": "您可以在这里自行添加和修改应用数据。请勿泄露密码。", "admin_stg_note": "这是审核版本：其中的数据不会带到正式版本，正式版本会有单独的密码。", "password": "密码", "inbox": "您网站表单收到的留言", "inbox_note": "每条留言也会通过邮件发送给您。此处显示最近 50 条。", "missing": "需要您提供——网站目前显示占位符", "source": "源代码", "zip": "下载 ZIP", "gh_label": "您的 GitHub 用户名——我们将邀请您加入私有仓库", "gh_btn": "获取访问权限", "title": "订单 #{id}", "status": "状态", "conversation": "沟通记录", "you": "您", "studio": "TPF Technology",
        "spec": "需求规格书", "pages": "页面", "criteria": "验收标准", "estimate": "报价",
        "hours": "工作量", "price": "价格", "duration": "工期", "days": "天", "h": "小时",
        "pay_pre": "预付款", "pay_final": "上线前尾款", "paid": "已付", "pay_now": "立即付款",
        "reply": "回复我们的问题", "send": "发送", "approve": "确认报价",
        "reject": "需要修改报价", "comment": "备注（可选）",
        "comment_req": "需要修改什么？", "staging": "查看成果", "accept": "验收通过",
        "changes": "提出修改", "live": "正式版本", "done": "已完成：{what}",
        "err": "操作失败：{why}", "open_link": "打开",
    },
}

STATUS: dict[str, dict[str, str]] = {
    "ru": {"new": "Заявка принята", "spec": "Готовим ТЗ и смету", "awaiting_client_info": "Ждём ваших ответов",
           "awaiting_estimate": "Ждём утверждения сметы", "awaiting_payment": "Ждём предоплату",
           "design": "Проектируем", "development": "Разрабатываем", "checks": "Проверяем качество",
           "staging": "Готовим показ", "awaiting_acceptance": "Ждём вашей приёмки",
           "awaiting_final_payment": "Ждём оплату остатка", "release": "Запускаем",
           "delivered": "Сдано", "paused": "Инженер разбирается с задачей", "cancelled": "Отменён"},
    "en": {"new": "Request received", "spec": "Preparing the spec and estimate", "awaiting_client_info": "Waiting for your answers",
           "awaiting_estimate": "Waiting for your estimate approval", "awaiting_payment": "Waiting for the deposit",
           "design": "Designing", "development": "Building", "checks": "Quality checks",
           "staging": "Preparing the preview", "awaiting_acceptance": "Waiting for your review",
           "awaiting_final_payment": "Waiting for the final payment", "release": "Releasing",
           "delivered": "Delivered", "paused": "An engineer is looking into it", "cancelled": "Cancelled"},
    "zh": {"new": "已收到需求", "spec": "正在准备规格书与报价", "awaiting_client_info": "等待您的回复",
           "awaiting_estimate": "等待您确认报价", "awaiting_payment": "等待预付款",
           "design": "设计中", "development": "开发中", "checks": "质量检查",
           "staging": "准备预览", "awaiting_acceptance": "等待您验收",
           "awaiting_final_payment": "等待尾款", "release": "上线中",
           "delivered": "已交付", "paused": "工程师正在处理", "cancelled": "已取消"},
}

DONE = {
    "ru": {"message": "ответ отправлен", "approved": "смета утверждена", "rejected": "замечания по смете отправлены",
           "accepted": "работа принята", "changes": "правки отправлены", "repo": "запрос отправлен, приглашение придёт от GitHub", "bot": "токен принят, подключаем бота"},
    "en": {"message": "reply sent", "approved": "estimate approved", "rejected": "estimate comments sent",
           "accepted": "work accepted", "changes": "change request sent", "repo": "request sent, GitHub will deliver the invitation", "bot": "token accepted, connecting the bot"},
    "zh": {"message": "回复已发送", "approved": "报价已确认", "rejected": "报价意见已发送",
           "accepted": "已验收", "changes": "修改意见已发送", "repo": "请求已发送，GitHub 将发出邀请", "bot": "令牌已接受，正在连接机器人"},
}

ERR = {
    "ru": {"state": "сейчас это действие недоступно", "comment": "напишите, что нужно изменить", "empty": "пустое сообщение",
           "gh": "имя пользователя GitHub указано неверно", "token": "Telegram не принял этот токен; скопируйте его из @BotFather ещё раз", "busy": "предыдущий запрос ещё выполняется"},
    "en": {"state": "this action is not available right now", "comment": "please tell us what to change", "empty": "the message is empty",
           "gh": "that GitHub username is not valid", "token": "Telegram did not accept this token; copy it from @BotFather again", "busy": "the previous request is still running"},
    "zh": {"state": "当前无法执行此操作", "comment": "请说明需要修改的内容", "empty": "消息为空",
           "gh": "GitHub 用户名无效", "token": "Telegram 未接受此令牌；请从 @BotFather 重新复制", "busy": "上一个请求仍在处理中"},
}
GH_USER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

CSS = """
:root{--bg:#07090C;--card:#0B0F14;--line:#1C2630;--ink:#E6EDF2;--muted:#9AA7B2;--accent:#5CE1F0;--accent-ink:#04161A;--ok:#7CF5C4;--warn:#FFB35C}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Manrope,system-ui,-apple-system,"Noto Sans SC",sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 16px 64px}header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:24px}
.brand{font-weight:700;letter-spacing:.02em}.brand span{color:var(--accent);font-weight:400}h1{font-size:28px;margin:0 0 6px}
.pill{display:inline-block;padding:6px 12px;border:1px solid var(--accent);border-radius:999px;color:var(--accent);font-size:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:16px}h2{font-size:18px;margin:0 0 12px}
.msg{padding:10px 0;border-top:1px solid var(--line);white-space:pre-line}.msg:first-of-type{border-top:0}.who{font-size:13px;color:var(--muted)}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:0}dt{color:var(--muted)}dd{margin:0}
ul{margin:6px 0 0;padding-left:20px}.muted{color:var(--muted)}
.btn{display:inline-block;min-height:44px;padding:11px 20px;border-radius:999px;border:0;background:var(--accent);color:var(--accent-ink);font:inherit;font-weight:700;text-decoration:none;cursor:pointer}
.btn.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}.btn:focus-visible,textarea:focus-visible{outline:2px solid var(--ok);outline-offset:2px}
form{margin-top:12px}input[type=text],input[type=password],input:not([type]){width:100%;height:48px;padding:0 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}textarea{width:100%;min-height:96px;padding:12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font:inherit}
label{display:block;font-size:14px;color:var(--muted);margin-bottom:6px}.row{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
.notice{border-color:var(--ok)}.error{border-color:var(--warn)}a{color:var(--accent)}
"""


async def _load(request: Request, order_id: int, token: str) -> dict[str, Any]:
    o = request.app.state.orch
    try:
        order = await o.get_order(order_id)
    except KeyError:
        raise HTTPException(404, "not found") from None
    if not token or not hmac.compare_digest(order["client_token"], token):
        raise HTTPException(404, "not found")  # same answer as a missing order
    return order


def _money(v: Any, cur: str) -> str:
    return f"{float(v):,.2f} {cur}".replace(",", " ")


def _form(action: str, token: str, inner: str) -> str:
    return f'<form method="post" action="{escape(action)}"><input type="hidden" name="t" value="{escape(token)}">{inner}</form>'


@router.get("/portal/{order_id}", response_class=HTMLResponse)
async def portal(order_id: int, request: Request, t: str = "", done: str = "", err: str = "") -> HTMLResponse:
    order = await _load(request, order_id, t)
    o = request.app.state.orch
    lang = order["language"] if order["language"] in T else "en"
    tr, st = T[lang], order["state"]
    spec = await o.latest_artifact(order_id, "spec")
    estimate = await o.latest_artifact(order_id, "estimate")
    messages = await o.messages(order_id)
    payments = await o.pool.fetch(
        "SELECT stage, amount, currency, status, url FROM payments WHERE order_id = $1 ORDER BY id", order_id)
    base = f"/portal/{order_id}"
    parts: list[str] = []

    if done:
        parts.append(f'<section class="notice" role="status">{escape(tr["done"].format(what=DONE[lang].get(done, done)))}</section>')
    if err:
        parts.append(f'<section class="error" role="alert">{escape(tr["err"].format(why=ERR[lang].get(err, err)))}</section>')

    # --- what the client can do right now ---
    if st == State.AWAITING_CLIENT_INFO:
        parts.append(f'<section><h2>{tr["reply"]}</h2>' + _form(f"{base}/message", t,
            f'<label for="m">{tr["reply"]}</label><textarea id="m" name="text" required></textarea>'
            f'<div class="row"><button class="btn">{tr["send"]}</button></div>') + "</section>")
    if st == State.AWAITING_ESTIMATE:
        parts.append(f'<section><h2>{tr["estimate"]}</h2>'
            + _form(f"{base}/estimate", t, f'<input type="hidden" name="approved" value="1"><button class="btn">{tr["approve"]}</button>')
            + _form(f"{base}/estimate", t,
                f'<input type="hidden" name="approved" value="0"><label for="c1">{tr["comment_req"]}</label>'
                f'<textarea id="c1" name="comment" required></textarea><div class="row"><button class="btn ghost">{tr["reject"]}</button></div>')
            + "</section>")
    pending = [p for p in payments if p["status"] == "pending" and p["url"]]
    if st in (State.AWAITING_PAYMENT, State.AWAITING_FINAL_PAYMENT) and pending:
        p = pending[-1]
        label = tr["pay_pre"] if p["stage"] == "prepayment" else tr["pay_final"]
        parts.append(f'<section><h2>{label}: {_money(p["amount"], p["currency"])}</h2>'
                     f'<a class="btn" href="{escape(p["url"])}">{tr["pay_now"]}</a></section>')
    if st == State.AWAITING_ACCEPTANCE:
        staging = f'<p><a class="btn ghost" href="{escape(order["staging_url"])}" target="_blank" rel="noopener">{tr["staging"]}</a></p>' if order["staging_url"] else ""
        parts.append(f'<section><h2>{tr["staging"]}</h2>{staging}'
            + _form(f"{base}/acceptance", t, f'<input type="hidden" name="approved" value="1"><button class="btn">{tr["accept"]}</button>')
            + _form(f"{base}/acceptance", t,
                f'<input type="hidden" name="approved" value="0"><label for="c2">{tr["comment_req"]}</label>'
                f'<textarea id="c2" name="comment" required></textarea><div class="row"><button class="btn ghost">{tr["changes"]}</button></div>')
            + "</section>")
    if kind_of(spec) == "bot" and st not in (State.CANCELLED,):
        parts.append(await _bot_block(o, order_id, t, tr))
    if st == State.AWAITING_ACCEPTANCE and order["staging_url"]:
        parts.append(await _admin_block(o, order_id, "stg", order["staging_url"], tr))
    if order["production_url"]:
        parts.append(f'<section><h2>{tr["live"]}</h2><a href="{escape(order["production_url"])}">{escape(order["production_url"])}</a></section>')
        if st == State.DELIVERED:
            parts.append(await _admin_block(o, order_id, "prod", order["production_url"], tr))

    if st == State.DELIVERED and await o.latest_artifact(order_id, "code"):
        published = await o.pool.fetchval(
            "SELECT 1 FROM events WHERE order_id = $1 AND name = 'code_published' LIMIT 1", order_id)
        gh = _form(f"{base}/repo-access", t,
                   f'<label for="gh">{tr["gh_label"]}</label><input id="gh" name="username" required maxlength="39" '
                   f'autocapitalize="none" autocomplete="username" spellcheck="false">'
                   f'<div class="row"><button class="btn ghost">{tr["gh_btn"]}</button></div>') if published else ""
        parts.append(f'<section><h2>{tr["source"]}</h2>'
                     f'<a class="btn" href="{base}/source.zip?t={quote(t)}">{tr["zip"]}</a>{gh}</section>')

    if st == State.DELIVERED:
        inbox = await o.pool.fetch(
            "SELECT created_at, data FROM form_submissions WHERE order_id = $1 ORDER BY id DESC LIMIT 50", order_id)
        if inbox:
            rows_html = "".join(
                f'<div class="msg"><div class="who">{f["created_at"]:%Y-%m-%d %H:%M} · {escape(str(f["data"].get("name", "")))} · '
                f'{escape(str(f["data"].get("contact", "")))}</div>{escape(str(f["data"].get("message", "")))}</div>' for f in inbox)
            parts.append(f'<section><h2>{tr["inbox"]}</h2><p class="muted">{tr["inbox_note"]}</p>{rows_html}</section>')

    # --- estimate and payments ---
    if estimate:
        cur = estimate.get("currency", "")
        rows = [(tr["hours"], f'{estimate.get("total_hours")} {tr["h"]}'), (tr["price"], _money(estimate.get("price", 0), cur)),
                (tr["duration"], f'{estimate.get("duration_days")} {tr["days"]}')]
        paid = {p["stage"] for p in payments if p["status"] == "paid"}
        for stage, key in (("prepayment", "pay_pre"), ("final", "pay_final")):
            amount = (estimate.get("payments") or {}).get(stage)
            if amount:
                rows.append((tr[key], _money(amount, cur) + (f' — {tr["paid"]}' if stage in paid else "")))
        parts.append(f'<section><h2>{tr["estimate"]}</h2><dl>'
                     + "".join(f"<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>" for k, v in rows) + "</dl></section>")

    # --- spec ---
    if spec:
        pages = "".join(f'<li><b>{escape(p.get("name", ""))}</b> — {escape(p.get("purpose", ""))}</li>' for p in spec.get("pages", []))
        crit = "".join(f"<li>{escape(c)}</li>" for c in spec.get("acceptance_criteria", []))
        miss = "".join(f"<li>{escape(c)}</li>" for c in spec.get("missing_content", []))
        parts.append(f'<section><h2>{tr["spec"]}: {escape(spec.get("title", ""))}</h2><p>{escape(spec.get("summary", ""))}</p>'
                     + (f'<h3 class="muted">{tr["pages"]}</h3><ul>{pages}</ul>' if pages else "")
                     + (f'<h3 class="muted">{tr["criteria"]}</h3><ul>{crit}</ul>' if crit else "")
                     + (f'<h3 style="color:var(--warn)">{tr["missing"]}</h3><ul>{miss}</ul>' if miss else "") + "</section>")

    # --- conversation ---
    if messages:
        msgs = "".join(
            f'<div class="msg"><div class="who">{tr["you"] if m["direction"] == "in" else tr["studio"]} · '
            f'{m["created_at"]:%Y-%m-%d %H:%M}</div>{escape(m["text"])}</div>' for m in messages)
        parts.append(f'<section><h2>{tr["conversation"]}</h2>{msgs}</section>')

    status = STATUS[lang].get(st, st)
    html = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            f'<title>{escape(tr["title"].format(id=order_id))} — TPF Technology</title><style>{CSS}</style></head><body><main>'
            f'<header><div class="brand">TPF<span>technology</span></div><span class="pill">{escape(status)}</span></header>'
            f'<h1>{escape(tr["title"].format(id=order_id))}</h1><p class="muted">{escape(order["client_name"])}</p>'
            + "".join(parts) + "</main></body></html>")
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def _form_data(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _back(order_id: int, token: str, **q: str) -> RedirectResponse:
    extra = "".join(f"&{k}={quote(v)}" for k, v in q.items())
    return RedirectResponse(f"/portal/{order_id}?t={quote(token)}{extra}", status_code=303)


async def _act(request: Request, order_id: int, token: str, event: Ev, done: str, comment: str = "") -> RedirectResponse:
    o = request.app.state.orch
    if comment:
        await o.add_message(order_id, "in", comment[:20_000])
    try:
        await o.fire(order_id, event, actor="client", data={"comment": comment[:2000]} if comment else {})
    except TransitionError:
        return _back(order_id, token, err="state")
    return _back(order_id, token, done=done)


@router.post("/portal/{order_id}/message")
async def portal_message(order_id: int, request: Request) -> RedirectResponse:
    form = await _form_data(request)
    order = await _load(request, order_id, form.get("t", ""))
    text = form.get("text", "").strip()[:20_000]
    if not text:
        return _back(order_id, form["t"], err="empty")
    if order["state"] == State.AWAITING_CLIENT_INFO:
        return await _act(request, order_id, form["t"], Ev.CLIENT_REPLIED, "message", comment=text)
    await request.app.state.orch.add_message(order_id, "in", text)
    return _back(order_id, form["t"], done="message")


@router.post("/portal/{order_id}/estimate")
async def portal_estimate(order_id: int, request: Request) -> RedirectResponse:
    form = await _form_data(request)
    await _load(request, order_id, form.get("t", ""))
    approved = form.get("approved") == "1"
    comment = form.get("comment", "").strip()
    if not approved and not comment:
        return _back(order_id, form["t"], err="comment")
    return await _act(request, order_id, form["t"], Ev.ESTIMATE_APPROVED if approved else Ev.ESTIMATE_REJECTED,
                      "approved" if approved else "rejected", comment)


@router.post("/portal/{order_id}/acceptance")
async def portal_acceptance(order_id: int, request: Request) -> RedirectResponse:
    form = await _form_data(request)
    await _load(request, order_id, form.get("t", ""))
    approved = form.get("approved") == "1"
    comment = form.get("comment", "").strip()
    if not approved and not comment:
        return _back(order_id, form["t"], err="comment")
    return await _act(request, order_id, form["t"], Ev.ACCEPTED if approved else Ev.CHANGES_REQUESTED,
                      "accepted" if approved else "changes", comment)


async def _bot_block(o: Any, order_id: int, t: str, tr: dict[str, str]) -> str:
    """For a Telegram bot order: where the client enters the bot token, and which bot is connected."""
    bot = await o.pool.fetchrow("SELECT username, connected_env FROM bot_connections WHERE order_id = $1", order_id)
    field = (f'<label for="bt">{tr["bot_token"]}</label><input id="bt" name="token" type="password" required '
             f'autocomplete="off" spellcheck="false" maxlength="80">')
    if bot is None:
        body = f'<p class="muted">{escape(tr["bot_how"])}</p>' + _form(
            f"/portal/{order_id}/bot-token", t, field + f'<div class="row"><button class="btn">{tr["bot_save"]}</button></div>')
    else:
        name = escape(bot["username"])
        status = tr["bot_wait"] if bot["connected_env"] is None else (tr["bot_stg"] if bot["connected_env"] == "stg" else "")
        body = (f'<p>{tr["bot_on"]}: <a href="https://t.me/{name}" target="_blank" rel="noopener">@{name}</a></p>'
                + (f'<p class="muted">{escape(status)}</p>' if status else "")
                + f'<details><summary class="muted">{tr["bot_change"]}</summary>' + _form(
                    f"/portal/{order_id}/bot-token", t, field + f'<div class="row"><button class="btn ghost">{tr["bot_save"]}</button></div>')
                + "</details>")
    return f'<section><h2>{tr["bot"]}</h2>{body}</section>'


async def _admin_block(o: Any, order_id: int, env: str, site_url: str, tr: dict[str, str]) -> str:
    """For an application: where its admin area is and the owner's password. Empty for a plain site."""
    password = await o.pool.fetchval(
        "SELECT admin_password FROM app_databases WHERE order_id = $1 AND env = $2", order_id, env)
    if not password:
        return ""
    url = site_url.rstrip("/") + "/admin"
    note = tr["admin_note"] + (" " + tr["admin_stg_note"] if env == "stg" else "")
    return (f'<section><h2>{tr["admin"]}</h2><p class="muted">{escape(note)}</p>'
            f'<dl><dt>{tr["open_link"]}</dt><dd><a href="{escape(url)}" target="_blank" rel="noopener">{escape(url)}</a></dd>'
            f'<dt>{tr["password"]}</dt><dd><code>{escape(password)}</code></dd></dl></section>')


@router.get("/portal/{order_id}/source.zip")
async def portal_source(order_id: int, request: Request, t: str = "") -> Response:
    """The delivered order's full source as a ZIP: the client owns the code, GitHub account or not."""
    order = await _load(request, order_id, t)
    code = await request.app.state.orch.latest_artifact(order_id, "code")
    if order["state"] != State.DELIVERED or not code:
        raise HTTPException(404, "not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        spec = await request.app.state.orch.latest_artifact(order_id, "spec")
        for f in all_files(code["files"], form_endpoint(get_settings().public_base_url, order_id), kind_of(spec)):
            z.writestr(f"tpf-order-{order_id}/{f['path'].lstrip('/')}", f["content"])
    return Response(buf.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="tpf-order-{order_id}.zip"', "Cache-Control": "no-store"})


@router.post("/portal/{order_id}/bot-token")
async def portal_bot_token(order_id: int, request: Request) -> RedirectResponse:
    form = await _form_data(request)
    order = await _load(request, order_id, form.get("t", ""))
    o = request.app.state.orch
    token = form.get("token", "").strip()
    if kind_of(await o.latest_artifact(order_id, "spec")) != "bot" or order["state"] == State.CANCELLED:
        return _back(order_id, form["t"], err="state")
    if not TOKEN_RE.match(token):
        return _back(order_id, form["t"], err="token")
    telegram = getattr(request.app.state, "telegram", None) or TelegramAPI()
    try:
        username = await telegram.get_me(token)
    except TelegramError:
        return _back(order_id, form["t"], err="token")
    await o.pool.execute(
        "INSERT INTO bot_connections (order_id, token, username) VALUES ($1, $2, $3) "
        "ON CONFLICT (order_id) DO UPDATE SET token = EXCLUDED.token, username = EXCLUDED.username, "
        "connected_env = NULL, updated_at = now()", order_id, token, username)
    await o.log(order_id, "bot_token_entered", "client", {"username": username}, kind="client")
    try:
        async with o.pool.acquire() as conn:
            await queue.enqueue(conn, order_id, "connect_bot", {})
    except asyncpg.UniqueViolationError:
        pass  # agents are working on the order right now: the next deployment connects the bot
    return _back(order_id, form["t"], done="bot")


@router.post("/portal/{order_id}/repo-access")
async def portal_repo_access(order_id: int, request: Request) -> RedirectResponse:
    form = await _form_data(request)
    order = await _load(request, order_id, form.get("t", ""))
    username = form.get("username", "").strip().lstrip("@")
    if order["state"] != State.DELIVERED:
        return _back(order_id, form["t"], err="state")
    if not GH_USER.match(username):
        return _back(order_id, form["t"], err="gh")
    o = request.app.state.orch
    try:
        async with o.pool.acquire() as conn:
            await queue.enqueue(conn, order_id, "grant_repo_access", {"username": username})
    except asyncpg.UniqueViolationError:  # one live task per order: the previous request is still in the queue
        return _back(order_id, form["t"], err="busy")
    await o.log(order_id, "repo_access_requested", "client", {"username": username}, kind="client")
    return _back(order_id, form["t"], done="repo")
