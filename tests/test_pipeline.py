"""End-to-end runs of the order pipeline against a real database, with fake model/sandbox/deployer."""
from __future__ import annotations

from tpf.orchestrator.states import Ev, State
from tpf.tools.sandbox import RunResult

REQUEST = "Нужен лендинг для кофейни: меню, адрес, часы работы, кнопка звонка."


async def new_order(env):
    order, token = await env.orch.create_order(
        client_name="Анна", client_email="anna@example.com", request_text=REQUEST, language="ru",
    )
    return order["id"], token


async def state(env, order_id) -> State:
    return State((await env.orch.get_order(order_id))["state"])


async def test_order_goes_from_request_to_delivery(env):
    oid, token = await new_order(env)
    assert token

    await env.drain()
    assert await state(env, oid) is State.AWAITING_ESTIMATE
    estimate = await env.orch.latest_artifact(oid, "estimate")
    assert estimate["total_hours"] == 16          # 6 + 9.5, rounded up by code
    assert estimate["price"] == 640               # 16 h x 40, computed by code, not the model

    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ACCEPTANCE
    order = await env.orch.get_order(oid)
    assert order["staging_url"] == f"https://tpf-order-{oid}-preview1.vercel.app"

    await env.orch.fire(oid, Ev.ACCEPTED, actor="client")
    await env.drain()
    order = await env.orch.get_order(oid)
    assert order["state"] == State.DELIVERED
    assert order["production_url"] == f"https://tpf-order-{oid}.vercel.app"
    assert [d[1] for d in env.deployer.deploys] == [False, True]   # staging, then production

    # The client was told at each of their decision points and at delivery.
    outgoing = [m["text"] for m in await env.orch.messages(oid) if m["direction"] == "out"]
    assert len(outgoing) == 3 and "640" in outgoing[0] and order["production_url"] in outgoing[-1]

    # The journal records every transition in order.
    transitions = [e["name"] for e in await env.orch.events(oid) if e["kind"] == "transition"]
    assert transitions == [
        "intake_accepted", "spec_ready", "estimate_approved", "design_ready", "code_ready",
        "checks_passed", "staging_deployed", "accepted", "released",
    ]


async def test_analyst_questions_wait_for_the_client(env):
    env.llm.queue("analyst", {"in_pilot_scope": True, "questions": ["Какой адрес кофейни?"]})
    oid, _ = await new_order(env)
    await env.drain()
    assert await state(env, oid) is State.AWAITING_CLIENT_INFO

    await env.orch.add_message(oid, "in", "Минск, ул. Ленина, 1")
    await env.orch.fire(oid, Ev.CLIENT_REPLIED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ESTIMATE


async def test_failed_builds_loop_back_then_escalate(env):
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    env.sandbox.results = [RunResult(20, "Type error in app/page.tsx")] * 3
    await env.drain()

    order = await env.orch.get_order(oid)
    assert order["state"] == State.PAUSED and order["paused_from"] == State.DEVELOPMENT
    assert env.llm.calls.count("developer") == 3        # first try + two fixes, then a human
    report = await env.orch.latest_artifact(oid, "qa_report")
    assert report["build"]["failed_at"] == "build" and not report["passed"]

    # An engineer resumes it: development runs again with a fresh set of attempts.
    await env.orch.fire(oid, Ev.RESUME, actor="operator", data={"note": "fixed the tsconfig"})
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ACCEPTANCE


async def test_reviewer_rejection_is_fixed_by_the_author(env):
    env.llm.queue("reviewer", {"approved": False, "issues": ["No acceptance criterion for contacts"]})
    oid, _ = await new_order(env)
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ESTIMATE
    assert env.llm.calls[:4] == ["analyst", "reviewer", "analyst", "reviewer"]


async def test_out_of_scope_order_goes_to_an_engineer(env):
    env.llm.queue("analyst", {"in_pilot_scope": False, "scope_note": "needs a mobile app", "questions": []})
    oid, _ = await new_order(env)
    await env.drain()
    assert await state(env, oid) is State.PAUSED


async def test_budget_exhaustion_pauses_the_order(env):
    env.llm.tokens_per_call = 60_000              # budget in the fixture is 100k tokens
    oid, _ = await new_order(env)
    await env.drain()                             # analyst + reviewer: 120k, the budget is crossed
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()                             # the architect's call is refused before it is sent
    assert await state(env, oid) is State.PAUSED
    assert "architect" not in env.llm.calls
    names = [e["name"] for e in await env.orch.events(oid)]
    assert "budget_warning" in names and names[-1] == "escalate"


async def test_client_change_request_reworks_and_redeploys(env):
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    await env.orch.fire(oid, Ev.CHANGES_REQUESTED, actor="client", data={"comment": "Сделайте кнопку звонка крупнее"})
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ACCEPTANCE
    assert env.llm.calls.count("developer") == 2
    assert await env.orch.pool.fetchval("SELECT count(*) FROM artifacts WHERE order_id=$1 AND kind='code'", oid) == 2


async def test_cancel_stops_queued_work(env):
    oid, _ = await new_order(env)
    await env.orch.fire(oid, Ev.CANCEL, actor="client")
    await env.drain()
    assert await state(env, oid) is State.CANCELLED
    assert env.llm.calls == []


async def test_events_are_append_only(env):
    oid, _ = await new_order(env)
    import asyncpg
    import pytest
    with pytest.raises(asyncpg.RaiseError):
        await env.pool.execute("DELETE FROM events WHERE order_id = $1", oid)
