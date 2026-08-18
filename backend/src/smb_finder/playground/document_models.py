"""선택 문서 대화 API 계약."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from smb_finder.models import ArtifactLink, DocumentCitation, RetrievalMetadata


class ChatMessage(BaseModel):
    """문서 대화에 전달하는 최근 대화 한 건."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class SelectedFileContext(BaseModel):
    """DB 검색 결과 또는 방금 업로드한 파일의 서버 발급 참조."""

    source: Literal["llmops", "upload"] = "llmops"
    doc_id: UUID
    revision_id: UUID
    file_name: str = Field(min_length=1, max_length=512)
    title: str = Field(default="", max_length=512)


class ChatRequest(BaseModel):
    """선택 문서 범위를 강제하는 대화 요청."""

    message: str = Field(min_length=1, max_length=2000)
    mode: Literal["document_qa"] = "document_qa"
    selected_files: list[SelectedFileContext] = Field(default_factory=list, max_length=5)
    session_id: str = Field(default="", max_length=128)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    model: str = Field(default="", max_length=200)


class ToolCallTrace(BaseModel):
    """사용자에게 공개하는 선택 문서 검색 trace."""

    tool_id: Literal["search_selected_llmops"] = "search_selected_llmops"
    tool_name: str = "선택 문서 근거 검색"
    status: Literal["ok", "error"] = "ok"
    elapsed_ms: float = 0.0
    result_count: int = 0
    error_code: str = ""


class TokenUsage(BaseModel):
    """Ollama가 반환한 토큰 사용량."""

    provider: Literal["local"] = "local"
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 1


class RagGroundingMetadata(BaseModel):
    """근거 존재 여부와 후보 수."""

    decision: Literal["answerable", "insufficient_evidence"]
    candidate_count: int = 0
    rejected_count: int = 0


class ChatResponse(BaseModel):
    """선택 문서 범위 안에서 생성한 답변과 근거."""

    request_id: str
    session_id: str
    provider_used: Literal["local"] = "local"
    model_used: str = ""
    assistant_message: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
    over_budget: bool = False
    rag_grounding: RagGroundingMetadata | None = None
    citations: list[DocumentCitation] = Field(default_factory=list)
    retrieval: RetrievalMetadata | None = None
    artifacts: list[ArtifactLink] = Field(default_factory=list)
    token_usage: TokenUsage | None = None
