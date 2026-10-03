"""Task kind -> agent handler."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from tpf import payments
from tpf.agents import analyst, architect, developer, devops, qa
from tpf.agents.base import Ctx, Outcome

HANDLERS: dict[str, Callable[[Ctx], Awaitable[Outcome]]] = {
    "analyst": analyst.run,
    "architect": architect.run,
    "developer": developer.run,
    "qa": qa.run,
    "deploy_staging": devops.deploy_staging,
    "deploy_production": devops.deploy_production,
    "request_prepayment": payments.request_prepayment,
    "request_final_payment": payments.request_final_payment,
    "grant_repo_access": devops.grant_repo_access,
}

# Tasks that serve a finished order without moving it; they run whatever state the order is in.
SIDE_TASKS = {"grant_repo_access"}
