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
    call: Callable[[], dict[str, Any]],
    usage_metadata: Callable[[dict[str, Any]], dict[str, int]],
) -> dict[str, Any]:
    """OpenAI 호환 chat completion 호출을 LangSmith LLM run으로 감싼다.

    입력/출력 본문은 LangSmith client 레벨에서 숨긴다. 모델명, provider, session_id,
    purpose, token usage만 남겨 모델별 사용량/비용 집계를 가능하게 한다.
    """
    if not is_langsmith_tracing_enabled(settings, provider):
        return call()

    try:
        from langsmith import Client, get_current_run_tree, traceable, tracing_context
    except ImportError:
        return call()

    client_kwargs: dict[str, Any] = {
        "hide_inputs": _hide_langsmith_inputs,
        "hide_outputs": _hide_langsmith_outputs_preserve_usage,
    }
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
    metadata = {
        "ls_provider": "openai",
        "ls_model_name": model,
        "purpose": purpose,
        "session_id": session_id,
        "message_count": message_count,
    }

    def process_inputs(_: dict[str, Any]) -> dict[str, Any]:
        return {"model": model, "purpose": purpose, "message_count": message_count}

    def process_outputs(output: dict[str, Any]) -> dict[str, Any]:
        return {"usage_metadata": output.get("usage_metadata", {})}

    def invoke(_: dict[str, Any]) -> dict[str, Any]:
        response_json = call()
        captured["response"] = response_json
        usage = usage_metadata(response_json)
        run = get_current_run_tree()
        if run is not None and usage:
            run.set(usage_metadata=usage)
        return {"usage_metadata": usage}

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
            invoke_traced({"model": model, "purpose": purpose})
    except TypeError:
        if "response" in captured:
            return captured["response"]
        try:
            with tracing_context(enabled=True):
                invoke_traced({"model": model, "purpose": purpose})
        except Exception:  # noqa: BLE001
            if "response" in captured:
                return captured["response"]
            return call()
    except Exception:  # noqa: BLE001
        if "response" in captured:
            return captured["response"]
        return call()

    return captured.get("response") or call()
