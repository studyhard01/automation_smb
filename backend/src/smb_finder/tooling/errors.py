"""공통 도구 실행 계층에서 사용하는 안전한 오류 계약."""

from __future__ import annotations


class ToolExecutionError(RuntimeError):
    """민감한 입력이나 내부 예외 문자열을 포함하지 않는 도구 오류."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.elapsed_ms = elapsed_ms
