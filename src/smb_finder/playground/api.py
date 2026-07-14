"""Playground FastAPI router."""

from __future__ import annotations

from collections.abc import Callable
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from smb_finder.reports.models import (
    CytogeneticsKaryotypeSummaryRequest,
    CytogeneticsKaryotypeSummaryResponse,
)

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
from .studio_observer import StudioObserver, build_studio_observer_event

_logger = logging.getLogger(__name__)


def create_playground_router(
    runtime_getter: Callable[[], PlaygroundRuntime],
    studio_observer: StudioObserver | None = None,
) -> APIRouter:
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
        "/api/playground/karyotype-summary",
        response_model=CytogeneticsKaryotypeSummaryResponse,
        operation_id="summarize_cytogenetics_karyotype",
        summary="ISCN 핵형분석요약결과 생성",
        responses={
            400: {"description": "입력 또는 LLM 설정 오류"},
            422: {"description": "길이 제한을 벗어난 ISCN 입력"},
            502: {"description": "LLM 호출 또는 응답 오류"},
        },
    )
    async def summarize_cytogenetics_karyotype(
        request: CytogeneticsKaryotypeSummaryRequest,
        x_playground_openai_key: str = Header(default="", alias="X-Playground-OpenAI-Key"),
    ) -> CytogeneticsKaryotypeSummaryResponse:
        runtime = runtime_getter()
        handler = build_tool_registry(runtime)["cytogenetics_karyotype_summary"]
        agent = PlaygroundAgent(runtime.settings)
        result = await run_in_threadpool(
            agent.execute_tool_direct,
            handler,
            {"iscn": request.iscn},
            provider=request.provider,
            local_base_url=request.local_base_url,
            model=request.model,
            openai_api_key=x_playground_openai_key,
        )
        if result.status == "error":
            if result.error_code == "iscn_too_long":
                status_code = 422
            elif result.error_code in {
                "karyotype_summary_llm_failed",
                "karyotype_summary_invalid_response",
                "llm_context_required",
            }:
                status_code = 502
            else:
                status_code = 400
            raise HTTPException(
                status_code=status_code,
                detail={"code": result.error_code, "message": result.result_text},
            )
        if result.result_payload is None:
            raise HTTPException(
                status_code=500,
                detail={"code": "karyotype_summary_failed", "message": "핵형분석요약결과를 만들지 못했습니다."},
            )
        return CytogeneticsKaryotypeSummaryResponse.model_validate(result.result_payload)

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
        request_id = str(uuid.uuid4())
        response = await run_in_threadpool(
            agent.run,
            request,
            registry,
            x_playground_openai_key,
            request_id,
        )
        log_completed = _logger.warning if response.over_budget else _logger.info
        log_completed(
            "Playground chat completed: request_id=%s provider=%s tool_calls=%d elapsed_ms=%.1f "
            "over_budget=%s error_code=%s",
            response.request_id,
            response.provider_used,
            len(response.tool_calls),
            response.elapsed_ms,
            response.over_budget,
            response.error_code or "none",
        )
        if studio_observer is not None:
            try:
                studio_observer.enqueue(build_studio_observer_event(request, response))
            except Exception:  # noqa: BLE001 - 관측 실패는 사용자 응답에 영향을 주지 않는다.
                _logger.warning("LangGraph Studio observer event rejected")
        return response

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
