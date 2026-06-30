"""FastAPI 앱 — 공유 폴더 찾기 도구(L4)의 진입점.

OpenAPI로 노출되어 L2(자체 디스패처)·L3(Dify Custom Tool)에 등록된다.
operation_id/summary/description이 그대로 도구 메타데이터가 되므로 명확히 쓴다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from .config import load_settings
from .finder import Finder
from .indexer import build_index, load_or_build
from .models import FindRequest, FindResponse

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger(__name__)

_settings = load_settings()
_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """시작 시 인덱스를 로드(캐시 우선)해 첫 요청부터 빠르게 응답한다."""
    index = await run_in_threadpool(load_or_build, _settings)
    _state["finder"] = Finder(index, _settings)
    _state["index"] = index
    _logger.info("서비스 준비 완료 (인덱스 %d건)", len(index))
    yield
    _state.clear()


app = FastAPI(
    title="공유 폴더 찾기 (smb-finder)",
    version="0.1.0",
    description="자연어 명령으로 온프레미스 SMB 공유폴더를 즉시 찾는다.",
    lifespan=lifespan,
)


@app.post("/find", response_model=FindResponse, operation_id="find_share_folder", summary="공유 폴더 찾기")
async def find_share_folder(request: FindRequest) -> FindResponse:
    """자연어 명령(예: 'OO검사 결과 폴더 찾아줘')으로 관련 공유폴더를 관련도순으로 반환한다."""
    finder: Finder = _state["finder"]
    # 인메모리 검색이라 빠르지만, 이벤트 루프 블로킹을 피해 threadpool에서 실행
    return await run_in_threadpool(finder.find, request)


@app.post("/refresh", operation_id="refresh_index", summary="폴더 인덱스 재빌드")
async def refresh_index() -> dict:
    """SMB를 다시 순회해 인덱스를 갱신한다 (관리용 — 느림, 사용자 경로 아님)."""
    index = await run_in_threadpool(build_index, _settings)
    _state["index"].replace(index.entries)  # 동일 인스턴스 갱신 (Finder가 참조 유지)
    return {"folders": len(index)}


@app.get("/health", summary="헬스체크")
async def health() -> dict:
    """서비스 상태와 현재 인덱스 크기를 반환한다."""
    index = _state.get("index")
    return {"status": "ok", "indexed_folders": len(index) if index else 0}
