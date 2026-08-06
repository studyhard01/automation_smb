"""DB 기반 문서 검색·선택·대화에 필요한 최소 FastAPI router."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool

from smb_finder.config import Settings
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

from .document_chat import DocumentChatService
from .document_models import ChatRequest, ChatResponse
from .upload_service import UploadError

_logger = logging.getLogger(__name__)


@dataclass
class DocumentRuntime:
    """FastAPI lifespan에서 준비한 읽기 전용 adapter 모음."""

    settings: Settings
    file_searcher: Any | None = None
    scoped_retriever: Any | None = None
    artifact_reader: Any | None = None
    graph_reader: Any | None = None
    upload_manager: Any | None = None


def create_document_router(runtime_getter: Callable[[], DocumentRuntime]) -> APIRouter:
    """현재 제품 목표에 포함된 문서 API만 생성한다."""

    chat_service = DocumentChatService(runtime_getter().settings)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            chat_service.close()

    router = APIRouter(tags=["document-playground"], lifespan=lifespan)

    @router.post(
        "/api/playground/files/search",
        response_model=DocumentSearchResponse,
        operation_id="search_playground_files",
        summary="멀티스토어 자연어 파일 검색",
    )
    async def search_files(request: DocumentSearchRequest) -> DocumentSearchResponse:
        runtime = runtime_getter()
        if runtime.file_searcher is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "llmops_search_not_configured", "message": "문서 DB 검색이 구성되지 않았습니다."},
            )
        try:
            return await run_in_threadpool(runtime.file_searcher.search, request)
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc

    @router.get(
        "/api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}",
        response_model=ArtifactViewResponse,
        operation_id="read_playground_file_artifact",
        summary="문서 Preview/Canonical 보기",
    )
    async def read_artifact(
        doc_id: UUID,
        revision_id: UUID,
        artifact_type: Literal["preview", "canonical"],
    ) -> ArtifactViewResponse:
        runtime = runtime_getter()
        if runtime.artifact_reader is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "artifact_reader_not_configured", "message": "문서 보기 저장소가 구성되지 않았습니다."},
            )
        try:
            return await run_in_threadpool(runtime.artifact_reader.read, str(doc_id), str(revision_id), artifact_type)
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
        summary="문서 버전 관계 보기",
    )
    async def read_graph(doc_id: UUID) -> DocumentGraphResponse:
        runtime = runtime_getter()
        if runtime.graph_reader is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "graph_reader_not_configured", "message": "문서 버전 저장소가 구성되지 않았습니다."},
            )
        try:
            return await run_in_threadpool(runtime.graph_reader.read_document_graph, str(doc_id))
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
        summary="문서 저장소 연결 상태",
    )
    async def stores_status() -> LlmopsStoresStatusResponse:
        runtime = runtime_getter()

        async def state_or_unconfigured(adapter: Any | None, name: str) -> StoreConnectionState:
            if adapter is None:
                return StoreConnectionState(
                    configured=False,
                    connected=False,
                    degraded=True,
                    message=f"{name}이 구성되지 않았습니다.",
                )
            return await run_in_threadpool(adapter.status)

        postgresql, minio, neo4j = await asyncio.gather(
            state_or_unconfigured(runtime.file_searcher, "PostgreSQL"),
            state_or_unconfigured(runtime.artifact_reader, "MinIO"),
            state_or_unconfigured(runtime.graph_reader, "Neo4j"),
        )
        overall: Literal["ok", "degraded", "unavailable"]
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
        "/api/playground/chat",
        response_model=ChatResponse,
        operation_id="run_document_chat",
        summary="선택 문서 근거 대화",
    )
    async def run_chat(request: ChatRequest) -> ChatResponse:
        runtime = runtime_getter()
        selections = [
            (str(item.doc_id), str(item.revision_id))
            for item in request.selected_files
            if item.source == "llmops"
        ]
        if selections:
            if runtime.file_searcher is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "selected_file_validation_unavailable", "message": "선택 문서를 확인할 수 없습니다."},
                )
            try:
                valid = await run_in_threadpool(runtime.file_searcher.validate_active_selections, selections)
            except LlmopsSearchError as exc:
                raise HTTPException(status_code=503, detail={"code": exc.code, "message": exc.message}) from exc
            if set(selections) != valid:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "selected_file_stale", "message": "선택한 파일의 활성 버전이 변경되었습니다. 다시 검색해 주세요."},
                )

        upload_selections = [
            (item.doc_id, item.revision_id)
            for item in request.selected_files
            if item.source == "upload"
        ]
        if upload_selections:
            if runtime.upload_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "upload_context_unavailable", "message": "첨부 파일을 확인할 수 없습니다."},
                )
            valid_uploads = await run_in_threadpool(runtime.upload_manager.validate_selections, upload_selections)
            if not valid_uploads:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "uploaded_file_stale", "message": "첨부 파일 참조가 만료됐습니다. 다시 첨부해 주세요."},
                )

        request_id = str(uuid.uuid4())
        try:
            response = await run_in_threadpool(
                chat_service.run,
                request,
                runtime.scoped_retriever,
                request_id=request_id,
                upload_manager=runtime.upload_manager,
            )
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc
        except UploadError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        log = _logger.warning if response.over_budget else _logger.info
        log(
            "Document chat completed: request_id=%s citations=%d elapsed_ms=%.1f over_budget=%s error_code=%s",
            response.request_id,
            len(response.citations),
            response.elapsed_ms,
            response.over_budget,
            response.error_code or "none",
        )
        return response

    return router
