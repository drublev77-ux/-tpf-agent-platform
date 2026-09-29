"""Worker: claims agent tasks from the queue and runs them. Run as many workers as you like."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

import asyncpg

from tpf.agents import HANDLERS
from tpf.agents.base import Ctx
from tpf.config import Settings, get_settings
from tpf.db import create_pool
from tpf.llm.gateway import AnthropicGateway, Budget, BudgetExceeded, LLMGateway
from tpf.notifications import make_notifier
from tpf.orchestrator import queue
from tpf.payments import PaymentGateway, make_gateway
from tpf.orchestrator.service import Orchestrator
from tpf.orchestrator.states import TASK_FOR_STATE, State, TransitionError
from tpf.tools.deploy import Deployer, VercelDeployer
from tpf.tools.github import GitHubPublisher
from tpf.tools.sandbox import Sandbox, make_sandbox

log = logging.getLogger("tpf.worker")

STATE_FOR_TASK = {kind: state for state, kind in TASK_FOR_STATE.items()}


class Worker:
    def __init__(
        self, *, pool: asyncpg.Pool, settings: Settings, orch: Orchestrator, llm: LLMGateway,
        sandbox: Sandbox, deployer: Deployer | None, github: GitHubPublisher | None,
        payments: PaymentGateway | None = None,
    ) -> None:
        self.pool, self.settings, self.orch, self.llm = pool, settings, orch, llm
        self.sandbox, self.deployer, self.github, self.payments = sandbox, deployer, github, payments
        self.stopping = asyncio.Event()

    async def run_once(self) -> bool:
        """Claim and run one task. Returns False when the queue is empty."""
        task = await queue.claim(self.pool, self.settings.task_lease_sec)
        if task is None:
            return False
        order = await self.orch.get_order(task["order_id"])
        expected = STATE_FOR_TASK.get(task["kind"])
        if expected is None or order["state"] != expected:
            async with self.pool.acquire() as conn:
                await queue.mark_failed(conn, task["id"], f"stale task: order is {order['state']}")
            return True

        handler = HANDLERS[task["kind"]]
        ctx = Ctx(self.orch, self.llm, self.settings, self.sandbox, self.deployer, self.github, order,
                  payments=self.payments)
        await self.orch.log(order["id"], "task_started", task["kind"], {"task": task["id"], "attempt": task["attempts"]})
        try:
            outcome = await handler(ctx)
        except BudgetExceeded as e:
            await self.orch.escalate(order["id"], f"Бюджет исчерпан: {e}", actor=task["kind"], failed_task=task["id"])
            return True
        except Exception as e:  # noqa: BLE001 — any agent failure is retried, then escalated
            log.exception("task %s (%s) failed", task["id"], task["kind"])
            err = f"{type(e).__name__}: {e}"[:1000]
            if task["attempts"] < self.settings.task_max_retries:
                async with self.pool.acquire() as conn:
                    await queue.retry_later(conn, task["id"], err, 30 * 2 ** (task["attempts"] - 1))
            else:
                await self.orch.escalate(order["id"], f"Задача {task['kind']} падает: {err}", actor=task["kind"], failed_task=task["id"])
            return True

        try:
            if outcome.escalate:
                await self.orch.escalate(order["id"], outcome.escalate, actor=task["kind"], failed_task=task["id"])
            elif outcome.event:
                await self.orch.fire(
                    order["id"], outcome.event, actor=task["kind"], data=outcome.data,
                    details=outcome.details, complete_task=task["id"], task_result=outcome.data,
                )
            else:  # the task did its work without moving the order (e.g. a payment link was sent)
                async with self.pool.acquire() as conn:
                    await queue.mark_done(conn, task["id"], outcome.data)
        except TransitionError as e:  # e.g. the client cancelled while the agent worked
            async with self.pool.acquire() as conn:
                await queue.mark_failed(conn, task["id"], f"transition rejected: {e}")
        return True

    async def loop(self, slot: int) -> None:
        while not self.stopping.is_set():
            try:
                busy = await self.run_once()
            except Exception:  # noqa: BLE001 — keep the loop alive; the lease returns the task
                log.exception("worker slot %s crashed on a task", slot)
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    missing = [name for name, value in (
        ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        ("VERCEL_TOKEN", settings.vercel_token if settings.sandbox == "vercel" else "n/a"),
        ("DATABASE_URL", settings.database_url if "localhost" not in settings.database_url else ""),
        ("STRIPE_SECRET_KEY", settings.stripe_secret_key if settings.payment_provider == "stripe" else "n/a"),
    ) if not value]
    if missing:
        # Idle instead of crash-looping: setting the variables on the host redeploys the worker.
        log.error("worker not started: set %s", ", ".join(missing))
        while True:
            await asyncio.sleep(3600)
    pool = await create_pool()
    orch = Orchestrator(pool, settings, make_notifier(settings))
    llm = AnthropicGateway(settings, Budget(pool, settings))
    worker = Worker(
        pool=pool, settings=settings, orch=orch, llm=llm, sandbox=make_sandbox(settings),
        deployer=VercelDeployer(settings) if settings.vercel_token else None,
        github=GitHubPublisher(settings) if settings.github_token and settings.github_owner else None,
        payments=make_gateway(settings),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stopping.set)
    slots = int(os.environ.get("WORKER_CONCURRENCY", "4"))
    log.info("worker started with %s slots", slots)
    try:
        await asyncio.gather(*(worker.loop(i) for i in range(slots)))
    finally:
        await llm.aclose()
        await pool.close()


def run() -> None:
    asyncio.run(main())
