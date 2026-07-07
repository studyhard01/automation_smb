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

from .models import ChatRequest, ChatResponse, PlannedToolCall, ToolCallTrace, ToolDraftRequest, ToolDraftResponse
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
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


class PlaygroundAgent:
    """선택된 tool만 사용할 수 있는 단순 tool-use agent."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, request: ChatRequest, registry: dict[str, ToolHandler]) -> ChatResponse:
        started = time.perf_counter()
        session_id = request.session_id or f"pg-{uuid.uuid4()}"
        model = (request.model or self.settings.llm_model).strip()
        warnings: list[str] = []

        if request.provider != "local":
            return self._error_response(
                session_id,
                started,
                model,
                "unsupported_provider",
                "Playground 1차 버전은 local/on-prem LLM만 지원합니다.",
            )
        if not model:
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_not_configured",
                "로컬 LLM 모델이 설정되지 않았습니다. LLM_MODEL 또는 화면의 모델명을 입력하세요.",
            )
        if not _is_internal_http_url(self.settings.llm_base_url):
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_url_not_internal",
                "로컬 LLM 주소는 localhost 또는 사내망 주소만 허용됩니다.",
            )

        selected = [tool_id for tool_id in request.selected_tool_ids if tool_id in registry]
        unknown = sorted(set(request.selected_tool_ids) - set(registry))
        if unknown:
            warnings.append(f"unknown_tools_ignored:{','.join(unknown)}")
        handlers = {tool_id: registry[tool_id] for tool_id in selected if registry[tool_id].definition.enabled}
        disabled = [tool_id for tool_id in selected if not registry[tool_id].definition.enabled]
        if disabled:
            warnings.append(f"disabled_tools_ignored:{','.join(disabled)}")

        try:
            planned = self._plan_tool_calls(request.message, handlers, model)
        except Exception:  # noqa: BLE001 - UI에는 안전한 오류 코드만 반환한다.
            return self._error_response(
                session_id,
                started,
                model,
                "local_llm_failed",
                "로컬 LLM 호출에 실패했습니다. 모델 서버와 LLM_BASE_URL을 확인하세요.",
                warnings=warnings,
            )

        traces: list[ToolCallTrace] = []
        for planned_call in planned[:2]:
            handler = handlers.get(planned_call.tool_id)
            if handler is None:
                warnings.append(f"unselected_tool_blocked:{planned_call.tool_id}")
                continue
            trace_started = time.perf_counter()
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
            traces.append(
                ToolCallTrace(
                    tool_id=handler.definition.id,
                    tool_name=handler.definition.display_name,
                    arguments_summary=arguments_summary,
                    status=status,
                    elapsed_ms=round((time.perf_counter() - trace_started) * 1000, 1),
                    result_text=result_text,
                    error_code=error_code,
                )
            )

        assistant_message = self._compose_answer(request.message, traces, model, warnings)
        return ChatResponse(
            session_id=session_id,
            provider_used="local",
            model_used=model,
            assistant_message=assistant_message,
            tool_calls=traces,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            warnings=warnings,
        )

    def draft_tool(self, request: ToolDraftRequest) -> ToolDraftResponse:
        model = (request.model or self.settings.llm_model).strip()
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
        prompt = (
            "Create a safe tool manifest draft for automation_smb. "
            "Return only JSON with keys: id, display_name, description, permission, inputs, safety_notes. "
            "Do not include credentials, internal IPs, patient data, or executable code.\n"
            f"Request: {request.instruction[:1000]}"
        )
        try:
            data = self._chat_json([
                {"role": "system", "content": "Return only compact JSON."},
                {"role": "user", "content": prompt},
            ], model=model, max_tokens=500)
        except Exception:  # noqa: BLE001
            return ToolDraftResponse(
                model_used=model,
                error_code="local_llm_failed",
                message="로컬 LLM 호출에 실패했습니다.",
            )
        return ToolDraftResponse(
            model_used=model,
            draft=data,
            message="초안이 생성되었습니다. 아직 저장되거나 활성화되지 않았습니다.",
        )

    def _plan_tool_calls(
        self,
        message: str,
        handlers: dict[str, ToolHandler],
        model: str,
    ) -> list[PlannedToolCall]:
        tool_specs = [
            {
                "id": handler.definition.id,
                "description": handler.definition.description,
                "inputs": handler.definition.input_schema,
            }
            for handler in handlers.values()
        ]
        data = self._chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "You route a Korean lab assistant chat message to selected tools. "
                        "Return only JSON: {\"tool_calls\":[{\"tool_id\":\"...\",\"arguments\":{...}}]}. "
                        "Use only provided tool ids. If no tool is needed, return {\"tool_calls\":[]}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message[:1000],
                            "selected_tools": tool_specs,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            model=model,
            max_tokens=300,
        )
        calls = data.get("tool_calls", [])
        if not isinstance(calls, list):
            return []
        planned: list[PlannedToolCall] = []
        for item in calls:
            if not isinstance(item, dict):
                continue
            tool_id = str(item.get("tool_id", "")).strip()
            arguments = item.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}
            if tool_id:
                planned.append(PlannedToolCall(tool_id=tool_id, arguments=arguments))
        return planned

    def _compose_answer(
        self,
        message: str,
        traces: list[ToolCallTrace],
        model: str,
        warnings: list[str],
    ) -> str:
        if not traces:
            try:
                data = self._chat_json(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Answer in Korean. If no selected tool is useful, ask the user to select a tool "
                                "or clarify the request. Return JSON: {\"answer\":\"...\"}."
                            ),
                        },
                        {"role": "user", "content": message[:1000]},
                    ],
                    model=model,
                    max_tokens=300,
                )
                answer = str(data.get("answer", "")).strip()
                if answer:
                    return answer
            except Exception:  # noqa: BLE001
                warnings.append("answer_llm_failed")
            return "선택된 tool로 실행할 작업을 찾지 못했습니다. 왼쪽에서 사용할 tool을 선택하고 다시 요청하세요."

        tool_results = [
            {
                "tool": trace.tool_id,
                "status": trace.status,
                "result": trace.result_text[:2000],
            }
            for trace in traces
        ]
        try:
            data = self._chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Answer in Korean using only the provided local tool results. "
                            "Do not invent paths or results. Return JSON: {\"answer\":\"...\"}."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"message": message[:1000], "tool_results": tool_results},
                            ensure_ascii=False,
                        ),
                    },
                ],
                model=model,
                max_tokens=500,
            )
            answer = str(data.get("answer", "")).strip()
            if answer:
                return answer
        except Exception:  # noqa: BLE001
            warnings.append("answer_llm_failed")
        return "\n\n".join(trace.result_text for trace in traces if trace.result_text)

    def _chat_json(self, messages: list[dict[str, str]], *, model: str, max_tokens: int) -> dict[str, Any]:
        base_url = self.settings.llm_base_url.rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        timeout_s = max(0.1, self.settings.llm_timeout_ms / 1000)
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return _json_from_text(content)

    def _error_response(
        self,
        session_id: str,
        started: float,
        model: str,
        code: str,
        message: str,
        *,
        warnings: list[str] | None = None,
    ) -> ChatResponse:
        return ChatResponse(
            session_id=session_id,
            provider_used="local",
            model_used=model,
            assistant_message=message,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            warnings=warnings or [],
            error_code=code,
        )
