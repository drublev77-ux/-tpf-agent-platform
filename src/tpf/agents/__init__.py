"""Task kind -> agent handler."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from tpf.agents import analyst, architect, developer, devops, qa
from tpf.agents.base import Ctx, Outcome

HANDLERS: dict[str, Callable[[Ctx], Awaitable[Outcome]]] = {
    "analyst": analyst.run,
    "architect": architect.run,
    "developer": developer.run,
    "qa": qa.run,
    "deploy_staging": devops.deploy_staging,
    "deploy_production": devops.deploy_production,
}
