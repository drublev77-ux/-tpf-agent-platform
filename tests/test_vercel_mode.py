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


# --- QA looks at the built pages ------------------------------------------------------------------

class FakeScreens:
    def __init__(self, defects: list[list[str]] | None = None) -> None:
        self.defects = defects or []
        self.captured: list[tuple[str, list[str], bool]] = []

    async def capture(self, url, routes, admin_cookie=""):
        from tpf.tools.screens import Shot
        self.captured.append((url, list(routes), bool(admin_cookie)))
        found = self.defects.pop(0) if self.defects else []
        return [Shot(r, v, b"\xff\xd8jpeg", defects=list(found) if v == "phone" else []) for v in ("desktop", "phone") for r in routes]


async def test_qa_sends_visibly_broken_pages_back_to_the_developer(vercel_env):
    env = vercel_env
    env.worker.screens = FakeScreens()
    env.llm.queue("visual", {"issues": [
        {"route": "/", "viewport": "phone", "severity": "broken", "problem": "menu prices overlap the drink names", "fix": "stack the columns"},
        {"route": "/", "viewport": "desktop", "severity": "minor", "problem": "uneven gap under the hero", "fix": "use one spacing step"}]})
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()

    assert await state(env, oid) is State.AWAITING_ACCEPTANCE
    assert env.llm.calls.count("developer") == 2 and env.llm.calls.count("visual") == 2
    reports = [r["content"] for r in await env.pool.fetch(
        "SELECT content FROM artifacts WHERE order_id=$1 AND kind='qa_report' ORDER BY version", oid)]
    first, last = reports[0], reports[-1]
    assert not first["passed"] and not first["visual"]["ok"] and "overlap" in first["visual"]["issues"][0]
    assert first["visual"]["minor"] and "uneven gap" in first["visual"]["minor"][0]      # noted, not blocking by itself
    assert last["passed"] and last["visual"] == {"ok": True, "issues": [], "minor": [], "pages": ["/"]}
    assert env.worker.screens.captured[0][1] == ["/"]
    labels = [i["label"] for i in env.llm.images]
    assert labels == ["Page / — desktop", "Page / — phone"] and env.llm.images[0]["media_type"] == "image/jpeg"


async def test_measured_defects_block_and_endless_visual_rounds_stop_blocking(vercel_env):
    env = vercel_env
    overflow = ["the page is 120px wider than the phone screen, so it scrolls sideways"]
    env.worker.screens = FakeScreens([overflow, overflow, overflow])       # the developer never fixes it
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()

    assert await state(env, oid) is State.AWAITING_ACCEPTANCE              # not stuck: after two rounds it is advisory
    reports = [r["content"] for r in await env.pool.fetch(
        "SELECT content FROM artifacts WHERE order_id=$1 AND kind='qa_report' ORDER BY version", oid)]
    assert [r["passed"] for r in reports] == [False, False, True]
    assert reports[-1]["visual"]["advisory"] and "wider than the phone" in reports[-1]["visual"]["issues"][0]


async def test_without_a_browser_the_visual_check_is_skipped_not_failed(vercel_env):
    from tpf.tools.screens import ScreensUnavailable

    class Broken:
        async def capture(self, *a, **k):
            raise ScreensUnavailable("no chromium")
    env = vercel_env
    env.worker.screens = Broken()
    oid, _ = await new_order(env)
    await env.drain()
    await env.orch.fire(oid, Ev.ESTIMATE_APPROVED, actor="client")
    await env.drain()
    assert await state(env, oid) is State.AWAITING_ACCEPTANCE
    report = await env.orch.latest_artifact(oid, "qa_report")
    assert report["passed"] and "no chromium" in report["visual"]["skipped"] and "visual" not in env.llm.calls


def test_pages_chosen_for_screenshots():
    from tpf.tools.screens import pick_routes

    assert pick_routes(["/admin/orders", "/", "/menu", "/menu/[id]", "/admin", "/admin/login", "/api/x"]) == ["/", "/menu", "/admin", "/admin/orders"]
    assert len(pick_routes(["/"] + [f"/p{i}" for i in range(12)] + ["/admin"])) == 6
