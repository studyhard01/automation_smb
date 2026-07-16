"""MLflow manual tracing을 core 관측 계약에 연결하는 평가 전용 구현."""

from __future__ import annotations

from contextlib import contextmanager
import sys
from typing import Any, Iterator

from smb_finder.telemetry import NoopTraceSpan, TraceSpan


class _MlflowTraceSpan:
    """MLflow span 기록 실패가 챗봇 실행을 중단하지 않도록 감싼다."""

    def __init__(self, span: Any, errors: list[str]) -> None:
        self._span = span
        self._errors = errors

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        try:
            for key, value in attributes.items():
                self._span.set_attribute(key, value)
        except Exception as exc:  # noqa: BLE001 - telemetry는 fail-open이어야 한다.
            self._errors.append(f"span_attributes:{exc.__class__.__name__}:{exc}")

    def set_outputs(self, outputs: Any) -> None:
        try:
            self._span.set_outputs(outputs)
        except Exception as exc:  # noqa: BLE001 - telemetry는 fail-open이어야 한다.
            self._errors.append(f"span_outputs:{exc.__class__.__name__}:{exc}")


class MlflowTraceObserver:
    """활성 MLflow run 아래에 nested span을 만들고 기록 실패는 수집만 한다."""

    def __init__(self, mlflow: Any, *, run_id: str = "", include_content: bool = False) -> None:
        self._mlflow = mlflow
        self._run_id = run_id
        self.include_content = include_content
        self.errors: list[str] = []
        self._noop = NoopTraceSpan()
        self._depth = 0

    @contextmanager
    def span(
        self,
        *,
        name: str,
        span_type: str,
        inputs: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[TraceSpan]:
        """MLflow span 생성·종료 오류를 본 실행과 분리한다."""

        context: Any | None = None
        try:
            context = self._mlflow.start_span(
                name=name,
                span_type=span_type,
                attributes=attributes,
                run_id=(self._run_id or None) if self._depth == 0 else None,
            )
            active_span = context.__enter__()
        except Exception as exc:  # noqa: BLE001 - 관측 실패 시 본 기능을 계속 실행한다.
            self.errors.append(f"span_start:{name}:{exc.__class__.__name__}:{exc}")
            yield self._noop
            return

        if inputs is not None:
            try:
                active_span.set_inputs(inputs)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"span_inputs:{name}:{exc.__class__.__name__}:{exc}")

        wrapped = _MlflowTraceSpan(active_span, self.errors)
        self._depth += 1
        try:
            yield wrapped
        except BaseException:
            exc_info = sys.exc_info()
            self._depth -= 1
            try:
                context.__exit__(*exc_info)
            except Exception as trace_exc:  # noqa: BLE001
                self.errors.append(f"span_end:{name}:{trace_exc.__class__.__name__}:{trace_exc}")
            raise
        else:
            self._depth -= 1
            try:
                context.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"span_end:{name}:{exc.__class__.__name__}:{exc}")
