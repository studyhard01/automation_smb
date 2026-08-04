"""Playground FastAPI router."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging
import uuid
from typing import Literal
from uuid import UUID
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from smb_finder.llmops_artifacts import LlmopsArtifactError
from smb_finder.llmops_graph import LlmopsGraphError
from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.models import (
    ArtifactViewResponse,
    DocumentGraphResponse,
    DocumentSearchRequest,
    DocumentSearchResponse,
    LlmopsStoresStatusResponse,
    StoreConnectionState,
)
from smb_finder.qc_audit import AttachmentStore, AttachmentStoreError, QcAuditService
from smb_finder.qc_audit.models import AttachmentMetadata
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

    def raise_attachment_error(exc: AttachmentStoreError) -> None:
        status_code = {
            "attachment_not_found": 404,
            "attachment_too_large": 413,
        }.get(exc.code, 400)
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from exc

    def registry_for(runtime: PlaygroundRuntime) -> dict:
        return build_tool_registry(
            runtime,
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
        "/api/playground/files/search",
        response_model=DocumentSearchResponse,
        operation_id="search_playground_files",
        summary="왼쪽 패널 자연어 파일 검색",
    )
    async def search_playground_files(request: DocumentSearchRequest) -> DocumentSearchResponse:
        """LLMOps 활성 문서 DB만 검색하며 미설정·장애는 명시적 503으로 반환한다."""

        runtime = runtime_getter()
        if runtime.llmops_file_searcher is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "llmops_search_not_configured",
                    "message": "구축 문서 DB 검색이 구성되지 않았습니다.",
                },
            )
        try:
            return await run_in_threadpool(runtime.llmops_file_searcher.search, request)
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc

    @router.get(
        "/api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}",
        response_model=ArtifactViewResponse,
        operation_id="read_playground_file_artifact",
        summary="선택 문서 Preview/Canonical 보기",
    )
    async def read_playground_file_artifact(
        doc_id: UUID,
        revision_id: UUID,
        artifact_type: Literal["preview", "canonical"],
    ) -> ArtifactViewResponse:
        runtime = runtime_getter()
        if runtime.llmops_artifact_reader is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "artifact_reader_not_configured", "message": "문서 보기 저장소가 구성되지 않았습니다."},
            )
        try:
            return await run_in_threadpool(
                runtime.llmops_artifact_reader.read,
                str(doc_id),
                str(revision_id),
                artifact_type,
            )
        except LlmopsArtifactError as exc:
            status_code = 404 if exc.code == "artifact_not_found" else 413 if exc.code == "artifact_too_large" else 503
            raise HTTPException(
                status_code=status_code,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc

    @router.get(
        "/api/playground/files/{doc_id}/graph",
        response_model=DocumentGraphResponse,
        operation_id="read_playground_file_graph",
        summary="선택 문서 Version Graph",
    )
    async def read_playground_file_graph(doc_id: UUID) -> DocumentGraphResponse:
        runtime = runtime_getter()
        if runtime.llmops_graph_reader is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "graph_reader_not_configured", "message": "문서 Graph 저장소가 구성되지 않았습니다."},
            )
        try:
            return await run_in_threadpool(runtime.llmops_graph_reader.read_document_graph, str(doc_id))
        except LlmopsGraphError as exc:
            status_code = 404 if exc.code == "graph_document_not_found" else 503
            raise HTTPException(
                status_code=status_code,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc

    @router.get(
        "/api/playground/stores/status",
        response_model=LlmopsStoresStatusResponse,
        operation_id="get_playground_stores_status",
        summary="LLMOps 읽기 저장소 상태",
    )
    async def get_playground_stores_status() -> LlmopsStoresStatusResponse:
        runtime = runtime_getter()

        async def state_or_unconfigured(adapter: object | None, name: str) -> StoreConnectionState:
            if adapter is None:
                return StoreConnectionState(
                    configured=False,
                    connected=False,
                    degraded=True,
                    message=f"{name}이 구성되지 않았습니다.",
                )
            return await run_in_threadpool(adapter.status)

        postgresql, minio, neo4j = await asyncio.gather(
            state_or_unconfigured(runtime.llmops_file_searcher, "PostgreSQL"),
            state_or_unconfigured(runtime.llmops_artifact_reader, "MinIO"),
            state_or_unconfigured(runtime.llmops_graph_reader, "Neo4j"),
        )
        if not postgresql.connected:
            overall = "unavailable"
        elif minio.degraded or neo4j.degraded:
            overall = "degraded"
        else:
            overall = "ok"
        return LlmopsStoresStatusResponse(
            overall=overall,
            postgresql=postgresql,
            minio=minio,
            neo4j=neo4j,
        )

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
        llmops_selections = [
            (str(item.doc_id), str(item.revision_id))
            for item in request.selected_files
            if item.source == "llmops" and item.doc_id is not None and item.revision_id is not None
        ]
        malformed_llmops_selection = any(
            item.source == "llmops" and (item.doc_id is None or item.revision_id is None)
            for item in request.selected_files
        )
        unsupported_selection = any(item.source != "llmops" for item in request.selected_files)
        if unsupported_selection:
            raise HTTPException(
                status_code=400,
                detail={"code": "unsupported_selected_file_source", "message": "구축 문서 DB 검색 결과만 선택할 수 있습니다."},
            )
        if malformed_llmops_selection:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_selected_file", "message": "선택 파일의 문서 ID가 올바르지 않습니다."},
            )
        if llmops_selections:
            if runtime.llmops_file_searcher is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "selected_file_validation_unavailable", "message": "선택 파일을 확인할 수 없습니다."},
                )
            try:
                valid_selections = await run_in_threadpool(
                    runtime.llmops_file_searcher.validate_active_selections,
                    llmops_selections,
                )
            except LlmopsSearchError as exc:
                raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
            if set(llmops_selections) != valid_selections:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "selected_file_stale", "message": "선택한 파일의 활성 버전이 변경되었습니다. 다시 검색하세요."},
                )
        request_id = str(uuid.uuid4())
        try:
            response = await run_in_threadpool(
                shared_agent.run,
                request,
                registry,
                x_playground_openai_key,
                request_id,
                runtime.llmops_scoped_retriever,
            )
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc
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
