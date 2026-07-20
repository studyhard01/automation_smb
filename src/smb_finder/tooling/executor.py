"""검색 도구의 검증·시간 제한·동시 실행·안전한 감사 로그를 담당한다."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import BoundedSemaphore
from typing import Any

from pydantic import BaseModel, ValidationError

from smb_finder.models import ContentSearchRequest, FindRequest

from .catalog import ToolCatalog, ToolSpec, ToolSurface

_logger = logging.getLogger(__name__)
_DEFAULT_MAX_CONCURRENCY = 4
_SHARED_POOL = ThreadPoolExecutor(max_workers=_DEFAULT_MAX_CONCURRENCY, thread_name_prefix="smb-tool")
_SHARED_SLOTS = BoundedSemaphore(_DEFAULT_MAX_CONCURRENCY)


class ToolExecutionError(RuntimeError):
    """민감한 입력이나 내부 예외 문자열을 포함하지 않는 도구 오류."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.elapsed_ms = elapsed_ms


class ToolExecutor:
    """동일한 검색 구현을 여러 도구 surface에서 안전하게 실행한다.

    동기 검색은 timeout 뒤에도 실행 중인 스레드를 강제로 종료할 수 없다. 따라서 읽기·멱등 도구만
    등록하며, semaphore는 실제 작업이 끝날 때 해제해 timeout 요청이 누적되어도 스레드가 무한히
    늘어나지 않게 한다.
    """

    def __init__(
        self,
        runtime: Any | Callable[[], Any],
        *,
        catalog: ToolCatalog | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._runtime = runtime
        self.catalog = ToolCatalog() if catalog is None else catalog
        workers = max(1, max_concurrency)
        if workers == _DEFAULT_MAX_CONCURRENCY:
            self._pool = _SHARED_POOL
            self._slots = _SHARED_SLOTS
        else:
            self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="smb-tool-isolated")
            self._slots = BoundedSemaphore(workers)

    def execute(self, tool_id: str, arguments: dict[str, Any], *, surface: ToolSurface) -> BaseModel:
        """도구를 동기 실행한다. Playground의 기존 동기 계약을 유지하기 위한 진입점이다."""

        started = time.perf_counter()
        spec, validated, timeout_ms = self._prepare(tool_id, arguments, surface)
        deadline = started + timeout_ms / 1000
        future = self._submit(spec, validated, surface, deadline, started)
        try:
            remaining = max(0.0, deadline - time.perf_counter())
            if remaining == 0.0 and not future.done():
                raise FutureTimeoutError
            result = future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            elapsed_ms = self._elapsed_ms(started)
            self._audit(tool_id, surface, "error", elapsed_ms, 0, "tool_timeout")
            raise ToolExecutionError(
                "tool_timeout",
                "도구 실행 시간이 제한을 초과했습니다.",
                retryable=True,
                elapsed_ms=elapsed_ms,
            ) from exc
        except ToolExecutionError as exc:
            self._audit(tool_id, surface, "error", self._elapsed_ms(started), 0, exc.code)
            raise
        except Exception as exc:
            elapsed_ms = self._elapsed_ms(started)
            self._audit(tool_id, surface, "error", elapsed_ms, 0, "tool_internal_error")
            raise ToolExecutionError(
                "tool_internal_error",
                "도구 실행 중 내부 오류가 발생했습니다.",
                retryable=True,
                elapsed_ms=elapsed_ms,
            ) from exc
        return self._finish(spec, result, surface, started)

    async def execute_async(self, tool_id: str, arguments: dict[str, Any], *, surface: ToolSurface) -> BaseModel:
        """동일한 실행기를 MCP 비동기 handler에서 사용한다."""

        started = time.perf_counter()
        spec, validated, timeout_ms = self._prepare(tool_id, arguments, surface)
        deadline = started + timeout_ms / 1000
        future = await asyncio.to_thread(self._submit, spec, validated, surface, deadline, started)
        try:
            remaining = max(0.0, deadline - time.perf_counter())
            if remaining == 0.0 and not future.done():
                raise TimeoutError
            if future.done():
                result = future.result()
            else:
                result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=remaining)
        except TimeoutError as exc:
            elapsed_ms = self._elapsed_ms(started)
            self._audit(tool_id, surface, "error", elapsed_ms, 0, "tool_timeout")
            raise ToolExecutionError(
                "tool_timeout",
                "도구 실행 시간이 제한을 초과했습니다.",
                retryable=True,
                elapsed_ms=elapsed_ms,
            ) from exc
        except ToolExecutionError as exc:
            self._audit(tool_id, surface, "error", self._elapsed_ms(started), 0, exc.code)
            raise
        except Exception as exc:
            elapsed_ms = self._elapsed_ms(started)
            self._audit(tool_id, surface, "error", elapsed_ms, 0, "tool_internal_error")
            raise ToolExecutionError(
                "tool_internal_error",
                "도구 실행 중 내부 오류가 발생했습니다.",
                retryable=True,
                elapsed_ms=elapsed_ms,
            ) from exc
        return self._finish(spec, result, surface, started)

    def _prepare(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        surface: ToolSurface,
    ) -> tuple[ToolSpec, BaseModel, int]:
        spec = self.catalog.get(tool_id, surface)
        if spec is None:
            self._audit(tool_id, surface, "denied", 0.0, 0, "tool_not_allowed")
            raise ToolExecutionError("tool_not_allowed", "이 surface에서 허용되지 않은 도구입니다.")
        try:
            validated = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            self._audit(tool_id, surface, "error", 0.0, 0, "invalid_arguments")
            raise ToolExecutionError("invalid_arguments", "도구 입력이 올바르지 않습니다.") from exc

        runtime = self._get_runtime()
        settings = runtime.settings
        timeout_ms = settings.find_budget_ms if tool_id == "find_folder" else settings.content_search_budget_ms
        return spec, validated, max(100, int(timeout_ms))

    def _submit(
        self,
        spec: ToolSpec,
        validated: BaseModel,
        surface: ToolSurface,
        deadline: float,
        started: float,
    ) -> Future[Any]:
        remaining = max(0.0, deadline - time.perf_counter())
        if not self._slots.acquire(timeout=remaining):
            elapsed_ms = self._elapsed_ms(started)
            self._audit(spec.id, surface, "error", elapsed_ms, 0, "tool_busy")
            raise ToolExecutionError(
                "tool_busy",
                "동시 실행 한도를 초과했습니다.",
                retryable=True,
                elapsed_ms=elapsed_ms,
            )
        try:
            future = self._pool.submit(self._run, spec.id, validated)
        except Exception:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return future

    def _run(self, tool_id: str, validated: BaseModel) -> dict[str, Any]:
        runtime = self._get_runtime()
        if tool_id == "find_folder":
            if runtime.finder is None:
                raise ToolExecutionError("finder_not_ready", "폴더 인덱스가 아직 준비되지 않았습니다.", retryable=True)
            request = FindRequest(
                query=validated.query,
                limit=validated.limit or runtime.settings.find_default_limit,
            )
            response = runtime.finder.find(request)
            for hit in response.hits:
                self._ensure_safe_runtime_output(hit.name, hit.path, runtime.settings)
            hits = [
                {
                    "name": hit.name,
                    "path": hit.path,
                    "score": getattr(hit, "score", 0.0),
                    "depth": getattr(hit, "depth", 0),
                }
                for hit in response.hits
            ]
            return {
                "hits": hits,
                "result_count": getattr(response, "result_count", len(hits)),
                "elapsed_ms": max(0.0, float(getattr(response, "elapsed_ms", 0.0))),
                "over_budget": bool(getattr(response, "over_budget", False)),
            }

        if runtime.content_searcher is None:
            raise ToolExecutionError(
                "content_searcher_not_ready",
                "내용 검색 인덱스가 아직 준비되지 않았습니다.",
                retryable=True,
            )
        request = ContentSearchRequest(
            query=validated.query,
            limit=validated.limit or runtime.settings.content_default_limit,
        )
        response = runtime.content_searcher.search(request)
        for hit in response.hits:
            self._ensure_safe_runtime_output(hit.name, hit.path, runtime.settings)
        hits = [
            {
                "name": hit.name,
                "path": hit.path,
                "ext": hit.ext,
                "score": getattr(hit, "score", 0.0),
            }
            for hit in response.hits
        ]
        return {
            "hits": hits,
            "result_count": getattr(response, "result_count", len(hits)),
            "elapsed_ms": max(0.0, float(getattr(response, "elapsed_ms", 0.0))),
            "over_budget": bool(getattr(response, "over_budget", False)),
            "_indexed_files": max(0, int(getattr(response, "indexed_files", 0))),
        }

    def _finish(self, spec: ToolSpec, result: Any, surface: ToolSurface, started: float) -> BaseModel:
        elapsed_ms = self._elapsed_ms(started)
        try:
            validated = spec.output_model.model_validate(result)
        except ValidationError as exc:
            self._audit(spec.id, surface, "error", elapsed_ms, 0, "unsafe_tool_output")
            raise ToolExecutionError("unsafe_tool_output", "도구 결과를 안전하게 반환할 수 없습니다.") from exc
        set_indexed_files = getattr(validated, "set_indexed_files", None)
        if callable(set_indexed_files) and isinstance(result, dict):
            set_indexed_files(result.get("_indexed_files", 0))
        result_count = int(getattr(validated, "result_count", 0))
        self._audit(spec.id, surface, "ok", elapsed_ms, result_count, "")
        return validated

    def _get_runtime(self) -> Any:
        runtime = self._runtime() if callable(self._runtime) else self._runtime
        if runtime is None:
            raise ToolExecutionError("runtime_not_ready", "검색 runtime이 아직 준비되지 않았습니다.", retryable=True)
        return runtime

    @staticmethod
    def _ensure_safe_runtime_output(name: str, path: str, settings: Any) -> None:
        """설정에 있는 내부 식별자·자격정보가 결과명/경로에 섞이면 전체 결과를 차단한다."""

        values = (str(name), str(path))
        exact_markers = [
            settings.smb_host,
            settings.smb_share_name,
            settings.smb_username,
        ]
        for configured_path in (settings.smb_index_cache_path, settings.content_index_db_path):
            normalized = str(configured_path or "").replace("\\", "/")
            exact_markers.append(normalized.rsplit("/", 1)[-1])
        secret_markers = [
            settings.smb_password,
            settings.admin_api_token,
            settings.mcp_api_token,
            settings.llm_api_key,
            settings.openai_api_key,
        ]

        for value in values:
            normalized = value.replace("\\", "/").casefold()
            components = {component for component in normalized.split("/") if component}
            components.add(normalized)
            for marker in exact_markers:
                candidate = str(marker or "").strip().replace("\\", "/").casefold()
                if candidate and candidate in components:
                    raise ToolExecutionError("unsafe_tool_output", "도구 결과를 안전하게 반환할 수 없습니다.")
            for marker in secret_markers:
                candidate = str(marker or "").strip().casefold()
                if candidate and candidate in normalized:
                    raise ToolExecutionError("unsafe_tool_output", "도구 결과를 안전하게 반환할 수 없습니다.")

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    @staticmethod
    def _audit(
        tool_id: str,
        surface: ToolSurface,
        outcome: str,
        elapsed_ms: float,
        result_count: int,
        error_code: str,
    ) -> None:
        _logger.info(
            "tool_audit tool=%s surface=%s outcome=%s elapsed_ms=%.1f result_count=%d error_code=%s",
            tool_id,
            surface,
            outcome,
            elapsed_ms,
            result_count,
            error_code,
        )
