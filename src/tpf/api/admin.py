"""Operator console: /admin. The owner signs in with a one-time link e-mailed to OPERATOR_EMAIL,
then sees every order and can resume a paused one, confirm a payment received outside Stripe,
raise the token budget or cancel. Server-rendered, no JavaScript."""
from __future__ import annotations

import hashlib
import hmac
import time
from html import escape
from typing import Any
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tpf.api.portal import CSS, STATUS
from tpf.config import get_settings
from tpf.notifications import EmailNotifier, email_html, portal_url
from tpf.orchestrator.states import TERMINAL, Ev, State, TransitionError

router = APIRouter(include_in_schema=False)

COOKIE = "tpf_admin"
LINK_TTL = 15 * 60          # the e-mailed link
SESSION_TTL = 14 * 24 * 3600

ADMIN_CSS = CSS + """
main{max-width:980px}dd{min-width:0;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;font-size:15px}th,td{text-align:left;padding:10px 8px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;border-top:0}td.n{white-space:nowrap}.tag{display:inline-block;padding:3px 10px;border-radius:999px;border:1px solid var(--line);font-size:13px;white-space:nowrap}
.tag.warn{border-color:var(--warn);color:var(--warn)}.tag.act{border-color:var(--accent);color:var(--accent)}.tag.ok{border-color:var(--ok);color:var(--ok)}
.scroll{overflow-x:auto}.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-start}.actions form{margin:0}
.btn.warn{background:transparent;color:var(--warn);border:1px solid var(--warn)}code{font-family:ui-monospace,Menlo,monospace;font-size:13px;word-break:break-word}
"""
NEEDS_OPERATOR = {State.PAUSED}
NEEDS_CLIENT = {State.AWAITING_CLIENT_INFO, State.AWAITING_ESTIMATE, State.AWAITING_PAYMENT,
                State.AWAITING_FINAL_PAYMENT, State.AWAITING_ACCEPTANCE}


def _secret() -> bytes:
    token = get_settings().admin_token
    if token == "change-me":
        raise HTTPException(503, "ADMIN_TOKEN is not set")
    return token.encode()


def _sign(kind: str, exp: int) -> str:
    return f"{exp}.{hmac.new(_secret(), f'{kind}:{exp}'.encode(), hashlib.sha256).hexdigest()}"


def _valid(kind: str, value: str | None) -> bool:
    try:
        exp_s, sig = (value or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    return exp > time.time() and hmac.compare_digest(_sign(kind, exp), f"{exp}.{sig}")


def _authed(request: Request) -> bool:
    return _valid("session", request.cookies.get(COOKIE))


def _require(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(401, "sign in at /admin")
    if request.method == "POST":  # the cookie is SameSite=Lax; also refuse cross-site form posts outright
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-site request refused")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{escape(title)} — TPF</title><style>{ADMIN_CSS}</style></head><body><main>'
            f'<header><a class="brand" href="/admin" style="color:inherit;text-decoration:none">TPF<span>technology</span> · оператор</a></header>'
            f'{body}</main></body></html>')
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _dt(value: Any) -> str:
    return f"{value:%d.%m %H:%M}" if value else ""


def _tag(state: str) -> str:
    cls = "warn" if state in NEEDS_OPERATOR else "act" if state in NEEDS_CLIENT else "ok" if state == State.DELIVERED else ""
    return f'<span class="tag {cls}">{escape(STATUS["ru"].get(state, state))}</span>'


def _post(action: str, label: str, cls: str = "", extra: str = "") -> str:
    return f'<form method="post" action="{escape(action)}">{extra}<button class="btn {cls}">{escape(label)}</button></form>'


# --- sign-in ----------------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request, sent: str = "", err: str = "") -> HTMLResponse:
    if not _authed(request):
        s = get_settings()
        note = ""
        if sent:
            note = '<section class="notice" role="status">Ссылка для входа отправлена на почту оператора. Она действует 15 минут.</section>'
        if err:
            why = {"mail": "почта не настроена: задайте EMAIL_* и OPERATOR_EMAIL", "wait": "ссылка уже отправлена, подождите минуту",
                   "link": "ссылка устарела или неверна, запросите новую", "send": "не удалось отправить письмо"}.get(err, err)
            note = f'<section class="error" role="alert">Не получилось: {escape(why)}</section>'
        masked = (s.operator_email[:2] + "…@" + s.operator_email.split("@")[-1]) if "@" in s.operator_email else "не задана"
        return _page("Вход", f'<h1>Вход оператора</h1>{note}<section><p>Отправим одноразовую ссылку на почту оператора ({escape(masked)}).</p>'
                             + _post("/admin/login", "Прислать ссылку для входа") + "</section>")

    pool = request.app.state.orch.pool
    rows = await pool.fetch(
        "SELECT o.id, o.state, o.client_name, o.client_email, o.updated_at, o.tokens_used, o.token_budget, o.request_text, "
        "(SELECT content->>'price' FROM artifacts a WHERE a.order_id = o.id AND a.kind = 'estimate' ORDER BY version DESC LIMIT 1) AS price, "
        "(SELECT content->>'currency' FROM artifacts a WHERE a.order_id = o.id AND a.kind = 'estimate' ORDER BY version DESC LIMIT 1) AS currency "
        "FROM orders o ORDER BY (o.state = 'paused') DESC, o.updated_at DESC LIMIT 200")
    paused = sum(1 for r in rows if r["state"] == State.PAUSED)
    body = "".join(
        f'<tr><td class="n"><a href="/admin/order/{r["id"]}">№{r["id"]}</a></td><td>{_tag(r["state"])}</td>'
        f'<td>{escape(r["client_name"])}<div class="muted">{escape(r["client_email"])}</div></td>'
        f'<td>{escape(r["request_text"][:90])}{"…" if len(r["request_text"]) > 90 else ""}</td>'
        f'<td class="n">{escape(r["price"] or "—")} {escape(r["currency"] or "")}</td>'
        f'<td class="n">{r["updated_at"]:%d.%m %H:%M}</td></tr>' for r in rows)
    head = f'<h1>Заказы</h1><p class="muted">Всего: {len(rows)}' + (f' · <span style="color:var(--warn)">ждут вас: {paused}</span>' if paused else "") + "</p>"
    table = (f'<section class="scroll"><table><thead><tr><th>Заказ</th><th>Статус</th><th>Клиент</th><th>Заявка</th><th>Сумма</th><th>Обновлён (UTC)</th></tr></thead>'
             f'<tbody>{body}</tbody></table></section>') if rows else '<section><p class="muted">Заказов пока нет.</p></section>'
    return _page("Заказы", head + table + '<div class="row">' + _post("/admin/logout", "Выйти", "ghost") + "</div>")


@router.post("/admin/login")
async def admin_login(request: Request) -> RedirectResponse:
    s = get_settings()
    _secret()
    if not (s.email_enabled and s.operator_email):
        return RedirectResponse("/admin?err=mail", status_code=303)
    pool = request.app.state.orch.pool
    fresh = await pool.fetchval(
        "INSERT INTO app_settings (key, value) VALUES ('admin_login_sent', '1') ON CONFLICT (key) DO UPDATE "
        "SET updated_at = now() WHERE app_settings.updated_at < now() - interval '60 seconds' RETURNING 1")
    if not fresh:
        return RedirectResponse("/admin?err=wait", status_code=303)
    link = f"{s.public_base_url.rstrip('/')}/admin/enter?s={_sign('link', int(time.time()) + LINK_TTL)}"
    text = f"Вход в консоль оператора TPF Technology (ссылка действует 15 минут):\n{link}\n\nЕсли вы не запрашивали вход, просто не открывайте ссылку."
    try:
        await EmailNotifier(s).send(s.operator_email, "[TPF] Вход оператора", text, email_html(text, "ru", None))
    except Exception:  # noqa: BLE001
        return RedirectResponse("/admin?err=send", status_code=303)
    return RedirectResponse("/admin?sent=1", status_code=303)


@router.get("/admin/enter")
async def admin_enter(s: str = "") -> RedirectResponse:
    if not _valid("link", s):
        return RedirectResponse("/admin?err=link", status_code=303)
    r = RedirectResponse("/admin", status_code=303)
    r.set_cookie(COOKIE, _sign("session", int(time.time()) + SESSION_TTL), max_age=SESSION_TTL,
                 httponly=True, secure=True, samesite="lax", path="/admin")
    return r


@router.post("/admin/logout")
async def admin_logout() -> RedirectResponse:
    r = RedirectResponse("/admin", status_code=303)
    r.delete_cookie(COOKIE, path="/admin")
    return r


# --- one order --------------------------------------------------------------------------------

@router.get("/admin/order/{order_id}", response_class=HTMLResponse)
async def admin_order(order_id: int, request: Request, done: str = "", err: str = "") -> HTMLResponse:
    _require(request)
    o = request.app.state.orch
    try:
        order = await o.get_order(order_id)
    except KeyError:
        raise HTTPException(404, "order not found") from None
    st = order["state"]
    base = f"/admin/order/{order_id}"
    parts: list[str] = []
    if done:
        parts.append(f'<section class="notice" role="status">Готово: {escape(done)}</section>')
    if err:
        parts.append(f'<section class="error" role="alert">Не получилось: {escape(err)}</section>')

    actions: list[str] = []
    if st == State.PAUSED:
        reason = await o.pool.fetchval(
            "SELECT data->>'reason' FROM events WHERE order_id = $1 AND to_state = 'paused' ORDER BY id DESC LIMIT 1", order_id)
        parts.append(f'<section class="error"><h2>Заказ на паузе</h2><p><code>{escape(reason or "причина не записана")}</code></p>'
                     f'<p class="muted">Вернётся на этап: {escape(STATUS["ru"].get(order["paused_from"] or "", order["paused_from"] or "—"))}. '
                     f'Сначала устраните причину, затем снимите паузу.</p></section>')
        actions.append(_post(f"{base}/resume", "Снять с паузы"))
    if st in (State.AWAITING_PAYMENT, State.AWAITING_FINAL_PAYMENT):
        actions.append(_post(f"{base}/payment", "Оплата получена вне Stripe"))
    if st not in TERMINAL:
        actions.append(_post(f"{base}/budget", "Добавить 500 000 токенов", "ghost"))
        actions.append(_post(f"{base}/cancel", "Отменить заказ", "warn",
                             '<label style="font-size:13px"><input type="checkbox" name="sure" value="1" required> подтверждаю</label> '))
    if actions:
        parts.append('<section><h2>Действия</h2><div class="actions">' + "".join(actions) + "</div></section>")

    s = get_settings()
    repo = await o.pool.fetchval("SELECT data->>'repo' FROM events WHERE order_id = $1 AND name = 'code_published' ORDER BY id DESC LIMIT 1", order_id)
    links = [("Кабинет клиента", portal_url(s.public_base_url, order_id, order["client_token"])), ("Тестовая версия", order["staging_url"]),
             ("Рабочая версия", order["production_url"]), ("Репозиторий", repo)]
    rows = [("Клиент", f'{escape(order["client_name"])} · {escape(order["client_email"])}'),
            ("Язык и канал", f'{escape(order["language"])} · {escape(order["channel"])}'),
            ("Токены", f'{order["tokens_used"]:,} из {order["token_budget"]:,}'.replace(",", " "))]
    rows += [(k, f'<a href="{escape(v)}" target="_blank" rel="noopener">{escape(v)}</a>') for k, v in links if v]
    parts.append("<section><h2>Заказ</h2><dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
                 + f'</dl><p style="white-space:pre-line;margin-top:14px">{escape(order["request_text"][:3000])}</p></section>')

    pays = await o.pool.fetch("SELECT stage, amount, currency, status, provider, paid_at FROM payments WHERE order_id = $1 ORDER BY id", order_id)
    if pays:
        parts.append('<section class="scroll"><h2>Платежи</h2><table><tbody>' + "".join(
            f'<tr><td>{ {"prepayment": "Предоплата", "final": "Остаток"}.get(p["stage"], escape(p["stage"])) }</td>'
            f'<td class="n">{float(p["amount"]):,.2f} {escape(p["currency"])}</td><td>{escape(p["status"])}</td>'
            f'<td>{escape(p["provider"])}</td><td class="n">{_dt(p["paid_at"])}</td></tr>'
            for p in pays) + "</tbody></table></section>")

    events: list[dict[str, Any]] = [dict(r) for r in await o.pool.fetch(
        "SELECT created_at, kind, name, actor, from_state, to_state, data FROM events WHERE order_id = $1 ORDER BY id DESC LIMIT 40", order_id)]
    if events:
        def line(e: dict[str, Any]) -> str:
            move = f' {escape(e["from_state"] or "")} → {escape(e["to_state"] or "")}' if e["to_state"] else ""
            data = e["data"] or {}
            note = data.get("reason") or data.get("note") or data.get("error") or data.get("comment") or ""
            return (f'<tr><td class="n">{e["created_at"]:%d.%m %H:%M}</td><td>{escape(e["actor"])}</td>'
                    f'<td>{escape(e["name"])}{move}<div class="muted"><code>{escape(str(note)[:400])}</code></div></td></tr>')
        parts.append('<section class="scroll"><h2>Журнал (последние 40 событий, UTC)</h2><table><tbody>'
                     + "".join(line(e) for e in events) + "</tbody></table></section>")

    return _page(f"Заказ №{order_id}", f'<p><a href="/admin">← Все заказы</a></p><h1>Заказ №{order_id} {_tag(st)}</h1>' + "".join(parts))


def _go(order_id: int, **q: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(f"/admin/order/{order_id}?{urlencode(q)}", status_code=303)


@router.post("/admin/order/{order_id}/resume")
async def admin_resume(order_id: int, request: Request) -> RedirectResponse:
    _require(request)
    try:
        await request.app.state.orch.fire(order_id, Ev.RESUME, actor="operator", data={"note": "resumed from the console"})
    except (TransitionError, KeyError) as e:
        return _go(order_id, err=str(e))
    return _go(order_id, done="пауза снята, агенты продолжают")


@router.post("/admin/order/{order_id}/payment")
async def admin_payment(order_id: int, request: Request) -> RedirectResponse:
    _require(request)
    o = request.app.state.orch
    order = await o.get_order(order_id)
    stage = {State.AWAITING_PAYMENT: "prepayment", State.AWAITING_FINAL_PAYMENT: "final"}.get(order["state"])
    if stage is None:
        return _go(order_id, err="сейчас оплата не ожидается")
    try:
        if not await o.record_payment(stage=stage, order_id=order_id, actor="operator"):
            return _go(order_id, err="платёж ещё не выставлен, повторите через минуту")
    except TransitionError as e:
        return _go(order_id, err=str(e))
    return _go(order_id, done="оплата отмечена как полученная")


@router.post("/admin/order/{order_id}/budget")
async def admin_budget(order_id: int, request: Request) -> RedirectResponse:
    _require(request)
    o = request.app.state.orch
    await o.pool.execute("UPDATE orders SET token_budget = token_budget + 500000 WHERE id = $1", order_id)
    await o.log(order_id, "budget_raised", "operator", {"add_tokens": 500_000}, kind="system")
    return _go(order_id, done="бюджет увеличен на 500 000 токенов")


@router.post("/admin/order/{order_id}/cancel")
async def admin_cancel(order_id: int, request: Request) -> RedirectResponse:
    _require(request)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    if form.get("sure") != ["1"]:
        return _go(order_id, err="отмена не подтверждена")
    try:
        await request.app.state.orch.fire(order_id, Ev.CANCEL, actor="operator", data={"reason": "cancelled by the operator"})
    except (TransitionError, KeyError) as e:
        return _go(order_id, err=str(e))
    return _go(order_id, done="заказ отменён")
