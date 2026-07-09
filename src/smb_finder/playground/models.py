"""Playground API 계약 모델."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ToolPermission = Literal["read", "admin"]
ToolStatus = Literal["ok", "error", "skipped"]
AgentAction = Literal["tool_call", "final_answer", "clarify"]
AgentStepKind = Literal["decision", "tool_call", "observation", "final", "blocked", "error"]
LlmProvider = Literal["local", "openai"]


class ToolDefinition(BaseModel):
    """채팅에서 사용할 수 있는 서버 측 tool 정의."""

    id: str
    display_name: str
    description: str
    permission: ToolPermission = "read"
    enabled: bool = True
    default_selected: bool = False
    requires_admin: bool = False
    timeout_ms: int = Field(default=1500, ge=100)
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """프론트엔드에서 전달하는 최근 대화 한 줄."""

    role: Literal["user", "assistant"] = "user"
    content: str


class PlannedToolCall(BaseModel):
    """LLM이 제안한 tool 호출."""

    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Playground 채팅 실행 요청."""

    message: str = Field(min_length=1, max_length=2000)
    selected_tool_ids: list[str] = Field(default_factory=list)
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
    error_code: str = ""


class ChatResponse(BaseModel):
    """Playground 채팅 실행 응답."""

    session_id: str
    provider_used: str = "local"
    model_used: str = ""
    assistant_message: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    agent_steps: list[AgentStepTrace] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
    over_budget: bool = False
    debug: PlaygroundDebug | None = None


class ToolExecutionResult(BaseModel):
    """tool handler 내부 실행 결과."""

    status: ToolStatus = "ok"
    result_text: str
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
