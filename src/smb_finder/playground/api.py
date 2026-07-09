"""Playground FastAPI router."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from .agent import PlaygroundAgent
from .models import (
    ChatRequest,
    ChatResponse,
    LlmStatusRequest,
    LlmStatusResponse,
    ToolDefinition,
    ToolDraftRequest,
    ToolDraftResponse,
)
from .tools import PlaygroundRuntime, build_tool_registry


def create_playground_router(runtime_getter: Callable[[], PlaygroundRuntime]) -> APIRouter:
    """Playground API 라우터를 생성한다."""

    router = APIRouter(tags=["playground"])

    @router.get(
        "/api/playground/tools",
        response_model=list[ToolDefinition],
        operation_id="list_playground_tools",
        summary="Playground tool 목록",
    )
    async def list_playground_tools() -> list[ToolDefinition]:
        runtime = runtime_getter()
        return [handler.definition for handler in build_tool_registry(runtime).values()]

    @router.post(
        "/api/playground/chat",
        response_model=ChatResponse,
        operation_id="run_playground_chat",
        summary="선택한 tool로 Playground 채팅 실행",
    )
    async def run_playground_chat(
        request: ChatRequest,
        x_playground_openai_key: str = Header(default="", alias="X-Playground-OpenAI-Key"),
    ) -> ChatResponse:
        runtime = runtime_getter()
        registry = build_tool_registry(runtime)
        unknown = sorted(set(request.selected_tool_ids) - set(registry))
        if unknown:
            raise HTTPException(status_code=400, detail={"code": "unknown_tool", "tools": unknown})
        agent = PlaygroundAgent(runtime.settings)
        return await run_in_threadpool(agent.run, request, registry, x_playground_openai_key)

    @router.post(
        "/api/playground/tool-draft",
        response_model=ToolDraftResponse,
        operation_id="draft_playground_tool",
        summary="Tool Lab tool manifest 초안 생성",
    )
    async def draft_playground_tool(
        request: ToolDraftRequest,
        x_playground_openai_key: str = Header(default="", alias="X-Playground-OpenAI-Key"),
    ) -> ToolDraftResponse:
        runtime = runtime_getter()
        agent = PlaygroundAgent(runtime.settings)
        return await run_in_threadpool(agent.draft_tool, request, x_playground_openai_key)

    @router.post(
        "/api/playground/llm-status",
        response_model=LlmStatusResponse,
        operation_id="check_playground_llm",
        summary="Playground local LLM 연결 확인",
    )
    async def check_playground_llm(
        request: LlmStatusRequest,
        x_playground_openai_key: str = Header(default="", alias="X-Playground-OpenAI-Key"),
    ) -> LlmStatusResponse:
        runtime = runtime_getter()
        agent = PlaygroundAgent(runtime.settings)
        return await run_in_threadpool(agent.check_llm, request, x_playground_openai_key)

    return router
