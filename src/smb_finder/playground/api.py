"""Playground FastAPI router."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging
import uuid

from fastapi import APIRouter, FastAPI, Header, HTTPException, Response, status
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
    SkillCreateRequest,
    SkillDefinition,
    SkillUpdateRequest,
    ToolDefinition,
    ToolDraftRequest,
    ToolDraftResponse,
)
from .skills import SkillStore, SkillStoreError
from .tools import PlaygroundRuntime, build_tool_registry

_logger = logging.getLogger(__name__)


def create_playground_router(runtime_getter: Callable[[], PlaygroundRuntime]) -> APIRouter:
    """Playground API 라우터를 생성한다."""

    settings = runtime_getter().settings
    shared_agent = PlaygroundAgent(settings, skill_store=SkillStore(settings.playground_skills_dir))

    @asynccontextmanager
    async def shared_agent_lifespan(_application: FastAPI) -> AsyncIterator[None]:
        """서비스 종료 시 재사용 중인 LLM HTTP 연결을 닫는다."""

        try:
            yield
        finally:
            shared_agent.close()

    router = APIRouter(tags=["playground"], lifespan=shared_agent_lifespan)

    def skill_store() -> SkillStore:
        return SkillStore(runtime_getter().settings.playground_skills_dir)

    def raise_skill_error(exc: SkillStoreError) -> None:
        status_code = {
            "skill_not_found": 404,
            "skill_exists": 409,
            "builtin_skill_readonly": 403,
        }.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc

    @router.get(
        "/api/playground/tools",
        response_model=list[ToolDefinition],
        operation_id="list_playground_tools",
        summary="Playground tool 목록",
    )
    async def list_playground_tools() -> list[ToolDefinition]:
        runtime = runtime_getter()
        return [handler.definition for handler in build_tool_registry(runtime).values()]

    @router.get(
        "/api/playground/skills",
        response_model=list[SkillDefinition],
        operation_id="list_playground_skills",
        summary="Playground SKILL.md 목록",
    )
    async def list_playground_skills() -> list[SkillDefinition]:
        return list(await run_in_threadpool(skill_store().list))

    @router.post(
        "/api/playground/skills",
        response_model=SkillDefinition,
        status_code=status.HTTP_201_CREATED,
        operation_id="create_playground_skill",
        summary="사용자 SKILL.md 생성",
    )
    async def create_playground_skill(request: SkillCreateRequest) -> SkillDefinition:
        try:
            return await run_in_threadpool(skill_store().create, request.skill_id, request.document)
        except SkillStoreError as exc:
            raise_skill_error(exc)
            raise AssertionError("unreachable")

    @router.put(
        "/api/playground/skills/{skill_id}",
        response_model=SkillDefinition,
        operation_id="update_playground_skill",
        summary="사용자 SKILL.md 수정",
    )
    async def update_playground_skill(skill_id: str, request: SkillUpdateRequest) -> SkillDefinition:
        try:
            return await run_in_threadpool(skill_store().update, skill_id, request.document)
        except SkillStoreError as exc:
            raise_skill_error(exc)
            raise AssertionError("unreachable")

    @router.delete(
        "/api/playground/skills/{skill_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        operation_id="delete_playground_skill",
        summary="사용자 SKILL.md 삭제",
    )
    async def delete_playground_skill(skill_id: str) -> Response:
        try:
            await run_in_threadpool(skill_store().delete, skill_id)
        except SkillStoreError as exc:
            raise_skill_error(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        result = await run_in_threadpool(
            shared_agent.execute_tool_direct,
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
        store = skill_store()
        known_skill_ids = {skill.id for skill in await run_in_threadpool(store.list)}
        unknown_skills = sorted(set(request.selected_skill_ids) - known_skill_ids)
        if unknown_skills:
            raise HTTPException(status_code=400, detail={"code": "unknown_skill", "skills": unknown_skills})
        request_id = str(uuid.uuid4())
        response = await run_in_threadpool(
            shared_agent.run,
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
        return await run_in_threadpool(shared_agent.draft_tool, request, x_playground_openai_key)

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
        return await run_in_threadpool(shared_agent.check_llm, request, x_playground_openai_key)

    return router
