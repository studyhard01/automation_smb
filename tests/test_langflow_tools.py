"""Langflow MCP tool 등록·동기화·Playground 실행 테스트."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.playground.api import create_playground_router
from smb_finder.playground.langflow_tools import (
    LangflowMcpGateway,
    LangflowToolError,
    LangflowToolStore,
    RemoteToolContract,
)
from smb_finder.playground.models import LangflowToolSourceRequest, ToolExecutionResult
from smb_finder.playground.tools import PlaygroundRuntime, build_tool_registry


def _settings(tmp_path, **overrides):  # noqa: ANN001
    return Settings(
        _env_file=None,
        playground_langflow_tools_path=str(tmp_path / "langflow-tools.json"),
        playground_langflow_allowed_hosts="127.0.0.1,localhost,::1",
        **overrides,
    )


def _request() -> LangflowToolSourceRequest:
    return LangflowToolSourceRequest(
        source_id="synthetic-flow",
        display_name="Synthetic Flow",
        mcp_url="http://127.0.0.1:8765/mcp",
        timeout_ms=3000,
    )


def _contracts() -> list[RemoteToolContract]:
    return [
        RemoteToolContract(
            name="synthetic_echo_workflow",
            description="합성 문자열을 되돌려줍니다.",
            input_schema={
                "type": "object",
                "properties": {"input_value": {"type": "string"}},
                "required": ["input_value"],
            },
        )
    ]


def test_store_persists_contract_without_api_key_and_registry_executes_cached_tool(tmp_path):  # noqa: ANN001
    settings = _settings(tmp_path, langflow_mcp_api_key="secret-test-key")
    store = LangflowToolStore(settings)
    source = store.save_sync(_request(), _contracts(), 12.5)

    serialized = json.loads((tmp_path / "langflow-tools.json").read_text(encoding="utf-8"))
    assert "secret-test-key" not in json.dumps(serialized)
    assert source.api_key_configured is True
    assert source.tools[0].definition.origin == "langflow"
    assert source.tools[0].definition.category == "workflow"

    class FakeGateway:
        def call_tool(self, current_source, remote_name, arguments):  # noqa: ANN001
            assert current_source.source_id == "synthetic-flow"
            assert remote_name == "synthetic_echo_workflow"
            assert arguments == {"input_value": "synthetic hello"}
            return ToolExecutionResult(
                result_text="synthetic result",
                result_payload={"synthetic": True},
                arguments_summary="argument_keys=input_value elapsed_ms=4.2",
            )

    registry = build_tool_registry(
        PlaygroundRuntime(settings=settings),
        langflow_store=store,
        langflow_gateway=FakeGateway(),  # type: ignore[arg-type]
    )
    tool_id = source.tools[0].definition.id

    result = registry[tool_id].execute({"input_value": "synthetic hello"})

    assert registry[tool_id].returns_final_answer is True
    assert result.status == "ok"
    assert result.result_text == "synthetic result"


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://unapproved.example/mcp", "langflow_host_not_allowed"),
        ("http://user:password@127.0.0.1:8765/mcp", "langflow_url_userinfo_forbidden"),
        ("http://127.0.0.1:8765/mcp?api_key=secret", "langflow_url_invalid"),
        ("file:///tmp/mcp", "langflow_url_invalid"),
    ],
)
def test_store_rejects_unsafe_outbound_urls(tmp_path, url, code):  # noqa: ANN001
    store = LangflowToolStore(_settings(tmp_path))

    with pytest.raises(LangflowToolError) as exc_info:
        store.validate_url(url)

    assert exc_info.value.code == code


def test_langflow_registration_sync_listing_direct_test_and_delete_api(tmp_path, monkeypatch):  # noqa: ANN001
    settings = _settings(tmp_path)

    def fake_list_tools(self, source):  # noqa: ANN001, ARG001
        return _contracts(), 14.2

    def fake_call_tool(self, source, remote_name, arguments):  # noqa: ANN001, ARG001
        assert remote_name == "synthetic_echo_workflow"
        assert arguments == {"input_value": "synthetic api"}
        return ToolExecutionResult(
            result_text="synthetic api result",
            result_payload={"source_id": source.source_id},
            arguments_summary="argument_keys=input_value elapsed_ms=3.1",
        )

    monkeypatch.setattr(LangflowMcpGateway, "list_tools", fake_list_tools)
    monkeypatch.setattr(LangflowMcpGateway, "call_tool", fake_call_tool)
    app = FastAPI()
    app.include_router(create_playground_router(lambda: PlaygroundRuntime(settings=settings)))

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            registered = await client.post("/api/playground/langflow-sources", json=_request().model_dump())
            assert registered.status_code == 200
            payload = registered.json()
            assert payload["tool_count"] == 1
            tool_id = payload["source"]["tools"][0]["definition"]["id"]

            tools = await client.get("/api/playground/tools")
            assert tools.status_code == 200
            dynamic = next(item for item in tools.json() if item["id"] == tool_id)
            assert dynamic["origin"] == "langflow"
            assert dynamic["source_id"] == "synthetic-flow"

            tested = await client.post(
                f"/api/playground/langflow-tools/{tool_id}/test",
                json={"arguments": {"input_value": "synthetic api"}},
            )
            assert tested.status_code == 200
            assert tested.json()["result_text"] == "synthetic api result"
            assert tested.json()["elapsed_ms"] >= 0

            synced = await client.post("/api/playground/langflow-sources/synthetic-flow/sync")
            assert synced.status_code == 200
            assert synced.json()["tool_count"] == 1

            deleted = await client.delete("/api/playground/langflow-sources/synthetic-flow")
            assert deleted.status_code == 204
            assert (await client.get("/api/playground/langflow-sources")).json() == []

    asyncio.run(exercise())
