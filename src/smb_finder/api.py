"""FastAPI 앱 — 공유 폴더 찾기 도구(L4)의 진입점.

OpenAPI로 노출되어 L2(자체 디스패처)·L3(Dify Custom Tool)에 등록된다.
operation_id/summary/description이 그대로 도구 메타데이터가 되므로 명확히 쓴다.
"""

from __future__ import annotations

import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .content_index import open_index
from .content_indexer import build_content_index
from .content_search import ContentSearcher
from .finder import Finder
from .indexer import build_index, load_or_build
from .jobs import ContentIndexJobStore
from .models import (
    ApiErrorResponse,
    ContentIndexJobCreateResponse,
    ContentIndexJobStatusResponse,
    ContentSearchRequest,
    ContentSearchResponse,
    FindRequest,
    FindResponse,
    HealthResponse,
    RefreshContentRequest,
    RefreshContentResponse,
    RefreshIndexResponse,
)
from .playground.api import create_playground_router
from .playground.tools import PlaygroundRuntime
from .rag_search import RagVectorSearcher

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

_settings = load_settings()
_state: dict = {}
_content_jobs = ContentIndexJobStore(_settings.content_index_job_retention)


def _playground_runtime() -> PlaygroundRuntime:
    """현재 FastAPI 상태를 검색 도구의 런타임 의존성으로 제공한다."""
    return PlaygroundRuntime(
        settings=_settings,
        finder=_state.get("finder"),
        content_searcher=_state.get("content_searcher"),
        rag_searcher=_state.get("rag_searcher"),
    )


_mcp_bundle = None
if _settings.mcp_enabled:
    from .mcp_server import McpExactRoute, create_mcp_bundle

    _mcp_bundle = create_mcp_bundle(_playground_runtime, _settings)


def _safe_config_path(value: str) -> str:
    """설정 경로를 응답에 싣기 전 축약한다.

    상대경로는 운영자가 알아볼 수 있게 보존하고, 절대경로는 로컬 사용자명/디렉터리 노출을
    피하기 위해 파일명만 남긴다.
    """
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        return path.name
    return value.replace("\\", "/")


def _require_admin_token(x_admin_token: str | None) -> None:
    """관리자 API 토큰을 확인한다.

    토큰이 설정되지 않은 환경에서는 새 /admin/* API를 닫아 둔다. 기존 호환 엔드포인트는
    그대로 동작하지만, 운영에서는 ADMIN_API_TOKEN을 설정한 뒤 admin job API로 전환한다.
    """
    if not _settings.admin_api_token:
        raise HTTPException(
            status_code=503,
            detail=ApiErrorResponse(
                code="admin_api_disabled",
                message="관리자 API 토큰이 설정되어 있지 않습니다.",
                retryable=False,
            ).model_dump(),
        )
    if x_admin_token != _settings.admin_api_token:
        raise HTTPException(
            status_code=403,
            detail=ApiErrorResponse(
                code="admin_forbidden",
                message="관리자 권한이 필요합니다.",
                retryable=False,
            ).model_dump(),
        )


def _require_allowed_smb_target(request: RefreshContentRequest) -> None:
    """관리자 인덱싱 target override가 서버 allowlist에 있는지 확인한다."""
    host = (request.host or "").strip().lower()
    share = (request.share_name or "").strip().lower()
    if host and host not in _settings.smb_allowed_host_set:
        raise HTTPException(
            status_code=403,
            detail=ApiErrorResponse(
                code="smb_host_not_allowed",
                message="허용되지 않은 SMB 호스트입니다.",
                retryable=False,
            ).model_dump(),
        )
    if share and share not in _settings.smb_allowed_share_set:
        raise HTTPException(
            status_code=403,
            detail=ApiErrorResponse(
                code="smb_share_not_allowed",
                message="허용되지 않은 SMB 공유명입니다.",
                retryable=False,
            ).model_dump(),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """시작 시 인덱스를 로드(캐시 우선)해 첫 요청부터 빠르게 응답한다.

    폴더 인덱스는 가벼워 시작 시 빌드하지만, 내용 인덱스(본문 추출)는 무거우므로
    기존 DB만 열고 비어 있으면 /refresh-content로 명시적으로 빌드한다(시작 지연 방지).
    """
    async with AsyncExitStack() as stack:
        stack.callback(_state.clear)
        index = await run_in_threadpool(load_or_build, _settings)
        _state["finder"] = Finder(index, _settings)
        _state["index"] = index

        content_index = await run_in_threadpool(open_index, _settings.content_index_db_path)
        stack.callback(content_index.close)
        _state["content_index"] = content_index
        _state["content_searcher"] = ContentSearcher(content_index, _settings)
        if _settings.rag_db_enabled:
            rag_searcher = RagVectorSearcher(_settings)
            stack.callback(rag_searcher.close)
            _state["rag_searcher"] = rag_searcher
        if _mcp_bundle is not None:
            await stack.enter_async_context(_mcp_bundle.server.session_manager.run())
        _logger.info(
            "서비스 준비 완료 (폴더 %d건, 내용 %d파일)", len(index), content_index.count()
        )
        yield


app = FastAPI(
    title="공유 폴더 찾기 (smb-finder)",
    version="0.1.0",
    description="자연어 명령으로 온프레미스 SMB 공유폴더를 즉시 찾는다.",
    lifespan=lifespan,
    responses={500: {"model": ApiErrorResponse, "description": "공통 서버 오류"}},
)

_WEB_DIR = Path(__file__).resolve().parent / "web"
app.mount(
    "/playground/assets",
    StaticFiles(directory=str(_WEB_DIR / "assets")),
    name="playground-assets",
)
if _mcp_bundle is not None:
    app.router.routes.append(McpExactRoute("/mcp", _mcp_bundle.app))


app.include_router(create_playground_router(_playground_runtime))


@app.get("/playground", include_in_schema=False)
async def playground_page() -> FileResponse:
    """자체 챗봇/tool Playground 화면을 반환한다."""
    return FileResponse(_WEB_DIR / "index.html")


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """예상 밖 오류도 민감한 내부 경로나 예외 원문을 응답에 노출하지 않는다."""
    _logger.error("처리되지 않은 서버 오류: %s", type(exc).__name__)
    payload = ApiErrorResponse(
        code="internal_error",
        message="요청 처리 중 오류가 발생했습니다.",
        retryable=True,
        elapsed_ms=0.0,
    )
    return JSONResponse(status_code=500, content=payload.model_dump())


@app.post("/find", response_model=FindResponse, operation_id="find_share_folder", summary="공유 폴더 찾기")
async def find_share_folder(request: FindRequest) -> FindResponse:
    """자연어 명령(예: 'OO검사 결과 폴더 찾아줘')으로 관련 공유폴더를 관련도순으로 반환한다."""
    finder: Finder = _state["finder"]
    # 인메모리 검색이라 빠르지만, 이벤트 루프 블로킹을 피해 threadpool에서 실행
    return await run_in_threadpool(finder.find, request)


@app.post(
    "/refresh",
    response_model=RefreshIndexResponse,
    operation_id="refresh_index",
    summary="폴더 인덱스 재빌드",
)
async def refresh_index(
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> RefreshIndexResponse:
    """SMB를 다시 순회해 인덱스를 갱신한다 (관리용 — 느림, 사용자 경로 아님)."""
    _require_admin_token(x_admin_token)
    started = time.perf_counter()
    index = await run_in_threadpool(build_index, _settings)
    _state["index"].replace(index.entries)  # 동일 인스턴스 갱신 (Finder가 참조 유지)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    budget_ms = _settings.smb_index_build_budget_sec * 1000 if _settings.smb_index_build_budget_sec else 0
    return RefreshIndexResponse(
        folders=len(index),
        indexed_folders=len(index),
        elapsed_ms=elapsed_ms,
        over_budget=bool(budget_ms and elapsed_ms > budget_ms),
        cache_path=_safe_config_path(_settings.smb_index_cache_path),
    )


@app.post(
    "/search-content",
    response_model=ContentSearchResponse,
    operation_id="search_file_content",
    summary="파일 내용 검색",
)
async def search_file_content(request: ContentSearchRequest) -> ContentSearchResponse:
    """파일 **본문**에 들어있는 키워드/내용으로 파일을 관련도순으로 찾는다.

    예: 'BRCA1 변이 보고서', '음성 판정 결과'. 파일명·경로로 찾는 폴더 검색(/find)과 별개다.
    """
    searcher: ContentSearcher = _state["content_searcher"]
    return await run_in_threadpool(searcher.search, request)


@app.post(
    "/refresh-content",
    response_model=RefreshContentResponse,
    operation_id="index_folder_content",
    summary="폴더 내용 DB화(인덱싱)",
)
async def refresh_content_index(
    request: RefreshContentRequest | None = None,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> RefreshContentResponse:
    """지정 폴더(path) 아래 파일 본문을 추출해 내용 인덱스에 적재한다 (느림 — 사용자가 명시 실행).

    path를 주면 그 폴더만 (재)색인하고 다른 폴더 인덱스는 보존한다. 비우면 공유 전체.
    이후 /search-content가 이 인덱스에서 즉시 검색한다.
    """
    return await _refresh_content_index_impl(request, x_admin_token=x_admin_token)


async def _refresh_content_index_impl(
    request: RefreshContentRequest | None,
    x_admin_token: str | None,
) -> RefreshContentResponse:
    """관리자 인증 후 내용 인덱스를 동기 갱신한다."""
    _require_admin_token(x_admin_token)
    req = request or RefreshContentRequest()
    _require_allowed_smb_target(req)
    started = time.perf_counter()
    try:
        stats = await run_in_threadpool(
            build_content_index, _settings, req.path, req.host, req.share_name
        )
    except RuntimeError as exc:
        if "already running" not in str(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail=ApiErrorResponse(
                code="content_index_busy",
                message="다른 내용 인덱싱 작업이 실행 중입니다. 잠시 뒤 다시 시도하세요.",
                retryable=True,
            ).model_dump(),
        ) from exc
    content_index = _state.get("content_index")
    indexed_files = content_index.count() if content_index else 0
    return RefreshContentResponse(
        **stats,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        indexed_files=indexed_files,
        db_path=_safe_config_path(_settings.content_index_db_path),
    )


@app.post(
    "/admin/content-index-jobs",
    response_model=ContentIndexJobCreateResponse,
    operation_id="create_content_index_job",
    summary="관리자용 내용 인덱싱 job 생성",
)
async def create_content_index_job(
    request: RefreshContentRequest,
    background_tasks: BackgroundTasks,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> ContentIndexJobCreateResponse:
    """내용 인덱싱을 백그라운드 job으로 실행한다.

    무거운 `/refresh-content` 직접 호출 대신 운영 UI/관리 자동화는 이 엔드포인트를 사용한다.
    새 API는 `ADMIN_API_TOKEN`이 설정된 환경에서만 열린다.
    """
    _require_admin_token(x_admin_token)
    _require_allowed_smb_target(request)
    job = _content_jobs.create(request)
    background_tasks.add_task(_content_jobs.run, job.job_id, _settings, build_content_index)
    _logger.info(
        "내용 인덱싱 job 생성: job_id=%s scope=%s target=%s",
        job.job_id,
        "subpath" if request.path else "full",
        "override" if request.host or request.share_name else "configured",
    )
    return ContentIndexJobCreateResponse(
        job_id=job.job_id,
        status=job.status,
        status_url=f"/admin/content-index-jobs/{job.job_id}",
    )


@app.get(
    "/admin/content-index-jobs/{job_id}",
    response_model=ContentIndexJobStatusResponse,
    operation_id="get_content_index_job",
    summary="관리자용 내용 인덱싱 job 상태 조회",
)
async def get_content_index_job(
    job_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> ContentIndexJobStatusResponse:
    """내용 인덱싱 job 상태를 조회한다."""
    _require_admin_token(x_admin_token)
    job = _content_jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=ApiErrorResponse(
                code="job_not_found",
                message="해당 job을 찾을 수 없습니다.",
                retryable=False,
            ).model_dump(),
        )
    return job


@app.get(
    "/admin/content-index-jobs",
    response_model=list[ContentIndexJobStatusResponse],
    operation_id="list_content_index_jobs",
    summary="관리자용 내용 인덱싱 job 목록",
)
async def list_content_index_jobs(
    limit: int = 20,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> list[ContentIndexJobStatusResponse]:
    """최근 내용 인덱싱 job 목록을 조회한다."""
    _require_admin_token(x_admin_token)
    return _content_jobs.recent(limit)


@app.get("/health", response_model=HealthResponse, operation_id="health_check", summary="헬스체크")
async def health() -> HealthResponse:
    """서비스 상태와 현재 인덱스 크기를 반환한다."""
    index = _state.get("index")
    content_index = _state.get("content_index")
    folder_loaded = index is not None
    content_loaded = content_index is not None
    ready = folder_loaded and content_loaded
    return HealthResponse(
        status="ok" if ready else "degraded",
        ready=ready,
        indexed_folders=len(index) if index else 0,
        indexed_files=content_index.count() if content_index else 0,
        folder_index_loaded=folder_loaded,
        content_index_loaded=content_loaded,
        cache_path=_safe_config_path(_settings.smb_index_cache_path),
        db_path=_safe_config_path(_settings.content_index_db_path),
    )
