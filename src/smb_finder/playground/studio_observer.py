"""Playground 실행 메타데이터를 로컬 LangGraph Studio로 비동기 전달한다.

환자/검사 데이터가 들어갈 수 있는 요청·대화·도구 인자·결과는 이벤트 계약에 존재하지 않는다.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import re
from typing import Literal, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from smb_finder.config import Settings

from .models import ChatRequest, ChatResponse, LlmProvider, ToolStatus
from .tool_ids import KNOWN_TOOL_IDS, KnownToolId

_logger = logging.getLogger(__name__)
_KNOWN_TOOL_ID_SET = frozenset(KNOWN_TOOL_IDS)
_SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")

ObserverWarningCode = Literal[
    "external_llm_provider",
    "unknown_tools_ignored",
    "playground_agent_over_budget",
    "playground_agent_tool_call_limit",
    "unselected_tool_blocked",
    "admin_tool_blocked",
    "duplicate_tool_call_blocked",
    "external_tool_result_hidden",
    "playground_agent_step_limit",
]
_WARNING_CODE_ALLOWLIST = frozenset(ObserverWarningCode.__args__)


class StudioToolCall(BaseModel):
    """도구 인자와 결과를 제외한 단일 호출 상태."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: KnownToolId
    status: ToolStatus
    error_code: str = Field(default="", max_length=64)
    elapsed_ms: float = Field(ge=0)


class StudioObserverEvent(BaseModel):
    """외부 전송 가능한 필드를 고정한 LangGraph Studio 관측 이벤트."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    event_type: Literal["playground_chat_completed"] = "playground_chat_completed"
    request_id: UUID
    provider: LlmProvider
    model_id: str = Field(min_length=1, max_length=80)
    selected_tool_ids: list[KnownToolId] = Field(default_factory=list)
    called_tool_ids: list[KnownToolId] = Field(default_factory=list)
    tool_calls: list[StudioToolCall] = Field(default_factory=list)
    outcome: Literal["ok", "error"]
    error_code: str = Field(default="", max_length=64)
    warning_codes: list[ObserverWarningCode] = Field(default_factory=list)
    tool_call_count: int = Field(ge=0)
    tool_error_count: int = Field(ge=0)
    agent_step_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    over_budget: bool
    token_total: int = Field(ge=0)
    token_calls: int = Field(ge=0)


def sanitize_model_id(value: str) -> str:
    """임의 문자열을 내보내지 않고 안전한 형식의 모델 ID만 보존한다."""

    candidate = value.strip()
    if not _SAFE_MODEL_ID.fullmatch(candidate):
        return "unrecognized-model"
    return candidate


def _safe_code(value: str) -> str:
    """고정 형식의 내부 상태 코드만 관측 이벤트에 보존한다."""

    candidate = value.strip().lower()
    if not candidate:
        return ""
    if not _SAFE_CODE.fullmatch(candidate):
        return "unrecognized_error"
    return candidate


def _warning_codes(values: list[str]) -> list[ObserverWarningCode]:
    """warning의 사용자 유래 suffix를 제거하고 허용된 코드만 반환한다."""

    result: list[ObserverWarningCode] = []
    for value in values:
        code = value.partition(":")[0]
        if code in _WARNING_CODE_ALLOWLIST and code not in result:
            result.append(cast(ObserverWarningCode, code))
    return result


def _known_tool_ids(values: list[str]) -> list[KnownToolId]:
    """등록된 고정 도구 ID만 순서를 보존해 반환한다."""

    result: list[KnownToolId] = []
    seen: set[str] = set()
    for value in values:
        if value in _KNOWN_TOOL_ID_SET and value not in seen:
            result.append(cast(KnownToolId, value))
            seen.add(value)
    return result


def build_studio_observer_event(request: ChatRequest, response: ChatResponse) -> StudioObserverEvent:
    """채팅 요청/응답에서 허용된 집계 메타데이터만 새 모델로 복사한다."""

    tool_calls = [
        StudioToolCall(
            tool_id=cast(KnownToolId, call.tool_id),
            status=call.status,
            error_code=_safe_code(call.error_code),
            elapsed_ms=max(0.0, call.elapsed_ms),
        )
        for call in response.tool_calls
        if call.tool_id in _KNOWN_TOOL_ID_SET
    ]
    token_usage = response.token_usage
    return StudioObserverEvent(
        request_id=response.request_id,
        provider=request.provider,
        model_id=sanitize_model_id(response.model_used),
        selected_tool_ids=_known_tool_ids(request.selected_tool_ids),
        called_tool_ids=_known_tool_ids([call.tool_id for call in response.tool_calls]),
        tool_calls=tool_calls,
        outcome="error" if response.error_code else "ok",
        error_code=_safe_code(response.error_code),
        warning_codes=_warning_codes(response.warnings),
        tool_call_count=len(response.tool_calls),
        tool_error_count=sum(call.status == "error" for call in response.tool_calls),
        agent_step_count=len(response.agent_steps),
        warning_count=len(response.warnings),
        elapsed_ms=max(0.0, response.elapsed_ms),
        over_budget=response.over_budget,
        token_total=max(0, token_usage.total_tokens) if token_usage else 0,
        token_calls=max(0, token_usage.calls) if token_usage else 0,
    )


class StudioObserver:
    """bounded queue와 단일 worker로 Studio 전달을 응답 경로에서 분리한다."""

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.langgraph_studio_observer_enabled
        self._url = settings.langgraph_studio_observer_url
        self._graph_id = settings.langgraph_studio_observer_graph_id
        self._timeout = settings.langgraph_studio_observer_timeout_ms / 1000
        self._queue: asyncio.Queue[StudioObserverEvent | None] = asyncio.Queue(
            maxsize=settings.langgraph_studio_observer_queue_size
        )
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """관측기 활성화 설정을 반환한다."""

        return self._enabled

    async def start(self) -> None:
        """활성화된 경우에만 비동기 전달 worker를 시작한다."""

        if not self._enabled or self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(self._worker(), name="playground-studio-observer")
        _logger.info("LangGraph Studio observer started")

    async def stop(self) -> None:
        """worker를 종료하되 서비스 shutdown을 외부 서버 응답에 종속시키지 않는다."""

        task = self._worker_task
        if task is None:
            return
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            task.cancel()
        try:
            await asyncio.wait_for(task, timeout=max(0.1, self._timeout))
        except TimeoutError:
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._worker_task = None
        _logger.info("LangGraph Studio observer stopped")

    def enqueue(self, event: StudioObserverEvent) -> bool:
        """이벤트를 기다림 없이 큐에 넣는다. 비활성/포화 상태는 채팅을 실패시키지 않는다."""

        if not self._enabled or self._worker_task is None:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            _logger.warning("LangGraph Studio observer queue full; event dropped")
            return False
        return True

    async def _worker(self) -> None:
        endpoint = f"{self._url}/runs/wait"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                event = await self._queue.get()
                try:
                    if event is None:
                        return
                    payload = {
                        "assistant_id": self._graph_id,
                        "input": event.model_dump(mode="json"),
                        "metadata": {
                            "source": "playground",
                            "request_id": str(event.request_id),
                        },
                        "on_completion": "keep",
                    }
                    try:
                        response = await client.post(endpoint, json=payload)
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        _logger.warning(
                            "LangGraph Studio observer delivery failed: error_type=%s",
                            type(exc).__name__,
                        )
                    except Exception as exc:  # noqa: BLE001 - 관측 worker 실패는 서비스 요청과 분리한다.
                        _logger.warning(
                            "LangGraph Studio observer delivery failed: error_type=%s",
                            type(exc).__name__,
                        )
                finally:
                    self._queue.task_done()
