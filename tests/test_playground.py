"""자체 Playground의 tool registry와 agent 계약 테스트."""

from __future__ import annotations

from types import SimpleNamespace

from smb_finder.config import Settings
from smb_finder.playground.agent import PlaygroundAgent, _json_from_text
from smb_finder.playground.models import AgentDecision, ChatRequest, LlmDebugCall, LlmStatusRequest, PlannedToolCall
from smb_finder.playground.tracing import _hide_langsmith_outputs_preserve_usage, is_langsmith_tracing_enabled
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


def _runtime(**settings_kwargs):
    settings = Settings(
        llm_model="local-model",
        llm_base_url="http://localhost:8080/v1",
        find_budget_ms=1500,
        **settings_kwargs,
    )
    return PlaygroundRuntime(settings=settings, finder=FakeFinder(), content_searcher=FakeContentSearcher())


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
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decisions = iter(
        [
            AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
            ),
            AgentDecision(action="final_answer", answer="차단된 tool 요청이 있어 실행하지 않았습니다."),
        ]
    )

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["search_content"], debug_trace=True),
        registry=registry,
    )

    assert response.tool_calls == []
    assert "unselected_tool_blocked:find_folder" in response.warnings
    assert any(step.error_code == "unselected_tool_blocked" for step in response.agent_steps)


def test_agent_executes_tool_then_final_answer(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decisions = iter(
        [
            AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
                rationale="폴더 위치 요청이므로 공유폴더 찾기를 사용합니다.",
            ),
            AgentDecision(action="final_answer", answer="OO검사 폴더를 찾았습니다."),
        ]
    )

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_trace=True),
        registry=registry,
    )

    assert response.error_code == ""
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool_id == "find_folder"
    assert response.assistant_message == "OO검사 폴더를 찾았습니다."
    assert [step.kind for step in response.agent_steps] == ["decision", "tool_call", "observation", "decision", "final"]


def test_agent_accepts_local_model_tool_call_aliases(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    def fake_chat(
        messages, *, model, base_url, max_tokens, api_key="", purpose="chat_json", debug_calls=None, **kwargs
    ):  # noqa: ANN001, ARG001
        if purpose == "agent_decision_step_1":
            return {"action": "tool_call", "tool_calls": [{"id": "find_folder", "inputs": {"query": "OO검사"}}]}
        return {"action": "final_answer", "answer": "완료"}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]),
        registry=registry,
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].tool_id == "find_folder"


def test_agent_blocks_duplicate_tool_call(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    repeated = AgentDecision(
        action="tool_call",
        tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
    )
    decisions = iter([repeated, repeated])

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_trace=True),
        registry=registry,
    )

    assert len(response.tool_calls) == 1
    assert "duplicate_tool_call_blocked:find_folder" in response.warnings
    assert any(step.error_code == "duplicate_tool_call_blocked" for step in response.agent_steps)


def test_agent_hides_agent_steps_when_trace_disabled(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: AgentDecision(action="final_answer", answer="완료"))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_trace=False),
        registry=registry,
    )

    assert response.assistant_message == "완료"
    assert response.agent_steps == []


def test_raw_llm_debug_requires_server_gate(monkeypatch):
    runtime = _runtime(playground_debug_raw_llm=False)
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: AgentDecision(action="final_answer", answer="완료"))

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_raw_llm=True),
        registry=registry,
    )

    assert response.debug is None
    assert "debug_raw_llm_denied" in response.warnings


def test_raw_llm_debug_is_returned_when_server_gate_allows(monkeypatch):
    runtime = _runtime(playground_debug_raw_llm=True)
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)

    def fake_decide(**kwargs):  # noqa: ANN003
        debug_calls = kwargs.get("debug_calls")
        if debug_calls is not None:
            debug_calls.append(LlmDebugCall(purpose="agent_decision_step_1", raw_response='{"action":"final_answer"}'))
        return AgentDecision(action="final_answer", answer="완료")

    monkeypatch.setattr(agent, "_decide_next_action", fake_decide)

    response = agent.run(
        ChatRequest(message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"], debug_raw_llm=True),
        registry=registry,
    )

    assert response.debug is not None
    assert response.debug.llm_calls[0].purpose == "agent_decision_step_1"


def test_safe_debug_text_redacts_secrets_and_limits_length():
    settings_kwargs = {"llm_api" + "_key": "secret-key-value"}
    agent = PlaygroundAgent(
        Settings(
            smb_host="fileserver.local",
            smb_share_name="labshare",
            smb_password="pw-value",
            admin_api_token="admin-token-value",
            playground_debug_preview_chars=60,
            **settings_kwargs,
        )
    )

    text = "http://user:pass@localhost/v1 fileserver.local labshare pw-value admin-token-value secret-key-value "
    text += "x" * 100
    safe = agent._safe_debug_text(text)

    assert "fileserver.local" not in safe
    assert "labshare" not in safe
    assert "pw-value" not in safe
    assert "admin-token-value" not in safe
    assert "secret-key-value" not in safe
    assert "user:pass" not in safe
    assert len(safe) < len(text)


def test_json_from_text_repairs_missing_outer_object_closer():
    raw = """```json
{"tool_calls":[{"tool_id":"find_folder","arguments":{"query":"BRCA"}}]
```"""

    assert _json_from_text(raw) == {
        "tool_calls": [{"tool_id": "find_folder", "arguments": {"query": "BRCA"}}],
    }


def test_llm_status_blocks_public_url():
    agent = PlaygroundAgent(Settings(llm_model="local-model", llm_base_url="http://localhost:8080/v1"))

    response = agent.check_llm(LlmStatusRequest(local_base_url="https://llm.example/v1", model="local-model"))

    assert response.error_code == "local_llm_url_not_internal"


def test_llm_status_lists_models_and_checks_chat(monkeypatch):
    fake_key = "secret-key"
    settings_kwargs = {"llm_api" + "_key": fake_key}
    agent = PlaygroundAgent(Settings(llm_model="", llm_base_url="http://localhost:8080/v1", **settings_kwargs))
    captured: dict[str, str] = {}

    def fake_models(base_url, api_key=""):  # noqa: ANN001, ARG001
        captured["models_base_url"] = base_url
        return ["model-a", "model-b"]

    def fake_chat(
        messages, *, model, base_url, max_tokens, api_key="", purpose="chat_json", debug_calls=None, **kwargs
    ):  # noqa: ANN001, ARG001
        captured["chat_base_url"] = base_url
        captured["model"] = model
        captured["purpose"] = purpose
        captured["api_key"] = api_key
        return {"ok": True}

    monkeypatch.setattr(agent, "_list_models", fake_models)
    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.check_llm(LlmStatusRequest(local_base_url="http://127.0.0.1:11434/v1", model="model-b"))

    assert response.models_ok is True
    assert response.chat_ok is True
    assert response.available_models == ["model-a", "model-b"]
    assert response.base_url_used == "http://127.0.0.1:11434/v1"
    assert response.model_used == "model-b"
    assert response.model_dump_json().find(fake_key) == -1
    assert captured == {
        "models_base_url": "http://127.0.0.1:11434/v1",
        "chat_base_url": "http://127.0.0.1:11434/v1",
        "model": "model-b",
        "purpose": "llm_status",
        "api_key": "",
    }


def test_openai_status_requires_transient_key():
    agent = PlaygroundAgent(Settings(openai_model="gpt-test", openai_api_key=""))

    response = agent.check_llm(LlmStatusRequest(provider="openai", model="gpt-test"))

    assert response.provider_used == "openai"
    assert response.error_code == "openai_api_key_missing"


def test_openai_status_uses_header_key_and_does_not_echo_it(monkeypatch):
    agent = PlaygroundAgent(Settings(openai_model="gpt-test", openai_api_key=""))
    captured: dict[str, str] = {}

    def fake_models(base_url, api_key=""):  # noqa: ANN001
        captured["models_base_url"] = base_url
        captured["models_api_key"] = api_key
        return ["gpt-test"]

    def fake_chat(
        messages, *, model, base_url, max_tokens, api_key="", purpose="chat_json", debug_calls=None, **kwargs
    ):  # noqa: ANN001, ARG001
        captured["chat_base_url"] = base_url
        captured["chat_api_key"] = api_key
        captured["model"] = model
        return {"ok": True}

    monkeypatch.setattr(agent, "_list_models", fake_models)
    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.check_llm(LlmStatusRequest(provider="openai", model="gpt-test"), "provider-token")

    assert response.provider_used == "openai"
    assert response.base_url_used == "https://api.openai.com/v1"
    assert response.chat_ok is True
    assert response.model_dump_json().find("provider-token") == -1
    assert captured == {
        "models_base_url": "https://api.openai.com/v1",
        "models_api_key": "provider-token",
        "chat_base_url": "https://api.openai.com/v1",
        "chat_api_key": "provider-token",
        "model": "gpt-test",
    }


def test_openai_provider_uses_same_tool_loop(monkeypatch):
    runtime = _runtime()
    registry = build_tool_registry(runtime)
    agent = PlaygroundAgent(runtime.settings)
    decisions = iter(
        [
            AgentDecision(
                action="tool_call",
                tool_calls=[PlannedToolCall(tool_id="find_folder", arguments={"query": "OO검사"})],
            ),
            AgentDecision(action="final_answer", answer="OpenAI provider에서도 tool 결과를 사용했습니다."),
        ]
    )

    monkeypatch.setattr(agent, "_decide_next_action", lambda **kwargs: next(decisions))

    response = agent.run(
        ChatRequest(provider="openai", model="gpt-test", message="OO검사 폴더 찾아줘", selected_tool_ids=["find_folder"]),
        registry,
        "provider-token",
    )

    assert response.provider_used == "openai"
    assert response.error_code == ""
    assert response.tool_calls[0].tool_id == "find_folder"
    assert "external_llm_provider:openai" in response.warnings
    assert response.model_dump_json().find("provider-token") == -1


def test_openai_chat_response_exposes_token_usage(monkeypatch):
    agent = PlaygroundAgent(Settings(openai_model="gpt-test", openai_api_key="", langsmith_tracing=False))

    class FakeResponse:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"action":"final_answer","answer":"token usage ok"}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):  # noqa: ANN201
            return self

        def __exit__(self, *args):  # noqa: ANN002, ANN201
            return False

        def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            return FakeResponse()

    monkeypatch.setattr("smb_finder.playground.agent.httpx.Client", FakeClient)

    response = agent.run(
        ChatRequest(provider="openai", model="gpt-test", message="synthetic token usage check"),
        {},
        "provider-token",
    )

    assert response.assistant_message == "token usage ok"
    assert response.token_usage is not None
    assert response.token_usage.provider == "openai"
    assert response.token_usage.model == "gpt-test"
    assert response.token_usage.prompt_tokens == 11
    assert response.token_usage.completion_tokens == 4
    assert response.token_usage.total_tokens == 15
    assert response.token_usage.calls == 1
    assert response.model_dump_json().find("provider-token") == -1


def test_langsmith_tracing_requires_openai_and_key():
    assert is_langsmith_tracing_enabled(Settings(_env_file=None), "openai") is False
    assert (
        is_langsmith_tracing_enabled(
            Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="ls-test-key"),
            "local",
        )
        is False
    )
    assert (
        is_langsmith_tracing_enabled(
            Settings(_env_file=None, langsmith_tracing=True, langsmith_api_key="ls-test-key"),
            "openai",
        )
        is True
    )


def test_langsmith_output_hiding_preserves_usage_metadata_only():
    hidden = _hide_langsmith_outputs_preserve_usage(
        {
            "raw_response": "do not send",
            "usage_metadata": {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11},
        }
    )

    assert hidden == {"usage_metadata": {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11}}
