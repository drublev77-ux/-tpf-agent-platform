"""A task's lease: renewed while its worker is alive, short when the worker dies, handed back on shutdown."""
from __future__ import annotations

import asyncio

from tpf.agents import HANDLERS
from tpf.agents.base import Outcome
from tpf.orchestrator import queue
from tpf.orchestrator.states import Ev, State


async def _order(env) -> int:
    order, _ = await env.orch.create_order(client_name="A", client_email="a@example.com", request_text="a landing page", language="en")
    return order["id"]


async def test_a_long_task_keeps_its_lease_and_is_not_run_twice(env, monkeypatch):
    env.settings.task_lease_sec = 1                        # renewed every third of a second
    started, runs = asyncio.Event(), []

    async def slow_analyst(ctx):
        runs.append(ctx.order_id)
        started.set()
        await asyncio.sleep(2.5)                           # far longer than the lease
        return Outcome(Ev.CLARIFICATION_NEEDED, details="- ?")

    monkeypatch.setitem(HANDLERS, "analyst", slow_analyst)
    oid = await _order(env)
    first = asyncio.create_task(env.worker.run_once())
    await started.wait()
    await asyncio.sleep(1.6)                               # the original lease has run out by now
    assert await queue.claim(env.pool, 60) is None         # ...but the worker kept it: nobody else may take the task
    assert await first is True
    assert runs == [oid] and env.worker.running == set()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_CLIENT_INFO
    assert await env.pool.fetchval("SELECT locked_until FROM tasks WHERE order_id = $1 AND kind = 'analyst'", oid) is None


async def test_a_dead_workers_task_is_retried_as_soon_as_the_short_lease_runs_out(env):
    env.settings.task_lease_sec = 1
    oid = await _order(env)
    claimed = await queue.claim(env.pool, env.settings.task_lease_sec)    # a worker took the task and died
    assert claimed["order_id"] == oid and await queue.claim(env.pool, 1) is None
    await asyncio.sleep(1.2)
    await env.drain()                                      # another worker picks it up and the order moves on
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ESTIMATE
    assert await env.pool.fetchval("SELECT attempts FROM tasks WHERE id = $1", claimed["id"]) == 2


async def test_a_stopping_worker_hands_its_task_back_at_once(env, monkeypatch):
    assert env.settings.task_lease_sec == 120              # the default: minutes, not the half hour it used to be
    started = asyncio.Event()

    async def stuck_analyst(ctx):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setitem(HANDLERS, "analyst", stuck_analyst)
    oid = await _order(env)
    running = asyncio.create_task(env.worker.run_once())
    await started.wait()
    task_id = next(iter(env.worker.running))

    await env.worker.release_running()                     # what SIGTERM does before the process leaves
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    assert env.worker.running == set()

    monkeypatch.undo()                                     # the next worker has the real agents
    again = await queue.claim(env.pool, 60)
    assert again is not None and again["id"] == task_id and again["order_id"] == oid   # no waiting for a lease
    await queue.release(env.pool, [task_id])
    await env.drain()
    assert State((await env.orch.get_order(oid))["state"]) is State.AWAITING_ESTIMATE
