"""Playground 환경 설정과 파일 첨부 FastAPI router."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from smb_finder.config import Settings

from .upload_models import FileUploadResponse, PlaygroundSettingsResponse, UploadSettingsPatch
from .upload_service import UploadError, UploadManager


def create_upload_router(settings: Settings, manager: UploadManager | None = None) -> APIRouter:
    """비밀을 받거나 반환하지 않는 설정·첨부 API를 생성한다."""

    upload_manager = manager or UploadManager(settings)

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            upload_manager.close()

    router = APIRouter(tags=["playground-settings"], lifespan=lifespan)

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
