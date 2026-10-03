"""Structured output: the gateway holds the model to the schema's required fields."""
from __future__ import annotations

import json

import httpx
import pytest

from tpf.config import Settings
from tpf.llm.gateway import AnthropicGateway, LLMError, missing_required

SCHEMA = {"type": "object", "required": ["title", "spec"], "properties": {
    "title": {"type": "string"},
    "spec": {"type": "object", "required": ["pages", "client_content"], "properties": {
        "pages": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}},
        "client_content": {"type": "array", "items": {"type": "string"}}}}}}


class NoBudget:
    async def check(self, order_id): ...
    async def record(self, *a): ...


def test_missing_required_walks_objects_and_arrays():
    assert missing_required(SCHEMA, {"title": "x", "spec": {"pages": [{"name": "Home"}], "client_content": []}}) == []
    assert missing_required(SCHEMA, {"spec": {"pages": [{"name": "a"}, {}]}}) == ["title", "spec.client_content", "spec.pages.1.name"]
    assert missing_required(SCHEMA, {"title": "x", "spec": "oops"}) == ["spec"]


def _gateway(replies: list[dict]) -> tuple[AnthropicGateway, list[dict]]:
    sent: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=replies[len(sent) - 1])
    gw = AnthropicGateway(Settings(anthropic_api_key="k"), NoBudget())
    gw.client = httpx.AsyncClient(base_url="https://api.anthropic.com", transport=httpx.MockTransport(handler))
    return gw, sent


def _call(value: dict, id_: str = "tu_1") -> dict:
    return {"stop_reason": "tool_use", "usage": {}, "content": [{"type": "tool_use", "id": id_, "name": "submit", "input": value}]}


async def test_incomplete_submit_is_sent_back_once():
    good = {"title": "x", "spec": {"pages": [{"name": "Home"}], "client_content": []}}
    gw, sent = _gateway([_call({"title": "x", "spec": {"pages": []}}), _call(good, "tu_2")])
    out = await gw.structured(order_id=1, agent="analyst", model_class="strong", system="s", prompt="p", schema=SCHEMA)
    assert out == good and len(sent) == 2
    retry = sent[1]["messages"][-1]["content"][0]
    assert retry["type"] == "tool_result" and retry["tool_use_id"] == "tu_1" and "spec.client_content" in retry["content"]
    assert sent[0]["tool_choice"] == {"type": "auto"}


async def test_plain_text_then_never_complete_fails_loudly():
    text = {"stop_reason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "Here is the spec…"}]}
    gw, sent = _gateway([text, _call({"title": "x"}), _call({"title": "x"}, "tu_3")])
    with pytest.raises(LLMError, match="missing required fields: spec"):
        await gw.structured(order_id=1, agent="analyst", model_class="strong", system="s", prompt="p", schema=SCHEMA)
    assert len(sent) == 3 and sent[1]["messages"][-1]["content"] == "Call the `submit` tool now with the complete result."
