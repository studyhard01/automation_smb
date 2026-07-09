"""사용자 요청을 Langflow 워크플로우 템플릿으로 매핑한다."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .models import DEFAULT_SERVICE_URL, LlmProvider, WorkflowSpec, is_internal_http_url

_FOLDER_SEARCH_HINTS = (
    "공유",
    "폴더",
    "찾",
    "검색",
    "경로",
    "smb",
    "folder",
    "find",
    "search",
    "locate",
    "path",
)
_UNSUPPORTED_TEMPLATE_HINTS = (
    "내용",
    "본문",
    "파일",
    "db화",
    "디비화",
    "인덱",
    "index",
    "content",
    "file",
)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _mask_sensitive_text(text: str) -> str:
    """LLM 판별에 보낼 수 있는 짧은 의도 문장으로 줄이고 경로/비밀 후보를 가린다."""
    masked = text[:500]
    patterns = [
        (r"\\\\[^\s]+", "[UNC_PATH]"),
        (r"\b[A-Za-z]:\\[^\s]+", "[LOCAL_PATH]"),
        (r"(?i)\bhttps?://[^\s]+", "[URL]"),
        (r"(?<!\w)/(?:[^\s/]+/)+[^\s]+", "[PATH]"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP_ADDRESS]"),
        (r"(?i)\b(SMB_PASSWORD|OPENAI_API_KEY|ADMIN_API_TOKEN|LLM_API_KEY)\s*=\s*\S+", r"\1=[REDACTED]"),
        (r"(?i)\b[\w-]*(password|token|secret|api[_-]?key)[\w-]*\s*[:=]\s*\S+", "[SECRET]"),
    ]
    for pattern, replacement in patterns:
        masked = re.sub(pattern, replacement, masked)
    return masked


def _workflow_intent_summary(text: str) -> str:
    """LLM에는 원문 대신 템플릿 판별에 필요한 비민감 feature만 보낸다."""
    normalized = text.lower()
    matched_folder_hints = sorted({hint for hint in _FOLDER_SEARCH_HINTS if hint in normalized})
    matched_unsupported_hints = sorted({hint for hint in _UNSUPPORTED_TEMPLATE_HINTS if hint in normalized})
    return json.dumps(
        {
            "has_folder_search_hint": bool(matched_folder_hints),
            "has_unsupported_template_hint": bool(matched_unsupported_hints),
            "matched_folder_hints": matched_folder_hints,
            "matched_unsupported_hints": matched_unsupported_hints,
            "char_len": min(len(text), 500),
        },
        ensure_ascii=False,
    )


class WorkflowPlanner:
    """규칙 기반을 기본으로, 설정된 경우에만 LLM으로 템플릿 선택을 보조한다."""

    def plan(
        self,
        instruction: str,
        *,
        service_url: str = DEFAULT_SERVICE_URL,
        limit: int = 5,
        timeout_ms: int = 1500,
        llm_provider: LlmProvider = "auto",
        local_base_url: str | None = None,
        local_model: str | None = None,
        openai_model: str | None = None,
    ) -> WorkflowSpec:
        """사용자 문장을 워크플로우 스펙으로 변환한다.

        `auto`는 로컬 LLM 설정이 있을 때만 로컬 호출을 시도하고, 없거나 실패하면 규칙 기반으로 즉시
        폴백한다. OpenAI는 `llm_provider="openai"`와 `LANGFLOW_COMPOSER_ALLOW_OPENAI=true`가 모두
        설정된 경우에만 호출한다.
        """
        provider = self._normalize_provider(llm_provider)
        rule_spec = self._rule_plan(
            instruction,
            service_url=service_url,
            limit=limit,
            timeout_ms=timeout_ms,
        )
        if provider == "rule":
            return rule_spec

        if provider == "auto":
            local_base_url = local_base_url or os.getenv("LANGFLOW_COMPOSER_LOCAL_BASE_URL", "").strip()
            local_model = local_model or os.getenv("LANGFLOW_COMPOSER_LOCAL_MODEL", "").strip()
            if local_base_url and local_model and rule_spec.confidence < 0.7:
                if not is_internal_http_url(local_base_url):
                    rule_spec.warnings.append("local_llm_url_not_internal")
                    return rule_spec
                return self._plan_with_llm(
                    instruction,
                    rule_spec=rule_spec,
                    provider="local",
                    base_url=local_base_url,
                    model=local_model,
                    api_key=os.getenv("LANGFLOW_COMPOSER_LOCAL_API_KEY", "").strip(),
                )
            return rule_spec

        if provider == "local":
            local_base_url = local_base_url or os.getenv("LANGFLOW_COMPOSER_LOCAL_BASE_URL", "").strip()
            local_model = local_model or os.getenv("LANGFLOW_COMPOSER_LOCAL_MODEL", "").strip()
            if not (local_base_url and local_model):
                rule_spec.warnings.append("local_llm_not_configured")
                return rule_spec
            if not is_internal_http_url(local_base_url):
                rule_spec.warnings.append("local_llm_url_not_internal")
                return rule_spec
            return self._plan_with_llm(
                instruction,
                rule_spec=rule_spec,
                provider="local",
                base_url=local_base_url,
                model=local_model,
                api_key=os.getenv("LANGFLOW_COMPOSER_LOCAL_API_KEY", "").strip(),
            )

        if provider == "openai":
            if not _env_bool("LANGFLOW_COMPOSER_ALLOW_OPENAI"):
                rule_spec.warnings.append("openai_not_allowed")
                return rule_spec
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                rule_spec.warnings.append("openai_api_key_missing")
                return rule_spec
            model = openai_model or os.getenv("LANGFLOW_COMPOSER_OPENAI_MODEL", "gpt-4.1-mini").strip()
            return self._plan_with_llm(
                instruction,
                rule_spec=rule_spec,
                provider="openai",
                base_url="https://api.openai.com/v1",
                model=model,
                api_key=api_key,
            )

        return rule_spec

    def _normalize_provider(self, value: str) -> LlmProvider:
        provider = (value or "auto").strip().lower()
        if provider not in {"auto", "rule", "local", "openai"}:
            return "auto"
        return provider  # type: ignore[return-value]

    def _rule_plan(self, instruction: str, *, service_url: str, limit: int, timeout_ms: int) -> WorkflowSpec:
        normalized = instruction.lower()
        has_folder_search = any(hint in normalized for hint in _FOLDER_SEARCH_HINTS)
        has_unsupported = any(hint in normalized for hint in _UNSUPPORTED_TEMPLATE_HINTS)
        confidence = 0.78 if has_folder_search and not has_unsupported else 0.52
        warnings: list[str] = []
        supported = confidence >= 0.6
        unsupported_reason = ""
        if not supported:
            warnings.append("unsupported_workflow_template")
            unsupported_reason = "현재 자동 생성 템플릿은 folder_search만 지원합니다."
        return WorkflowSpec(
            template_id="folder_search",
            service_url=service_url,
            limit=limit,
            timeout_ms=timeout_ms,
            planner="rule",
            confidence=confidence,
            supported=supported,
            unsupported_reason=unsupported_reason,
            warnings=warnings,
        )

    def _plan_with_llm(
        self,
        instruction: str,
        *,
        rule_spec: WorkflowSpec,
        provider: str,
        base_url: str,
        model: str,
        api_key: str,
    ) -> WorkflowSpec:
        try:
            llm_result = self._call_openai_compatible_chat(
                instruction=_workflow_intent_summary(instruction),
                base_url=base_url,
                model=model,
                api_key=api_key,
            )
            template_id = llm_result.get("template_id")
            if template_id != "folder_search":
                rule_spec.warnings.append("llm_template_unsupported")
                return rule_spec
            return rule_spec.model_copy(
                update={
                    "planner": provider,
                    "confidence": max(float(llm_result.get("confidence", rule_spec.confidence)), rule_spec.confidence),
                }
            )
        except Exception:  # noqa: BLE001 - 생성기는 실패해도 규칙 기반으로 계속 동작해야 한다.
            fallback = rule_spec.model_copy(deep=True)
            fallback.warnings.append(f"{provider}_llm_failed")
            return fallback

    def _call_openai_compatible_chat(
        self,
        *,
        instruction: str,
        base_url: str,
        model: str,
        api_key: str,
    ) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 80,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You classify automation_smb Langflow workflow requests. "
                        "Return only JSON with template_id and confidence. "
                        "Only supported template_id is folder_search."
                    ),
                },
                {"role": "user", "content": instruction},
            ],
        }
        timeout_s = max(0.1, int(os.getenv("LANGFLOW_COMPOSER_LLM_TIMEOUT_MS", "1200")) / 1000)
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)


def plan_workflow(instruction: str, **kwargs: Any) -> WorkflowSpec:
    """간단 호출용 헬퍼."""
    return WorkflowPlanner().plan(instruction, **kwargs)
