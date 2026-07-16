"""Playground LLM 호출의 선택적 LangSmith 추적 유틸리티."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from smb_finder.config import Settings


def _hide_langsmith_inputs(_: dict[str, Any]) -> dict[str, Any]:
    return {}


def _hide_langsmith_outputs_preserve_usage(outputs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(outputs, dict):
        return {}
    usage = outputs.get("usage_metadata")
    return {"usage_metadata": usage} if isinstance(usage, dict) else {}


def _langsmith_trace_inputs(
    settings: Settings,
    *,
    model: str,
    purpose: str,
    message_count: int,
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """설정에 따라 LangSmith에 기록할 LLM 입력을 만든다."""

    inputs: dict[str, Any] = {"model": model, "purpose": purpose, "message_count": message_count}
    if not settings.langsmith_hide_inputs:
        inputs["messages"] = messages
    return inputs


def _langsmith_trace_outputs(settings: Settings, outputs: dict[str, Any]) -> dict[str, Any]:
    """설정에 따라 LangSmith에 기록할 LLM 출력을 만든다."""

    if settings.langsmith_hide_outputs:
        return _hide_langsmith_outputs_preserve_usage(outputs)
    return outputs


def _langsmith_trace_metadata(*, model: str, purpose: str, session_id: str, request_id: str, message_count: int) -> dict[str, Any]:
    """Playground 요청과 개별 LLM run을 연결할 공개 메타데이터를 만든다."""

    return {
        "ls_provider": "openai",
        "ls_model_name": model,
        "purpose": purpose,
        "request_id": request_id,
        "session_id": session_id,
        "message_count": message_count,
    }


def is_langsmith_tracing_enabled(settings: Settings, provider: str) -> bool:
    """OpenAI provider에 대해서만 LangSmith 추적을 opt-in으로 켠다."""
    return provider == "openai" and settings.langsmith_tracing and bool(settings.langsmith_api_key.strip())


def trace_openai_chat_completion(
    *,
    settings: Settings,
    provider: str,
    model: str,
    purpose: str,
    session_id: str,
    message_count: int,
    messages: list[dict[str, str]],
    call: Callable[[], dict[str, Any]],
    usage_metadata: Callable[[dict[str, Any]], dict[str, int]],
    request_id: str = "",
) -> dict[str, Any]:
    """OpenAI 호환 chat completion 호출을 LangSmith LLM run으로 감싼다.

    입력/출력 본문 공개 여부는 LANGSMITH_HIDE_INPUTS/OUTPUTS 설정을 따른다.
    합성 테스트 기본값은 false라 전체 LLM call을 바로 확인할 수 있다.
    """
    if not is_langsmith_tracing_enabled(settings, provider):
        return call()

    try:
        from langsmith import Client, get_current_run_tree, traceable, tracing_context
    except ImportError:
        return call()

    client_kwargs: dict[str, Any] = {}
    if settings.langsmith_hide_inputs:
        client_kwargs["hide_inputs"] = _hide_langsmith_inputs
    if settings.langsmith_hide_outputs:
        client_kwargs["hide_outputs"] = _hide_langsmith_outputs_preserve_usage
    api_key = settings.langsmith_api_key.strip()
    endpoint = settings.langsmith_endpoint.strip()
    if api_key:
        client_kwargs["api_key"] = api_key
    if endpoint:
        client_kwargs["api_url"] = endpoint

    try:
        langsmith_client = Client(**client_kwargs)
    except Exception:  # noqa: BLE001 - 추적 실패가 서비스 호출 실패로 번지면 안 된다.
        return call()

    captured: dict[str, Any] = {}
    metadata = _langsmith_trace_metadata(
        model=model,
        purpose=purpose,
        session_id=session_id,
        request_id=request_id,
        message_count=message_count,
    )

    def process_inputs(_: dict[str, Any]) -> dict[str, Any]:
        return _langsmith_trace_inputs(
            settings,
            model=model,
            purpose=purpose,
            message_count=message_count,
            messages=messages,
        )

    def process_outputs(output: dict[str, Any]) -> dict[str, Any]:
        return _langsmith_trace_outputs(settings, output)

    def invoke(_: dict[str, Any]) -> dict[str, Any]:
        response_json = call()
        captured["response"] = response_json
        usage = usage_metadata(response_json)
        run = get_current_run_tree()
        if run is not None and usage:
            run.set(usage_metadata=usage)
        traced_output: dict[str, Any] = {"usage_metadata": usage}
        if not settings.langsmith_hide_outputs:
            traced_output["response"] = response_json
        return traced_output

    trace_options: dict[str, Any] = {
        "name": "playground.openai_chat_completion",
        "run_type": "llm",
        "metadata": metadata,
        "tags": ["automation_smb", "playground", "openai"],
        "client": langsmith_client,
        "process_inputs": process_inputs,
        "process_outputs": process_outputs,
    }
    try:
        invoke_traced = traceable(**trace_options)(invoke)
    except TypeError:
        trace_options.pop("process_inputs", None)
        trace_options.pop("process_outputs", None)
        invoke_traced = traceable(**trace_options)(invoke)

    try:
        with tracing_context(enabled=True, project_name=settings.langsmith_project.strip() or None):
            invoke_traced(process_inputs({}))
    except TypeError:
        if "response" in captured:
            return captured["response"]
        try:
            with tracing_context(enabled=True):
                invoke_traced(process_inputs({}))
        except Exception:  # noqa: BLE001
            if "response" in captured:
                return captured["response"]
            return call()
    except Exception:  # noqa: BLE001
        if "response" in captured:
            return captured["response"]
        return call()

    return captured.get("response") or call()
