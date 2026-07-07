"""자체 Playground의 tool registry와 agent 계약 테스트."""

from __future__ import annotations

from types import SimpleNamespace

from smb_finder.config import Settings
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.models import ChatRequest, PlannedToolCall
from smb_finder.playground.tools import PlaygroundRuntime, build_tool_registry


class FakeFinder:
    def find(self, request):  # noqa: ANN001
        return SimpleNamespace(
            normalized_query=request.query,
            elapsed_ms=12.3,
            hits=[SimpleNamespace(name="OO검사", path="검사결과/2026/OO검사")],
        )


class FakeContentSearcher:
    def search(self, request):  # noqa: ANN001
        return SimpleNamespace(
            terms=[request.query],
            elapsed_ms=10.0,
            indexed_files=1,
            hits=[SimpleNamespace(name="report.txt", ext=".txt", path="검사결과/report.txt")],
        )


def test_tool_registry_exposes_builtin_tools_without_admin():
    registry = build_tool_registry(
        PlaygroundRuntime(
            settings=Settings(admin_api_token="", find_budget_ms=1500),
            finder=FakeFinder(),
            content_searcher=FakeContentSearcher(),
        )
    )

    assert set(registry) == {"find_folder", "search_content", "refresh_content"}
    assert registry["find_folder"].definition.default_selected is True
    assert registry["refresh_content"].definition.enabled is False

    result = registry["find_folder"].run({"query": "OO검사 폴더 찾아줘"})

    assert result.status == "ok"
    assert "OO검사" in result.result_text
    assert "query_len=" in result.arguments_summary


def test_agent_requires_local_llm_model():
    agent = PlaygroundAgent(Settings(llm_model="", llm_base_url="http://localhost:8080/v1"))

    response = agent.run(ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]), registry={})

    assert response.error_code == "local_llm_not_configured"
    assert response.tool_calls == []


def test_agent_blocks_unselected_tool_from_llm(monkeypatch):
    runtime = PlaygroundRuntime(
        settings=Settings(llm_model="local-model", llm_base_url="http://localhost:8080/v1"),
        finder=FakeFinder(),
        content_searcher=FakeContentSearcher(),
    )
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    def fake_plan(message, handlers, model):  # noqa: ANN001, ARG001
        return [PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})]

    monkeypatch.setattr(agent, "_plan_tool_calls", fake_plan)
    monkeypatch.setattr(agent, "_compose_answer", lambda message, traces, model, warnings: "blocked")

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["search_content"]),
        registry=registry,
    )

    assert response.tool_calls == []
    assert "unselected_tool_blocked:find_folder" in response.warnings


def test_agent_executes_selected_tool(monkeypatch):
    runtime = PlaygroundRuntime(
        settings=Settings(llm_model="local-model", llm_base_url="http://localhost:8080/v1"),
        finder=FakeFinder(),
        content_searcher=FakeContentSearcher(),
    )
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    def fake_plan(message, handlers, model):  # noqa: ANN001, ARG001
        return [PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})]

    monkeypatch.setattr(agent, "_plan_tool_calls", fake_plan)
    monkeypatch.setattr(agent, "_compose_answer", lambda message, traces, model, warnings: traces[0].result_text)

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]),
        registry=registry,
    )

    assert response.error_code == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool_id == "find_folder"
    assert "OO검사" in response.assistant_message
