"""LLMOps 전용 Playground API의 실패·상태 계약을 검증한다."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.models import StoreConnectionState
from smb_finder.playground.document_api import DocumentRuntime, create_document_router
from smb_finder.playground.upload_service import UploadError


def _app(runtime: DocumentRuntime) -> FastAPI:
    app = FastAPI()
    app.include_router(create_document_router(lambda: runtime))
    return app


def _request(app: FastAPI, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json)

    return asyncio.run(send())


def _settings() -> Settings:
    return Settings(_env_file=None, llmops_chat_model="local-model", ollama_base_url="http://127.0.0.1:11434")


def test_file_search_has_no_local_fallback_when_llmops_is_not_configured():
    runtime = DocumentRuntime(settings=_settings(), file_searcher=None)

    response = _request(
        _app(runtime),
        "POST",
        "/api/playground/files/search",
        json={"query": "synthetic handbook", "limit": 10},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "llmops_search_not_configured"


class FailingFileSearcher:
    def search(self, _request):  # noqa: ANN001
        raise LlmopsSearchError("llmops_search_unavailable", "Synthetic database unavailable.", 12.5)


def test_file_search_database_failure_is_503():
    runtime = DocumentRuntime(settings=_settings(), file_searcher=FailingFileSearcher())

    response = _request(
        _app(runtime),
        "POST",
        "/api/playground/files/search",
        json={"query": "synthetic handbook", "limit": 10},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "llmops_search_unavailable",
        "message": "Synthetic database unavailable.",
        "elapsed_ms": 12.5,
    }


class StaleFileSearcher:
    def __init__(self) -> None:
        self.deadline = None

    def validate_active_selections(self, _selections, *, deadline=None):  # noqa: ANN001, ANN202
        self.deadline = deadline
        return set()


def test_chat_rejects_stale_selected_revision_before_retrieval():
    searcher = StaleFileSearcher()
    runtime = DocumentRuntime(settings=_settings(), file_searcher=searcher)

    response = _request(
        _app(runtime),
        "POST",
        "/api/playground/chat",
        json={
            "message": "Summarize this selected synthetic document.",
            "mode": "document_qa",
            "provider": "local",
            "selected_files": [
                {
                    "source": "llmops",
                    "doc_id": "11111111-1111-1111-1111-111111111111",
                    "revision_id": "22222222-2222-2222-2222-222222222222",
                    "file_name": "synthetic_handbook.md",
                    "title": "Synthetic handbook",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert searcher.deadline is not None
    assert response.json()["detail"]["code"] == "selected_file_stale"


class StaleUploadManager:
    def __init__(self) -> None:
        self.deadline = None

    def validate_selections(self, _selections, *, deadline=None):  # noqa: ANN001
        self.deadline = deadline
        return False


def test_chat_rejects_unknown_uploaded_file_reference():
    upload_manager = StaleUploadManager()
    runtime = DocumentRuntime(settings=_settings(), upload_manager=upload_manager)

    response = _request(
        _app(runtime),
        "POST",
        "/api/playground/chat",
        json={
            "message": "Summarize this uploaded synthetic document.",
            "mode": "document_qa",
            "selected_files": [
                {
                    "source": "upload",
                    "doc_id": "33333333-3333-3333-3333-333333333333",
                    "revision_id": "44444444-4444-4444-4444-444444444444",
                    "file_name": "synthetic-note.md",
                    "title": "Synthetic note",
                }
            ],
        },
    )

    assert response.status_code == 409
    assert upload_manager.deadline is not None
    assert response.json()["detail"]["code"] == "uploaded_file_stale"


class ExpiredUploadManager:
    def validate_selections(self, _selections, *, deadline=None):  # noqa: ANN001, ARG002
        raise UploadError(
            "uploaded_retrieval_budget_exhausted",
            "첨부 문서 검색 시간 예산이 소진되었습니다.",
            504,
        )


def test_chat_maps_expired_upload_selection_validation_to_safe_504():
    runtime = DocumentRuntime(settings=_settings(), upload_manager=ExpiredUploadManager())

    response = _request(
        _app(runtime),
        "POST",
        "/api/playground/chat",
        json={
            "message": "Summarize this uploaded synthetic document.",
            "mode": "document_qa",
            "selected_files": [
                {
                    "source": "upload",
                    "doc_id": "33333333-3333-3333-3333-333333333333",
                    "revision_id": "44444444-4444-4444-4444-444444444444",
                    "file_name": "synthetic-note.md",
                    "title": "Synthetic note",
                }
            ],
        },
    )

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "uploaded_retrieval_budget_exhausted"


class StatusAdapter:
    def __init__(self, state: StoreConnectionState) -> None:
        self.state = state

    def status(self) -> StoreConnectionState:
        return self.state


def test_store_status_reports_postgresql_minio_and_degraded_neo4j_without_addresses():
    runtime = DocumentRuntime(
        settings=_settings(),
        file_searcher=StatusAdapter(
            StoreConnectionState(
                configured=True,
                connected=True,
                message="connected",
                metadata={"schema": "llmops", "read_only": True},
            )
        ),
        artifact_reader=StatusAdapter(
            StoreConnectionState(
                configured=True,
                connected=True,
                message="connected",
                metadata={"bucket_configured": True, "read_operations": ["stat", "get"]},
            )
        ),
        graph_reader=StatusAdapter(
            StoreConnectionState(
                configured=True,
                connected=True,
                degraded=True,
                message="connected with home database fallback",
                metadata={"mode": "graph_space", "warnings": ["neo4j_home_database_fallback"]},
            )
        ),
    )

    response = _request(_app(runtime), "GET", "/api/playground/stores/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall"] == "degraded"
    assert payload["postgresql"]["metadata"]["read_only"] is True
    assert payload["minio"]["metadata"]["read_operations"] == ["stat", "get"]
    assert payload["neo4j"]["metadata"]["warnings"] == ["neo4j_home_database_fallback"]
    serialized = response.text.lower()
    assert "password" not in serialized
    assert "object_uri" not in serialized
    assert "bolt://" not in serialized
    assert "s3://" not in serialized
