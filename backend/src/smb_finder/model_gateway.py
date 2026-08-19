"""온프레미스 JSON 모델 호출의 공통 deadline·오류·검증 계약."""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from smb_finder.config import Settings

ModelPurpose = Literal[
    "search_query_expansion",
    "document_answer",
    "proposal_generation",
    "proposal_validation",
]
ModelGatewayErrorCode = Literal[
    "model_not_configured",
    "model_url_not_internal",
    "model_timeout",
    "model_unavailable",
    "invalid_model_response",
    "output_schema_invalid",
    "model_budget_exhausted",
    "model_context_limit",
]

_CONTEXT_LIMIT_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
    "token limit",
    "num_ctx",
)

PayloadT = TypeVar("PayloadT", bound=BaseModel)


def is_internal_http_url(value: str) -> bool:
    """자격증명 없는 loopback·사설망 HTTP(S) 주소만 허용한다."""

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold()
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return address.is_private or address.is_loopback or address.is_link_local


@dataclass(frozen=True)
class ModelTokenUsage:
    """원문 없이 집계하는 최소 token 사용량."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ModelGatewayResult(Generic[PayloadT]):
    """검증된 구조화 payload와 비민감 호출 metadata."""

    payload: PayloadT
    provider: Literal["ollama"]
    model: str
    usage: ModelTokenUsage
    elapsed_ms: float


class ModelGatewayError(RuntimeError):
    """내부 예외 문자열을 포함하지 않는 안정된 Gateway 오류."""

    def __init__(self, code: ModelGatewayErrorCode, *, retryable: bool, elapsed_ms: float = 0.0) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.elapsed_ms = elapsed_ms


class JsonModelGateway(Protocol):
    """Pydantic schema로 검증하는 동기 JSON 모델 Gateway."""

    def invoke_json(
        self,
        *,
        purpose: ModelPurpose,
        model: str,
        messages: list[dict[str, str]],
        response_model: type[PayloadT],
        deadline: float,
        max_timeout_ms: int,
        max_output_tokens: int,
        context_window_tokens: int | None = None,
    ) -> ModelGatewayResult[PayloadT]: ...

    def close(self) -> None: ...


class OllamaModelGateway:
    """하나의 재사용 HTTP client로 온프레미스 Ollama JSON 호출을 수행한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root_url = settings.ollama_base_url.strip().rstrip("/")
        self._client = client or httpx.Client()
        self._clock = clock

    def invoke_json(
        self,
        *,
        purpose: ModelPurpose,
        model: str,
        messages: list[dict[str, str]],
        response_model: type[PayloadT],
        deadline: float,
        max_timeout_ms: int,
        max_output_tokens: int,
        context_window_tokens: int | None = None,
    ) -> ModelGatewayResult[PayloadT]:
        """남은 절대 deadline 안에서 호출하고 응답 schema를 검증한다."""

        del purpose  # 호출 목적은 adapter 선택용 계약이며 prompt/log에는 기록하지 않는다.
        started = self._clock()
        selected_model = model.strip()
        if not self._root_url or not selected_model:
            raise self._error("model_not_configured", retryable=False, started=started)
        if not is_internal_http_url(self._root_url):
            raise self._error("model_url_not_internal", retryable=False, started=started)
        if max_timeout_ms <= 0 or max_output_tokens <= 0:
            raise ValueError("Gateway timeout과 출력 token 상한은 1 이상이어야 합니다.")
        if context_window_tokens is not None and context_window_tokens <= 0:
            raise ValueError("Gateway context window token 상한은 1 이상이어야 합니다.")

        remaining_seconds = deadline - self._clock()
        if remaining_seconds <= 0:
            raise self._error("model_budget_exhausted", retryable=True, started=started)
        timeout_seconds = min(max_timeout_ms / 1000, remaining_seconds)

        options = {"temperature": 0, "num_predict": max_output_tokens}
        if context_window_tokens is not None:
            options["num_ctx"] = context_window_tokens
        request_payload = {
            "model": selected_model,
            "messages": messages,
            "stream": False,
            "think": False,
            "format": response_model.model_json_schema(),
            "options": options,
        }
        try:
            response = self._client.post(
                f"{self._root_url}/api/chat",
                json=request_payload,
                timeout=max(0.001, timeout_seconds),
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise self._error("model_timeout", retryable=True, started=started) from exc
        except httpx.HTTPStatusError as exc:
            if self._is_context_limit_http_error(exc):
                raise self._error("model_context_limit", retryable=True, started=started) from exc
            if self._is_output_schema_http_error(exc):
                raise self._error("output_schema_invalid", retryable=False, started=started) from exc
            raise self._error("model_unavailable", retryable=True, started=started) from exc
        except httpx.HTTPError as exc:
            raise self._error("model_unavailable", retryable=True, started=started) from exc

        try:
            response_json = response.json()
            if self._contains_context_limit_marker(response_json):
                raise self._error("model_context_limit", retryable=True, started=started)
            raw_payload = self._extract_payload(response_json)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise self._error("invalid_model_response", retryable=False, started=started) from exc
        try:
            validated = response_model.model_validate(raw_payload)
        except ValidationError as exc:
            raise self._error("output_schema_invalid", retryable=False, started=started) from exc

        if self._clock() > deadline:
            raise self._error("model_budget_exhausted", retryable=True, started=started)
        usage = ModelTokenUsage(
            prompt_tokens=self._safe_count(response_json.get("prompt_eval_count")),
            completion_tokens=self._safe_count(response_json.get("eval_count")),
        )
        return ModelGatewayResult(
            payload=validated,
            provider="ollama",
            model=selected_model,
            usage=usage,
            elapsed_ms=self._elapsed_ms(started),
        )

    def close(self) -> None:
        """재사용 HTTP 연결을 닫는다."""

        self._client.close()

    @staticmethod
    def _extract_payload(response_json: Any) -> Any:
        if not isinstance(response_json, dict):
            raise ValueError("모델 응답이 JSON 객체가 아닙니다.")
        if "message" not in response_json:
            return response_json
        message = response_json["message"]
        if not isinstance(message, dict):
            raise ValueError("모델 응답 message가 JSON 객체가 아닙니다.")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("모델 응답 본문이 없습니다.")
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)

    def _error(
        self,
        code: ModelGatewayErrorCode,
        *,
        retryable: bool,
        started: float,
    ) -> ModelGatewayError:
        return ModelGatewayError(code, retryable=retryable, elapsed_ms=self._elapsed_ms(started))

    def _elapsed_ms(self, started: float) -> float:
        return round(max(0.0, (self._clock() - started) * 1000), 1)

    @staticmethod
    def _safe_count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _is_context_limit_text(value: str) -> bool:
        normalized = value.casefold()
        return any(marker in normalized for marker in _CONTEXT_LIMIT_MARKERS)

    @classmethod
    def _contains_context_limit_marker(cls, value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        error = value.get("error")
        return isinstance(error, str) and cls._is_context_limit_text(error)

    @classmethod
    def _is_context_limit_http_error(cls, exc: httpx.HTTPStatusError) -> bool:
        response = exc.response
        if response.status_code == 413:
            return True
        try:
            response_text = response.text
        except (AttributeError, RuntimeError):
            return False
        return cls._is_context_limit_text(response_text)

    @staticmethod
    def _is_output_schema_http_error(exc: httpx.HTTPStatusError) -> bool:
        """Ollama grammar 생성 실패를 연결 장애가 아닌 schema 오류로 분류한다."""

        if exc.response.status_code != 400:
            return False
        try:
            response_text = exc.response.text
        except (AttributeError, RuntimeError):
            return False
        return "failed to parse grammar" in response_text.casefold()
