"""온프레미스 LLM과 PostgreSQL·MinIO·Neo4j를 결합한 파일 검색."""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from . import intent
from .config import Settings
from .llmops_search import LlmopsSearchError, collapse_physical_hits
from .models import DocumentSearchHit, DocumentSearchRequest, DocumentSearchResponse, StoreConnectionState

_logger = logging.getLogger(__name__)


def _is_internal_http_url(value: str) -> bool:
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
class QueryExpansion:
    """검색어 확장 결과와 안전한 상태 코드."""

    terms: list[str]
    used: bool
    elapsed_ms: float
    warning: str = ""


class SearchQueryExpander(Protocol):
    """테스트에서 교체 가능한 검색어 확장기."""

    def expand(self, query: str) -> QueryExpansion: ...

    def close(self) -> None: ...


class LocalSearchQueryExpander:
    """문서 본문 없이 사용자 질의만 온프레미스 Ollama로 확장한다."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._root_url = settings.ollama_base_url.strip().rstrip("/")
        self._model = settings.llmops_file_search_llm_model.strip() or settings.llmops_chat_model.strip()
        self._client = client or httpx.Client(
            timeout=max(0.2, settings.llmops_file_search_llm_timeout_ms / 1000)
        )

    def expand(self, query: str) -> QueryExpansion:
        """고유명사와 업무 동의어를 최대 설정 개수까지 확장한다."""

        started = time.perf_counter()
        baseline = self._baseline_terms(query)
        if not self._settings.llmops_file_search_llm_enabled:
            return self._fallback(baseline, started, "search_llm_disabled")
        if not self._root_url or not self._model:
            return self._fallback(baseline, started, "search_llm_not_configured")
        if not _is_internal_http_url(self._root_url):
            return self._fallback(baseline, started, "search_llm_url_not_internal")

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "한국어 공유 문서 파일 검색어 확장기입니다. 사용자의 원래 의미를 바꾸지 말고 "
                        "파일명·부서명·문서 종류에 쓸 핵심어와 일반적인 동의어만 반환하세요. "
                        "경로, SQL, 설명은 만들지 말고 JSON {\"terms\":[\"...\"]}만 반환하세요."
                    ),
                },
                {"role": "user", "content": query[:500]},
            ],
            "stream": False,
            "think": False,
            "format": {
                "type": "object",
                "properties": {
                    "terms": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 80},
                        "maxItems": self._settings.llmops_file_search_llm_max_terms,
                    }
                },
                "required": ["terms"],
                "additionalProperties": False,
            },
            "options": {"temperature": 0, "num_predict": 120},
        }
        try:
            response = self._client.post(f"{self._root_url}/api/chat", json=payload)
            response.raise_for_status()
            response_json = response.json()
            content: Any = response_json
            if isinstance(response_json, dict) and "terms" not in response_json:
                content = response_json.get("message", {}).get("content")
            if isinstance(content, str):
                text = content.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                content = json.loads(text)
            raw_terms = content.get("terms") if isinstance(content, dict) else None
            if not isinstance(raw_terms, list):
                raise ValueError("검색어 배열이 없습니다.")
            terms = [*baseline]
            for value in raw_terms:
                normalized = intent.normalize_rule(str(value))[:80].strip()
                if normalized:
                    terms.append(normalized.casefold())
            unique = list(dict.fromkeys(terms))[: self._settings.llmops_file_search_llm_max_terms]
            return QueryExpansion(
                terms=unique,
                used=True,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return self._fallback(baseline, started, "search_llm_unavailable")

    def close(self) -> None:
        """재사용 HTTP 연결을 닫는다."""

        self._client.close()

    @staticmethod
    def _baseline_terms(query: str) -> list[str]:
        normalized = intent.normalize_rule(query)
        return list(dict.fromkeys(term.casefold() for term in normalized.split() if term))

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

    def search(self, request: DocumentSearchRequest) -> DocumentSearchResponse:
        """질의를 확장하고 세 저장소를 조회한 뒤 중복 문서를 통합한다."""

        started = time.perf_counter()
        expansion = self._expander.expand(request.query)
        timings = {"llm_query_expansion": expansion.elapsed_ms}
        warnings = [expansion.warning] if expansion.warning else []
        queried_stores = ["postgresql"]
        tasks: dict[str, Callable[[], Any]] = {
            "postgresql": lambda: self._postgres.search(request, search_terms=expansion.terms),
        }
        if self._artifacts is not None:
            queried_stores.append("minio")
            tasks["minio"] = lambda: self._artifacts.search_document_refs(
                expansion.terms,
                limit=self._settings.llmops_file_search_source_limit,
            )
        else:
            warnings.append("minio_search_not_configured")
        if self._graph is not None:
            queried_stores.append("neo4j")
            tasks["neo4j"] = lambda: self._graph.search_document_refs(
                expansion.terms,
                limit=self._settings.llmops_file_search_source_limit,
            )
        else:
            warnings.append("neo4j_search_not_configured")

        values: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="file-search") as executor:
            futures = {
                executor.submit(self._timed, task): source
                for source, task in tasks.items()
            }
            for future in as_completed(futures):
                source = futures[future]
                try:
                    value, elapsed_ms = future.result()
                    values[source] = value
                    timings[source] = elapsed_ms
                except Exception as exc:
                    if source == "postgresql":
                        if isinstance(exc, LlmopsSearchError):
                            raise
                        raise LlmopsSearchError(
                            "llmops_search_unavailable",
                            "구축 문서 DB 검색에 연결할 수 없습니다.",
                        ) from exc
                    warnings.append(f"{source}_search_degraded")

        postgres_response = values.get("postgresql")
        if not isinstance(postgres_response, DocumentSearchResponse):
            raise LlmopsSearchError("llmops_search_unavailable", "구축 문서 DB 검색 결과를 확인할 수 없습니다.")

        refs: dict[tuple[str, str], set[str]] = {}
        for source in ("minio", "neo4j"):
            for pair in values.get(source, set()):
                refs.setdefault((str(pair[0]), str(pair[1])), set()).add(source)

        external_hits: list[DocumentSearchHit] = []
        if refs:
            hydration_started = time.perf_counter()
            external_hits = self._postgres.hydrate_document_refs(
                refs,
                limit=self._settings.llmops_file_search_max_limit,
            )
            timings["postgresql_hydrate"] = round((time.perf_counter() - hydration_started) * 1000, 1)

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
        over_budget = elapsed_ms > self._settings.llmops_file_search_budget_ms
        log = _logger.warning if over_budget else _logger.info
        log(
            "멀티스토어 파일 검색 완료: hits=%d elapsed_ms=%.1f budget_ms=%d stores=%s llm_used=%s warnings=%d",
            len(hits),
            elapsed_ms,
            self._settings.llmops_file_search_budget_ms,
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

    def validate_active_selections(self, selections: Any) -> set[tuple[str, str]]:
        """선택 문서 검증은 PostgreSQL 기준 원장에 위임한다."""

        return self._postgres.validate_active_selections(selections)

    def status(self) -> StoreConnectionState:
        """검색 기준 원장인 PostgreSQL 상태를 반환한다."""

        return self._postgres.status()

    def close(self) -> None:
        """검색 전용 LLM HTTP 연결을 닫는다."""

        self._expander.close()

    @staticmethod
    def _timed(task: Callable[[], Any]) -> tuple[Any, float]:
        started = time.perf_counter()
        return task(), round((time.perf_counter() - started) * 1000, 1)
