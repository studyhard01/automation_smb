"""Playground FastAPI router."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging
import time
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from smb_finder.qc_audit import AttachmentStore, AttachmentStoreError, QcAuditService
from smb_finder.qc_audit.models import AttachmentMetadata
from smb_finder.reports.models import (
    CytogeneticsKaryotypeSummaryRequest,
    CytogeneticsKaryotypeSummaryResponse,
)

from .agent import PlaygroundAgent
from .langflow_tools import LangflowMcpGateway, LangflowToolError, LangflowToolStore
from .models import (
    ChatRequest,
    ChatResponse,
    LangflowToolSource,
    LangflowToolSourceRequest,
    LangflowToolSyncResponse,
    LangflowToolTestRequest,
    LlmStatusRequest,
    LlmStatusResponse,
    SkillCreateRequest,
    SkillDefinition,
    SkillUpdateRequest,
    ToolDefinition,
    ToolCallTrace,
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
    langflow_store = LangflowToolStore(settings)
    langflow_gateway = LangflowMcpGateway(settings)
    attachment_store = AttachmentStore(
        settings.playground_upload_dir,
        max_bytes=settings.playground_upload_max_bytes,
        max_text_chars=settings.playground_document_max_chars,
    )
    qc_audit_service = QcAuditService(
        attachment_store,
        settings.playground_qc_sop_path,
        budget_ms=settings.playground_qc_audit_budget_ms,
    )

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

    def raise_langflow_error(exc: LangflowToolError) -> None:
        status_code = {
            "langflow_source_not_found": 404,
            "langflow_sync_failed": 502,
            "langflow_tool_call_failed": 502,
        }.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc

    def raise_attachment_error(exc: AttachmentStoreError) -> None:
        status_code = {
            "attachment_not_found": 404,
            "attachment_too_large": 413,
        }.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc

    def registry_for(runtime: PlaygroundRuntime) -> dict:
        return build_tool_registry(
            runtime,
            langflow_store=langflow_store,
            langflow_gateway=langflow_gateway,
            qc_audit_service=qc_audit_service,
        )

    @router.get(
        "/api/playground/tools",
        response_model=list[ToolDefinition],
        operation_id="list_playground_tools",
        summary="Playground tool 목록",
    )
    async def list_playground_tools() -> list[ToolDefinition]:
        runtime = runtime_getter()
        return [handler.definition for handler in registry_for(runtime).values()]

    @router.post(
        "/api/playground/attachments",
        response_model=AttachmentMetadata,
        status_code=status.HTTP_201_CREATED,
        operation_id="upload_playground_attachment",
        summary="QC 감사용 PDF/Markdown 첨부",
    )
    async def upload_playground_attachment(
        request: Request,
        x_playground_filename: str = Header(default="", alias="X-Playground-Filename"),
    ) -> AttachmentMetadata:
        """multipart 의존성 없이 한 파일의 원시 바이트를 제한 크기로 받는다."""

        raw_length = request.headers.get("content-length", "")
        if raw_length.isdigit() and int(raw_length) > settings.playground_upload_max_bytes:
            raise HTTPException(
                status_code=413,
                detail={"code": "attachment_too_large", "message": "첨부파일 크기 제한을 넘었습니다."},
            )
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > settings.playground_upload_max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "attachment_too_large", "message": "첨부파일 크기 제한을 넘었습니다."},
                )
        try:
            return await run_in_threadpool(
                attachment_store.save,
                unquote(x_playground_filename),
                bytes(data),
                request.headers.get("content-type", ""),
            )
        except AttachmentStoreError as exc:
            raise_attachment_error(exc)
            raise AssertionError("unreachable")

    @router.delete(
        "/api/playground/attachments/{attachment_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        operation_id="delete_playground_attachment",
        summary="Playground 로컬 첨부 삭제",
    )
    async def delete_playground_attachment(attachment_id: str) -> Response:
        try:
            await run_in_threadpool(attachment_store.delete, attachment_id)
        except AttachmentStoreError as exc:
            raise_attachment_error(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/api/playground/langflow-sources",
        response_model=list[LangflowToolSource],
        operation_id="list_playground_langflow_sources",
        summary="등록된 Langflow MCP 연결 목록",
    )
    async def list_langflow_sources() -> list[LangflowToolSource]:
        try:
            return list(await run_in_threadpool(langflow_store.list_sources))
        except LangflowToolError as exc:
            raise_langflow_error(exc)
            raise AssertionError("unreachable")

    @router.post(
        "/api/playground/langflow-sources",
        response_model=LangflowToolSyncResponse,
        operation_id="register_playground_langflow_source",
        summary="Langflow MCP 연결 등록 및 tool 동기화",
    )
    async def register_langflow_source(request: LangflowToolSourceRequest) -> LangflowToolSyncResponse:
        try:
            contracts, elapsed_ms = await run_in_threadpool(langflow_gateway.list_tools, request)
            source = await run_in_threadpool(langflow_store.save_sync, request, contracts, elapsed_ms)
        except LangflowToolError as exc:
            raise_langflow_error(exc)
            raise AssertionError("unreachable")
        return LangflowToolSyncResponse(source=source, tool_count=len(source.tools), elapsed_ms=elapsed_ms)

    @router.post(
        "/api/playground/langflow-sources/{source_id}/sync",
        response_model=LangflowToolSyncResponse,
        operation_id="sync_playground_langflow_source",
        summary="등록된 Langflow MCP tool 다시 동기화",
    )
    async def sync_langflow_source(source_id: str) -> LangflowToolSyncResponse:
        try:
            current = await run_in_threadpool(langflow_store.get_source, source_id)
            request = LangflowToolSourceRequest(
                source_id=current.source_id,
                display_name=current.display_name,
                mcp_url=current.mcp_url,
                timeout_ms=current.timeout_ms,
                enabled=current.enabled,
            )
            contracts, elapsed_ms = await run_in_threadpool(langflow_gateway.list_tools, request)
            source = await run_in_threadpool(langflow_store.save_sync, request, contracts, elapsed_ms)
        except LangflowToolError as exc:
            await run_in_threadpool(langflow_store.mark_sync_error, source_id, exc.message)
            raise_langflow_error(exc)
            raise AssertionError("unreachable")
        return LangflowToolSyncResponse(source=source, tool_count=len(source.tools), elapsed_ms=elapsed_ms)

    @router.delete(
        "/api/playground/langflow-sources/{source_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        operation_id="delete_playground_langflow_source",
        summary="Langflow MCP 연결 삭제",
    )
    async def delete_langflow_source(source_id: str) -> Response:
        try:
            await run_in_threadpool(langflow_store.delete, source_id)
        except LangflowToolError as exc:
            raise_langflow_error(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/api/playground/langflow-tools/{tool_id}/test",
        response_model=ToolCallTrace,
        operation_id="test_playground_langflow_tool",
        summary="Langflow tool을 LLM 없이 직접 테스트",
    )
    async def test_langflow_tool(tool_id: str, request: LangflowToolTestRequest) -> ToolCallTrace:
        handler = registry_for(runtime_getter()).get(tool_id)
        if handler is None or handler.definition.origin != "langflow":
            raise HTTPException(
                status_code=404,
                detail={"code": "langflow_tool_not_found", "message": "등록된 Langflow tool을 찾지 못했습니다."},
            )
        started = time.perf_counter()
        result = await run_in_threadpool(handler.execute, request.arguments)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return ToolCallTrace(
            tool_id=handler.definition.id,
            tool_name=handler.definition.display_name,
            arguments_summary=result.arguments_summary,
            status=result.status,
            elapsed_ms=elapsed_ms,
            result_text=result.result_text,
            result_payload=result.result_payload,
            error_code=result.error_code,
        )

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
        handler = registry_for(runtime)["cytogenetics_karyotype_summary"]
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
        registry = registry_for(runtime)
        unknown = sorted(set(request.selected_tool_ids) - set(registry))
        if unknown:
            raise HTTPException(status_code=400, detail={"code": "unknown_tool", "tools": unknown})
        store = skill_store()
        known_skill_ids = {skill.id for skill in await run_in_threadpool(store.list)}
        unknown_skills = sorted(set(request.selected_skill_ids) - known_skill_ids)
        if unknown_skills:
            raise HTTPException(status_code=400, detail={"code": "unknown_skill", "skills": unknown_skills})
        unknown_attachments: list[str] = []
        for attachment_id in request.attachment_ids:
            try:
                await run_in_threadpool(attachment_store.get, attachment_id)
            except AttachmentStoreError:
                unknown_attachments.append(attachment_id)
        if unknown_attachments:
            raise HTTPException(
                status_code=400,
                detail={"code": "unknown_attachment", "attachments": unknown_attachments},
            )
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
