"""LLMOps 문서 검색·대화 서비스의 FastAPI 진입점."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import AuthService, AuthSettings, PostgresAuthStore, SeeLisClient, create_auth_router
from .auth.security import hash_password
from .auth.store import AuthStoreError
from .config import load_settings
from .llmops_artifacts import LlmopsArtifactReader
from .llmops_graph import LlmopsGraphReader
from .llmops_multistore_search import LlmopsMultiStoreFileSearcher
from .llmops_retrieval import LlmopsScopedRetriever
from .llmops_search import LlmopsFileSearcher
from .models import ApiErrorResponse
from .playground.document_api import DocumentRuntime, create_document_router
from .playground.upload_api import create_upload_router

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
_logger = logging.getLogger(__name__)
_settings = load_settings()
_auth_settings = AuthSettings()
_auth_store = PostgresAuthStore(_auth_settings)
_seelis_client = SeeLisClient(_auth_settings)
_auth_service = AuthService(
    _auth_store,
    session_ttl_seconds=_auth_settings.auth_session_ttl_seconds,
    seelis_authenticator=_seelis_client,
)
_state: dict[str, object] = {}


def _runtime() -> DocumentRuntime:
    return DocumentRuntime(
        settings=_settings,
        file_searcher=_state.get("file_searcher"),
        scoped_retriever=_state.get("scoped_retriever"),
        artifact_reader=_state.get("artifact_reader"),
        graph_reader=_state.get("graph_reader"),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """구성된 읽기 전용 adapter만 시작하고 종료 시 연결을 정리한다."""

    async with AsyncExitStack() as stack:
        stack.callback(_state.clear)
        stack.callback(_seelis_client.close)
        stack.callback(_auth_store.close)
        if _auth_settings.configured:
            initial_password_hash = None
            if _auth_settings.auth_initial_admin_password:
                initial_password_hash = await asyncio.to_thread(
                    hash_password,
                    _auth_settings.auth_initial_admin_password,
                )
            try:
                await asyncio.to_thread(
                    _auth_store.initialize,
                    initial_password_hash=initial_password_hash,
                )
            except AuthStoreError:
                # 인증 저장소 장애는 신규 인증 화면만 degraded 처리하고 기존 Playground 기동은 유지한다.
                pass
        graph_reader = None
        if _settings.llmops_neo4j_configured:
            graph_reader = LlmopsGraphReader(_settings)
            stack.callback(graph_reader.close)
            _state["graph_reader"] = graph_reader

        if _settings.llmops_db_configured:
            postgres_searcher = LlmopsFileSearcher(_settings)
            stack.callback(postgres_searcher.close)

            if _settings.ollama_base_url.strip() and _settings.embedding_model.strip():
                scoped_retriever = LlmopsScopedRetriever(_settings)
                stack.callback(scoped_retriever.close)
                _state["scoped_retriever"] = scoped_retriever

            if _settings.llmops_minio_configured:
                artifact_reader = LlmopsArtifactReader(_settings)
                stack.callback(artifact_reader.close)
                _state["artifact_reader"] = artifact_reader
            else:
                artifact_reader = None

            file_searcher = LlmopsMultiStoreFileSearcher(
                _settings,
                postgres_searcher=postgres_searcher,
                artifact_reader=artifact_reader,
                graph_reader=graph_reader,
            )
            stack.callback(file_searcher.close)
            _state["file_searcher"] = file_searcher

        _logger.info(
            "서비스 준비 완료: postgresql=%s retrieval=%s minio=%s neo4j=%s",
            "file_searcher" in _state,
            "scoped_retriever" in _state,
            "artifact_reader" in _state,
            "graph_reader" in _state,
        )
        yield


app = FastAPI(
    title="공유 문서 검색 챗봇",
    version="0.2.0",
    description="LLMOps PostgreSQL·MinIO·Neo4지를 읽기 전용으로 조회하는 문서 챗봇입니다.",
    lifespan=lifespan,
    responses={500: {"model": ApiErrorResponse, "description": "공통 서버 오류"}},
)

_WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount("/playground/assets", StaticFiles(directory=str(_WEB_DIR / "assets")), name="playground-assets")
app.include_router(create_document_router(_runtime))
app.include_router(create_upload_router(_settings))
app.include_router(create_auth_router(_auth_settings, _auth_service))


@app.get("/playground", include_in_schema=False)
@app.get("/playground/", include_in_schema=False)
async def playground_page() -> FileResponse:
    """Vue production bundle을 반환한다."""

    return FileResponse(_WEB_DIR / "index.html")


@app.get("/login", include_in_schema=False)
@app.get("/register", include_in_schema=False)
@app.get("/user", include_in_schema=False)
async def auth_spa_page() -> FileResponse:
    """Vue SPA의 로그인·회원가입·사용자 관리 route를 반환한다."""

    return FileResponse(_WEB_DIR / "index.html")


@app.get("/health", operation_id="health_check", summary="서비스 상태")
async def health() -> dict[str, object]:
    """민감한 주소 없이 adapter 준비 상태를 반환한다."""

    ready = "file_searcher" in _state and "scoped_retriever" in _state
    return {
        "status": "ok" if ready else "degraded",
        "ready": ready,
        "adapters": {
            "postgresql": "file_searcher" in _state,
            "retrieval": "scoped_retriever" in _state,
            "minio": "artifact_reader" in _state,
            "neo4j": "graph_reader" in _state,
        },
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """내부 주소와 예외 문자열을 숨긴다."""

    _logger.error("처리되지 않은 서버 오류: %s", type(exc).__name__)
    payload = ApiErrorResponse(
        code="internal_error",
        message="요청 처리 중 오류가 발생했습니다.",
        retryable=True,
    )
    return JSONResponse(status_code=500, content=payload.model_dump())
