"""Orchestrator: the only code that changes an order's state.

Every transition runs in one transaction: lock the order, compute the transition with the pure
state machine, persist it, append an event, finish the task that caused it and queue the next one.
Notifications go out after commit.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

import asyncpg

from tpf.config import Settings
from tpf.notifications import Notifier, render
from tpf.orchestrator import queue
from tpf.orchestrator.states import TASK_FOR_STATE, Ev, State, Transition, apply

log = logging.getLogger("tpf.orchestrator")


class Orchestrator:
    def __init__(self, pool: asyncpg.Pool, settings: Settings, notifier: Notifier) -> None:
        self.pool = pool
        self.settings = settings
        self.notifier = notifier

    # --- orders -----------------------------------------------------------------------------

    async def create_order(
        self, *, client_name: str, client_email: str, request_text: str,
        channel: str = "site", language: str = "ru",
    ) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(24)
        async with self.pool.acquire() as conn, conn.transaction():
            order_id = await conn.fetchval(
                "INSERT INTO orders (state, client_name, client_email, client_token, channel, language, "
                "request_text, token_budget) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                State.NEW, client_name, client_email, token, channel, language, request_text,
                self.settings.order_token_budget,
            )
            await conn.execute(
                "INSERT INTO messages (order_id, direction, text) VALUES ($1, 'in', $2)", order_id, request_text
            )
            await self._event(conn, order_id, "client", "order_created", "client", {"channel": channel})
        await self.fire(order_id, Ev.INTAKE_ACCEPTED, actor="intake")
        order = await self.get_order(order_id)
        await self._welcome(order)
        return order, token

    async def _welcome(self, order: dict[str, Any]) -> None:
        """Confirmation with the portal link; the operator hears about every new order."""
        try:
            text = render("received", order["language"], order["id"], "")
            if text:
                await self.notifier.to_client(order, text)
            await self.notifier.to_operator(
                order, f"Новый заказ #{order['id']} от {order['client_name']} <{order['client_email']}> "
                       f"({order['channel']}, {order['language']}):\n{order['request_text'][:1500]}")
        except Exception:
            log.exception("welcome notification failed for order %s", order["id"])

    async def get_order(self, order_id: int) -> dict[str, Any]:
        row = await self.pool.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        if row is None:
            raise KeyError(order_id)
        return dict(row)

    # --- transitions ------------------------------------------------------------------------

    async def fire(
        self,
        order_id: int,
        event: Ev,
        *,
        actor: str,
        data: dict[str, Any] | None = None,
        details: str = "",
        complete_task: int | None = None,
        task_result: dict[str, Any] | None = None,
    ) -> Transition:
        """Apply `event` to the order. `complete_task` is the task whose result caused it."""
        data = data or {}
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1 FOR UPDATE", order_id)
            if row is None:
                raise KeyError(order_id)
            state = State(row["state"])
            tr = apply(
                state, event,
                check_attempts=row["check_attempts"],
                max_check_attempts=self.settings.max_check_attempts,
                payment_required=self.settings.payment_required,
                final_payment_required=self.settings.final_payment_required,
                paused_from=State(row["paused_from"]) if row["paused_from"] else None,
            )
            await conn.execute(
                "UPDATE orders SET state = $2, check_attempts = $3, paused_from = $4, updated_at = now() WHERE id = $1",
                order_id, tr.to_state, tr.check_attempts,
                tr.paused_from if tr.to_state is State.PAUSED else None,
            )
            await self._event(
                conn, order_id, "transition", event, actor,
                {**data, **({"reason": tr.reason} if tr.reason else {})},
                from_state=state, to_state=tr.to_state,
            )
            if complete_task is not None:
                await queue.mark_done(conn, complete_task, task_result)
            if tr.to_state in (State.PAUSED, State.CANCELLED):
                await queue.cancel_live(conn, order_id)
            kind = TASK_FOR_STATE.get(tr.to_state)
            if kind:
                await queue.enqueue(conn, order_id, kind)
            order = dict(row) | {"state": tr.to_state}

        await self._notify(order, tr, details or data.get("reason", "") or tr.reason or "")
        return tr

    async def escalate(self, order_id: int, reason: str, *, actor: str, failed_task: int | None = None) -> None:
        if failed_task is not None:
            async with self.pool.acquire() as conn:
                await queue.mark_failed(conn, failed_task, reason)
        await self.fire(order_id, Ev.ESCALATE, actor=actor, data={"reason": reason}, details=reason)

    # --- journal, artifacts, messages --------------------------------------------------------

    async def log(self, order_id: int, name: str, actor: str, data: dict[str, Any] | None = None, kind: str = "agent") -> None:
        async with self.pool.acquire() as conn:
            await self._event(conn, order_id, kind, name, actor, data or {})

    async def save_artifact(self, order_id: int, kind: str, content: dict[str, Any], author: str) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            version = await conn.fetchval(
                "SELECT coalesce(max(version), 0) + 1 FROM artifacts WHERE order_id = $1 AND kind = $2",
                order_id, kind,
            )
            await conn.execute(
                "INSERT INTO artifacts (order_id, kind, version, content, author) VALUES ($1,$2,$3,$4,$5)",
                order_id, kind, version, content, author,
            )
            await self._event(conn, order_id, "agent", "artifact_saved", author, {"artifact": kind, "version": version})
        return version

    async def latest_artifact(self, order_id: int, kind: str) -> dict[str, Any] | None:
        return await self.pool.fetchval(
            "SELECT content FROM artifacts WHERE order_id = $1 AND kind = $2 ORDER BY version DESC LIMIT 1",
            order_id, kind,
        )

    async def add_message(self, order_id: int, direction: str, text: str) -> None:
        await self.pool.execute(
            "INSERT INTO messages (order_id, direction, text) VALUES ($1, $2, $3)", order_id, direction, text
        )

    async def messages(self, order_id: int) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT direction, text, created_at FROM messages WHERE order_id = $1 ORDER BY id", order_id
        )
        return [dict(r) for r in rows]

    async def events(self, order_id: int, after_id: int = 0) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT id, kind, name, from_state, to_state, actor, data, created_at FROM events "
            "WHERE order_id = $1 AND id > $2 ORDER BY id",
            order_id, after_id,
        )
        return [dict(r) for r in rows]

    # --- internals --------------------------------------------------------------------------

    @staticmethod
    async def _event(
        conn: asyncpg.Connection, order_id: int, kind: str, name: str, actor: str, data: dict[str, Any],
        from_state: str | None = None, to_state: str | None = None,
    ) -> None:
        await conn.execute(
            "INSERT INTO events (order_id, kind, name, from_state, to_state, actor, data) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            order_id, kind, str(name), from_state, to_state, actor, data,
        )

    # --- payments ---------------------------------------------------------------------------

    async def record_payment(
        self, *, stage: str, actor: str, session_id: str | None = None, order_id: int | None = None,
    ) -> bool:
        """Mark a payment paid (by Stripe session id, or the latest pending one of an order) and move
        the order on. Idempotent: a repeated webhook finds nothing pending and changes nothing."""
        async with self.pool.acquire() as conn, conn.transaction():
            if session_id:
                row = await conn.fetchrow(
                    "UPDATE payments SET status = 'paid', paid_at = now() "
                    "WHERE provider_session_id = $1 AND status IN ('pending', 'expired') RETURNING order_id, stage, amount, currency",
                    session_id)
            else:
                row = await conn.fetchrow(
                    "UPDATE payments SET status = 'paid', paid_at = now() WHERE id = ("
                    "  SELECT id FROM payments WHERE order_id = $1 AND stage = $2 AND status = 'pending' ORDER BY id DESC LIMIT 1"
                    ") RETURNING order_id, stage, amount, currency",
                    order_id, stage)
            if row is None:
                return False
            # Other open links for the same stage are now void.
            await conn.execute(
                "UPDATE payments SET status = 'cancelled' WHERE order_id = $1 AND stage = $2 AND status = 'pending'",
                row["order_id"], row["stage"])
        event = Ev.PAYMENT_RECEIVED if row["stage"] == "prepayment" else Ev.FINAL_PAYMENT_RECEIVED
        await self.fire(row["order_id"], event, actor=actor,
                        data={"stage": row["stage"], "amount": str(row["amount"]), "currency": row["currency"]})
        return True

    async def notify_client(self, order: dict[str, Any], text: str) -> None:
        await self.add_message(order["id"], "out", text)
        try:
            await self.notifier.to_client(order, text)
        except Exception:
            log.exception("notification failed for order %s", order["id"])

    async def notify_operator(self, order: dict[str, Any], text: str) -> None:
        try:
            await self.notifier.to_operator(order, text)
        except Exception:
            log.exception("operator notification failed for order %s", order["id"])

    async def _notify(self, order: dict[str, Any], tr: Transition, details: str) -> None:
        if tr.to_state in (State.AWAITING_PAYMENT, State.AWAITING_FINAL_PAYMENT):
            return  # the payment task sends its own message, with the checkout link
        try:
            if tr.to_state is State.PAUSED:
                await self.notifier.to_operator(order, f"Заказ #{order['id']} на паузе: {details or 'эскалация'}")
                return
            text = render(tr.to_state, order["language"], order["id"], details)
            if text:
                await self.add_message(order["id"], "out", text)
                await self.notifier.to_client(order, text)
        except Exception:  # a failed notification must never roll back or block the order
            log.exception("notification failed for order %s", order["id"])
