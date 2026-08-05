"""DB 기반 문서 검색·조회 API의 Pydantic 계약."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ApiErrorResponse(BaseModel):
    """민감한 내부 예외를 숨기는 공통 오류 응답."""

    code: str
    message: str
    retryable: bool = False
    elapsed_ms: float = 0.0


class DocumentSearchRequest(BaseModel):
    """왼쪽 파일 찾기 패널의 자연어 검색 요청."""

    query: str = Field(min_length=1, max_length=500)
    limit: int | None = Field(default=None, ge=1, le=50)


class DocumentSearchHit(BaseModel):
    """선택 가능한 활성 문서 Revision 후보."""

    source: Literal["llmops"] = "llmops"
    doc_id: UUID
    revision_id: UUID
    file_name: str
    title: str = ""
    extension: str = ""
    size_bytes: int | None = None
    modified_at: datetime | None = None
    score: float = 0.0
    match_source: Literal["metadata", "content"] = "metadata"
    matched_stores: list[Literal["postgresql", "minio", "neo4j"]] = Field(
        default_factory=lambda: ["postgresql"]
    )


class DocumentSearchResponse(BaseModel):
    """멀티스토어 검색 결과와 측정 지연."""

    query: str
    normalized_query: str
    hits: list[DocumentSearchHit] = Field(default_factory=list)
    result_count: int = 0
    elapsed_ms: float
    over_budget: bool = False
    source: Literal["llmops"] = "llmops"
    search_mode: Literal["postgresql", "multistore"] = "postgresql"
    queried_stores: list[Literal["postgresql", "minio", "neo4j"]] = Field(default_factory=list)
    llm_expanded: bool = False
    timings_ms: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RetrievalScope(BaseModel):
    """검색 SQL에 강제로 적용한 문서/Revision 범위."""

    doc_id: UUID
    revision_id: UUID


class RetrievalScores(BaseModel):
    """Hybrid 검색 후보의 공개 가능한 점수."""

    vector: float | None = None
    lexical: float | None = None
    trigram: float | None = None
    rrf: float


class DocumentCitation(BaseModel):
    """선택 문서 안에서 찾은 근거 Chunk."""

    index: int
    doc_id: UUID
    revision_id: UUID
    chunk_id: UUID
    title: str
    section_path: list[str] = Field(default_factory=list)
    location: dict[str, Any] = Field(default_factory=dict)
    excerpt: str
    scores: RetrievalScores


class RetrievalMetadata(BaseModel):
    """선택 문서 검색의 범위·판정·지연."""

    trace_id: UUID
    scope: list[RetrievalScope] = Field(default_factory=list)
    result_count: int = 0
    candidate_count: int = 0
    grounded: bool = False
    decision: Literal["answerable", "insufficient_evidence"] = "insufficient_evidence"
    degraded_dependencies: list[str] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    elapsed_ms: float = 0.0
    over_budget: bool = False


class ArtifactLink(BaseModel):
    """Object URI를 노출하지 않는 Backend 문서 링크."""

    doc_id: UUID
    revision_id: UUID
    preview_url: str
    canonical_url: str
    graph_url: str


class ArtifactViewResponse(BaseModel):
    """활성 Revision 검증 뒤 반환하는 Text Artifact."""

    doc_id: UUID
    revision_id: UUID
    artifact_type: Literal["preview", "canonical"]
    media_type: str
    content: str
    size_bytes: int
    elapsed_ms: float


class GraphNode(BaseModel):
    """내부 경로를 제외한 문서 버전 Graph 노드."""

    id: str
    type: Literal["document", "revision"]
    label: str
    status: str = ""


class GraphEdge(BaseModel):
    """문서와 Revision 사이의 허용된 관계."""

    source: str
    target: str
    type: Literal["HAS_REVISION", "LATEST", "SUPERSEDES"]


class DocumentGraphResponse(BaseModel):
    """문서/Revision 버전 관계."""

    doc_id: UUID
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0


class StoreConnectionState(BaseModel):
    """주소와 Credential을 제외한 저장소 연결 상태."""

    configured: bool
    connected: bool
    degraded: bool = False
    message: str
    latency_ms: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class LlmopsStoresStatusResponse(BaseModel):
    """읽기 저장소별 안전한 연결 상태."""

    overall: Literal["ok", "degraded", "unavailable"]
    postgresql: StoreConnectionState
    minio: StoreConnectionState
    neo4j: StoreConnectionState
