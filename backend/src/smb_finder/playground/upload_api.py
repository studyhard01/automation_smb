"""Playground 환경 설정과 파일 첨부 FastAPI router."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from smb_finder.config import Settings
from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.models import DocumentCitation

from .document_models import SelectedFileContext
from .proposal_draft import (
    ProposalDraftClarificationRequest,
    ProposalDraftClarificationResponse,
    ProposalDraftError,
    ProposalDraftGenerateRequest,
    ProposalDraftGenerateResponse,
    ProposalDraftRevisionRequest,
    ProposalDraftRevisionResponse,
    ProposalDraftService,
)
from .upload_models import (
    FileUploadResponse,
    PlaygroundSettingsResponse,
    ProposalDraftSettingsPatch,
    UploadSettingsPatch,
)
from .upload_service import UploadError, UploadManager

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def create_upload_router(
    settings: Settings,
    manager: UploadManager | None = None,
    proposal_draft_service: ProposalDraftService | None = None,
    *,
    runtime_getter: Callable[[], object] | None = None,
) -> APIRouter:
    """비밀을 받거나 반환하지 않는 설정·첨부 API를 생성한다."""

    upload_manager = manager or UploadManager(settings)
    draft_service = proposal_draft_service or ProposalDraftService(settings, upload_manager)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            draft_service.close()
            upload_manager.close()

    router = APIRouter(tags=["playground-settings"], lifespan=lifespan)

    def proposal_error_detail(exc: ProposalDraftError) -> dict[str, object]:
        detail: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.context_usage is not None:
            detail["context_usage"] = exc.context_usage.model_dump(mode="json")
        if exc.evidence_filter is not None:
            detail["evidence_filter"] = exc.evidence_filter.model_dump(mode="json")
        return detail

    async def collect_proposal_evidence(
        selected_files: list[SelectedFileContext],
        query: str,
    ) -> tuple[list[DocumentCitation], float, float]:
        """선택 snapshot을 재검증하고 같은 범위 안에서만 근거를 다시 검색한다."""

        if runtime_getter is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "proposal_context_unavailable", "message": "선택 문서 검색기가 구성되지 않았습니다."},
            )
        runtime = runtime_getter()
        database_selections = [
            (str(item.doc_id), str(item.revision_id)) for item in selected_files if item.source == "llmops"
        ]
        upload_selections = [
            (item.doc_id, item.revision_id) for item in selected_files if item.source == "upload"
        ]

        validation_started = time.perf_counter()
        if database_selections:
            file_searcher = getattr(runtime, "file_searcher", None)
            if file_searcher is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "selected_file_validation_unavailable", "message": "선택 문서를 확인할 수 없습니다."},
                )
            valid = await run_in_threadpool(file_searcher.validate_active_selections, database_selections)
            if set(database_selections) != valid:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "selected_file_stale",
                        "message": "선택한 파일의 활성 버전이 변경되었습니다. 다시 검색해 주세요.",
                    },
                )
        context_upload_manager = getattr(runtime, "upload_manager", None)
        if upload_selections:
            if context_upload_manager is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "upload_context_unavailable", "message": "첨부 파일을 확인할 수 없습니다."},
                )
            valid_uploads = await run_in_threadpool(context_upload_manager.validate_selections, upload_selections)
            if not valid_uploads:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "uploaded_file_stale",
                        "message": "첨부 파일 참조가 만료됐습니다. 다시 첨부해 주세요.",
                    },
                )
        validation_ms = round((time.perf_counter() - validation_started) * 1000, 1)

        retrieval_started = time.perf_counter()
        evidence: list[DocumentCitation] = []
        if database_selections:
            scoped_retriever = getattr(runtime, "scoped_retriever", None)
            if scoped_retriever is None:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "proposal_retrieval_unavailable", "message": "선택 문서 근거를 검색할 수 없습니다."},
                )
            database_result = await run_in_threadpool(scoped_retriever.retrieve, query, database_selections)
            evidence.extend(database_result.citations)
        if upload_selections:
            upload_result = await run_in_threadpool(context_upload_manager.retrieve, query, upload_selections)
            evidence.extend(upload_result.citations)
        retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 1)
        if not evidence:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "proposal_evidence_unavailable",
                    "message": "선택한 문서에서 기안 작성에 사용할 근거를 찾지 못했습니다.",
                },
            )
        return evidence, validation_ms, retrieval_ms

    @router.get(
        "/api/playground/settings",
        response_model=PlaygroundSettingsResponse,
        operation_id="get_playground_settings",
        summary="Playground 공개 설정 조회",
    )
    async def get_settings() -> PlaygroundSettingsResponse:
        return upload_manager.get_settings()

    @router.patch(
        "/api/playground/settings/upload",
        response_model=PlaygroundSettingsResponse,
        operation_id="update_playground_upload_settings",
        summary="파일 첨부 상대 폴더 설정",
    )
    async def update_upload_settings(request: UploadSettingsPatch) -> PlaygroundSettingsResponse:
        try:
            return upload_manager.update_relative_directory(request.relative_directory)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_relative_directory", "message": str(exc)},
            ) from exc

    @router.patch(
        "/api/playground/settings/proposal-draft",
        response_model=PlaygroundSettingsResponse,
        operation_id="update_playground_proposal_draft_settings",
        summary="기안 초안 저장 상대 폴더 설정",
    )
    async def update_proposal_draft_settings(request: ProposalDraftSettingsPatch) -> PlaygroundSettingsResponse:
        try:
            return upload_manager.update_proposal_draft_relative_directory(request.relative_directory)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_relative_directory", "message": str(exc)},
            ) from exc

    @router.get(
        "/api/playground/drafts/proposal",
        response_class=Response,
        operation_id="download_playground_proposal_draft",
        summary="빈 기안 초안 XLSX 다운로드",
        responses={
            200: {
                "content": {_XLSX_MEDIA_TYPE: {}},
                "description": "원본 서식을 보존한 기안 초안 XLSX",
            }
        },
    )
    async def download_proposal_draft() -> Response:
        try:
            download = await run_in_threadpool(draft_service.get_template_download)
        except ProposalDraftError as exc:
            detail: dict[str, object] = {"code": exc.code, "message": exc.message}
            if exc.context_usage is not None:
                detail["context_usage"] = exc.context_usage.model_dump(mode="json")
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc
        return _xlsx_response(download.file_name, download.content)

    @router.post(
        "/api/playground/drafts/proposal",
        response_model=ProposalDraftGenerateResponse,
        status_code=201,
        operation_id="generate_playground_proposal_draft",
        summary="선택 문서 기반 구조화 기안 생성 또는 필수 정보 확인",
    )
    async def generate_proposal_draft(request: ProposalDraftGenerateRequest) -> ProposalDraftGenerateResponse:
        try:
            evidence, validation_ms, retrieval_ms = await collect_proposal_evidence(
                request.selected_files,
                request.instruction,
            )
            return await run_in_threadpool(
                draft_service.generate,
                request.instruction,
                evidence,
                selected_files=request.selected_files,
                proposal_type=request.proposal_type,
                validation_ms=validation_ms,
                retrieval_ms=retrieval_ms,
            )
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except ProposalDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=proposal_error_detail(exc)) from exc

    @router.post(
        "/api/playground/drafts/proposal/{draft_id}/clarifications",
        response_model=ProposalDraftClarificationResponse,
        status_code=201,
        operation_id="clarify_playground_proposal_draft",
        summary="확인 답변 반영 후 기안 완성·저장",
    )
    async def clarify_proposal_draft(
        draft_id: UUID,
        request: ProposalDraftClarificationRequest,
    ) -> ProposalDraftClarificationResponse:
        try:
            base = await run_in_threadpool(
                draft_service.get_clarification_record,
                draft_id,
                request.selected_files,
            )
            question_map = {question.question_id: question.prompt for question in base.completion.questions}
            query = f"{base.instruction}\n" + "\n".join(
                f"{question_map.get(answer.question_id, answer.question_id)}: {answer.answer}" for answer in request.answers
            )
            evidence, validation_ms, retrieval_ms = await collect_proposal_evidence(request.selected_files, query)
            return await run_in_threadpool(
                draft_service.clarify,
                draft_id,
                request.answers,
                request.selected_files,
                evidence,
                validation_ms=validation_ms,
                retrieval_ms=retrieval_ms,
            )
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except ProposalDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=proposal_error_detail(exc)) from exc

    @router.post(
        "/api/playground/drafts/proposal/{draft_id}/revisions",
        response_model=ProposalDraftRevisionResponse,
        status_code=201,
        operation_id="revise_playground_proposal_draft",
        summary="피드백 기반 기안 수정본 생성·저장",
    )
    async def revise_proposal_draft(
        draft_id: UUID,
        request: ProposalDraftRevisionRequest,
    ) -> ProposalDraftRevisionResponse:
        try:
            base = await run_in_threadpool(
                draft_service.get_revision_record,
                draft_id,
                request.selected_files,
            )
            query = f"{base.instruction}\n수정 피드백: {request.feedback}"
            evidence, validation_ms, retrieval_ms = await collect_proposal_evidence(request.selected_files, query)
            return await run_in_threadpool(
                draft_service.revise,
                draft_id,
                request.feedback,
                request.selected_files,
                evidence,
                validation_ms=validation_ms,
                retrieval_ms=retrieval_ms,
            )
        except LlmopsSearchError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": exc.code, "message": exc.message, "elapsed_ms": exc.elapsed_ms},
            ) from exc
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        except ProposalDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail=proposal_error_detail(exc)) from exc

    @router.get(
        "/api/playground/drafts/proposal/{draft_id}",
        response_class=Response,
        operation_id="download_generated_playground_proposal_draft",
        summary="생성한 기안 초안 XLSX 다운로드",
        responses={200: {"content": {_XLSX_MEDIA_TYPE: {}}, "description": "LLM 제목을 넣은 기안 초안 XLSX"}},
    )
    async def download_generated_proposal_draft(draft_id: UUID) -> Response:
        try:
            download = await run_in_threadpool(draft_service.get_download, draft_id)
        except ProposalDraftError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        return _xlsx_response(download.file_name, download.content)

    def _xlsx_response(file_name: str, content: bytes) -> Response:
        """내부 경로 없이 안전한 XLSX 다운로드 응답을 만든다."""

        encoded_name = quote(file_name, safe="")
        return Response(
            content=content,
            media_type=_XLSX_MEDIA_TYPE,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="proposal_draft.xlsx"; filename*=UTF-8\'\'{encoded_name}'
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(
        "/api/playground/files/upload",
        response_model=FileUploadResponse,
        status_code=201,
        operation_id="upload_playground_file",
        summary="공유폴더에 파일 첨부",
    )
    async def upload_file(file: UploadFile = File(...)) -> FileUploadResponse:
        try:
            return await run_in_threadpool(upload_manager.upload, file.file, file.filename or "")
        except UploadError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
        finally:
            await file.close()

    return router
