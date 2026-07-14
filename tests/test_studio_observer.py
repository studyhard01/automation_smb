"""로컬 LangGraph Studio 관측기의 계약·비동기·데이터 최소화 테스트."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from smb_finder import api
from smb_finder.config import Settings
from smb_finder.index import FolderIndex
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.api import create_playground_router
from smb_finder.playground.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TokenUsage,
    ToolCallTrace,
)
from smb_finder.playground.studio_observer import (
    StudioObserver,
    build_studio_observer_event,
    sanitize_model_id,
)
from smb_finder.playground.tools import PlaygroundRuntime


def _safe_event():  # noqa: ANN202
    secret = "PATIENT-SECRET-001"
    request = ChatRequest(
        message=f"find {secret}",
        history=[ChatMessage(role="user", content=f"history-{secret}")],
        session_id=f"session-{secret}",
        local_base_url=f"http://127.0.0.1/{secret}",
        model=f"unsafe/model/{secret}",
        provider="local",
        selected_tool_ids=["find_folder", f"unknown-{secret}"],
    )
    response = ChatResponse(
        request_id="7db82189-688a-4269-9b06-a426cf9386ae",
        session_id=f"session-{secret}",
        provider_used="local",
        model_used=f"unsafe/model/{secret}",
        assistant_message=f"result-{secret}",
        tool_calls=[
            ToolCallTrace(
                tool_id="find_folder",
                tool_name=f"name-{secret}",
                arguments_summary=f"query={secret}",
                status="error",
                elapsed_ms=12.5,
                result_text=f"path-{secret}",
                result_payload={"iscn": secret},
                error_code="tool_timeout",
            ),
            ToolCallTrace(
                tool_id=f"unknown-{secret}",
                tool_name=secret,
                status="ok",
                elapsed_ms=1,
            ),
        ],
        elapsed_ms=20.0,
        warnings=[f"unknown_tools_ignored:{secret}", f"not_allowlisted:{secret}"],
        error_code="local_llm_failed",
        over_budget=False,
        token_usage=TokenUsage(
            provider="local",
            model=f"unsafe/model/{secret}",
            prompt_tokens=7,
            completion_tokens=3,
            total_tokens=10,
            calls=1,
        ),
    )
    return secret, build_studio_observer_event(request, response)


def test_observer_event_contains_only_safe_fixed_metadata():
    secret, event = _safe_event()
    payload = event.model_dump(mode="json")
    serialized = json.dumps(payload)

    assert secret not in serialized
    assert payload["model_id"] == "unrecognized-model"
    assert payload["selected_tool_ids"] == ["find_folder"]
    assert payload["called_tool_ids"] == ["find_folder"]
    assert payload["tool_calls"] == [
        {
            "tool_id": "find_folder",
            "status": "error",
            "error_code": "tool_timeout",
            "elapsed_ms": 12.5,
        }
    ]
    assert payload["error_code"] == "local_llm_failed"
    assert payload["warning_codes"] == ["unknown_tools_ignored"]
    assert payload["token_total"] == 10
    assert payload["token_calls"] == 1
    for forbidden in (
        "message",
        "history",
        "arguments_summary",
        "result_text",
        "result_payload",
        "session_id",
        "local_base_url",
        "prompt_tokens",
        "completion_tokens",
    ):
        assert forbidden not in serialized


def test_observer_event_rejects_extra_fields_and_model_id_is_not_best_effort_redaction():
    _, event = _safe_event()

    with pytest.raises(ValidationError):
        event.__class__(**event.model_dump(), message="must-not-exist")

    assert sanitize_model_id("gpt-4.1-mini") == "gpt-4.1-mini"
    assert sanitize_model_id("../../patient/path") == "unrecognized-model"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:2024",
        "http://10.0.0.5:2024",
        "http://user:pass@127.0.0.1:2024",
        "http://127.0.0.1:2024/path",
        "http://127.0.0.1:2024?token=secret",
        "http://127.0.0.1:2024#fragment",
    ],
)
def test_observer_url_is_strict_loopback_http(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, langgraph_studio_observer_url=url)


def test_disabled_observer_is_noop():
    _, event = _safe_event()
    observer = StudioObserver(Settings(_env_file=None))

    assert observer.enabled is False
    assert observer.enqueue(event) is False


def test_observer_posts_safe_event_without_blocking_enqueue(monkeypatch):
    _, event = _safe_event()
    delivered = asyncio.Event()
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *args):  # noqa: ANN002, ANN204
            return False

        async def post(self, url, *, json):  # noqa: ANN001, ANN201
            captured["url"] = url
            captured["payload"] = json
            delivered.set()
            return FakeResponse()

    monkeypatch.setattr("smb_finder.playground.studio_observer.httpx.AsyncClient", FakeClient)
    observer = StudioObserver(
        Settings(_env_file=None, langgraph_studio_observer_enabled=True)
    )

    async def exercise() -> None:
        await observer.start()
        assert observer.enqueue(event) is True
        await asyncio.wait_for(delivered.wait(), timeout=1)
        await observer.stop()

    asyncio.run(exercise())

    payload = captured["payload"]
    assert captured["url"] == "http://127.0.0.1:2024/runs/wait"
    assert captured["timeout"] == 0.3
    assert payload["assistant_id"] == "playground_observer"
    assert payload["on_completion"] == "keep"
    assert payload["metadata"] == {
        "source": "playground",
        "request_id": str(event.request_id),
    }
    assert payload["input"] == event.model_dump(mode="json")


def test_observer_delivery_failure_is_fail_open(monkeypatch):
    _, event = _safe_event()
    attempted = asyncio.Event()

    class FailingClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            pass

        async def __aenter__(self):  # noqa: ANN204
            return self

        async def __aexit__(self, *args):  # noqa: ANN002, ANN204
            return False

        async def post(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
            attempted.set()
            raise httpx.ConnectError("synthetic down")

    monkeypatch.setattr("smb_finder.playground.studio_observer.httpx.AsyncClient", FailingClient)
    observer = StudioObserver(
        Settings(_env_file=None, langgraph_studio_observer_enabled=True)
    )

    async def exercise() -> None:
        await observer.start()
        assert observer.enqueue(event) is True
        await asyncio.wait_for(attempted.wait(), timeout=1)
        await observer.stop()

    asyncio.run(exercise())


def test_langgraph_config_registers_static_observer_graph():
    config_path = Path("integrations/langgraph/langgraph.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    graph_source = Path("integrations/langgraph/smb_agent/observer_graph.py").read_text(encoding="utf-8")

    assert config["graphs"]["playground_observer"] == "./smb_agent/observer_graph.py:graph"
    assert "ChatOpenAI" not in graph_source
    assert "smb_finder" not in graph_source
    assert "request_received" in graph_source
    assert "tool_summary" in graph_source


def test_request_id_is_uuid_compatible():
    _, event = _safe_event()

    assert UUID(str(event.request_id)) == event.request_id


def test_chat_route_enqueues_completed_event_but_unknown_tool_preflight_does_not(monkeypatch):
    events = []

    class CaptureObserver:
        def enqueue(self, event):  # noqa: ANN001, ANN201
            events.append(event)
            return True

    def fake_run(self, request, registry, openai_api_key="", request_id=""):  # noqa: ANN001, ARG001
        return ChatResponse(
            request_id=request_id,
            session_id="synthetic-session",
            provider_used=request.provider,
            model_used="local-model",
            assistant_message="synthetic ok",
        )

    monkeypatch.setattr(PlaygroundAgent, "run", fake_run)
    test_app = FastAPI()
    test_app.include_router(
        create_playground_router(
            lambda: PlaygroundRuntime(settings=Settings(_env_file=None, llm_model="local-model")),
            CaptureObserver(),
        )
    )

    async def post(payload):  # noqa: ANN001, ANN202
        transport = httpx.ASGITransport(app=test_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/playground/chat", json=payload)

    ok_response = asyncio.run(
        post({"message": "synthetic", "selected_tool_ids": ["find_folder"], "provider": "local"})
    )
    unknown_response = asyncio.run(
        post({"message": "synthetic", "selected_tool_ids": ["unknown-tool"], "provider": "local"})
    )

    assert ok_response.status_code == 200
    assert len(events) == 1
    assert str(events[0].request_id) == ok_response.json()["request_id"]
    assert unknown_response.status_code == 400
    assert len(events) == 1


def test_fastapi_lifespan_starts_and_stops_observer(monkeypatch):
    class FakeContentIndex:
        def count(self) -> int:
            return 0

        def close(self) -> None:
            return None

    class FakeObserver:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    content_index = FakeContentIndex()
    observer = FakeObserver()

    async def fake_threadpool(function, *args):  # noqa: ANN001, ARG001
        if function is api.load_or_build:
            return FolderIndex([])
        if function is api.open_index:
            return content_index
        raise AssertionError("unexpected lifespan function")

    monkeypatch.setattr(api, "run_in_threadpool", fake_threadpool)
    monkeypatch.setattr(api, "_studio_observer", observer)
    monkeypatch.setattr(api, "_mcp_bundle", None)

    async def exercise() -> None:
        async with api.lifespan(api.app):
            assert observer.started is True
            assert observer.stopped is False

    asyncio.run(exercise())

    assert observer.stopped is True
