"""선택적 관측 도구가 core 실행 경로에 의존성을 만들지 않게 하는 공통 계약."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, ContextManager, Protocol


class TraceSpan(Protocol):
    """실행 중 속성과 출력을 추가할 수 있는 최소 span 계약."""

    def set_attributes(self, attributes: dict[str, Any]) -> None: ...

    def set_outputs(self, outputs: Any) -> None: ...


class TraceObserver(Protocol):
    """MLflow 같은 구현체를 런타임에서 선택적으로 주입하는 계약."""

    include_content: bool

    def span(
        self,
        *,
        name: str,
        span_type: str,
        inputs: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> ContextManager[TraceSpan]: ...


class NoopTraceSpan:
    """관측 비활성 상태에서 아무 작업도 하지 않는 span."""

    def set_attributes(self, attributes: dict[str, Any]) -> None:  # noqa: ARG002
        return None

    def set_outputs(self, outputs: Any) -> None:  # noqa: ARG002
        return None


class NoopTraceObserver:
    """서비스 기본값으로 사용하는 의존성 없는 observer."""

    include_content = False

    def __init__(self) -> None:
        self._span = NoopTraceSpan()

    def span(
        self,
        *,
        name: str,  # noqa: ARG002
        span_type: str,  # noqa: ARG002
        inputs: Any | None = None,  # noqa: ARG002
        attributes: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> ContextManager[TraceSpan]:
        return nullcontext(self._span)
