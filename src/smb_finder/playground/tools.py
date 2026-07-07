"""Playground tool registry."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from smb_finder.config import Settings
from smb_finder.models import ContentSearchRequest, FindRequest

from .models import ToolDefinition, ToolExecutionResult


@dataclass(frozen=True)
class PlaygroundRuntime:
    """Playground tool 실행에 필요한 런타임 객체 묶음."""

    settings: Settings
    finder: Any | None = None
    content_searcher: Any | None = None


@dataclass(frozen=True)
class ToolHandler:
    """tool 정의와 실행 함수를 함께 보관한다."""

    definition: ToolDefinition
    run: Callable[[dict[str, Any]], ToolExecutionResult]


def _text_arg(args: dict[str, Any], key: str, fallback: str = "") -> str:
    value = args.get(key, fallback)
    return str(value or "").strip()


def _format_find_response(data: Any) -> str:
    hits = list(getattr(data, "hits", []))
    if not hits:
        return f"'{getattr(data, 'normalized_query', getattr(data, 'query', ''))}' 관련 폴더를 찾지 못했습니다."
    lines = [
        f"폴더 검색 결과 {len(hits)}건 ({getattr(data, 'elapsed_ms', 0)}ms):",
    ]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit.name} - {hit.path}")
    return "\n".join(lines)


def _format_content_response(data: Any) -> str:
    hits = list(getattr(data, "hits", []))
    if not hits:
        indexed_files = getattr(data, "indexed_files", 0)
        if indexed_files == 0:
            return "내용 인덱스가 비어 있습니다. 먼저 필요한 폴더를 DB화해야 합니다."
        return "본문 내용과 일치하는 파일을 찾지 못했습니다."
    terms = " ".join(getattr(data, "terms", []))
    lines = [
        f"내용 검색 결과 {len(hits)}건 ({terms}, {getattr(data, 'elapsed_ms', 0)}ms):",
    ]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit.name} ({hit.ext}) - {hit.path}")
    return "\n".join(lines)


def build_tool_registry(runtime: PlaygroundRuntime) -> dict[str, ToolHandler]:
    """현재 런타임 상태에 맞는 tool registry를 만든다."""

    settings = runtime.settings

    def find_folder(args: dict[str, Any]) -> ToolExecutionResult:
        query = _text_arg(args, "query")
        if not query:
            return ToolExecutionResult(status="error", result_text="검색어가 비어 있습니다.", error_code="empty_query")
        if runtime.finder is None:
            return ToolExecutionResult(
                status="error",
                result_text="폴더 인덱스가 아직 준비되지 않았습니다.",
                error_code="finder_not_ready",
                arguments_summary=f"query_len={len(query)}",
            )
        started = time.perf_counter()
        response = runtime.finder.find(FindRequest(query=query, limit=settings.find_default_limit))
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return ToolExecutionResult(
            result_text=_format_find_response(response),
            arguments_summary=f"query_len={len(query)} elapsed_ms={elapsed_ms}",
        )

    def search_content(args: dict[str, Any]) -> ToolExecutionResult:
        query = _text_arg(args, "query")
        if not query:
            return ToolExecutionResult(status="error", result_text="검색어가 비어 있습니다.", error_code="empty_query")
        if runtime.content_searcher is None:
            return ToolExecutionResult(
                status="error",
                result_text="내용 검색 인덱스가 아직 준비되지 않았습니다.",
                error_code="content_searcher_not_ready",
                arguments_summary=f"query_len={len(query)}",
            )
        started = time.perf_counter()
        response = runtime.content_searcher.search(ContentSearchRequest(query=query, limit=settings.content_default_limit))
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return ToolExecutionResult(
            result_text=_format_content_response(response),
            arguments_summary=f"query_len={len(query)} elapsed_ms={elapsed_ms}",
        )

    def refresh_content(args: dict[str, Any]) -> ToolExecutionResult:
        path = _text_arg(args, "path")
        if not settings.admin_api_token.strip():
            return ToolExecutionResult(
                status="error",
                result_text="ADMIN_API_TOKEN이 설정되지 않아 내용 DB화 tool은 비활성화되어 있습니다.",
                error_code="admin_api_disabled",
                arguments_summary=f"path_len={len(path)}",
            )
        return ToolExecutionResult(
            status="skipped",
            result_text=(
                "내용 DB화는 무거운 관리자 작업이라 Playground 1차 버전에서는 자동 실행하지 않습니다. "
                "/admin/content-index-jobs API 또는 Langflow의 DB화 컴포넌트를 사용하세요."
            ),
            error_code="admin_tool_manual_only",
            arguments_summary=f"path_len={len(path)}",
        )

    find_timeout = max(100, settings.find_budget_ms)
    content_timeout = max(100, settings.content_search_budget_ms)
    admin_enabled = bool(settings.admin_api_token.strip())
    return {
        "find_folder": ToolHandler(
            definition=ToolDefinition(
                id="find_folder",
                display_name="공유폴더 찾기",
                description="폴더 이름이나 경로 단서로 사내 SMB 공유폴더를 찾습니다.",
                permission="read",
                enabled=True,
                default_selected=True,
                timeout_ms=find_timeout,
                input_schema={"query": "찾을 폴더 설명 또는 키워드"},
            ),
            run=find_folder,
        ),
        "search_content": ToolHandler(
            definition=ToolDefinition(
                id="search_content",
                display_name="파일 내용 검색",
                description="로컬 내용 인덱스에서 파일 본문 키워드를 검색합니다.",
                permission="read",
                enabled=True,
                default_selected=False,
                timeout_ms=content_timeout,
                input_schema={"query": "찾을 본문 키워드"},
            ),
            run=search_content,
        ),
        "refresh_content": ToolHandler(
            definition=ToolDefinition(
                id="refresh_content",
                display_name="폴더 내용 DB화",
                description="관리자 승인 후 특정 폴더의 파일 본문을 로컬 검색 DB에 적재합니다.",
                permission="admin",
                enabled=admin_enabled,
                default_selected=False,
                requires_admin=True,
                timeout_ms=max(100, settings.content_index_build_budget_sec * 1000),
                input_schema={"path": "공유 루트 기준 상대 폴더 경로"},
            ),
            run=refresh_content,
        ),
    }
