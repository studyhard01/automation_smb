"""LLMOps PostgreSQL에서 선택 가능한 문서를 읽기 전용으로 검색한다."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from . import intent
from .config import Settings
from .models import DocumentSearchHit, DocumentSearchRequest, DocumentSearchResponse, StoreConnectionState

_logger = logging.getLogger(__name__)


def collapse_physical_hits(hits: Iterable[DocumentSearchHit]) -> list[DocumentSearchHit]:
    """서로 다른 source/doc ID로 중복 적재된 동일 물리 파일을 한 건으로 통합한다.

    파일명만으로 합치지 않으므로 같은 이름의 서로 다른 경로는 그대로 유지한다.
    """

    collapsed: dict[tuple[str, ...], DocumentSearchHit] = {}
    stores_by_key: dict[tuple[str, ...], list[str]] = {}
    for hit in hits:
        key = hit.physical_identity or ("revision", str(hit.doc_id), str(hit.revision_id))
        stores_by_key[key] = list(dict.fromkeys([*stores_by_key.get(key, []), *hit.matched_stores]))
        current = collapsed.get(key)
        if current is None or _hit_rank(hit) > _hit_rank(current):
            collapsed[key] = hit

    for key, hit in collapsed.items():
        hit.matched_stores = stores_by_key[key]
    return sorted(collapsed.values(), key=_hit_rank, reverse=True)


def _hit_rank(hit: DocumentSearchHit) -> tuple[float, float, str, str]:
    modified_timestamp = hit.modified_at.timestamp() if hit.modified_at is not None else float("-inf")
    return (hit.score, modified_timestamp, str(hit.doc_id), str(hit.revision_id))


class LlmopsSearchError(RuntimeError):
    """LLMOps 파일 검색의 안전한 공개 오류."""

    def __init__(self, code: str, message: str, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.elapsed_ms = elapsed_ms


class LlmopsFileSearcher:
    """활성 Revision의 문서 메타데이터와 chunk 검색 텍스트를 조회한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._connect = connect or psycopg.connect
        self._clock = clock

    def close(self) -> None:
        """요청별 연결 방식이라 유지 중인 연결이 없다."""

    def get_document_metadata(
        self,
        doc_id: UUID,
        revision_id: UUID | None = None,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """문서 UUID로 경로·이름·본문을 제외한 revision 상태만 조회한다."""

        started = self._clock()
        if not self._settings.llmops_db_configured:
            raise LlmopsSearchError(
                "metadata_not_configured",
                "문서 메타데이터 저장소가 구성되지 않았습니다.",
            )
        connection_kwargs = self._metadata_connection_kwargs(deadline, started)
        schema = sql.Identifier(self._settings.llmops_postgres_schema)
        query = sql.SQL(
            """
            SELECT
                d.doc_id,
                r.revision_id,
                (r.revision_id = d.active_revision_id) AS is_active,
                r.status::text AS revision_status,
                COALESCE(d.extension, '') AS extension,
                d.file_size,
                d.source_modified_at
            FROM {}.documents AS d
            LEFT JOIN {}.document_revisions AS r
              ON r.doc_id = d.doc_id
             AND r.revision_id = COALESCE(%s::uuid, d.active_revision_id)
            WHERE d.doc_id = %s::uuid
              AND d.deleted_at IS NULL
            LIMIT 1
            """
        ).format(schema, schema)
        try:
            with self._connect(**connection_kwargs) as connection:
                self._ensure_metadata_budget(deadline, started)
                with connection.cursor(row_factory=dict_row) as cursor:
                    timeout_ms = self._remaining_metadata_timeout_ms(deadline, started)
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
                    self._ensure_metadata_budget(deadline, started)
                    cursor.execute(query, (revision_id, doc_id))
                    row = cursor.fetchone()
                    self._ensure_metadata_budget(deadline, started)
        except LlmopsSearchError:
            raise
        except Exception as exc:
            raise LlmopsSearchError(
                "metadata_unavailable",
                "문서 메타데이터를 조회할 수 없습니다.",
                self._metadata_elapsed_ms(started),
            ) from exc

        if row is None:
            raise LlmopsSearchError(
                "document_not_found",
                "문서를 찾을 수 없습니다.",
                self._metadata_elapsed_ms(started),
            )
        if row.get("revision_id") is None:
            raise LlmopsSearchError(
                "revision_not_found",
                "문서 revision을 찾을 수 없습니다.",
                self._metadata_elapsed_ms(started),
            )
        elapsed_ms = self._metadata_elapsed_ms(started)
        return {
            "source": "llmops",
            "doc_id": row["doc_id"],
            "revision_id": row["revision_id"],
            "is_active": bool(row["is_active"]),
            "revision_status": str(row.get("revision_status") or "unknown").lower(),
            "extension": str(row.get("extension") or ""),
            "size_bytes": int(row["file_size"]) if row.get("file_size") is not None else None,
            "modified_at": row.get("source_modified_at"),
            "elapsed_ms": elapsed_ms,
            "over_budget": False,
            "degraded_dependencies": [],
        }

    def search(
        self,
        request: DocumentSearchRequest,
        *,
        search_terms: Iterable[str] | None = None,
    ) -> DocumentSearchResponse:
        """자연어 검색문으로 활성 문서를 찾고 파일 선택용 최소 메타데이터만 반환한다."""

        started = time.perf_counter()
        normalized = intent.normalize_rule(request.query)
        raw_terms = [term for term in normalized.split() if term]
        if search_terms is not None:
            raw_terms.extend(term for term in search_terms if term.strip())
        terms = list(dict.fromkeys(term.casefold() for term in raw_terms))[
            : self._settings.llmops_file_search_llm_max_terms
        ]
        limit = min(
            request.limit or self._settings.llmops_file_search_limit,
            self._settings.llmops_file_search_max_limit,
        )
        candidate_limit = min(
            max(self._settings.llmops_file_search_source_limit, limit),
            max(limit * 3, self._settings.llmops_file_search_max_limit),
        )
        if not terms:
            return DocumentSearchResponse(
                query=request.query,
                normalized_query=normalized,
                hits=[],
                result_count=0,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                source="llmops",
                search_mode="postgresql",
                queried_stores=["postgresql"],
            )

        patterns = [f"%{term}%" for term in terms]
        schema = sql.Identifier(self._settings.llmops_postgres_schema)
        query = sql.SQL(
            """
            WITH candidates AS (
                SELECT
                    d.doc_id,
                    d.active_revision_id AS revision_id,
                    d.file_name,
                    COALESCE(d.title, d.logical_name, d.file_name) AS title,
                    COALESCE(d.extension, '') AS extension,
                    d.file_size,
                    d.source_modified_at,
                    d.source_uri,
                    d.source_path,
                    d.source_item_id,
                    concat_ws(' ', d.file_name, d.title, d.logical_name, d.document_key) AS metadata_text,
                    COALESCE(
                        (
                            SELECT max(similarity(c.search_text, %s))
                            FROM {}.document_chunks AS c
                            WHERE c.doc_id = d.doc_id
                              AND c.revision_id = d.active_revision_id
                              AND c.active IS TRUE
                        ),
                        0.0
                    ) AS content_score,
                    EXISTS (
                        SELECT 1
                        FROM {}.document_chunks AS c
                        WHERE c.doc_id = d.doc_id
                          AND c.revision_id = d.active_revision_id
                          AND c.active IS TRUE
                          AND c.search_text ILIKE ANY(%s)
                    ) AS content_matches
                FROM {}.documents AS d
                WHERE d.deleted_at IS NULL
                  AND d.active_revision_id IS NOT NULL
            )
            SELECT
                doc_id,
                revision_id,
                file_name,
                title,
                extension,
                file_size,
                source_modified_at,
                source_uri,
                source_path,
                source_item_id,
                GREATEST(
                    similarity(metadata_text, %s),
                    content_score
                ) + CASE WHEN metadata_text ILIKE ALL(%s) THEN 1.0 ELSE 0.5 END AS score,
                CASE WHEN metadata_text ILIKE ANY(%s) THEN 'metadata' ELSE 'content' END AS match_source
            FROM candidates
            WHERE metadata_text ILIKE ANY(%s) OR content_matches
            ORDER BY score DESC, source_modified_at DESC NULLS LAST, file_name
            LIMIT %s
            """
        ).format(schema, schema, schema)

        try:
            with self._connect(**self._connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        query,
                        (normalized, patterns, normalized, patterns, patterns, patterns, candidate_limit),
                    )
                    rows = cursor.fetchall()
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _logger.warning("LLMOps 파일 검색 실패: type=%s elapsed_ms=%.1f", type(exc).__name__, elapsed_ms)
            raise LlmopsSearchError(
                "llmops_search_unavailable",
                "구축 문서 DB 검색에 연결할 수 없습니다. DB 실행 상태와 LLMOps 설정을 확인하세요.",
                elapsed_ms,
            ) from exc

        hits = collapse_physical_hits(self._row_to_hit(row) for row in rows)[:limit]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        over_budget = elapsed_ms > self._settings.llmops_db_query_timeout_ms
        if over_budget:
            _logger.warning(
                "LLMOps 파일 검색 시간 예산 초과: elapsed_ms=%.1f budget_ms=%d hits=%d",
                elapsed_ms,
                self._settings.llmops_db_query_timeout_ms,
                len(hits),
            )
        return DocumentSearchResponse(
            query=request.query,
            normalized_query=normalized,
            hits=hits,
            result_count=len(hits),
            elapsed_ms=elapsed_ms,
            over_budget=over_budget,
            source="llmops",
            search_mode="postgresql",
            queried_stores=["postgresql"],
            timings_ms={"postgresql": elapsed_ms},
        )

    def hydrate_document_refs(
        self,
        refs: dict[tuple[str, str], set[str]],
        *,
        limit: int,
    ) -> list[DocumentSearchHit]:
        """외부 저장소가 찾은 활성 문서 pair를 PostgreSQL 공개 메타데이터로 변환한다."""

        pairs = list(refs)[: self._settings.llmops_file_search_source_limit]
        if not pairs:
            return []
        pair_sql = sql.SQL(", ").join(sql.SQL("(%s::uuid, %s::uuid)") for _ in pairs)
        query = sql.SQL(
            """
            WITH requested(doc_id, revision_id) AS (VALUES {})
            SELECT
                d.doc_id,
                d.active_revision_id AS revision_id,
                d.file_name,
                COALESCE(d.title, d.logical_name, d.file_name) AS title,
                COALESCE(d.extension, '') AS extension,
                d.file_size,
                d.source_modified_at,
                d.source_uri,
                d.source_path,
                d.source_item_id,
                0.75 AS score,
                'metadata' AS match_source
            FROM {}.documents AS d
            JOIN requested AS requested
              ON requested.doc_id = d.doc_id AND requested.revision_id = d.active_revision_id
            WHERE d.deleted_at IS NULL
            ORDER BY d.source_modified_at DESC NULLS LAST, d.file_name
            LIMIT %s
            """
        ).format(pair_sql, sql.Identifier(self._settings.llmops_postgres_schema))
        parameters = [value for pair in pairs for value in pair]
        parameters.append(limit)
        try:
            with self._connect(**self._connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(query, parameters)
                    rows = cursor.fetchall()
        except Exception as exc:
            raise LlmopsSearchError(
                "llmops_external_candidate_hydration_failed",
                "다른 저장소에서 찾은 문서 후보를 확인할 수 없습니다.",
            ) from exc
        hits: list[DocumentSearchHit] = []
        for row in rows:
            key = (str(row["doc_id"]), str(row["revision_id"]))
            stores = ["postgresql", *sorted(refs.get(key, set()))]
            hits.append(self._row_to_hit(row, matched_stores=list(dict.fromkeys(stores))))
        return collapse_physical_hits(hits)[:limit]

    def validate_active_selections(
        self,
        selections: Iterable[tuple[str, str]],
        *,
        deadline: float | None = None,
    ) -> set[tuple[str, str]]:
        """선택한 doc/revision 쌍이 현재 활성 문서인지 한 번의 읽기 쿼리로 확인한다."""

        started = self._clock()
        pairs = list(dict.fromkeys(selections))
        if not pairs:
            return set()
        pair_sql = sql.SQL(", ").join(sql.SQL("(%s::uuid, %s::uuid)") for _ in pairs)
        query = sql.SQL(
            "SELECT doc_id::text, active_revision_id::text FROM {}.documents "
            "WHERE deleted_at IS NULL AND (doc_id, active_revision_id) IN ({})"
        ).format(sql.Identifier(self._settings.llmops_postgres_schema), pair_sql)
        parameters = [value for pair in pairs for value in pair]
        try:
            connection_kwargs = self._connection_kwargs()
            if deadline is not None:
                remaining_seconds = deadline - self._clock()
                if remaining_seconds <= 0:
                    raise self._selection_budget_error(started)
                if remaining_seconds < 1:
                    raise self._selection_budget_error(started)
                connection_kwargs = self._connection_kwargs(
                    connect_timeout_sec=min(
                        max(1, math.ceil(self._settings.llmops_db_connect_timeout_ms / 1000)),
                        math.floor(remaining_seconds),
                    ),
                    statement_timeout_ms=self._remaining_statement_timeout_ms(deadline, started),
                )
            with self._connect(**connection_kwargs) as connection:
                if deadline is not None:
                    self._ensure_selection_budget(deadline, started)
                with connection.cursor() as cursor:
                    if deadline is not None:
                        statement_timeout_ms = self._remaining_statement_timeout_ms(deadline, started)
                        cursor.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (f"{statement_timeout_ms}ms",),
                        )
                        self._ensure_selection_budget(deadline, started)
                    cursor.execute(query, parameters)
                    return {(str(row[0]), str(row[1])) for row in cursor.fetchall()}
        except LlmopsSearchError:
            raise
        except Exception as exc:
            raise LlmopsSearchError(
                "llmops_selection_validation_failed",
                "선택한 문서의 활성 버전을 확인할 수 없습니다.",
            ) from exc

    def status(self) -> StoreConnectionState:
        """주소·계정 없이 PostgreSQL read-only 연결과 필수 Table만 확인한다."""

        started = time.perf_counter()
        if not self._settings.llmops_db_configured:
            return StoreConnectionState(
                configured=False,
                connected=False,
                degraded=True,
                message="LLMOps PostgreSQL이 구성되지 않았습니다.",
            )
        try:
            with self._connect(**self._connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        "SELECT current_setting('transaction_read_only') AS read_only, "
                        "to_regclass(%s) IS NOT NULL AS documents_ready, "
                        "to_regclass(%s) IS NOT NULL AS chunks_ready",
                        (
                            f"{self._settings.llmops_postgres_schema}.documents",
                            f"{self._settings.llmops_postgres_schema}.document_chunks",
                        ),
                    )
                    row = cursor.fetchone()
            ready = bool(row and row["documents_ready"] and row["chunks_ready"] and row["read_only"] == "on")
            return StoreConnectionState(
                configured=True,
                connected=ready,
                degraded=not ready,
                message="연결됨" if ready else "필수 read-only 계약을 확인하지 못했습니다.",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                metadata={"schema": self._settings.llmops_postgres_schema, "read_only": bool(ready)},
            )
        except Exception:
            return StoreConnectionState(
                configured=True,
                connected=False,
                degraded=True,
                message="PostgreSQL 연결을 확인할 수 없습니다.",
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                metadata={"schema": self._settings.llmops_postgres_schema},
            )

    def _connection_kwargs(
        self,
        *,
        connect_timeout_sec: int | None = None,
        statement_timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        effective_connect_timeout_sec = connect_timeout_sec or max(
            1,
            math.ceil(self._settings.llmops_db_connect_timeout_ms / 1000),
        )
        effective_statement_timeout_ms = statement_timeout_ms or self._settings.llmops_db_query_timeout_ms
        return {
            "host": self._settings.effective_llmops_db_host,
            "port": self._settings.postgres_port,
            "dbname": self._settings.llmops_postgres_db,
            "user": self._settings.postgres_user,
            "password": self._settings.postgres_password,
            "connect_timeout": effective_connect_timeout_sec,
            "application_name": "automation_smb_file_search",
            "options": (f"-c default_transaction_read_only=on -c statement_timeout={effective_statement_timeout_ms}"),
        }

    def _remaining_statement_timeout_ms(self, deadline: float, started: float) -> int:
        now = self._clock()
        if now >= deadline:
            raise self._selection_budget_error(started)
        return min(
            self._settings.llmops_db_query_timeout_ms,
            max(1, math.floor((deadline - now) * 1000)),
        )

    def _ensure_selection_budget(self, deadline: float, started: float) -> None:
        if self._clock() >= deadline:
            raise self._selection_budget_error(started)

    def _selection_budget_error(self, started: float) -> LlmopsSearchError:
        return LlmopsSearchError(
            "llmops_selection_validation_budget_exhausted",
            "선택 문서 검증 시간 예산이 소진됐습니다.",
            round((self._clock() - started) * 1000, 1),
        )

    def _metadata_connection_kwargs(self, deadline: float, started: float) -> dict[str, Any]:
        remaining_seconds = deadline - self._clock()
        if remaining_seconds < 1:
            raise self._metadata_budget_error(started)
        return self._connection_kwargs(
            connect_timeout_sec=min(
                max(1, math.ceil(self._settings.llmops_db_connect_timeout_ms / 1000)),
                math.floor(remaining_seconds),
            ),
            statement_timeout_ms=self._remaining_metadata_timeout_ms(deadline, started),
        )

    def _remaining_metadata_timeout_ms(self, deadline: float, started: float) -> int:
        now = self._clock()
        if now >= deadline:
            raise self._metadata_budget_error(started)
        return min(
            self._settings.llmops_db_query_timeout_ms,
            max(1, math.floor((deadline - now) * 1000)),
        )

    def _ensure_metadata_budget(self, deadline: float, started: float) -> None:
        if self._clock() >= deadline:
            raise self._metadata_budget_error(started)

    def _metadata_budget_error(self, started: float) -> LlmopsSearchError:
        return LlmopsSearchError(
            "metadata_budget_exhausted",
            "문서 메타데이터 조회 시간 예산이 소진됐습니다.",
            self._metadata_elapsed_ms(started),
        )

    def _metadata_elapsed_ms(self, started: float) -> float:
        return round(max(0.0, self._clock() - started) * 1000, 1)

    @staticmethod
    def _row_to_hit(
        row: dict[str, Any],
        *,
        matched_stores: list[str] | None = None,
    ) -> DocumentSearchHit:
        score = float(row.get("score") or 0.0)
        hit = DocumentSearchHit(
            source="llmops",
            doc_id=row["doc_id"],
            revision_id=row["revision_id"],
            file_name=str(row.get("file_name") or ""),
            title=str(row.get("title") or ""),
            extension=str(row.get("extension") or ""),
            size_bytes=int(row["file_size"]) if row.get("file_size") is not None else None,
            modified_at=row.get("source_modified_at"),
            score=round(score, 4),
            match_source="content" if row.get("match_source") == "content" else "metadata",
            matched_stores=matched_stores or ["postgresql"],
        )
        hit.set_physical_identity(
            source_uri=str(row.get("source_uri") or ""),
            source_path=str(row.get("source_path") or ""),
            source_item_id=str(row.get("source_item_id") or ""),
        )
        return hit
