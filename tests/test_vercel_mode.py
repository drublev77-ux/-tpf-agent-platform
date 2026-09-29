"""SANDBOX=vercel: build checks run as Vercel previews, and a green preview becomes staging."""
from __future__ import annotations

import pytest

from tpf.db import uses_transaction_pooler
from tpf.orchestrator.states import Ev, State
from tpf.tools.deploy import DeployError

from test_pipeline import new_order, state


@pytest.fixture
def vercel_env(env):
    env.settings.sandbox = "vercel"
    return env


async def test_green_preview_is_reused_as_staging(vercel_env):
    env = vercel_env
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()

    assert await state(env, oid) is State.AWAITING_ACCEPTANCE
    assert env.sandbox.runs == []                               # no local/Docker build at all
    assert len(env.deployer.deploys) == 1                       # QA preview, reused for staging
    assert "npm audit" in env.deployer.build_commands[0]
    report = await env.orch.latest_artifact(oid, "qa_report")
    order = await env.orch.get_order(oid)
    assert order["staging_url"] == report["preview"]["url"]


async def test_failed_vercel_build_goes_back_to_the_developer_with_the_log(vercel_env):
    env = vercel_env
    env.deployer.failures = [DeployError("ERROR", log="Type error: Property 'x' does not exist", deployment_id="dpl_x")]
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()

    assert env.llm.calls.count("developer") == 2               # fixed after the failed build
    assert await state(env, oid) is State.AWAITING_ACCEPTANCE
    first_report = await env.pool.fetchval(
        "SELECT content FROM artifacts WHERE order_id=$1 AND kind='qa_report' ORDER BY version LIMIT 1", oid
    )
    assert first_report["build"]["failed_at"] == "build"
    assert "Property 'x'" in first_report["build"]["log_tail"]


def test_transaction_pooler_detection():
    assert uses_transaction_pooler("postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:6543/postgres")
    assert not uses_transaction_pooler("postgresql://u:p@db.x.supabase.co:5432/postgres")
