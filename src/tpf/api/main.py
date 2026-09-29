"""HTTP API: order intake, client decisions, live status, operator controls."""
from __future__ import annotations

import asyncio
import hmac
import json
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field

from tpf.config import get_settings
from tpf.db import create_pool
from tpf.notifications import make_notifier
from tpf.orchestrator.service import Orchestrator
from tpf.orchestrator.states import Ev, State, TransitionError


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.pool = await create_pool()
    app.state.orch = Orchestrator(app.state.pool, settings, make_notifier(settings))
    yield
    await app.state.pool.close()


app = FastAPI(title="TPF Agent Platform", version="0.1.0", lifespan=lifespan)


def orch(request: Request) -> Orchestrator:
    return request.app.state.orch


# --- models ---------------------------------------------------------------------------------

class OrderIn(BaseModel):
    client_name: str = Field(min_length=1, max_length=200)
    client_email: EmailStr
    request_text: str = Field(min_length=10, max_length=20_000)
    channel: Literal["site", "telegram", "whatsapp", "wechat", "email", "crm"] = "site"
    language: Literal["ru", "en", "zh"] = "ru"


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class DecisionIn(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=20_000)


class OperatorNote(BaseModel):
    note: str = Field(default="", max_length=20_000)


PUBLIC_FIELDS = ("id", "state", "client_name", "language", "staging_url", "production_url", "created_at", "updated_at")


def public(order: dict[str, Any]) -> dict[str, Any]:
    return {k: order[k] for k in PUBLIC_FIELDS}


# --- auth -----------------------------------------------------------------------------------

async def client_order(order_id: int, request: Request, x_order_token: str = Header(...)) -> dict[str, Any]:
    try:
        order = await orch(request).get_order(order_id)
    except KeyError:
        raise HTTPException(404, "order not found") from None
    if not hmac.compare_digest(order["client_token"], x_order_token):
        raise HTTPException(404, "order not found")  # same answer as a missing order
    return order


def operator(x_admin_token: str = Header(...)) -> None:
    expected = get_settings().admin_token
    if expected == "change-me" or not hmac.compare_digest(expected, x_admin_token):
        raise HTTPException(401, "operator token required")


async def _fire(o: Orchestrator, order_id: int, event: Ev, actor: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        await o.fire(order_id, event, actor=actor, data=data or {})
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    return public(await o.get_order(order_id))


# --- client endpoints -----------------------------------------------------------------------

@app.post("/orders", status_code=201)
async def create_order(body: OrderIn, request: Request) -> dict[str, Any]:
    order, token = await orch(request).create_order(**body.model_dump())
    return {"order": public(order), "client_token": token}


@app.get("/orders/{order_id}")
async def get_order(request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    return {
        "order": public(order),
        "spec": await o.latest_artifact(order["id"], "spec"),
        "estimate": await o.latest_artifact(order["id"], "estimate"),
        "messages": await o.messages(order["id"]),
    }


@app.post("/orders/{order_id}/messages")
async def post_message(body: MessageIn, request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    await o.add_message(order["id"], "in", body.text)
    if order["state"] == State.AWAITING_CLIENT_INFO:
        return await _fire(o, order["id"], Ev.CLIENT_REPLIED, "client")
    await o.log(order["id"], "client_message", "client", {}, kind="client")
    return public(order)


@app.post("/orders/{order_id}/estimate")
async def decide_estimate(body: DecisionIn, request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    if body.comment:
        await o.add_message(order["id"], "in", body.comment)
    event = Ev.ESTIMATE_APPROVED if body.approved else Ev.ESTIMATE_REJECTED
    return await _fire(o, order["id"], event, "client", {"comment": body.comment})


@app.post("/orders/{order_id}/acceptance")
async def decide_acceptance(body: DecisionIn, request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    o = orch(request)
    if body.comment:
        await o.add_message(order["id"], "in", body.comment)
    event = Ev.ACCEPTED if body.approved else Ev.CHANGES_REQUESTED
    return await _fire(o, order["id"], event, "client", {"comment": body.comment})


@app.post("/orders/{order_id}/cancel")
async def cancel(request: Request, order: dict = Depends(client_order)) -> dict[str, Any]:
    return await _fire(orch(request), order["id"], Ev.CANCEL, "client")


@app.get("/orders/{order_id}/events")
async def stream_events(request: Request, order: dict = Depends(client_order)) -> StreamingResponse:
    """Server-sent events: the live status feed for the client portal."""
    o = orch(request)

    async def gen():
        last = 0
        while not await request.is_disconnected():
            for ev in await o.events(order["id"], last):
                last = ev["id"]
                payload = {k: ev[k] for k in ("id", "name", "from_state", "to_state", "actor")}
                payload["at"] = ev["created_at"].isoformat()
                yield f"id: {ev['id']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- operator endpoints ---------------------------------------------------------------------

@app.get("/admin/orders", dependencies=[Depends(operator)])
async def list_orders(request: Request, state: State | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM orders" + (" WHERE state = $1" if state else "") + " ORDER BY id DESC LIMIT 200"
    rows = await orch(request).pool.fetch(q, *([state] if state else []))
    return [public(dict(r)) | {"tokens_used": r["tokens_used"], "token_budget": r["token_budget"]} for r in rows]


@app.get("/admin/orders/{order_id}/journal", dependencies=[Depends(operator)])
async def journal(order_id: int, request: Request) -> list[dict[str, Any]]:
    return await orch(request).events(order_id)


@app.post("/admin/orders/{order_id}/resume", dependencies=[Depends(operator)])
async def resume(order_id: int, body: OperatorNote, request: Request) -> dict[str, Any]:
    return await _fire(orch(request), order_id, Ev.RESUME, "operator", {"note": body.note})


@app.post("/admin/orders/{order_id}/budget", dependencies=[Depends(operator)])
async def raise_budget(order_id: int, request: Request, add_tokens: int = 500_000) -> dict[str, Any]:
    o = orch(request)
    await o.pool.execute("UPDATE orders SET token_budget = token_budget + $2 WHERE id = $1", order_id, add_tokens)
    await o.log(order_id, "budget_raised", "operator", {"add_tokens": add_tokens}, kind="system")
    return public(await o.get_order(order_id))


@app.post("/admin/orders/{order_id}/payment", dependencies=[Depends(operator)])
async def payment_received(order_id: int, request: Request) -> dict[str, Any]:
    """Call from the payment provider's webhook handler (Stripe, ЮKassa) once prepayment clears."""
    return await _fire(orch(request), order_id, Ev.PAYMENT_RECEIVED, "payments")


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    await orch(request).pool.fetchval("SELECT 1")
    return {"status": "ok"}


def run() -> None:
    import uvicorn

    uvicorn.run("tpf.api.main:app", host="0.0.0.0", port=8000)
