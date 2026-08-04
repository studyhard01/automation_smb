"""LLMOps PostgreSQL에서 선택 가능한 문서를 읽기 전용으로 검색한다."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from . import intent
from .config import Settings
from .models import DocumentSearchHit, DocumentSearchRequest, DocumentSearchResponse, StoreConnectionState

_logger = logging.getLogger(__name__)


class LlmopsSearchError(RuntimeError):
    """LLMOps 파일 검색의 안전한 공개 오류."""

    def __init__(self, code: str, message: str, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.elapsed_ms = elapsed_ms


class LlmopsFileSearcher:
    """활성 Revision의 문서 메타데이터와 chunk 검색 텍스트를 조회한다."""

    def __init__(self, settings: Settings, *, connect: Callable[..., Any] | None = None) -> None:
        self._settings = settings
        self._connect = connect or psycopg.connect

    def close(self) -> None:
        """요청별 연결 방식이라 유지 중인 연결이 없다."""

    def search(self, request: DocumentSearchRequest) -> DocumentSearchResponse:
        """자연어 검색문으로 활성 문서를 찾고 파일 선택용 최소 메타데이터만 반환한다."""

        started = time.perf_counter()
        normalized = intent.normalize_rule(request.query)
        terms = [term for term in normalized.split() if term][:8]
        limit = min(
            request.limit or self._settings.llmops_file_search_limit,
            self._settings.llmops_file_search_max_limit,
        )
        if not terms:
            return DocumentSearchResponse(
                query=request.query,
                normalized_query=normalized,
                hits=[],
                result_count=0,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                source="llmops",
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
                          AND c.search_text ILIKE ALL(%s)
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
                GREATEST(
                    similarity(metadata_text, %s),
                    content_score
                ) + CASE WHEN metadata_text ILIKE ALL(%s) THEN 1.0 ELSE 0.5 END AS score,
                CASE WHEN metadata_text ILIKE ALL(%s) THEN 'metadata' ELSE 'content' END AS match_source
            FROM candidates
            WHERE metadata_text ILIKE ALL(%s) OR content_matches
            ORDER BY score DESC, source_modified_at DESC NULLS LAST, file_name
            LIMIT %s
            """
        ).format(schema, schema, schema)

        try:
            with self._connect(**self._connection_kwargs()) as connection:
                with connection.cursor(row_factory=dict_row) as cursor:
                    cursor.execute(
                        query,
                        (normalized, patterns, normalized, patterns, patterns, patterns, limit),
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

        hits = [self._row_to_hit(row) for row in rows]
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
        )

    def validate_active_selections(self, selections: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        """선택한 doc/revision 쌍이 현재 활성 문서인지 한 번의 읽기 쿼리로 확인한다."""

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
            with self._connect(**self._connection_kwargs()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query, parameters)
                    return {(str(row[0]), str(row[1])) for row in cursor.fetchall()}
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

    def _connection_kwargs(self) -> dict[str, Any]:
        connect_timeout_sec = max(1, math.ceil(self._settings.llmops_db_connect_timeout_ms / 1000))
        return {
            "host": self._settings.effective_llmops_db_host,
            "port": self._settings.postgres_port,
            "dbname": self._settings.llmops_postgres_db,
            "user": self._settings.postgres_user,
            "password": self._settings.postgres_password,
            "connect_timeout": connect_timeout_sec,
            "application_name": "automation_smb_file_search",
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={self._settings.llmops_db_query_timeout_ms}"
            ),
        }

    @staticmethod
    def _row_to_hit(row: dict[str, Any]) -> DocumentSearchHit:
        score = float(row.get("score") or 0.0)
        return DocumentSearchHit(
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
        )
