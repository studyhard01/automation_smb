"""온프레미스 LLM과 PostgreSQL·MinIO·Neo4j를 결합한 파일 검색."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from . import intent
from .bot_core import JsonModelGateway, ModelGatewayError, OllamaModelGateway
from .config import Settings
from .llmops_search import LlmopsSearchError, collapse_physical_hits
from .models import DocumentSearchHit, DocumentSearchRequest, DocumentSearchResponse, StoreConnectionState

_logger = logging.getLogger(__name__)


_FILE_SEARCH_HARD_BUDGET_MS = 1000
_QUERY_EXPANSION_HARD_BUDGET_MS = 500
_STORE_WORKERS = 3
_OPTIONAL_STORE_WORKERS = _STORE_WORKERS - 1
_AMBIGUOUS_QUERY_MARKERS = (
    "비슷",
    "유사",
    "관련된",
    "어떤 문서",
    "어디 있는",
    "기억나지",
    "similar",
    "related",
)


def _effective_file_search_budget_ms(settings: Settings) -> int:
    """환경값이 목표를 느슨하게 만들어도 제품 p95 목표 1초를 넘기지 않는다."""

    return min(settings.llmops_file_search_budget_ms, _FILE_SEARCH_HARD_BUDGET_MS)


class QueryTermsPayload(BaseModel):
    """검색어 확장 Gateway의 strict 출력."""

    model_config = ConfigDict(extra="forbid")

    terms: list[Annotated[str, Field(max_length=80)]] = Field(max_length=20)


@dataclass(frozen=True)
class QueryExpansion:
    """검색어 확장 결과와 안전한 상태 코드."""

    terms: list[str]
    used: bool
    elapsed_ms: float
    warning: str = ""


class SearchQueryExpander(Protocol):
    """테스트에서 교체 가능한 검색어 확장기."""

    def expand(self, query: str, *, deadline: float | None = None) -> QueryExpansion: ...

    def close(self) -> None: ...


class LocalSearchQueryExpander:
    """문서 본문 없이 사용자 질의만 온프레미스 Ollama로 확장한다."""

    def __init__(self, settings: Settings, *, gateway: JsonModelGateway | None = None) -> None:
        self._settings = settings
        self._model = settings.llmops_file_search_llm_model.strip() or settings.llmops_chat_model.strip()
        self._gateway = gateway or OllamaModelGateway(settings)
        self._owns_gateway = gateway is None

    def expand(self, query: str, *, deadline: float | None = None) -> QueryExpansion:
        """고유명사와 업무 동의어를 최대 설정 개수까지 확장한다."""

        started = time.perf_counter()
        baseline = self._baseline_terms(query)
        if not self._settings.llmops_file_search_llm_enabled:
            return self._fallback(baseline, started, "search_llm_disabled")
        if not self._needs_llm_expansion(query, baseline):
            return QueryExpansion(
                terms=baseline,
                used=False,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        effective_deadline = deadline or (time.monotonic() + _effective_file_search_budget_ms(self._settings) / 1000)
        try:
            result = self._gateway.invoke_json(
                purpose="search_query_expansion",
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "한국어 공유 문서 파일 검색어 확장기입니다. 사용자의 원래 의미를 바꾸지 말고 "
                            "파일명·부서명·문서 종류에 쓸 핵심어와 일반적인 동의어만 반환하세요. "
                            '경로, SQL, 설명은 만들지 말고 JSON {"terms":["..."]}만 반환하세요.'
                        ),
                    },
                    {"role": "user", "content": query[:500]},
                ],
                response_model=QueryTermsPayload,
                deadline=effective_deadline,
                max_timeout_ms=min(
                    self._settings.llmops_file_search_llm_timeout_ms,
                    _QUERY_EXPANSION_HARD_BUDGET_MS,
                ),
                max_output_tokens=120,
            )
            terms = [*baseline]
            for value in result.payload.terms:
                normalized = intent.normalize_rule(str(value))[:80].strip()
                if normalized:
                    terms.append(normalized.casefold())
            unique = list(dict.fromkeys(terms))[: self._settings.llmops_file_search_llm_max_terms]
            return QueryExpansion(
                terms=unique,
                used=True,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except ModelGatewayError as exc:
            warning = {
                "model_not_configured": "search_llm_not_configured",
                "model_url_not_internal": "search_llm_url_not_internal",
                "model_timeout": "search_llm_timeout",
                "model_budget_exhausted": "search_llm_timeout",
            }.get(exc.code, "search_llm_unavailable")
            return self._fallback(baseline, started, warning)

    def close(self) -> None:
        """재사용 HTTP 연결을 닫는다."""

        if self._owns_gateway:
            self._gateway.close()

    @staticmethod
    def _baseline_terms(query: str) -> list[str]:
        normalized = intent.normalize_rule(query)
        return list(dict.fromkeys(term.casefold() for term in normalized.split() if term))

    @staticmethod
    def _needs_llm_expansion(query: str, baseline: list[str]) -> bool:
        normalized_query = query.casefold()
        return len(baseline) > 4 or any(marker in normalized_query for marker in _AMBIGUOUS_QUERY_MARKERS)

    @staticmethod
    def _fallback(terms: list[str], started: float, warning: str) -> QueryExpansion:
        return QueryExpansion(
            terms=terms,
            used=False,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            warning=warning,
        )


class LlmopsMultiStoreFileSearcher:
    """세 저장소의 후보를 PostgreSQL 활성 문서 계약으로 통합한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        postgres_searcher: Any,
        artifact_reader: Any | None,
        graph_reader: Any | None,
        query_expander: SearchQueryExpander | None = None,
    ) -> None:
        self._settings = settings
        self._postgres = postgres_searcher
        self._artifacts = artifact_reader
        self._graph = graph_reader
        self._expander = query_expander or LocalSearchQueryExpander(settings)
        self._executor = ThreadPoolExecutor(max_workers=_STORE_WORKERS, thread_name_prefix="file-search")
        self._executor_slots = BoundedSemaphore(_STORE_WORKERS)
        self._optional_executor_slots = BoundedSemaphore(_OPTIONAL_STORE_WORKERS)
        self._executor_lock = Lock()
        self._closed = False

    def search(self, request: DocumentSearchRequest) -> DocumentSearchResponse:
        """질의를 확장하고 세 저장소를 조회한 뒤 중복 문서를 통합한다."""

        started = time.perf_counter()
        deadline = time.monotonic() + _effective_file_search_budget_ms(self._settings) / 1000
        expansion = self._expander.expand(request.query, deadline=deadline)
        timings = {"llm_query_expansion": expansion.elapsed_ms}
        warnings = [expansion.warning] if expansion.warning else []
        queried_stores: list[str] = []
        tasks: dict[str, Callable[[], Any]] = {
            "postgresql": lambda: self._postgres.search(request, search_terms=expansion.terms),
        }
        if self._artifacts is not None:
            tasks["minio"] = lambda: self._artifacts.search_document_refs(
                expansion.terms,
                limit=self._settings.llmops_file_search_source_limit,
            )
        else:
            warnings.append("minio_search_not_configured")
        if self._graph is not None:
            tasks["neo4j"] = lambda: self._graph.search_document_refs(
                expansion.terms,
                limit=self._settings.llmops_file_search_source_limit,
            )
        else:
            warnings.append("neo4j_search_not_configured")

        values: dict[str, Any] = {}
        futures: dict[Future[tuple[Any, float]], str] = {}
        for source, task in tasks.items():
            optional = source != "postgresql"
            future = self._submit_before_deadline(task, deadline, optional=optional)
            if future is None:
                if optional:
                    warnings.append(f"{source}_search_degraded")
                continue
            futures[future] = source
            queried_stores.append(source)

        remaining = max(0.0, deadline - time.monotonic())
        completed, unfinished = wait(futures, timeout=remaining)
        for future in unfinished:
            future.cancel()
            source = futures[future]
            if source != "postgresql":
                warnings.append(f"{source}_search_degraded")

        postgres_error: Exception | None = None
        for future in completed:
            source = futures[future]
            try:
                value, elapsed_ms = future.result()
                values[source] = value
                timings[source] = elapsed_ms
            except Exception as exc:
                if source == "postgresql":
                    postgres_error = exc
                else:
                    warnings.append(f"{source}_search_degraded")

        if postgres_error is not None:
            if isinstance(postgres_error, LlmopsSearchError):
                raise postgres_error
            raise LlmopsSearchError(
                "llmops_search_unavailable",
                "구축 문서 DB 검색에 연결할 수 없습니다.",
            ) from postgres_error

        postgres_response = values.get("postgresql")
        if not isinstance(postgres_response, DocumentSearchResponse):
            raise LlmopsSearchError("llmops_search_unavailable", "구축 문서 DB 검색 결과를 확인할 수 없습니다.")

        refs: dict[tuple[str, str], set[str]] = {}
        for source in ("minio", "neo4j"):
            for pair in values.get(source, set()):
                refs.setdefault((str(pair[0]), str(pair[1])), set()).add(source)

        external_hits: list[DocumentSearchHit] = []
        if refs:
            hydration_future = self._submit_before_deadline(
                lambda: self._postgres.hydrate_document_refs(
                    refs,
                    limit=self._settings.llmops_file_search_max_limit,
                ),
                deadline,
                optional=True,
            )
            if hydration_future is None:
                warnings.append("postgresql_hydrate_degraded")
            else:
                try:
                    remaining = max(0.0, deadline - time.monotonic())
                    external_hits, hydration_elapsed_ms = hydration_future.result(timeout=remaining)
                    timings["postgresql_hydrate"] = hydration_elapsed_ms
                except FutureTimeoutError:
                    hydration_future.cancel()
                    warnings.append("postgresql_hydrate_degraded")
                except Exception:
                    warnings.append("postgresql_hydrate_degraded")

        merged: dict[tuple[str, str], DocumentSearchHit] = {}
        for hit in [*postgres_response.hits, *external_hits]:
            key = (str(hit.doc_id), str(hit.revision_id))
            current = merged.get(key)
            if current is None:
                merged[key] = hit
                continue
            stores = list(dict.fromkeys([*current.matched_stores, *hit.matched_stores]))
            current.matched_stores = stores
            current.score = round(max(current.score, hit.score) + 0.08 * (len(stores) - 1), 4)

        limit = min(
            request.limit or self._settings.llmops_file_search_limit,
            self._settings.llmops_file_search_max_limit,
        )
        hits = collapse_physical_hits(merged.values())[:limit]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        effective_budget_ms = _effective_file_search_budget_ms(self._settings)
        over_budget = elapsed_ms > effective_budget_ms
        log = _logger.warning if over_budget else _logger.info
        log(
            "멀티스토어 파일 검색 완료: hits=%d elapsed_ms=%.1f budget_ms=%d stores=%s llm_used=%s warnings=%d",
            len(hits),
            elapsed_ms,
            effective_budget_ms,
            ",".join(queried_stores),
            expansion.used,
            len(warnings),
        )
        return DocumentSearchResponse(
            query=request.query,
            normalized_query=postgres_response.normalized_query,
            hits=hits,
            result_count=len(hits),
            elapsed_ms=elapsed_ms,
            over_budget=over_budget,
            source="llmops",
            search_mode="multistore",
            queried_stores=queried_stores,
            llm_expanded=expansion.used,
            timings_ms=timings,
            warnings=list(dict.fromkeys(warnings)),
        )

    def validate_active_selections(
        self,
        selections: Any,
        *,
        deadline: float | None = None,
    ) -> set[tuple[str, str]]:
        """선택 문서 검증은 PostgreSQL 기준 원장에 위임한다."""

        return self._postgres.validate_active_selections(selections, deadline=deadline)

    def status(self) -> StoreConnectionState:
        """검색 기준 원장인 PostgreSQL 상태를 반환한다."""

        return self._postgres.status()

    def close(self) -> None:
        """검색 worker와 검색 전용 LLM HTTP 연결을 닫는다."""

        with self._executor_lock:
            self._closed = True
        # 실제 adapter는 모두 설정된 connect/read/query timeout을 가진다. worker가 adapter를 더 사용하지 않을 때까지
        # 기다린 뒤 반환해야 ExitStack의 다음 adapter.close()와 경합하지 않고 프로세스 종료도 지연시키지 않는다.
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._expander.close()

    def _submit_before_deadline(
        self,
        task: Callable[[], Any],
        deadline: float,
        *,
        optional: bool = False,
    ) -> Future[tuple[Any, float]] | None:
        """남은 예산 안에 bounded worker를 확보한 읽기 작업만 제출한다."""

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if optional and not self._optional_executor_slots.acquire(blocking=False):
            return None
        optional_slot_acquired = optional
        if optional:
            worker_slot_acquired = self._executor_slots.acquire(blocking=False)
        else:
            worker_slot_acquired = self._executor_slots.acquire(timeout=remaining)
        if not worker_slot_acquired:
            if optional_slot_acquired:
                self._optional_executor_slots.release()
            return None
        try:
            if time.monotonic() >= deadline:
                self._executor_slots.release()
                if optional_slot_acquired:
                    self._optional_executor_slots.release()
                return None
            with self._executor_lock:
                if self._closed:
                    self._executor_slots.release()
                    if optional_slot_acquired:
                        self._optional_executor_slots.release()
                    return None
                future = self._executor.submit(self._timed_before_deadline, task, deadline)
        except Exception:
            self._executor_slots.release()
            if optional_slot_acquired:
                self._optional_executor_slots.release()
            raise
        future.add_done_callback(lambda _future: self._release_executor_slots(optional=optional_slot_acquired))
        return future

    def _release_executor_slots(self, *, optional: bool) -> None:
        self._executor_slots.release()
        if optional:
            self._optional_executor_slots.release()

    @staticmethod
    def _timed_before_deadline(task: Callable[[], Any], deadline: float) -> tuple[Any, float]:
        if time.monotonic() >= deadline:
            raise FutureTimeoutError
        started = time.perf_counter()
        return task(), round((time.perf_counter() - started) * 1000, 1)
