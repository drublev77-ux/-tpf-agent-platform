"""Reminders to a client whose order waits for them: unanswered questions, an estimate or a review version
nobody has decided on. Without them such an order simply stops, and the operator finds it days later.

A reminder goes out after each number of days in CLIENT_REMINDER_DAYS (counted from the moment the order began
to wait, or from the client's last message if that was later), never two within a day, and never for an order
that has waited so long that a letter would come out of nowhere. Each one is written to the order's journal;
the journal is what says how many were sent."""
from __future__ import annotations

import asyncio
import logging

from tpf.notifications import REMINDER_LAST, REMINDERS
from tpf.orchestrator.service import Orchestrator
from tpf.orchestrator.states import State

log = logging.getLogger("tpf.reminders")

EVERY_SEC = 1800
FORGOTTEN_AFTER_DAYS = 14     # past the last reminder's day by this much, the order is left alone
LOCK = 770102                 # several workers run this loop: one at a time

DUE = """
WITH waiting AS (
    SELECT o.*, greatest(o.updated_at, coalesce(
               (SELECT max(m.created_at) FROM messages m WHERE m.order_id = o.id AND m.direction = 'in'), o.updated_at)) AS since
    FROM orders o WHERE o.state = ANY($1::text[])
)
SELECT w.*, extract(epoch FROM now() - w.since) / 86400.0 AS waited,
       (SELECT count(*) FROM events e WHERE e.order_id = w.id AND e.name = 'client_reminder' AND e.created_at >= w.since) AS sent,
       (SELECT max(e.created_at) FROM events e WHERE e.order_id = w.id AND e.name = 'client_reminder') AS last_sent
FROM waiting w
ORDER BY w.id
"""


def states_to_remind(orch: Orchestrator) -> list[str]:
    states = [State.AWAITING_CLIENT_INFO, State.AWAITING_ESTIMATE, State.AWAITING_ACCEPTANCE]
    if orch.settings.payment_provider != "stripe":
        # With Stripe the client already gets a fresh payment link whenever the previous one expires.
        states += [State.AWAITING_PAYMENT, State.AWAITING_FINAL_PAYMENT]
    return [str(s) for s in states]


async def remind_due(orch: Orchestrator) -> list[int]:
    """Send the reminders whose day has come. Returns the ids of the orders reminded."""
    days = orch.settings.reminder_days
    if not days:
        return []
    due: list[tuple[dict, int]] = []
    async with orch.pool.acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", LOCK):
            return []
        for row in await conn.fetch(DUE, states_to_remind(orch)):
            sent, waited = row["sent"], float(row["waited"])
            if sent >= len(days) or waited < days[sent] or waited > days[-1] + FORGOTTEN_AFTER_DAYS:
                continue
            if row["last_sent"] is not None and await conn.fetchval("SELECT $1 > now() - interval '1 day'", row["last_sent"]):
                continue
            number = sent + 1
            await orch._event(conn, row["id"], "system", "client_reminder", "reminders",
                              {"number": number, "of": len(days), "state": row["state"], "waited_days": round(waited, 1)})
            due.append((dict(row), number))
    for order, number in due:      # after the journal is written: a failed letter is not sent twice
        texts = REMINDERS[order["state"]]
        text = texts.get(order["language"], texts["en"]).format(id=order["id"])
        if number == len(days):
            text += " " + REMINDER_LAST.get(order["language"], REMINDER_LAST["en"])
        await orch.notify_client(order, text)
        log.info("order %s: reminder %s of %s (%s)", order["id"], number, len(days), order["state"])
    return [order["id"] for order, _ in due]


async def run_forever(orch: Orchestrator, stopping: asyncio.Event) -> None:
    while orch.settings.reminder_days and not stopping.is_set():
        try:
            await remind_due(orch)
        except Exception:  # noqa: BLE001 — a passing database or mail error; the next round tries again
            log.exception("reminder round failed")
        try:
            await asyncio.wait_for(stopping.wait(), timeout=EVERY_SEC)
        except asyncio.TimeoutError:
            pass
