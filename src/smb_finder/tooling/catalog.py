"""공유 도구 카탈로그와 surface 공개 정책."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel

from .contracts import FindFolderInput, FindFolderOutput, SearchContentInput, SearchContentOutput

ToolSurface = Literal["playground", "mcp"]


@dataclass(frozen=True)
class ToolSpec:
    """도구의 계약과 공개 범위를 나타내는 불변 메타데이터."""

    id: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_surfaces: frozenset[ToolSurface]
    permission: Literal["read"] = "read"
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
        if spec is None or surface not in spec.allowed_surfaces:
            return None
        return spec

    def list(self, surface: ToolSurface) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec in self._specs.values() if surface in spec.allowed_surfaces)


SEARCH_TOOL_SPECS = (
    ToolSpec(
        id="find_folder",
        description="사전 구축된 인메모리 인덱스에서 공유 폴더를 찾습니다.",
        input_model=FindFolderInput,
        output_model=FindFolderOutput,
        allowed_surfaces=frozenset({"playground", "mcp"}),
    ),
    ToolSpec(
        id="search_content",
        description="사전 구축된 로컬 내용 인덱스에서 파일을 찾습니다.",
        input_model=SearchContentInput,
        output_model=SearchContentOutput,
        allowed_surfaces=frozenset({"playground", "mcp"}),
    ),
)
