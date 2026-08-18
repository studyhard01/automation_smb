"""단일 MCP metadata 도구의 bounded 실행기."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import BoundedSemaphore, Lock
from typing import Any, Protocol

from pydantic import ValidationError

from smb_finder.llmops_search import LlmopsSearchError

from .contracts import DocumentMetadataOutput, GetDocumentMetadataInput
from .errors import ToolExecutionError


class MetadataReader(Protocol):
    """PostgreSQL metadata adapter의 필요한 표면만 정의한다."""

    def get_document_metadata(
        self,
        doc_id: Any,
        revision_id: Any | None = None,
        *,
        deadline: float,
    ) -> dict[str, Any]: ...


class ToolExecutor:
    """고정된 metadata 도구를 제한된 worker와 하나의 deadline으로 실행한다."""

    def __init__(
        self,
        metadata_reader: MetadataReader,
        *,
        timeout_ms: int = 1500,
        max_concurrency: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        workers = max(1, max_concurrency)
        self._metadata_reader = metadata_reader
        self._timeout_seconds = max(0.1, timeout_ms / 1000)
        self._clock = clock
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-metadata")
        self._slots = BoundedSemaphore(workers)
        self._state_lock = Lock()
        self._closed = False

    def execute(self, arguments: dict[str, Any]) -> DocumentMetadataOutput:
        """동기 호출자가 사용할 metadata 실행 진입점."""

        prepared, deadline = self._prepare(arguments)
        future = self._submit(prepared, deadline)
        try:
            return self._finish(future, deadline)
        except FutureTimeoutError as exc:
            raise self._timeout_error() from exc

    async def execute_async(self, arguments: dict[str, Any]) -> DocumentMetadataOutput:
        """MCP handler가 event loop를 막지 않고 결과를 기다린다."""

        prepared, deadline = self._prepare(arguments)
        future = self._submit(prepared, deadline)
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise self._timeout_error()
        try:
            raw = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=remaining)
        except TimeoutError as exc:
            raise self._timeout_error() from exc
        except LlmopsSearchError as exc:
            raise self._map_search_error(exc) from exc
        except Exception as exc:
            raise self._internal_error() from exc
        return self._validate_output(raw)

    def close(self) -> None:
        """DB hard timeout 안에서 worker를 모두 종료해 adapter close와의 경합을 막는다."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)

    def _prepare(self, arguments: dict[str, Any]) -> tuple[GetDocumentMetadataInput, float]:
        started = self._clock()
        deadline = started + self._timeout_seconds
        try:
            prepared = GetDocumentMetadataInput.model_validate(arguments)
        except ValidationError as exc:
            raise ToolExecutionError("invalid_arguments", "도구 입력이 올바르지 않습니다.") from exc
        return prepared, deadline

    def _submit(self, prepared: GetDocumentMetadataInput, deadline: float) -> Future[dict[str, Any]]:
        with self._state_lock:
            if self._closed:
                raise ToolExecutionError("internal_error", "메타데이터 도구가 종료되었습니다.")
        if not self._slots.acquire(blocking=False):
            raise self._timeout_error()
        try:
            future = self._pool.submit(
                self._metadata_reader.get_document_metadata,
                prepared.doc_id,
                prepared.revision_id,
                deadline=deadline,
            )
        except Exception as exc:
            self._slots.release()
            raise self._internal_error() from exc
        future.add_done_callback(lambda _future: self._slots.release())
        return future

    def _finish(self, future: Future[dict[str, Any]], deadline: float) -> DocumentMetadataOutput:
        remaining = deadline - self._clock()
        if remaining <= 0 and not future.done():
            raise FutureTimeoutError
        try:
            raw = future.result(timeout=max(0.0, remaining))
        except LlmopsSearchError as exc:
            raise self._map_search_error(exc) from exc
        except FutureTimeoutError:
            raise
        except Exception as exc:
            raise self._internal_error() from exc
        return self._validate_output(raw)

    @staticmethod
    def _validate_output(raw: Any) -> DocumentMetadataOutput:
        try:
            return DocumentMetadataOutput.model_validate(raw)
        except ValidationError as exc:
            raise ToolExecutionError("internal_error", "메타데이터 결과를 반환할 수 없습니다.") from exc

    @staticmethod
    def _map_search_error(exc: LlmopsSearchError) -> ToolExecutionError:
        if exc.code in {"metadata_not_configured", "document_not_found", "revision_not_found"}:
            return ToolExecutionError(exc.code, exc.message, elapsed_ms=exc.elapsed_ms)
        if exc.code == "metadata_budget_exhausted":
            return ToolExecutionError(
                "tool_timeout",
                "메타데이터 도구 실행 시간이 제한을 초과했습니다.",
                retryable=True,
                elapsed_ms=exc.elapsed_ms,
            )
        return ToolExecutionError(
            "internal_error",
            "메타데이터 도구를 실행할 수 없습니다.",
            retryable=True,
            elapsed_ms=exc.elapsed_ms,
        )

    def _timeout_error(self) -> ToolExecutionError:
        return ToolExecutionError(
            "tool_timeout",
            "메타데이터 도구 실행 시간이 제한을 초과했습니다.",
            retryable=True,
            elapsed_ms=round(self._timeout_seconds * 1000, 1),
        )

    @staticmethod
    def _internal_error() -> ToolExecutionError:
        return ToolExecutionError(
            "internal_error",
            "메타데이터 도구를 실행할 수 없습니다.",
            retryable=True,
        )
