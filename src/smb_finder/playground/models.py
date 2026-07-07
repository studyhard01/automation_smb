"""Playground API 계약 모델."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ToolPermission = Literal["read", "admin"]
ToolStatus = Literal["ok", "error", "skipped"]


class ToolDefinition(BaseModel):
    """챗봇이 사용할 수 있는 서버 측 tool 정의."""

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
    """프론트에서 전달하는 대화 이력 한 줄."""

    role: Literal["user", "assistant"] = "user"
    content: str


class ChatRequest(BaseModel):
    """Playground 채팅 실행 요청."""

    message: str = Field(min_length=1, max_length=2000)
    selected_tool_ids: list[str] = Field(default_factory=list)
    session_id: str = ""
    history: list[ChatMessage] = Field(default_factory=list)
    provider: Literal["local"] = "local"
    model: str = ""


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
    elapsed_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""


class ToolExecutionResult(BaseModel):
    """tool handler 내부 실행 결과."""

    status: ToolStatus = "ok"
    result_text: str
    error_code: str = ""
    arguments_summary: str = ""


class PlannedToolCall(BaseModel):
    """LLM이 제안한 tool 호출."""

    tool_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolDraftRequest(BaseModel):
    """Tool Lab 초안 생성 요청."""

    instruction: str = Field(min_length=1, max_length=2000)
    provider: Literal["local"] = "local"
    model: str = ""


class ToolDraftResponse(BaseModel):
    """Tool Lab 초안 생성 응답."""

    provider_used: str = "local"
    model_used: str = ""
    draft: dict[str, Any] = Field(default_factory=dict)
    message: str = ""
    warnings: list[str] = Field(default_factory=list)
    error_code: str = ""
