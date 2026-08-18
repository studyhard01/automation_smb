"""MCP 문서 메타데이터 도구의 최소 입출력 계약."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GetDocumentMetadataInput(BaseModel):
    """서버가 발급한 문서 ID와 선택적 revision ID를 검증한다."""

    model_config = ConfigDict(extra="forbid")

    doc_id: UUID = Field(description="서버가 발급한 문서 UUID")
    revision_id: UUID | None = Field(default=None, description="생략하면 현재 활성 revision을 조회")


class DocumentMetadataOutput(BaseModel):
    """경로·이름·본문을 제외한 canonical 문서 상태."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["llmops"] = "llmops"
    doc_id: UUID
    revision_id: UUID
    is_active: bool
    revision_status: str = Field(min_length=1, max_length=64)
    extension: str = Field(max_length=32)
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: datetime | None = None
    elapsed_ms: float = Field(ge=0)
    over_budget: bool = False
    degraded_dependencies: list[str] = Field(default_factory=list)
