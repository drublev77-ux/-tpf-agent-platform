"""LLM gateway: the only way agents reach a model.

- Structured output: each call forces a single `submit` tool whose input schema is the agent's
  output schema, so results arrive as validated JSON instead of free text.
- Budgets: every call is checked against the order's token budget before it is sent and recorded
  after; crossing the warning share logs an event, crossing the budget raises BudgetExceeded.
- Model routing: agents ask for a class (strong, code, fast, reviewer); the model comes from config.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import asyncpg
import httpx

from tpf.config import Settings

log = logging.getLogger("tpf.llm")

MODEL_CLASSES = ("strong", "code", "fast", "reviewer")


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class LLMGateway(Protocol):
    async def structured(
        self, *, order_id: int, agent: str, model_class: str, system: str, prompt: str,
        schema: dict[str, Any], max_tokens: int = 8000,
    ) -> dict[str, Any]: ...


def missing_required(schema: dict[str, Any], value: Any, path: str = "") -> list[str]:
    """Paths of the fields a JSON schema marks as required that `value` does not have."""
    out: list[str] = []
    if schema.get("type") == "object" and isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value or value[key] is None:
                out.append(f"{path}{key}")
        for key, sub in schema.get("properties", {}).items():
            if key in value and value[key] is not None:
                out += missing_required(sub, value[key], f"{path}{key}.")
    elif schema.get("type") == "array" and isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value[:200]):
            out += missing_required(schema["items"], item, f"{path}{i}.")
    elif schema.get("type") == "object" and path:
        out.append(path.rstrip("."))  # an object was expected here
    return out


class Budget:
    """Token accounting per order, stored on the order row."""

    def __init__(self, pool: asyncpg.Pool, settings: Settings) -> None:
        self.pool = pool
        self.settings = settings

    async def check(self, order_id: int) -> None:
        row = await self.pool.fetchrow("SELECT tokens_used, token_budget FROM orders WHERE id = $1", order_id)
        if row and row["tokens_used"] >= row["token_budget"]:
            raise BudgetExceeded(f"token budget used up: {row['tokens_used']} of {row['token_budget']}")

    async def record(self, order_id: int, agent: str, model: str, input_tokens: int, output_tokens: int) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO llm_usage (order_id, agent, model, input_tokens, output_tokens) VALUES ($1,$2,$3,$4,$5)",
                order_id, agent, model, input_tokens, output_tokens,
            )
            row = await conn.fetchrow(
                "UPDATE orders SET tokens_used = tokens_used + $2 WHERE id = $1 RETURNING tokens_used, token_budget",
                order_id, input_tokens + output_tokens,
            )
            before = row["tokens_used"] - input_tokens - output_tokens
            warn_at = row["token_budget"] * self.settings.budget_warning_share
            if before < warn_at <= row["tokens_used"]:
                await conn.execute(
                    "INSERT INTO events (order_id, kind, name, actor, data) VALUES ($1, 'system', 'budget_warning', 'llm-gateway', $2)",
                    order_id, {"tokens_used": row["tokens_used"], "token_budget": row["token_budget"]},
                )


class AnthropicGateway:
    API_VERSION = "2023-06-01"
    RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

    def __init__(self, settings: Settings, budget: Budget) -> None:
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self.settings = settings
        self.budget = budget
        self.client = httpx.AsyncClient(
            base_url=settings.anthropic_base_url,
            timeout=settings.llm_timeout_sec,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": self.API_VERSION,
                "content-type": "application/json",
            },
        )

    def model_for(self, model_class: str) -> str:
        if model_class not in MODEL_CLASSES:
            raise ValueError(f"unknown model class {model_class!r}")
        return getattr(self.settings, f"model_{model_class}")

    async def structured(
        self, *, order_id: int, agent: str, model_class: str, system: str, prompt: str,
        schema: dict[str, Any], max_tokens: int = 8000,
    ) -> dict[str, Any]:
        await self.budget.check(order_id)
        model = self.model_for(model_class)
        # Newer models reject a forced tool_choice, so the tool is offered with "auto" and the
        # instruction to call it; a reply without the call gets one corrective turn.
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system + "\n\nReturn your result only by calling the `submit` tool, exactly once. "
                               "Do not answer in plain text.",
            "messages": messages,
            "tools": [{
                "name": "submit",
                "description": "Submit your finished result. Call exactly once.",
                "input_schema": schema,
            }],
            "tool_choice": {"type": "auto"},
        }
        problem = "model returned no submit call"
        for attempt in range(3):
            data = await self._post(body)
            usage = data.get("usage", {})
            await self.budget.record(order_id, agent, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            if data.get("stop_reason") == "max_tokens":
                raise LLMError(f"{agent}: output hit max_tokens={max_tokens}")
            call = next((b for b in data.get("content", []) if b.get("type") == "tool_use" and b.get("name") == "submit"), None)
            if call is not None:
                # Without a forced tool call the API does not hold the model to the schema's required
                # fields, so they are checked here and the model is sent back for whatever is missing.
                missing = missing_required(schema, call["input"])
                if not missing:
                    return call["input"]
                problem = "submit call is missing required fields: " + ", ".join(missing[:20])
            if attempt == 2:
                break
            messages.append({"role": "assistant", "content": data.get("content") or [{"type": "text", "text": "(no output)"}]})
            if call is None:
                messages.append({"role": "user", "content": "Call the `submit` tool now with the complete result."})
            else:
                results = [{"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                            "content": f"Rejected: {problem}. Call `submit` again with the complete result, every required field filled."}
                           for b in data["content"] if b.get("type") == "tool_use"]
                messages.append({"role": "user", "content": results})
        raise LLMError(f"{agent}: {problem}")

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        delay = 2.0
        for attempt in range(4):
            try:
                r = await self.client.post("/v1/messages", json=body)
            except httpx.TransportError as e:
                if attempt == 3:
                    raise LLMError(f"transport error: {e}") from e
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code not in self.RETRY_STATUS or attempt == 3:
                    raise LLMError(f"API {r.status_code}: {r.text[:500]}")
                retry_after = r.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            log.warning("LLM call retry %s in %.0fs", attempt + 1, delay)
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError("unreachable")

    async def aclose(self) -> None:
        await self.client.aclose()
