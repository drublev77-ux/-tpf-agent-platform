"""Fixtures: a real Postgres (TEST_DATABASE_URL), fake model, fake sandbox and fake deployer."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tpf.config import Settings
from tpf.db import create_pool, migrate
from tpf.llm.gateway import Budget, BudgetExceeded
from tpf.notifications import LogNotifier
from tpf.orchestrator.service import Orchestrator
from tpf.tools.deploy import Deployment, DeployError
from tpf.tools.sandbox import RunResult
from tpf.worker import Worker

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@/tpf_test?host=/tmp&port=5433")

SPEC = {
    "title": "Landing for a coffee shop", "summary": "One-page site", "audience": "locals",
    "pages": [{"name": "Home", "purpose": "sell", "sections": ["Hero", "Menu", "Contacts"]}],
    "features": ["responsive"], "acceptance_criteria": ["Menu section lists drinks"],
}
ARCH = {
    "site_name": "Coffee", "routes": [{"path": "/", "title": "Home", "purpose": "sell", "sections": ["Hero"]}],
    "components": [{"name": "Header", "purpose": "nav"}],
    "design": {"palette": {"background": "#fff", "text": "#111", "accent": "#b45309"},
               "fonts": {"heading": "Georgia", "body": "system-ui"}, "tone": "warm"},
}
CODE = {"files": [
    {"path": "app/layout.tsx", "content": "export default function L({children}:{children:React.ReactNode}){return <html><body>{children}</body></html>}"},
    {"path": "app/page.tsx", "content": "export default function P(){return <main><h1>Coffee</h1></main>}"},
], "notes": "first version"}


class FakeLLM:
    """Returns canned outputs per agent; scripts can override the next outputs of an agent."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.script: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []
        self.tokens_per_call = 1000

    def queue(self, agent: str, *outputs: dict[str, Any]) -> None:
        self.script.setdefault(agent, []).extend(outputs)

    async def structured(self, *, order_id, agent, model_class, system, prompt, schema, max_tokens=8000):
        await self.budget.check(order_id)
        self.calls.append(agent)
        await self.budget.record(order_id, agent, f"fake-{model_class}", self.tokens_per_call, 0)
        if self.script.get(agent):
            return self.script[agent].pop(0)
        return {
            "analyst": {"in_pilot_scope": True, "questions": [], "spec": SPEC,
                        "estimate": {"items": [{"name": "Design", "hours": 6}, {"name": "Build", "hours": 9.5}], "duration_days": 5}},
            "reviewer": {"approved": True, "issues": []},
            "architect": ARCH,
            "developer": CODE,
        }[agent]


class FakeSandbox:
    def __init__(self) -> None:
        self.results: list[RunResult] = []
        self.runs: list[Path] = []

    async def run(self, workdir, command, timeout=None):
        self.runs.append(workdir)
        return self.results.pop(0) if self.results else RunResult(0, "build ok")


class FakeDeployer:
    def __init__(self) -> None:
        self.deploys: list[tuple[str, bool, int]] = []
        self.build_commands: list[str | None] = []
        self.failures: list[DeployError] = []
        self.env: dict[str, dict[str, str]] = {}
        self.health_problems: list[str | None] = []
        self.health_checks: list[str] = []

    async def set_env(self, project, env):
        self.env.setdefault(project, {}).update(env)

    async def health(self, url, path="/api/tpf-health"):
        self.health_checks.append(url)
        return self.health_problems.pop(0) if self.health_problems else None

    async def deploy(self, project, files, production, build_command=None):
        self.deploys.append((project, production, len(files)))
        self.build_commands.append(build_command)
        if self.failures:
            raise self.failures.pop(0)
        n = len(self.deploys)
        return Deployment(f"dpl_{n}", f"https://{project}{'' if production else f'-preview{n}'}.vercel.app", "READY")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=TEST_DB, admin_token="test-admin", order_token_budget=100_000,
        max_check_attempts=3, max_review_rounds=2, task_max_retries=2, hourly_rate=40,
        currency="USD", workspace_root=str(tmp_path), sandbox="local",
    )


@pytest.fixture
async def pool(settings):
    try:
        p = await create_pool(settings.database_url)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres not available: {e}")
    await p.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate(p)
    yield p
    await p.close()


@pytest.fixture
async def env(pool, settings):
    orch = Orchestrator(pool, settings, LogNotifier())
    llm = FakeLLM(Budget(pool, settings))
    sandbox, deployer = FakeSandbox(), FakeDeployer()
    worker = Worker(pool=pool, settings=settings, orch=orch, llm=llm, sandbox=sandbox, deployer=deployer, github=None)

    async def drain(limit: int = 50) -> None:
        for _ in range(limit):
            if not await worker.run_once():
                return
        raise AssertionError("queue did not drain")

    class Env:
        pass

    e = Env()
    e.pool, e.orch, e.llm, e.sandbox, e.deployer, e.worker, e.drain, e.settings = pool, orch, llm, sandbox, deployer, worker, drain, settings
    return e


__all__ = ["BudgetExceeded"]
