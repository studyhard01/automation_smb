"""Playground API 계약 모델."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from smb_finder.models import ArtifactLink, DocumentCitation, RetrievalMetadata

ToolPermission = Literal["read", "write", "admin"]
ToolExecutionType = Literal["code", "llm"]
ToolCategory = Literal["smb", "database", "report", "skill"]
ToolStatus = Literal["ok", "error", "skipped"]
ToolOrigin = Literal["builtin"]
AgentAction = Literal["tool_call", "final_answer", "clarify"]
AgentStepKind = Literal["decision", "tool_call", "observation", "final", "blocked", "error"]
LlmProvider = Literal["local", "openai"]
ChatMode = Literal["chat", "document_qa"]
SkillSource = Literal["builtin", "user"]
RagGroundingDecision = Literal["answerable", "insufficient_evidence"]


class ToolDefinition(BaseModel):
    """채팅에서 사용할 수 있는 서버 측 tool 정의."""

    id: str
    display_name: str
    description: str
    category: ToolCategory
    permission: ToolPermission = "read"
    execution_type: ToolExecutionType
    enabled: bool = True
    default_selected: bool = False
    requires_admin: bool = False
    timeout_ms: int = Field(default=1500, ge=100)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    origin: ToolOrigin = "builtin"
    source_id: str = ""


class SkillDefinition(BaseModel):
    """Playground agent가 사용할 SKILL.md 문서와 공개 메타데이터."""

    id: str
    name: str
    description: str
    instructions: str
    document: str
    source: SkillSource
    editable: bool = False


class SkillCreateRequest(BaseModel):
    """사용자 SKILL.md 생성 요청."""

    skill_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    document: str = Field(min_length=1, max_length=20_000)


class SkillUpdateRequest(BaseModel):
    """사용자 SKILL.md 수정 요청."""

    document: str = Field(min_length=1, max_length=20_000)


class ChatMessage(BaseModel):
    """프론트엔드에서 전달하는 최근 대화 한 줄."""

    role: Literal["user", "assistant"] = "user"
    content: str


class SelectedFileContext(BaseModel):
    """왼쪽 패널에서 사용자가 명시적으로 선택한 파일."""

    source: Literal["llmops", "local_index"]
    doc_id: UUID | None = None
    revision_id: UUID | None = None
    file_name: str = Field(min_length=1, max_length=512)
    title: str = Field(default="", max_length=512)


class PlannedToolCall(BaseModel):
    """LLM이 제안한 tool 호출."""

    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Playground 채팅 실행 요청."""

    message: str = Field(min_length=1, max_length=2000)
    mode: ChatMode = "chat"
    selected_tool_ids: list[str] = Field(default_factory=list)
    selected_skill_ids: list[str] = Field(default_factory=list)
    selected_files: list[SelectedFileContext] = Field(default_factory=list, max_length=5)
    attachment_ids: list[str] = Field(default_factory=list, max_length=1)
    session_id: str = ""
    history: list[ChatMessage] = Field(default_factory=list)
    provider: LlmProvider = "local"
    local_base_url: str = ""
    model: str = ""
    debug_trace: bool = False
    debug_raw_llm: bool = False


class AgentDecision(BaseModel):
    """LLM이 제안한 다음 agent 행동."""

    action: AgentAction = "final_answer"
    tool_calls: list[PlannedToolCall] = Field(default_factory=list)
    answer: str = ""
    question: str = ""
    rationale: str = ""


class AgentStepTrace(BaseModel):
    """사용자에게 공개 가능한 agent 실행 단계."""

    step: int
    kind: AgentStepKind
    title: str
    detail: str = ""
    action: str = ""
    tool_id: str = ""
    tool_name: str = ""
    status: ToolStatus | Literal["blocked"] = "ok"
    elapsed_ms: float = 0.0
    error_code: str = ""


class LlmDebugCall(BaseModel):
    """테스트용 raw LLM 입출력 디버그 정보."""

    purpose: str
    elapsed_ms: float = 0.0
    request_messages: list[dict[str, str]] = Field(default_factory=list)
    raw_response: str = ""
    parsed_json: dict[str, Any] = Field(default_factory=dict)
    token_usage: "TokenUsage | None" = None
    json_repaired: bool = False
    error_code: str = ""


class PlaygroundDebug(BaseModel):
    """Playground 테스트 디버그 묶음."""

    llm_calls: list[LlmDebugCall] = Field(default_factory=list)


class ToolCallTrace(BaseModel):
    """실제로 실행된 tool 호출 이력."""

    tool_id: str
    tool_name: str
    arguments_summary: str = ""
    status: ToolStatus = "ok"
    elapsed_ms: float = 0.0
    result_text: str = ""
    result_payload: dict[str, Any] | None = None
    error_code: str = ""


class TokenUsage(BaseModel):
    """LLM 호출에서 반환된 토큰 사용량 요약."""

    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0


class RagGroundingMetadata(BaseModel):
    """RAG cutoff가 내린 답변 가능 여부와 공개 가능한 점수 요약."""

    decision: RagGroundingDecision
    similarity_cutoff: float = 0.0
    top_similarity: float | None = None
    candidate_count: int = 0
    rejected_count: int = 0


class ChatResponse(BaseModel):
    """Playground 채팅 실행 응답."""

    request_id: str
    session_id: str
    provider_used: str = "local"
    model_used: str = ""
    assistant_message: str
    active_skill_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    agent_steps: list[AgentStepTrace] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
    over_budget: bool = False
    rag_grounding: RagGroundingMetadata | None = None
    citations: list[DocumentCitation] = Field(default_factory=list)
    retrieval: RetrievalMetadata | None = None
    artifacts: list[ArtifactLink] = Field(default_factory=list)
    debug: PlaygroundDebug | None = None
    token_usage: TokenUsage | None = None


class ToolExecutionResult(BaseModel):
    """tool handler 내부 실행 결과."""

    status: ToolStatus = "ok"
    result_text: str
    observation_text: str = ""
    result_payload: dict[str, Any] | None = None
    error_code: str = ""
    arguments_summary: str = ""


class ToolDraftRequest(BaseModel):
    """Tool Lab 초안 생성 요청."""

    instruction: str = Field(min_length=1, max_length=2000)
    provider: LlmProvider = "local"
    local_base_url: str = ""
    model: str = ""


class ToolDraftResponse(BaseModel):
    """Tool Lab 초안 생성 응답."""

    provider_used: str = "local"
    model_used: str = ""
    draft: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
    token_usage: TokenUsage | None = None


class LlmStatusRequest(BaseModel):
    """local LLM 연결 확인 요청."""

    provider: LlmProvider = "local"
    local_base_url: str = ""
    model: str = ""


class LlmStatusResponse(BaseModel):
    """local LLM 연결 확인 응답."""

    provider_used: str = "local"
    base_url_used: str = ""
    model_used: str = ""
    available_models: list[str] = Field(default_factory=list)
    models_ok: bool = False
    chat_ok: bool = False
    elapsed_ms: float = 0.0
    message: str = ""
    error_code: str = ""
    token_usage: TokenUsage | None = None
