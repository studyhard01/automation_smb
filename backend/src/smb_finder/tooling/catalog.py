"""공유 도구 카탈로그와 surface 공개 정책."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from smb_finder.models import ContentSearchRequest, FindRequest

from .contracts import FindFolderInput, FindFolderOutput, SearchContentInput, SearchContentOutput
from .errors import ToolExecutionError

ToolSurface = Literal["playground", "mcp"]


class ToolHandler(Protocol):
    """검증된 입력으로 도메인 runtime을 호출하는 함수 계약."""

    def __call__(self, runtime: Any, validated: BaseModel) -> dict[str, Any]: ...


class TimeoutResolver(Protocol):
    """runtime 설정에서 도구별 시간 예산을 읽는 함수 계약."""

    def __call__(self, runtime: Any) -> int: ...


@dataclass(frozen=True)
class ToolSpec:
    """도구의 계약과 공개 범위를 나타내는 불변 메타데이터."""

    id: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler
    timeout_resolver: TimeoutResolver
    allowed_surfaces: frozenset[ToolSurface]
    permission: Literal["read", "admin"] = "read"
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False


class ToolCatalog:
    """허용된 검색 도구만 조회하는 카탈로그."""

    def __init__(self, specs: tuple[ToolSpec, ...] | None = None) -> None:
        selected = SEARCH_TOOL_SPECS if specs is None else specs
        self._specs = MappingProxyType({spec.id: spec for spec in selected})

    def get(self, tool_id: str, surface: ToolSurface) -> ToolSpec | None:
        spec = self._specs.get(tool_id)
        if spec is None or surface not in spec.allowed_surfaces or not self._surface_allows(spec, surface):
            return None
        return spec

    def list(self, surface: ToolSurface) -> tuple[ToolSpec, ...]:
        return tuple(
            spec
            for spec in self._specs.values()
            if surface in spec.allowed_surfaces and self._surface_allows(spec, surface)
        )

    @staticmethod
    def _surface_allows(spec: ToolSpec, surface: ToolSurface) -> bool:
        if surface != "mcp":
            return True
        return spec.permission == "read" and spec.read_only and not spec.destructive and spec.idempotent


def _find_folder(runtime: Any, validated: BaseModel) -> dict[str, Any]:
    """폴더 인덱스를 한 번 검색하고 외부 공개용 최소 결과로 변환한다."""

    request_input = FindFolderInput.model_validate(validated)
    if runtime.finder is None:
        raise ToolExecutionError("finder_not_ready", "폴더 인덱스가 아직 준비되지 않았습니다.", retryable=True)
    request = FindRequest(
        query=request_input.query,
        limit=request_input.limit or runtime.settings.find_default_limit,
    )
    response = runtime.finder.find(request)
    for hit in response.hits:
        _ensure_safe_runtime_output(hit.name, hit.path, runtime.settings)
    hits = [
        {
            "name": hit.name,
            "path": hit.path,
            "score": getattr(hit, "score", 0.0),
            "depth": getattr(hit, "depth", 0),
        }
        for hit in response.hits
    ]
    return {
        "hits": hits,
        "result_count": getattr(response, "result_count", len(hits)),
        "elapsed_ms": max(0.0, float(getattr(response, "elapsed_ms", 0.0))),
        "over_budget": bool(getattr(response, "over_budget", False)),
    }


def _search_content(runtime: Any, validated: BaseModel) -> dict[str, Any]:
    """내용 인덱스를 한 번 검색하고 본문 없는 최소 결과로 변환한다."""

    request_input = SearchContentInput.model_validate(validated)
    if runtime.content_searcher is None:
        raise ToolExecutionError(
            "content_searcher_not_ready",
            "내용 검색 인덱스가 아직 준비되지 않았습니다.",
            retryable=True,
        )
    request = ContentSearchRequest(
        query=request_input.query,
        limit=request_input.limit or runtime.settings.content_default_limit,
    )
    response = runtime.content_searcher.search(request)
    for hit in response.hits:
        _ensure_safe_runtime_output(hit.name, hit.path, runtime.settings)
    hits = [
        {
            "name": hit.name,
            "path": hit.path,
            "ext": hit.ext,
            "score": getattr(hit, "score", 0.0),
        }
        for hit in response.hits
    ]
    return {
        "hits": hits,
        "result_count": getattr(response, "result_count", len(hits)),
        "elapsed_ms": max(0.0, float(getattr(response, "elapsed_ms", 0.0))),
        "over_budget": bool(getattr(response, "over_budget", False)),
        "_indexed_files": max(0, int(getattr(response, "indexed_files", 0))),
    }


def _find_timeout_ms(runtime: Any) -> int:
    """폴더 검색 시간 예산을 반환한다."""

    return int(runtime.settings.find_budget_ms)


def _content_timeout_ms(runtime: Any) -> int:
    """내용 검색 시간 예산을 반환한다."""

    return int(runtime.settings.content_search_budget_ms)


def _ensure_safe_runtime_output(name: str, path: str, settings: Any) -> None:
    """설정의 내부 식별자·자격정보가 결과명이나 상대경로에 섞이면 차단한다."""

    values = (str(name), str(path))
    exact_markers = [
        settings.smb_host,
        settings.smb_share_name,
        settings.smb_username,
    ]
    for configured_path in (settings.smb_index_cache_path, settings.content_index_db_path):
        normalized = str(configured_path or "").replace("\\", "/")
        exact_markers.append(normalized.rsplit("/", 1)[-1])
    secret_markers = [
        settings.smb_password,
        settings.admin_api_token,
        settings.mcp_api_token,
        settings.llm_api_key,
        settings.openai_api_key,
    ]

    for value in values:
        normalized = value.replace("\\", "/").casefold()
        components = {component for component in normalized.split("/") if component}
        components.add(normalized)
        for marker in exact_markers:
            candidate = str(marker or "").strip().replace("\\", "/").casefold()
            if candidate and candidate in components:
                raise ToolExecutionError("unsafe_tool_output", "도구 결과를 안전하게 반환할 수 없습니다.")
        for marker in secret_markers:
            candidate = str(marker or "").strip().casefold()
            if candidate and candidate in normalized:
                raise ToolExecutionError("unsafe_tool_output", "도구 결과를 안전하게 반환할 수 없습니다.")


SEARCH_TOOL_SPECS = (
    ToolSpec(
        id="find_folder",
        description="사전 구축된 인메모리 인덱스에서 공유 폴더를 찾습니다.",
        input_model=FindFolderInput,
        output_model=FindFolderOutput,
        handler=_find_folder,
        timeout_resolver=_find_timeout_ms,
        allowed_surfaces=frozenset({"playground", "mcp"}),
    ),
    ToolSpec(
        id="search_content",
        description="사전 구축된 로컬 내용 인덱스에서 파일을 찾습니다.",
        input_model=SearchContentInput,
        output_model=SearchContentOutput,
        handler=_search_content,
        timeout_resolver=_content_timeout_ms,
        allowed_surfaces=frozenset({"playground", "mcp"}),
    ),
)
