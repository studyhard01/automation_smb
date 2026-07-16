"""Local LLM 기반 Playground agent."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse
import ipaddress

import httpx

from smb_finder.config import Settings

from .models import (
    AgentDecision,
    AgentStepTrace,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    LlmDebugCall,
    LlmStatusRequest,
    LlmStatusResponse,
    PlannedToolCall,
    PlaygroundDebug,
    SkillDefinition,
    TokenUsage,
    ToolCallTrace,
    ToolDraftRequest,
    ToolDraftResponse,
    ToolExecutionResult,
)
from .skills import SkillStore, format_skills_help, format_tools_help
from .tracing import trace_openai_chat_completion
from .tools import ToolExecutionContext, ToolHandler


@dataclass(frozen=True)
class LlmConnection:
    """Provider별 LLM 접속 정보."""

    provider: str
    base_url: str
    model: str
    api_key: str = ""
    external: bool = False
    error_code: str = ""
    error_message: str = ""


class LlmResponseFormatError(RuntimeError):
    """LLM 응답 본문이 비었거나 agent JSON으로 해석되지 않은 오류."""


def _json_request_messages(
    messages: list[dict[str, str]],
    *,
    provider: str,
    model: str,
) -> list[dict[str, str]]:
    """Qwen3 JSON 호출에서 thinking이 응답 예산을 소진하지 않도록 한다."""

    prepared = [dict(message) for message in messages]
    if provider != "local" or "qwen3" not in model.lower() or not prepared:
        return prepared

    content = str(prepared[-1].get("content") or "")
    if "/no_think" not in content:
        prepared[-1]["content"] = f"{content}\n/no_think"
    return prepared


def _is_internal_http_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _is_https_url_without_userinfo(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def _json_from_text(text: str) -> dict[str, Any]:
    data, _ = _json_from_text_with_meta(text)
    return data


def _json_from_text_with_meta(text: str) -> tuple[dict[str, Any], bool]:
    stripped = _strip_json_fence(text)
    first_object = stripped.find("{")
    candidates = [stripped]
    if first_object >= 0:
        candidates.append(stripped[first_object:])
        last_object = stripped.rfind("}")
        if last_object > first_object:
            candidates.append(stripped[first_object : last_object + 1])

    last_error: Exception | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            return _loads_json_object(candidate), False
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = exc

        repaired = _append_missing_json_closers(candidate)
        if repaired != candidate:
            try:
                return _loads_json_object(repaired), True
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc

    if last_error:
        raise last_error
    raise json.JSONDecodeError("No JSON object found", stripped, 0)


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _loads_json_object(value: str) -> dict[str, Any]:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise TypeError("LLM response JSON must be an object")
    return data


def _append_missing_json_closers(value: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]":
            if not stack or stack[-1] != char:
                return value
            stack.pop()

    if in_string or not stack:
        return value
    return value + "".join(reversed(stack))


def _limit_text(value: str, limit: int) -> str:
    limit = max(0, limit)
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...[truncated {len(value) - limit} chars]"


class PlaygroundAgent:
    """선택된 read tool만 사용하는 제한형 tool-use agent."""

    def __init__(self, settings: Settings, skill_store: SkillStore | None = None):
        self.settings = settings
        self.skill_store = skill_store or SkillStore(settings.playground_skills_dir)

    def _resolve_llm_connection(
        self,
        *,
        provider: str,
        local_base_url: str,
        model: str,
        openai_api_key: str = "",
    ) -> LlmConnection:
        """요청 provider를 실제 LLM 접속 정보로 변환한다."""
        if provider == "local":
            resolved_model = (model or self.settings.llm_model).strip()
            base_url = self._local_base_url(local_base_url)
            if not resolved_model:
                return LlmConnection(
                    provider="local",
                    base_url=base_url,
                    model=resolved_model,
                    error_code="local_llm_not_configured",
                    error_message="로컬 LLM 모델명이 설정되지 않았습니다. 화면에서 모델명을 입력하세요.",
                )
            if not _is_internal_http_url(base_url):
                return LlmConnection(
                    provider="local",
                    base_url=base_url,
                    model=resolved_model,
                    error_code="local_llm_url_not_internal",
                    error_message="로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
                )
            return LlmConnection(
                provider="local",
                base_url=base_url,
                model=resolved_model,
                api_key=self._local_api_key(base_url),
            )

        if provider == "openai":
            resolved_model = (model or self.settings.openai_model).strip()
            base_url = self.settings.openai_base_url.strip().rstrip("/")
            api_key = (openai_api_key or self.settings.openai_api_key).strip()
            if not resolved_model:
                return LlmConnection(
                    provider="openai",
                    base_url=base_url,
                    model=resolved_model,
                    external=True,
                    error_code="openai_model_not_configured",
                    error_message="OpenAI 모델명이 설정되지 않았습니다.",
                )
            if not api_key:
                return LlmConnection(
                    provider="openai",
                    base_url=base_url,
                    model=resolved_model,
                    external=True,
                    error_code="openai_api_key_missing",
                    error_message="Settings에서 OpenAI API key를 입력하세요.",
                )
            if not _is_https_url_without_userinfo(base_url):
                return LlmConnection(
                    provider="openai",
                    base_url=base_url,
                    model=resolved_model,
                    external=True,
                    error_code="openai_base_url_invalid",
                    error_message="OpenAI API base URL은 HTTPS 주소여야 합니다.",
                )
            return LlmConnection(
                provider="openai",
                base_url=base_url,
                model=resolved_model,
                api_key=api_key,
                external=True,
            )

        return LlmConnection(
            provider=provider,
            base_url="",
            model=model.strip(),
            error_code="unsupported_provider",
            error_message="지원하지 않는 LLM provider입니다.",
        )

    def _tool_execution_context(
        self,
        *,
        connection: LlmConnection,
        session_id: str,
        request_id: str = "",
        usage_calls: list[TokenUsage] | None = None,
        debug_calls: list[LlmDebugCall] | None = None,
    ) -> ToolExecutionContext:
        """agent와 LLM-backed tool이 같은 provider/model 호출 경로를 쓰게 한다."""

        def invoke_json(messages: list[dict[str, str]], max_tokens: int, purpose: str) -> dict[str, Any]:
            return self._chat_json(
                messages,
                model=connection.model,
                base_url=connection.base_url,
                api_key=connection.api_key,
                max_tokens=max_tokens,
                purpose=purpose,
                provider=connection.provider,
                session_id=session_id,
                request_id=request_id,
                usage_calls=usage_calls,
                debug_calls=debug_calls,
            )

        return ToolExecutionContext(
            provider=connection.provider,
            model=connection.model,
            invoke_json=invoke_json,
        )

    def execute_tool_direct(
        self,
        handler: ToolHandler,
        arguments: dict[str, Any],
        *,
        provider: str,
        local_base_url: str = "",
        model: str = "",
        openai_api_key: str = "",
    ) -> ToolExecutionResult:
        """agent 판단 단계 없이 API에서 하나의 LLM-backed tool을 직접 실행한다."""

        connection = self._resolve_llm_connection(
            provider=provider,
            local_base_url=local_base_url,
            model=model,
            openai_api_key=openai_api_key,
        )
        if connection.error_code:
            return ToolExecutionResult(
                status="error",
                result_text=connection.error_message,
                error_code=connection.error_code,
            )
        if handler.definition.permission == "admin" or handler.definition.requires_admin:
            return ToolExecutionResult(
                status="error",
                result_text="관리자 전용 tool은 Playground에서 직접 실행할 수 없습니다.",
                error_code="admin_api_only",
            )
        if not handler.definition.enabled:
            return ToolExecutionResult(
                status="error",
                result_text="현재 비활성화된 tool입니다.",
                error_code="tool_disabled",
            )

        context = self._tool_execution_context(
            connection=connection,
            session_id=f"pg-direct-{uuid.uuid4()}",
        )
        return handler.execute(arguments, context=context)

    def run(
        self,
        request: ChatRequest,
        registry: dict[str, ToolHandler],
        openai_api_key: str = "",
        request_id: str = "",
    ) -> ChatResponse:
        started = time.perf_counter()
        request_id = request_id or str(uuid.uuid4())
        session_id = request.session_id or f"pg-{uuid.uuid4()}"
        warnings: list[str] = []
        available_skills = self.skill_store.list()
        skill_map = {skill.id: skill for skill in available_skills}
        unknown_skills = sorted(set(request.selected_skill_ids) - set(skill_map))
        if unknown_skills:
            warnings.append(f"unknown_skills_ignored:{','.join(unknown_skills)}")
        active_skills = [skill_map[skill_id] for skill_id in request.selected_skill_ids if skill_id in skill_map]
        command = request.message.strip().lower()
        if command in {"/tools", "/skills"}:
            assistant_message = (
                format_tools_help(registry) if command == "/tools" else format_skills_help(available_skills)
            )
            command_skill = command.removeprefix("/")
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                provider_used=request.provider,
                model_used=request.model,
                assistant_message=assistant_message,
                active_skill_ids=[command_skill],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                warnings=warnings,
            )
        connection = self._resolve_llm_connection(
            provider=request.provider,
            local_base_url=request.local_base_url,
            model=request.model,
            openai_api_key=openai_api_key,
        )
        model = connection.model
        base_url = connection.base_url
        all_steps: list[AgentStepTrace] = []
        traces: list[ToolCallTrace] = []
        debug_calls: list[LlmDebugCall] = []
        usage_calls: list[TokenUsage] = []
        debug_allowed = request.debug_raw_llm
        if connection.external:
            warnings.append("external_llm_provider:openai")

        if connection.error_code:
            return self._error_response(
                session_id,
                started,
                model,
                connection.error_code,
                connection.error_message,
                warnings=warnings,
                provider=connection.provider,
                request_id=request_id,
            )

        if request.provider not in {"local", "openai"}:
            return self._error_response(
                session_id,
                started,
                model,
                "unsupported_provider",
                "Playground 1차 버전은 local/on-prem LLM만 지원합니다.",
                warnings=warnings,
                request_id=request_id,
            )
        if not model:
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_not_configured",
                "로컬 LLM 모델이 설정되지 않았습니다. LLM_MODEL 또는 화면의 모델명을 입력하세요.",
                warnings=warnings,
                request_id=request_id,
            )
        if connection.provider == "local" and not _is_internal_http_url(base_url):
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_url_not_internal",
                "로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
                warnings=warnings,
                request_id=request_id,
            )

        selected = [tool_id for tool_id in request.selected_tool_ids if tool_id in registry]
        if (
            any(skill.id == "skill-creator" for skill in active_skills)
            and "create_playground_skill" in registry
            and "create_playground_skill" not in selected
        ):
            selected.append("create_playground_skill")
        unknown = sorted(set(request.selected_tool_ids) - set(registry))
        if unknown:
            warnings.append(f"unknown_tools_ignored:{','.join(unknown)}")
        admin_only = [
            tool_id
            for tool_id in selected
            if registry[tool_id].definition.permission == "admin" or registry[tool_id].definition.requires_admin
        ]
        if admin_only:
            warnings.append(f"admin_tools_blocked:{','.join(admin_only)}")
            return self._error_response(
                session_id,
                started,
                model,
                "admin_api_only",
                "관리자 전용 tool은 Playground 채팅에서 실행할 수 없습니다.",
                warnings=warnings,
                provider=connection.provider,
                request_id=request_id,
            )
        disabled = [tool_id for tool_id in selected if not registry[tool_id].definition.enabled]
        if disabled:
            warnings.append(f"disabled_tools_blocked:{','.join(disabled)}")
            return self._error_response(
                session_id,
                started,
                model,
                "tool_disabled",
                "현재 비활성화된 tool은 실행할 수 없습니다.",
                warnings=warnings,
                provider=connection.provider,
                request_id=request_id,
            )
        handlers = {tool_id: registry[tool_id] for tool_id in selected}

        if (
            set(request.selected_tool_ids) == {"search_rag_chunks"}
            and set(request.selected_skill_ids) == {"rag-grounded-answer"}
        ):
            return self._run_rag_grounded_fast_path(
                request=request,
                handler=registry["search_rag_chunks"],
                active_skills=active_skills,
                connection=connection,
                session_id=session_id,
                request_id=request_id,
                started=started,
                warnings=warnings,
                debug_allowed=debug_allowed,
            )

        tool_context = self._tool_execution_context(
            connection=connection,
            session_id=session_id,
            request_id=request_id,
            usage_calls=usage_calls,
            debug_calls=debug_calls if debug_allowed else None,
        )

        observations: list[dict[str, str]] = []
        final_answer = ""
        response_error_code = ""
        over_budget = False
        max_steps = max(1, self.settings.playground_agent_max_steps)
        max_tool_calls = max(0, self.settings.playground_agent_max_tool_calls)
        tool_call_count = 0
        executed_call_keys: set[str] = set()

        for step in range(1, max_steps + 1):
            if self._elapsed_ms(started) > self.settings.playground_agent_budget_ms:
                over_budget = True
                warnings.append("playground_agent_over_budget")
                break

            try:
                decision = self._decide_next_action(
                    request=request,
                    handlers=handlers,
                    observations=observations,
                    model=model,
                    base_url=base_url,
                    api_key=connection.api_key,
                    step=step,
                    provider=connection.provider,
                    session_id=session_id,
                    request_id=request_id,
                    usage_calls=usage_calls,
                    debug_calls=debug_calls if debug_allowed else None,
                    active_skills=active_skills,
                )
            except LlmResponseFormatError:
                return self._error_response(
                    session_id,
                    started,
                    model,
                    "llm_response_invalid_json",
                    "로컬 모델 응답을 agent JSON으로 해석하지 못했습니다. 모델의 JSON mode/thinking 설정을 확인하세요.",
                    warnings=warnings,
                    agent_steps=all_steps if request.debug_trace else [],
                    debug_calls=debug_calls if debug_allowed else [],
                    provider=connection.provider,
                    token_usage=self._aggregate_token_usage(usage_calls, connection.provider, model),
                    request_id=request_id,
                )
            except Exception:  # noqa: BLE001 - UI에는 안전한 오류 코드만 반환한다.
                return self._error_response(
                    session_id,
                    started,
                    model,
                    "local_llm_failed",
                    "로컬 LLM 호출에 실패했습니다. 모델 서버와 LLM_BASE_URL을 확인하세요.",
                    warnings=warnings,
                    agent_steps=all_steps if request.debug_trace else [],
                    debug_calls=debug_calls if debug_allowed else [],
                    provider=connection.provider,
                    token_usage=self._aggregate_token_usage(usage_calls, connection.provider, model),
                    request_id=request_id,
                )

            all_steps.append(
                AgentStepTrace(
                    step=step,
                    kind="decision",
                    title="Agent 판단",
                    detail=decision.rationale or decision.action,
                    action=decision.action,
                )
            )

            if decision.action == "clarify":
                final_answer = decision.question.strip() or "요청을 조금 더 구체적으로 알려주세요."
                all_steps.append(
                    AgentStepTrace(step=step, kind="final", title="추가 질문", detail=final_answer, action="clarify")
                )
                break

            if decision.action == "final_answer":
                final_answer = decision.answer.strip() or self._fallback_answer(traces)
                all_steps.append(
                    AgentStepTrace(step=step, kind="final", title="최종 답변", detail=final_answer, action="final_answer")
                )
                break

            calls = decision.tool_calls[:1]
            if not calls:
                final_answer = self._fallback_answer(traces) or "선택된 tool로 실행할 작업을 찾지 못했습니다."
                all_steps.append(
                    AgentStepTrace(
                        step=step,
                        kind="blocked",
                        title="tool 호출 없음",
                        detail=final_answer,
                        action="tool_call",
                        status="blocked",
                        error_code="empty_tool_call",
                    )
                )
                break

            for planned_call in calls:
                if tool_call_count >= max_tool_calls:
                    warnings.append("playground_agent_tool_call_limit")
                    all_steps.append(
                        AgentStepTrace(
                            step=step,
                            kind="blocked",
                            title="tool 호출 제한 도달",
                            detail="설정된 최대 tool 호출 수에 도달했습니다.",
                            action="tool_call",
                            tool_id=planned_call.tool_id,
                            status="blocked",
                            error_code="tool_call_limit",
                        )
                    )
                    final_answer = self._fallback_answer(traces) or "tool 호출 제한에 도달해 여기서 중단했습니다."
                    break

                handler = handlers.get(planned_call.tool_id)
                if handler is None:
                    warnings.append(f"unselected_tool_blocked:{planned_call.tool_id}")
                    blocked_detail = f"선택되지 않았거나 비활성화된 tool 요청이 차단되었습니다: {planned_call.tool_id}"
                    observations.append({"tool": planned_call.tool_id, "status": "blocked", "result": blocked_detail})
                    all_steps.append(
                        AgentStepTrace(
                            step=step,
                            kind="blocked",
                            title="tool 요청 차단",
                            detail=blocked_detail,
                            action="tool_call",
                            tool_id=planned_call.tool_id,
                            status="blocked",
                            error_code="unselected_tool_blocked",
                        )
                    )
                    continue

                if handler.definition.permission == "admin" or handler.definition.requires_admin:
                    warnings.append(f"admin_tool_blocked:{planned_call.tool_id}")
                    blocked_detail = "Playground agent는 admin tool을 자동 실행하지 않습니다."
                    observations.append({"tool": planned_call.tool_id, "status": "blocked", "result": blocked_detail})
                    all_steps.append(
                        AgentStepTrace(
                            step=step,
                            kind="blocked",
                            title="admin tool 차단",
                            detail=blocked_detail,
                            action="tool_call",
                            tool_id=planned_call.tool_id,
                            tool_name=handler.definition.display_name,
                            status="blocked",
                            error_code="admin_tool_blocked",
                        )
                    )
                    continue

                call_key = self._tool_call_key(planned_call)
                if call_key in executed_call_keys:
                    warnings.append(f"duplicate_tool_call_blocked:{planned_call.tool_id}")
                    detail = "같은 tool과 인자로 반복 호출하려는 요청을 차단했습니다."
                    all_steps.append(
                        AgentStepTrace(
                            step=step,
                            kind="blocked",
                            title="중복 tool 호출 차단",
                            detail=detail,
                            action="tool_call",
                            tool_id=planned_call.tool_id,
                            tool_name=handler.definition.display_name,
                            status="blocked",
                            error_code="duplicate_tool_call_blocked",
                        )
                    )
                    final_answer = self._fallback_answer(traces) or detail
                    break

                trace_started = time.perf_counter()
                all_steps.append(
                    AgentStepTrace(
                        step=step,
                        kind="tool_call",
                        title="tool 실행",
                        detail=self._public_arguments_summary(planned_call.arguments),
                        action="tool_call",
                        tool_id=handler.definition.id,
                        tool_name=handler.definition.display_name,
                    )
                )
                try:
                    result = handler.execute(planned_call.arguments, context=tool_context)
                    status = result.status
                    result_text = result.result_text
                    result_payload = result.result_payload
                    error_code = result.error_code
                    arguments_summary = result.arguments_summary
                except Exception:  # noqa: BLE001
                    status = "error"
                    result_text = "tool 실행 중 오류가 발생했습니다."
                    result_payload = None
                    error_code = "tool_failed"
                    arguments_summary = ""

                tool_elapsed_ms = round((time.perf_counter() - trace_started) * 1000, 1)
                tool_call_count += 1
                executed_call_keys.add(call_key)
                traces.append(
                    ToolCallTrace(
                        tool_id=handler.definition.id,
                        tool_name=handler.definition.display_name,
                        arguments_summary=arguments_summary,
                        status=status,
                        elapsed_ms=tool_elapsed_ms,
                        result_text=result_text,
                        result_payload=result_payload,
                        error_code=error_code,
                    )
                )
                observation_text = _limit_text(result_text, self.settings.playground_agent_result_chars)
                observations.append(
                    {
                        "tool": handler.definition.id,
                        "status": status,
                        "result": observation_text,
                    }
                )
                if status == "ok" and handler.definition.id == "create_playground_skill" and result_payload:
                    created_skill_id = str(result_payload.get("id", "")).strip()
                    created_skill = self.skill_store.get(created_skill_id) if created_skill_id else None
                    if created_skill is not None and all(skill.id != created_skill.id for skill in active_skills):
                        active_skills.append(created_skill)
                all_steps.append(
                    AgentStepTrace(
                        step=step,
                        kind="observation",
                        title="tool 결과",
                        detail=observation_text,
                        action="tool_call",
                        tool_id=handler.definition.id,
                        tool_name=handler.definition.display_name,
                        status=status,
                        elapsed_ms=tool_elapsed_ms,
                        error_code=error_code,
                    )
                )
                if status != "ok":
                    final_answer = result_text
                    response_error_code = error_code or "tool_failed"
                    all_steps.append(
                        AgentStepTrace(
                            step=step,
                            kind="error",
                            title="tool 오류로 종료",
                            detail=result_text,
                            action="final_answer",
                            tool_id=handler.definition.id,
                            tool_name=handler.definition.display_name,
                            status="error",
                            error_code=response_error_code,
                        )
                    )
                    break
                if status == "ok" and handler.returns_final_answer:
                    final_answer = result_text
                    all_steps.append(
                        AgentStepTrace(
                            step=step,
                            kind="final",
                            title="tool 최종 결과",
                            detail=result_text,
                            action="final_answer",
                            tool_id=handler.definition.id,
                            tool_name=handler.definition.display_name,
                        )
                    )
                    break

            if final_answer:
                break

        if not final_answer:
            warnings.append("playground_agent_step_limit")
            final_answer = self._fallback_answer(traces) or "정해진 agent 단계 안에서 최종 답변을 만들지 못했습니다."
            all_steps.append(
                AgentStepTrace(
                    step=max_steps,
                    kind="final",
                    title="단계 제한 종료",
                    detail=final_answer,
                    action="final_answer",
                )
            )

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if elapsed_ms > self.settings.playground_agent_budget_ms and "playground_agent_over_budget" not in warnings:
            over_budget = True
            warnings.append("playground_agent_over_budget")

        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            provider_used=connection.provider,
            model_used=model,
            assistant_message=final_answer,
            active_skill_ids=[skill.id for skill in active_skills],
            tool_calls=traces,
            agent_steps=all_steps if request.debug_trace else [],
            elapsed_ms=elapsed_ms,
            warnings=warnings,
            error_code=response_error_code,
            over_budget=over_budget,
            debug=PlaygroundDebug(llm_calls=debug_calls) if debug_allowed else None,
            token_usage=self._aggregate_token_usage(usage_calls, connection.provider, model),
        )

    def _run_rag_grounded_fast_path(
        self,
        *,
        request: ChatRequest,
        handler: ToolHandler,
        active_skills: list[SkillDefinition],
        connection: LlmConnection,
        session_id: str,
        request_id: str,
        started: float,
        warnings: list[str],
        debug_allowed: bool,
    ) -> ChatResponse:
        """단일 RAG tool과 근거 답변 skill 조합을 결정 LLM 없이 실행한다."""

        arguments = {"query": request.message, "limit": self.settings.rag_db_default_limit}
        trace_started = time.perf_counter()
        try:
            result = handler.execute(arguments)
        except Exception:  # noqa: BLE001 - 공개 응답에는 내부 예외 대신 안정된 코드만 반환한다.
            result = ToolExecutionResult(
                status="error",
                result_text="tool 실행 중 오류가 발생했습니다.",
                error_code="tool_failed",
            )
        tool_elapsed_ms = round((time.perf_counter() - trace_started) * 1000, 1)
        trace = ToolCallTrace(
            tool_id=handler.definition.id,
            tool_name=handler.definition.display_name,
            arguments_summary=result.arguments_summary,
            status=result.status,
            elapsed_ms=tool_elapsed_ms,
            result_text=result.result_text,
            result_payload=result.result_payload,
            error_code=result.error_code,
        )
        observation_text = _limit_text(result.result_text, self.settings.playground_agent_result_chars)
        steps = [
            AgentStepTrace(
                step=1,
                kind="tool_call",
                title="tool 실행",
                detail=self._public_arguments_summary(arguments),
                action="tool_call",
                tool_id=handler.definition.id,
                tool_name=handler.definition.display_name,
            ),
            AgentStepTrace(
                step=1,
                kind="observation",
                title="tool 결과",
                detail=observation_text,
                action="tool_call",
                tool_id=handler.definition.id,
                tool_name=handler.definition.display_name,
                status=result.status,
                elapsed_ms=tool_elapsed_ms,
                error_code=result.error_code,
            ),
        ]
        if result.status != "ok":
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                provider_used=connection.provider,
                model_used=connection.model,
                assistant_message=result.result_text,
                active_skill_ids=[skill.id for skill in active_skills],
                tool_calls=[trace],
                agent_steps=steps if request.debug_trace else [],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                warnings=warnings,
                error_code=result.error_code or "tool_failed",
            )

        result_payload = result.result_payload or {}
        raw_hits = result_payload.get("hits")
        hits = raw_hits if isinstance(raw_hits, list) else []
        result_count = result_payload.get("result_count", len(hits))
        if not hits or result_count == 0:
            steps.append(
                AgentStepTrace(
                    step=1,
                    kind="final",
                    title="검색 근거 없음",
                    detail=result.result_text,
                    action="final_answer",
                    tool_id=handler.definition.id,
                    tool_name=handler.definition.display_name,
                )
            )
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                provider_used=connection.provider,
                model_used=connection.model,
                assistant_message=result.result_text,
                active_skill_ids=[skill.id for skill in active_skills],
                tool_calls=[trace],
                agent_steps=steps if request.debug_trace else [],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                warnings=warnings,
            )

        usage_calls: list[TokenUsage] = []
        debug_calls: list[LlmDebugCall] = []
        skill_instructions = self._skill_system_instructions(active_skills)
        try:
            synthesis = self._chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Answer the user's question in Korean using only the supplied local document evidence. "
                            "Cite the file name and section or location for every material claim. "
                            "If the evidence is insufficient, say so explicitly. "
                            "Never invent a document, location, or fact. Return only JSON: {\"answer\":\"...\"}."
                            f"\n\n{skill_instructions}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"question": request.message, "evidence": observation_text},
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=connection.model,
                base_url=connection.base_url,
                api_key=connection.api_key,
                max_tokens=500,
                purpose="rag_grounded_synthesis",
                provider=connection.provider,
                session_id=session_id,
                request_id=request_id,
                usage_calls=usage_calls,
                debug_calls=debug_calls if debug_allowed else None,
            )
            final_answer = str(synthesis.get("answer", "")).strip()
            if not final_answer:
                raise LlmResponseFormatError("RAG synthesis response has no answer")
        except LlmResponseFormatError:
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                provider_used=connection.provider,
                model_used=connection.model,
                assistant_message="LLM 응답을 근거 답변 JSON으로 해석하지 못했습니다.",
                active_skill_ids=[skill.id for skill in active_skills],
                tool_calls=[trace],
                agent_steps=steps if request.debug_trace else [],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                warnings=warnings,
                error_code="llm_response_invalid_json",
                debug=PlaygroundDebug(llm_calls=debug_calls) if debug_calls else None,
                token_usage=self._aggregate_token_usage(usage_calls, connection.provider, connection.model),
            )
        except Exception:  # noqa: BLE001 - LLM 호출 상세는 공개하지 않는다.
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                provider_used=connection.provider,
                model_used=connection.model,
                assistant_message="근거 답변을 생성하는 LLM 호출이 실패했습니다.",
                active_skill_ids=[skill.id for skill in active_skills],
                tool_calls=[trace],
                agent_steps=steps if request.debug_trace else [],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                warnings=warnings,
                error_code="local_llm_failed",
                debug=PlaygroundDebug(llm_calls=debug_calls) if debug_calls else None,
                token_usage=self._aggregate_token_usage(usage_calls, connection.provider, connection.model),
            )

        citations = self._rag_evidence_citations(hits)
        if citations:
            final_answer = f"{final_answer}\n\n근거 문서:\n" + "\n".join(f"- {citation}" for citation in citations)
        steps.append(
            AgentStepTrace(
                step=2,
                kind="final",
                title="근거 기반 최종 답변",
                detail=final_answer,
                action="final_answer",
            )
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        over_budget = elapsed_ms > self.settings.playground_agent_budget_ms
        if over_budget:
            warnings.append("playground_agent_over_budget")
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            provider_used=connection.provider,
            model_used=connection.model,
            assistant_message=final_answer,
            active_skill_ids=[skill.id for skill in active_skills],
            tool_calls=[trace],
            agent_steps=steps if request.debug_trace else [],
            elapsed_ms=elapsed_ms,
            warnings=warnings,
            over_budget=over_budget,
            debug=PlaygroundDebug(llm_calls=debug_calls) if debug_allowed else None,
            token_usage=self._aggregate_token_usage(usage_calls, connection.provider, connection.model),
        )

    def _rag_evidence_citations(self, hits: list[Any]) -> list[str]:
        """검색 payload에 실제 존재하는 hit만 근거 문서 목록으로 정규화한다."""

        citations: list[str] = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            file_name = str(hit.get("file_name", "")).strip()
            if not file_name:
                continue
            location = str(
                hit.get("section_path") or hit.get("location_label") or hit.get("location_type") or ""
            ).strip()
            citation = f"{file_name} / {location}" if location else file_name
            if citation not in citations:
                citations.append(citation)
        return citations

    def draft_tool(self, request: ToolDraftRequest, openai_api_key: str = "") -> ToolDraftResponse:
        connection = self._resolve_llm_connection(
            provider=request.provider,
            local_base_url=request.local_base_url,
            model=request.model,
            openai_api_key=openai_api_key,
        )
        model = connection.model
        base_url = connection.base_url
        if connection.error_code:
            return ToolDraftResponse(
                provider_used=connection.provider,
                model_used=model,
                error_code=connection.error_code,
                message=connection.error_message,
                warnings=["external_llm_provider:openai"] if connection.external else [],
            )
        if request.provider not in {"local", "openai"}:
            return ToolDraftResponse(
                model_used=model,
                error_code="unsupported_provider",
                message="Tool Lab 1차 버전은 local/on-prem LLM만 지원합니다.",
            )
        if not model:
            return ToolDraftResponse(
                model_used=model,
                error_code="local_llm_not_configured",
                message="로컬 LLM 모델이 설정되지 않았습니다.",
            )
        if connection.provider == "local" and not _is_internal_http_url(base_url):
            return ToolDraftResponse(
                model_used=model,
                error_code="local_llm_url_not_internal",
                message="로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
            )
        usage_calls: list[TokenUsage] = []
        prompt = (
            "Create a tool manifest draft for the automation_smb synthetic chatbot test environment. "
            "Return only JSON with keys: id, display_name, description, permission, inputs, test_notes.\n"
            f"Request: {request.instruction[:1000]}"
        )
        try:
            data = self._chat_json(
                [
                    {"role": "system", "content": "Return only compact JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                base_url=base_url,
                api_key=connection.api_key,
                max_tokens=500,
                purpose="tool_draft",
                provider=connection.provider,
                session_id=f"tool-draft-{uuid.uuid4()}",
                usage_calls=usage_calls,
            )
        except Exception:  # noqa: BLE001
            return ToolDraftResponse(
                provider_used=connection.provider,
                model_used=model,
                error_code="local_llm_failed",
                warnings=["external_llm_provider:openai"] if connection.external else [],
                message="로컬 LLM 호출에 실패했습니다.",
            )
        return ToolDraftResponse(
            provider_used=connection.provider,
            model_used=model,
            draft=data,
            warnings=["external_llm_provider:openai"] if connection.external else [],
            message="초안이 생성되었습니다. 아직 저장되거나 활성화되지 않습니다.",
            token_usage=self._aggregate_token_usage(usage_calls, connection.provider, model),
        )

    def check_llm(self, request: LlmStatusRequest, openai_api_key: str = "") -> LlmStatusResponse:
        """local LLM의 모델 목록과 JSON 응답 가능 여부를 확인한다."""
        started = time.perf_counter()
        connection = self._resolve_llm_connection(
            provider=request.provider,
            local_base_url=request.local_base_url,
            model=request.model,
            openai_api_key=openai_api_key,
        )
        model = connection.model
        base_url = connection.base_url
        if connection.error_code:
            return LlmStatusResponse(
                provider_used=connection.provider,
                base_url_used=base_url,
                model_used=model,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                error_code=connection.error_code,
                message=connection.error_message,
            )
        if request.provider not in {"local", "openai"}:
            return LlmStatusResponse(
                provider_used=connection.provider,
                base_url_used=base_url,
                model_used=model,
                error_code="unsupported_provider",
                message="Playground 1차 버전은 local/on-prem LLM만 지원합니다.",
            )
        if connection.provider == "local" and not _is_internal_http_url(base_url):
            return LlmStatusResponse(
                provider_used=connection.provider,
                base_url_used=base_url,
                model_used=model,
                error_code="local_llm_url_not_internal",
                message="로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
            )

        available_models: list[str] = []
        try:
            available_models = self._list_models(base_url, api_key=connection.api_key)
        except Exception:  # noqa: BLE001
            return LlmStatusResponse(
                provider_used=connection.provider,
                base_url_used=base_url,
                model_used=model,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                error_code="local_llm_models_failed",
                message="로컬 LLM 모델 목록을 가져오지 못했습니다.",
            )

        if not model:
            return LlmStatusResponse(
                provider_used=connection.provider,
                base_url_used=base_url,
                available_models=available_models,
                models_ok=True,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                error_code="local_llm_model_missing",
                message="모델 목록은 확인했지만 사용할 모델명이 비어 있습니다.",
            )

        usage_calls: list[TokenUsage] = []
        try:
            data = self._chat_json(
                [
                    {
                        "role": "system",
                        "content": "Return only JSON. Do not include reasoning or step-by-step explanation.",
                    },
                    {"role": "user", "content": "Return {\"ok\": true}."},
                ],
                model=model,
                base_url=base_url,
                api_key=connection.api_key,
                max_tokens=512,
                purpose="llm_status",
                provider=connection.provider,
                session_id=f"llm-status-{uuid.uuid4()}",
                usage_calls=usage_calls,
            )
        except Exception:  # noqa: BLE001
            return LlmStatusResponse(
                provider_used=connection.provider,
                base_url_used=base_url,
                model_used=model,
                available_models=available_models,
                models_ok=True,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                error_code="local_llm_chat_failed",
                message="모델 목록은 확인했지만 chat completions 호출에 실패했습니다.",
            )

        chat_ok = data.get("ok") is True
        return LlmStatusResponse(
            provider_used=connection.provider,
            base_url_used=base_url,
            model_used=model,
            available_models=available_models,
            models_ok=True,
            chat_ok=chat_ok,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            message="로컬 LLM 연결이 정상입니다." if chat_ok else "응답은 받았지만 JSON 확인값이 예상과 다릅니다.",
            error_code="" if chat_ok else "local_llm_unexpected_response",
            token_usage=self._aggregate_token_usage(usage_calls, connection.provider, model),
        )

    def _decide_next_action(
        self,
        *,
        request: ChatRequest,
        handlers: dict[str, ToolHandler],
        observations: list[dict[str, str]],
        model: str,
        base_url: str,
        api_key: str,
        step: int,
        provider: str = "local",
        session_id: str = "",
        request_id: str = "",
        usage_calls: list[TokenUsage] | None = None,
        debug_calls: list[LlmDebugCall] | None = None,
        active_skills: list[SkillDefinition] | None = None,
    ) -> AgentDecision:
        tool_specs = [
            {
                "id": handler.definition.id,
                "description": handler.definition.description,
                "inputs": handler.definition.input_schema,
                "permission": handler.definition.permission,
            }
            for handler in handlers.values()
        ]
        prompt_payload = {
            "message": request.message[:1000],
            "recent_history": self._recent_history(request.history),
            "selected_tools": tool_specs,
            "active_skill_ids": [skill.id for skill in active_skills or []],
            "observations": observations[-3:],
            "step": step,
            "limits": {
                "max_steps": max(1, self.settings.playground_agent_max_steps),
                "max_tool_calls": max(0, self.settings.playground_agent_max_tool_calls),
            },
        }
        system_prompt = (
            "You are a constrained Korean lab assistant agent. "
            "Return only JSON with keys: action, tool_calls, answer, question, rationale. "
            "action must be one of: tool_call, final_answer, clarify. "
            "Each tool_calls item must use exactly: {\"tool_id\":\"...\",\"arguments\":{...}}. "
            "Call at most one tool at a time and use only selected tool ids. "
            "Use final_answer when local tool results are enough. "
            "Do not invent paths or results. "
            "rationale must be a short public explanation, not hidden chain-of-thought."
        )
        skill_instructions = self._skill_system_instructions(active_skills or [])
        if skill_instructions:
            system_prompt = f"{system_prompt}\n\n{skill_instructions}"
        data = self._chat_json(
            [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ],
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_tokens=500,
            purpose=f"agent_decision_step_{step}",
            provider=provider,
            session_id=session_id,
            request_id=request_id,
            usage_calls=usage_calls,
            debug_calls=debug_calls,
        )
        return self._decision_from_json(data)

    def _skill_system_instructions(self, skills: list[SkillDefinition]) -> str:
        """선택된 SKILL.md 원문을 설정된 전체 글자 예산 안에서 system prompt에 주입한다."""

        if not skills:
            return ""
        remaining = max(0, self.settings.playground_skill_prompt_chars)
        sections: list[str] = [
            "The following user-selected SKILL.md documents are active. Follow each relevant workflow instruction."
        ]
        for skill in skills:
            block = f'<skill id="{skill.id}">\n{skill.document.strip()}\n</skill>'
            if len(block) > remaining:
                if remaining > 200:
                    sections.append(f"{block[:remaining]}\n[skill truncated]")
                break
            sections.append(block)
            remaining -= len(block)
        return "\n\n".join(sections)

    def _decision_from_json(self, data: dict[str, Any]) -> AgentDecision:
        action = str(data.get("action", "")).strip()
        if action == "tool":
            action = "tool_call"
        elif action == "final":
            action = "final_answer"
        if action not in {"tool_call", "final_answer", "clarify"}:
            action = "tool_call" if data.get("tool_calls") or data.get("tool_call") else "final_answer"

        raw_calls = data.get("tool_calls", [])
        if isinstance(data.get("tool_call"), dict):
            raw_calls = [data["tool_call"]]
        if not isinstance(raw_calls, list):
            raw_calls = []

        calls: list[PlannedToolCall] = []
        for item in raw_calls:
            if not isinstance(item, dict):
                continue
            tool_id = str(item.get("tool_id") or item.get("id") or item.get("name") or "").strip()
            arguments = item.get("arguments") or item.get("inputs") or item.get("input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            if tool_id:
                calls.append(PlannedToolCall(tool_id=tool_id, arguments=arguments))

        return AgentDecision(
            action=action,  # type: ignore[arg-type]
            tool_calls=calls,
            answer=str(data.get("answer", "") or "").strip(),
            question=str(data.get("question", "") or "").strip(),
            rationale=str(data.get("rationale", "") or "").strip(),
        )

    def _fallback_answer(self, traces: list[ToolCallTrace]) -> str:
        return "\n\n".join(trace.result_text for trace in traces if trace.result_text)

    def _recent_history(self, history: list[ChatMessage]) -> list[dict[str, str]]:
        max_messages = max(0, self.settings.playground_agent_context_messages)
        recent = history[-max_messages:] if max_messages else []
        return [
            {"role": item.role, "content": _limit_text(item.content, 500)}
            for item in recent
            if item.content.strip()
        ]

    def _public_arguments_summary(self, arguments: dict[str, Any]) -> str:
        safe = self._redact_text(json.dumps(arguments, ensure_ascii=False))
        return _limit_text(safe, 500)

    def _tool_call_key(self, planned_call: PlannedToolCall) -> str:
        arguments = json.dumps(planned_call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        return f"{planned_call.tool_id}:{arguments}"

    def _chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        base_url: str,
        max_tokens: int,
        api_key: str = "",
        purpose: str = "chat_json",
        provider: str = "local",
        session_id: str = "",
        request_id: str = "",
        usage_calls: list[TokenUsage] | None = None,
        debug_calls: list[LlmDebugCall] | None = None,
    ) -> dict[str, Any]:
        base_url = base_url.rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = self._headers_for(base_url, api_key=api_key)
        request_messages = _json_request_messages(messages, provider=provider, model=model)
        payload = {
            "model": model,
            "messages": request_messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if provider == "local" and "qwen3" in model.lower():
            payload["reasoning_effort"] = "none"
        started = time.perf_counter()
        raw_content = ""
        usage: TokenUsage | None = None

        def post_json() -> dict[str, Any]:
            timeout_s = max(0.1, self.settings.llm_timeout_ms / 1000)
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(url, headers=headers, json=payload)
                if "reasoning_effort" in payload and response.status_code in {400, 422}:
                    fallback_payload = dict(payload)
                    fallback_payload.pop("reasoning_effort", None)
                    response = client.post(url, headers=headers, json=fallback_payload)
                response.raise_for_status()
                return response.json()

        try:
            response_json = trace_openai_chat_completion(
                settings=self.settings,
                provider=provider,
                model=model,
                purpose=purpose,
                session_id=session_id,
                message_count=len(request_messages),
                messages=request_messages,
                call=post_json,
                usage_metadata=lambda body: self._langsmith_usage_metadata(body, provider, model),
                request_id=request_id,
            )
            usage = self._token_usage_from_response(response_json, provider, model)
            if usage is not None and usage_calls is not None:
                usage_calls.append(usage)
            raw_value = response_json["choices"][0]["message"].get("content")
            raw_content = raw_value if isinstance(raw_value, str) else ""
            if not raw_content.strip():
                raise LlmResponseFormatError("LLM response content is empty")
            try:
                data, repaired = _json_from_text_with_meta(raw_content)
            except (json.JSONDecodeError, TypeError) as exc:
                raise LlmResponseFormatError("LLM response content is not a JSON object") from exc
            if debug_calls is not None:
                debug_calls.append(
                    LlmDebugCall(
                        purpose=purpose,
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                        request_messages=self._safe_debug_messages(request_messages),
                        raw_response=self._safe_debug_text(raw_content),
                        parsed_json=self._safe_debug_json(data),
                        token_usage=usage,
                        json_repaired=repaired,
                    )
                )
            return data
        except Exception as exc:
            if debug_calls is not None:
                debug_calls.append(
                    LlmDebugCall(
                        purpose=purpose,
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                        request_messages=self._safe_debug_messages(request_messages),
                        raw_response=self._safe_debug_text(raw_content),
                        token_usage=usage,
                        error_code=exc.__class__.__name__,
                    )
                )
            raise

    def _token_usage_from_response(self, response_json: dict[str, Any], provider: str, model: str) -> TokenUsage | None:
        usage = response_json.get("usage")
        if not isinstance(usage, dict):
            return None

        prompt_tokens = self._usage_int(usage, "prompt_tokens", "input_tokens")
        completion_tokens = self._usage_int(usage, "completion_tokens", "output_tokens")
        total_tokens = self._usage_int(usage, "total_tokens")
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens

        if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
            return None
        return TokenUsage(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            calls=1,
        )

    def _langsmith_usage_metadata(self, response_json: dict[str, Any], provider: str, model: str) -> dict[str, int]:
        usage = self._token_usage_from_response(response_json, provider, model)
        if usage is None:
            return {}
        return {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }

    def _aggregate_token_usage(
        self, usage_calls: list[TokenUsage], provider: str, model: str
    ) -> TokenUsage | None:
        if not usage_calls:
            return None
        return TokenUsage(
            provider=provider,
            model=model,
            prompt_tokens=sum(item.prompt_tokens for item in usage_calls),
            completion_tokens=sum(item.completion_tokens for item in usage_calls),
            total_tokens=sum(item.total_tokens for item in usage_calls),
            calls=sum(item.calls for item in usage_calls),
        )

    def _usage_int(self, usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return max(0, value)
            if isinstance(value, float):
                return max(0, int(value))
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return 0

    def _list_models(self, base_url: str, api_key: str = "") -> list[str]:
        url = f"{base_url.rstrip('/')}/models"
        headers = self._headers_for(base_url, api_key=api_key)
        timeout_s = max(0.1, self.settings.llm_timeout_ms / 1000)
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
        data = response.json()
        models = data.get("data", [])
        return [str(item.get("id", "")).strip() for item in models if isinstance(item, dict) and item.get("id")]

    def _local_base_url(self, override: str = "") -> str:
        return (override or self.settings.llm_base_url).strip().rstrip("/")

    def _local_api_key(self, base_url: str) -> str:
        configured_base_url = self.settings.llm_base_url.strip().rstrip("/")
        if self.settings.llm_api_key and base_url.rstrip("/") == configured_base_url:
            return self.settings.llm_api_key
        return ""

    def _headers_for(self, base_url: str, api_key: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _safe_debug_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        return [
            {
                "role": str(item.get("role", "")),
                "content": self._safe_debug_text(str(item.get("content", ""))),
            }
            for item in messages
        ]

    def _safe_debug_json(self, data: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(data, ensure_ascii=False, default=str)
        safe = self._safe_debug_text(text)
        try:
            parsed = json.loads(safe)
        except json.JSONDecodeError:
            return {"preview": safe}
        return parsed if isinstance(parsed, dict) else {"preview": safe}

    def _safe_debug_text(self, value: str) -> str:
        return _limit_text(self._redact_text(value), self.settings.playground_debug_preview_chars)

    def _redact_text(self, value: str) -> str:
        redacted = value
        secret_values = [
            self.settings.smb_password,
            self.settings.llm_api_key,
            self.settings.openai_api_key,
            self.settings.admin_api_token,
        ]
        for secret in secret_values:
            if secret and len(secret) >= 3:
                redacted = redacted.replace(secret, "[SECRET_REDACTED]")
        if self.settings.smb_host and len(self.settings.smb_host) >= 3:
            redacted = redacted.replace(self.settings.smb_host, "[SMB_HOST_REDACTED]")
        if self.settings.smb_share_name and len(self.settings.smb_share_name) >= 3:
            redacted = redacted.replace(self.settings.smb_share_name, "[SMB_SHARE_REDACTED]")
        redacted = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[USERINFO_REDACTED]@", redacted)
        redacted = re.sub(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+", r"\1 [TOKEN_REDACTED]", redacted)
        redacted = re.sub(r"(?i)(api[_-]?key|token|password)(['\"\s:=]+)([^,'\"\s}]+)", r"\1\2[SECRET_REDACTED]", redacted)
        redacted = re.sub(r"\\\\[^\\\s]+\\[^\\\s]+", r"[UNC_PATH_REDACTED]", redacted)
        return redacted

    def _elapsed_ms(self, started: float) -> float:
        return (time.perf_counter() - started) * 1000

    def _error_response(
        self,
        session_id: str,
        started: float,
        model: str,
        code: str,
        message: str,
        *,
        warnings: list[str] | None = None,
        agent_steps: list[AgentStepTrace] | None = None,
        debug_calls: list[LlmDebugCall] | None = None,
        provider: str = "local",
        token_usage: TokenUsage | None = None,
        request_id: str = "",
    ) -> ChatResponse:
        return ChatResponse(
            request_id=request_id or str(uuid.uuid4()),
            session_id=session_id,
            provider_used=provider,
            model_used=model,
            assistant_message=message,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            warnings=warnings or [],
            error_code=code,
            agent_steps=agent_steps or [],
            debug=PlaygroundDebug(llm_calls=debug_calls) if debug_calls else None,
            token_usage=token_usage,
        )
