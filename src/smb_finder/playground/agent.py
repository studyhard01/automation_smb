"""Local LLM 기반 Playground agent."""

from __future__ import annotations

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
    ToolCallTrace,
    ToolDraftRequest,
    ToolDraftResponse,
)
from .tools import ToolHandler


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

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, request: ChatRequest, registry: dict[str, ToolHandler]) -> ChatResponse:
        started = time.perf_counter()
        session_id = request.session_id or f"pg-{uuid.uuid4()}"
        model = (request.model or self.settings.llm_model).strip()
        base_url = self._local_base_url(request.local_base_url)
        warnings: list[str] = []
        all_steps: list[AgentStepTrace] = []
        traces: list[ToolCallTrace] = []
        debug_calls: list[LlmDebugCall] = []
        debug_allowed = request.debug_raw_llm and self.settings.playground_debug_raw_llm
        if request.debug_raw_llm and not self.settings.playground_debug_raw_llm:
            warnings.append("debug_raw_llm_denied")

        if request.provider != "local":
            return self._error_response(
                session_id,
                started,
                model,
                "unsupported_provider",
                "Playground 1차 버전은 local/on-prem LLM만 지원합니다.",
                warnings=warnings,
            )
        if not model:
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_not_configured",
                "로컬 LLM 모델이 설정되지 않았습니다. LLM_MODEL 또는 화면의 모델명을 입력하세요.",
                warnings=warnings,
            )
        if not _is_internal_http_url(base_url):
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_url_not_internal",
                "로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
                warnings=warnings,
            )

        selected = [tool_id for tool_id in request.selected_tool_ids if tool_id in registry]
        unknown = sorted(set(request.selected_tool_ids) - set(registry))
        if unknown:
            warnings.append(f"unknown_tools_ignored:{','.join(unknown)}")
        handlers = {tool_id: registry[tool_id] for tool_id in selected if registry[tool_id].definition.enabled}
        disabled = [tool_id for tool_id in selected if not registry[tool_id].definition.enabled]
        if disabled:
            warnings.append(f"disabled_tools_ignored:{','.join(disabled)}")

        observations: list[dict[str, str]] = []
        final_answer = ""
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
                    step=step,
                    debug_calls=debug_calls if debug_allowed else None,
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
                    result = handler.run(planned_call.arguments)
                    status = result.status
                    result_text = result.result_text
                    error_code = result.error_code
                    arguments_summary = result.arguments_summary
                except Exception:  # noqa: BLE001
                    status = "error"
                    result_text = "tool 실행 중 오류가 발생했습니다."
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
            session_id=session_id,
            provider_used="local",
            model_used=model,
            assistant_message=final_answer,
            tool_calls=traces,
            agent_steps=all_steps if request.debug_trace else [],
            elapsed_ms=elapsed_ms,
            warnings=warnings,
            over_budget=over_budget,
            debug=PlaygroundDebug(llm_calls=debug_calls) if debug_allowed else None,
        )

    def draft_tool(self, request: ToolDraftRequest) -> ToolDraftResponse:
        model = (request.model or self.settings.llm_model).strip()
        base_url = self._local_base_url(request.local_base_url)
        if request.provider != "local":
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
        if not _is_internal_http_url(base_url):
            return ToolDraftResponse(
                model_used=model,
                error_code="local_llm_url_not_internal",
                message="로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
            )
        prompt = (
            "Create a safe tool manifest draft for automation_smb. "
            "Return only JSON with keys: id, display_name, description, permission, inputs, safety_notes. "
            "Do not include credentials, internal IPs, patient data, or executable code.\n"
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
                max_tokens=500,
                purpose="tool_draft",
            )
        except Exception:  # noqa: BLE001
            return ToolDraftResponse(
                model_used=model,
                error_code="local_llm_failed",
                message="로컬 LLM 호출에 실패했습니다.",
            )
        return ToolDraftResponse(
            model_used=model,
            draft=data,
            message="초안이 생성되었습니다. 아직 저장되거나 활성화되지 않습니다.",
        )

    def check_llm(self, request: LlmStatusRequest) -> LlmStatusResponse:
        """local LLM의 모델 목록과 JSON 응답 가능 여부를 확인한다."""
        started = time.perf_counter()
        model = (request.model or self.settings.llm_model).strip()
        base_url = self._local_base_url(request.local_base_url)
        if request.provider != "local":
            return LlmStatusResponse(
                base_url_used=base_url,
                model_used=model,
                error_code="unsupported_provider",
                message="Playground 1차 버전은 local/on-prem LLM만 지원합니다.",
            )
        if not _is_internal_http_url(base_url):
            return LlmStatusResponse(
                base_url_used=base_url,
                model_used=model,
                error_code="local_llm_url_not_internal",
                message="로컬 LLM 주소는 localhost 또는 사내망 주소만 허용합니다.",
            )

        available_models: list[str] = []
        try:
            available_models = self._list_models(base_url)
        except Exception:  # noqa: BLE001
            return LlmStatusResponse(
                base_url_used=base_url,
                model_used=model,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                error_code="local_llm_models_failed",
                message="로컬 LLM 모델 목록을 가져오지 못했습니다.",
            )

        if not model:
            return LlmStatusResponse(
                base_url_used=base_url,
                available_models=available_models,
                models_ok=True,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                error_code="local_llm_model_missing",
                message="모델 목록은 확인했지만 사용할 모델명이 비어 있습니다.",
            )

        try:
            data = self._chat_json(
                [
                    {"role": "system", "content": "Return only JSON."},
                    {"role": "user", "content": "Return {\"ok\": true}."},
                ],
                model=model,
                base_url=base_url,
                max_tokens=40,
                purpose="llm_status",
            )
        except Exception:  # noqa: BLE001
            return LlmStatusResponse(
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
            base_url_used=base_url,
            model_used=model,
            available_models=available_models,
            models_ok=True,
            chat_ok=chat_ok,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            message="로컬 LLM 연결이 정상입니다." if chat_ok else "응답은 받았지만 JSON 확인값이 예상과 다릅니다.",
            error_code="" if chat_ok else "local_llm_unexpected_response",
        )

    def _decide_next_action(
        self,
        *,
        request: ChatRequest,
        handlers: dict[str, ToolHandler],
        observations: list[dict[str, str]],
        model: str,
        base_url: str,
        step: int,
        debug_calls: list[LlmDebugCall] | None = None,
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
            "observations": observations[-3:],
            "step": step,
            "limits": {
                "max_steps": max(1, self.settings.playground_agent_max_steps),
                "max_tool_calls": max(0, self.settings.playground_agent_max_tool_calls),
            },
        }
        data = self._chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a constrained Korean lab assistant agent. "
                        "Return only JSON with keys: action, tool_calls, answer, question, rationale. "
                        "action must be one of: tool_call, final_answer, clarify. "
                        "Each tool_calls item must use exactly: {\"tool_id\":\"...\",\"arguments\":{...}}. "
                        "Call at most one tool at a time and use only selected tool ids. "
                        "Use final_answer when local tool results are enough. "
                        "Do not invent paths or results. "
                        "rationale must be a short public explanation, not hidden chain-of-thought."
                    ),
                },
                {"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)},
            ],
            model=model,
            base_url=base_url,
            max_tokens=500,
            purpose=f"agent_decision_step_{step}",
            debug_calls=debug_calls,
        )
        return self._decision_from_json(data)

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
        purpose: str = "chat_json",
        debug_calls: list[LlmDebugCall] | None = None,
    ) -> dict[str, Any]:
        base_url = base_url.rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = self._headers_for(base_url)
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        raw_content = ""
        try:
            timeout_s = max(0.1, self.settings.llm_timeout_ms / 1000)
            with httpx.Client(timeout=timeout_s) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
            raw_content = response.json()["choices"][0]["message"]["content"]
            data, repaired = _json_from_text_with_meta(raw_content)
            if debug_calls is not None:
                debug_calls.append(
                    LlmDebugCall(
                        purpose=purpose,
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                        request_messages=self._safe_debug_messages(messages),
                        raw_response=self._safe_debug_text(raw_content),
                        parsed_json=self._safe_debug_json(data),
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
                        request_messages=self._safe_debug_messages(messages),
                        raw_response=self._safe_debug_text(raw_content),
                        error_code=exc.__class__.__name__,
                    )
                )
            raise

    def _list_models(self, base_url: str) -> list[str]:
        url = f"{base_url.rstrip('/')}/models"
        headers = self._headers_for(base_url)
        timeout_s = max(0.1, self.settings.llm_timeout_ms / 1000)
        with httpx.Client(timeout=timeout_s) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
        data = response.json()
        models = data.get("data", [])
        return [str(item.get("id", "")).strip() for item in models if isinstance(item, dict) and item.get("id")]

    def _local_base_url(self, override: str = "") -> str:
        return (override or self.settings.llm_base_url).strip().rstrip("/")

    def _headers_for(self, base_url: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        configured_base_url = self.settings.llm_base_url.strip().rstrip("/")
        if self.settings.llm_api_key and base_url.rstrip("/") == configured_base_url:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
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
    ) -> ChatResponse:
        return ChatResponse(
            session_id=session_id,
            provider_used="local",
            model_used=model,
            assistant_message=message,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            warnings=warnings or [],
            error_code=code,
            agent_steps=agent_steps or [],
            debug=PlaygroundDebug(llm_calls=debug_calls) if debug_calls else None,
        )
