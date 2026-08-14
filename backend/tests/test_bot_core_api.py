"""Bot Core runner 주입이 공개 문서 대화 계약을 바꾸지 않는지 검증한다."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.playground.document_api import DocumentRuntime, create_document_router
from smb_finder.playground.document_models import ChatResponse


def _settings() -> Settings:
    return Settings(_env_file=None, llmops_chat_model="local-model", ollama_base_url="http://127.0.0.1:11434")


def _app(runtime: DocumentRuntime) -> FastAPI:
    app = FastAPI()
    app.include_router(create_document_router(lambda: runtime))
    return app


def _request(app: FastAPI, payload: dict) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/playground/chat", json=payload)

    return asyncio.run(send())


def _get(app: FastAPI, path: str) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(send())


class CountingBotCoreRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request, *, request_id: str):  # noqa: ANN001, ANN204
        self.calls += 1
        return ChatResponse(
            request_id=request_id,
            session_id=request.session_id or "session-synthetic",
            model_used="deterministic-fake",
            assistant_message="합성 Bot Core 응답",
        )


def test_injected_bot_core_runner_is_called_exactly_once():
    runner = CountingBotCoreRunner()
    app = _app(DocumentRuntime(settings=_settings(), bot_core_runner=runner))

    response = _request(
        app,
        {"message": "일반 합성 요청", "mode": "document_qa", "session_id": "session-synthetic"},
    )

    assert response.status_code == 200
    assert response.json()["assistant_message"] == "합성 Bot Core 응답"
    assert runner.calls == 1


def test_missing_bot_core_runner_falls_back_to_document_chat_service():
    app = _app(DocumentRuntime(settings=_settings()))

    response = _request(app, {"message": "일반 합성 요청", "mode": "document_qa"})

    assert response.status_code == 200
    assert response.json()["error_code"] == "selected_file_required"


def test_bot_core_internal_state_is_not_added_to_public_chat_schema():
    schema = _app(DocumentRuntime(settings=_settings())).openapi()
    operation = schema["paths"]["/api/playground/chat"]["post"]
    response_schema = schema["components"]["schemas"]["ChatResponse"]["properties"]

    assert operation["operationId"] == "run_document_chat"
    assert set(response_schema) == {
        "request_id",
        "session_id",
        "provider_used",
        "model_used",
        "assistant_message",
        "tool_calls",
        "elapsed_ms",
        "warnings",
        "error_code",
        "over_budget",
        "rag_grounding",
        "citations",
        "retrieval",
        "artifacts",
        "token_usage",
    }


def test_mcp_route_remains_disabled():
    app = _app(DocumentRuntime(settings=_settings()))

    response = _get(app, "/mcp")

    assert response.status_code == 404
