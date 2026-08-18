"""선택한 LLMOps 문서 UUID 범위 안에서만 Hybrid Chunk를 검색한다."""

from __future__ import annotations

import math
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .config import Settings
from .llmops_search import LlmopsSearchError
from .models import DocumentCitation, RetrievalMetadata, RetrievalScope, RetrievalScores

_logger = logging.getLogger(__name__)


class QueryEmbeddingClient(Protocol):
    """테스트에서 교체 가능한 질의 embedding client."""

    def embed_query(self, query: str, *, deadline: float | None = None) -> list[float]: ...

    def close(self) -> None: ...


class OllamaQueryEmbeddingClient:
    """구축 파이프라인과 같은 Ollama `/api/embed` 연결을 재사용한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = settings.embedding_model.strip()
        self._dimension = settings.embedding_dim
        self._prefix = settings.embedding_query_prefix
        self._timeout_seconds = max(0.1, settings.llmops_embedding_timeout_ms / 1000)
        self._client = client or httpx.Client(base_url=f"{settings.ollama_base_url.strip().rstrip('/')}/")
        self._clock = clock

    def embed_query(self, query: str, *, deadline: float | None = None) -> list[float]:
        """문서 적재와 동일한 query prefix를 사용해 1개 질의를 embedding한다."""

        timeout_seconds = self._timeout_seconds
        if deadline is not None:
            remaining_seconds = deadline - self._clock()
            if remaining_seconds <= 0:
                raise LlmopsSearchError(
                    "llmops_retrieval_budget_exhausted",
                    "선택 문서 검색 시간 예산이 소진됐습니다.",
                )
            timeout_seconds = min(timeout_seconds, remaining_seconds)
        prefixed = query if query.startswith(self._prefix) else f"{self._prefix}{query}"
        try:
            response = self._client.post(
                "api/embed",
                json={"model": self._model, "input": prefixed, "truncate": True},
                timeout=max(0.001, timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            embedding = [float(value) for value in payload["embeddings"][0]]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmopsSearchError(
                "llmops_embedding_unavailable",
                "선택 문서 검색용 로컬 embedding을 만들 수 없습니다.",
            ) from exc
        if len(embedding) != self._dimension:
            raise LlmopsSearchError(
                "llmops_embedding_dimension_mismatch",
                "선택 문서 검색용 embedding 차원이 데이터셋 계약과 다릅니다.",
            )
        if not all(math.isfinite(value) for value in embedding):
            raise LlmopsSearchError("llmops_embedding_invalid", "질의 embedding에 유효하지 않은 값이 있습니다.")
        return embedding

    def close(self) -> None:
        """재사용 HTTP 연결을 닫는다."""

        self._client.close()


@dataclass(frozen=True)
class ScopedRetrievalResult:
    """Agent가 바로 소비하는 Citation과 검색 Metadata."""

    citations: list[DocumentCitation]
    metadata: RetrievalMetadata


class LlmopsScopedRetriever:
    """UUID 문서/Revision scope를 모든 Hybrid 후보 SQL에 강제한다."""

    _LOCATION_KEYS = {
        "page",
        "slide",
        "sheet",
        "sheet_name",
        "cell",
        "cell_range",
        "line",
        "line_start",
        "line_end",
        "bounding_box",
        "bbox",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: QueryEmbeddingClient | None = None,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._embedding_client = embedding_client or OllamaQueryEmbeddingClient(settings)
        self._connect = connect or psycopg.connect
        self._clock = clock
        self._query = self._build_query(settings.llmops_postgres_schema)

    @staticmethod
    def _build_query(schema_name: str) -> sql.Composed:
        schema = sql.Identifier(schema_name)
        return sql.SQL(
            """
            WITH selected(doc_id, revision_id) AS (
                SELECT * FROM unnest(%s::uuid[], %s::uuid[])
            ),
            vector_candidates AS MATERIALIZED (
                SELECT
                    c.chunk_id,
                    row_number() OVER (ORDER BY c.embedding <=> %s::vector) AS vector_rank,
                    1 - (c.embedding <=> %s::vector) AS vector_score
                FROM {}.document_chunks AS c
                JOIN selected AS s
                  ON s.doc_id = c.doc_id AND s.revision_id = c.revision_id
                JOIN {}.documents AS d
                  ON d.doc_id = s.doc_id AND d.active_revision_id = s.revision_id
                WHERE c.active IS TRUE
                  AND c.embedding IS NOT NULL
                  AND d.deleted_at IS NULL
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
            ),
            fts_candidates AS MATERIALIZED (
                SELECT
                    c.chunk_id,
                    row_number() OVER (
                        ORDER BY ts_rank_cd(c.search_vector, plainto_tsquery('simple', %s)) DESC
                    ) AS lexical_rank,
                    ts_rank_cd(c.search_vector, plainto_tsquery('simple', %s)) AS lexical_score
                FROM {}.document_chunks AS c
                JOIN selected AS s
                  ON s.doc_id = c.doc_id AND s.revision_id = c.revision_id
                JOIN {}.documents AS d
                  ON d.doc_id = s.doc_id AND d.active_revision_id = s.revision_id
                WHERE c.active IS TRUE
                  AND d.deleted_at IS NULL
                  AND c.search_vector @@ plainto_tsquery('simple', %s)
                ORDER BY lexical_score DESC
                LIMIT %s
            ),
            trigram_candidates AS MATERIALIZED (
                SELECT
                    c.chunk_id,
                    row_number() OVER (ORDER BY word_similarity(%s, c.search_text) DESC) AS trigram_rank,
                    word_similarity(%s, c.search_text) AS trigram_score
                FROM {}.document_chunks AS c
                JOIN selected AS s
                  ON s.doc_id = c.doc_id AND s.revision_id = c.revision_id
                JOIN {}.documents AS d
                  ON d.doc_id = s.doc_id AND d.active_revision_id = s.revision_id
                WHERE c.active IS TRUE
                  AND d.deleted_at IS NULL
                  AND %s <%% c.search_text
                ORDER BY trigram_score DESC
                LIMIT %s
            ),
            candidate_ids AS (
                SELECT chunk_id FROM vector_candidates
                UNION
                SELECT chunk_id FROM fts_candidates
                UNION
                SELECT chunk_id FROM trigram_candidates
            ),
            ranked AS (
                SELECT
                    c.chunk_id,
                    c.doc_id,
                    c.revision_id,
                    COALESCE(d.title, d.logical_name, d.file_name) AS title,
                    COALESCE(c.section_path, ARRAY[]::text[]) AS section_path,
                    COALESCE(c.citation_anchor, '{{}}'::jsonb) AS citation_anchor,
                    COALESCE(c.content, '') AS content,
                    vc.vector_score,
                    fc.lexical_score,
                    tc.trigram_score,
                    (
                        COALESCE(1.0 / (60 + vc.vector_rank), 0.0)
                        + COALESCE(1.0 / (60 + fc.lexical_rank), 0.0)
                        + COALESCE(1.0 / (60 + tc.trigram_rank), 0.0)
                    ) AS rrf_score
                FROM candidate_ids AS ids
                JOIN {}.document_chunks AS c ON c.chunk_id = ids.chunk_id
                JOIN selected AS s
                  ON s.doc_id = c.doc_id AND s.revision_id = c.revision_id
                JOIN {}.documents AS d
                  ON d.doc_id = s.doc_id AND d.active_revision_id = s.revision_id
                LEFT JOIN vector_candidates AS vc ON vc.chunk_id = c.chunk_id
                LEFT JOIN fts_candidates AS fc ON fc.chunk_id = c.chunk_id
                LEFT JOIN trigram_candidates AS tc ON tc.chunk_id = c.chunk_id
                WHERE c.active IS TRUE AND d.deleted_at IS NULL
            )
            SELECT ranked.*, count(*) OVER () AS candidate_count
            FROM ranked
            ORDER BY rrf_score DESC, chunk_id
            LIMIT %s
            """
        ).format(schema, schema, schema, schema, schema, schema, schema, schema)

    def retrieve(
        self,
        query: str,
        selections: Iterable[tuple[str, str]],
        *,
        top_k: int | None = None,
        candidate_k: int | None = None,
        deadline: float | None = None,
    ) -> ScopedRetrievalResult:
        """선택한 활성 Revision 밖의 Chunk를 반환하지 않는 Hybrid 검색을 실행한다."""

        started = self._clock()
        effective_deadline = (
            deadline
            if deadline is not None
            else started
            + (self._settings.llmops_embedding_timeout_ms + self._settings.llmops_db_query_timeout_ms) / 1000
        )
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise LlmopsSearchError("empty_query", "선택 문서에서 검색할 질문을 입력하세요.")
        pairs = list(dict.fromkeys((str(doc_id), str(revision_id)) for doc_id, revision_id in selections))
        if not 1 <= len(pairs) <= 5:
            raise LlmopsSearchError("invalid_selected_files", "선택 문서는 1개에서 5개까지 허용합니다.")

        result_limit = min(max(1, top_k or self._settings.llmops_retrieval_top_k), 20)
        candidate_limit = min(
            max(result_limit, candidate_k or self._settings.llmops_retrieval_candidate_k),
            200,
        )
        self._ensure_budget(effective_deadline, started)
        embedding_started = self._clock()
        embedding = self._embedding_client.embed_query(normalized_query, deadline=effective_deadline)
        embedding_ms = round((self._clock() - embedding_started) * 1000, 1)
        vector_literal = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"
        doc_ids = [pair[0] for pair in pairs]
        revision_ids = [pair[1] for pair in pairs]

        remaining_seconds = effective_deadline - self._clock()
        if remaining_seconds <= 0:
            raise self._budget_error(started)
        if remaining_seconds < 1:
            raise self._budget_error(started)
        connect_timeout_sec = min(
            max(1, math.ceil(self._settings.llmops_db_connect_timeout_ms / 1000)),
            math.floor(remaining_seconds),
        )
        statement_timeout_ms = self._remaining_statement_timeout_ms(effective_deadline, started)
        db_started = self._clock()
        try:
            with self._connect(
                **self._connection_kwargs(
                    connect_timeout_sec=connect_timeout_sec,
                    statement_timeout_ms=statement_timeout_ms,
                )
            ) as connection:
                self._ensure_budget(effective_deadline, started)
                with connection.cursor(row_factory=dict_row) as cursor:
                    self._ensure_budget(effective_deadline, started)
                    cursor.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                    self._ensure_budget(effective_deadline, started)
                    statement_timeout_ms = self._remaining_statement_timeout_ms(effective_deadline, started)
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{statement_timeout_ms}ms",),
                    )
                    self._ensure_budget(effective_deadline, started)
                    cursor.execute(
                        self._query,
                        (
                            doc_ids,
                            revision_ids,
                            vector_literal,
                            vector_literal,
                            vector_literal,
                            candidate_limit,
                            normalized_query,
                            normalized_query,
                            normalized_query,
                            candidate_limit,
                            normalized_query,
                            normalized_query,
                            normalized_query,
                            candidate_limit,
                            result_limit,
                        ),
                    )
                    rows = cursor.fetchall()
        except LlmopsSearchError:
            raise
        except Exception as exc:
            elapsed_ms = round((self._clock() - started) * 1000, 1)
            raise LlmopsSearchError(
                "llmops_retrieval_unavailable",
                "선택 문서의 근거를 검색할 수 없습니다.",
                elapsed_ms,
            ) from exc
        db_ms = round((self._clock() - db_started) * 1000, 1)

        allowed_pairs = set(pairs)
        for row in rows:
            returned_pair = (str(row.get("doc_id") or ""), str(row.get("revision_id") or ""))
            if returned_pair not in allowed_pairs:
                raise LlmopsSearchError(
                    "llmops_scope_violation",
                    "선택 문서 범위를 벗어난 검색 결과가 감지되어 응답을 중단했습니다.",
                    round((self._clock() - started) * 1000, 1),
                )
        accepted_rows = [
            row for row in rows if float(row.get("rrf_score") or 0.0) >= self._settings.llmops_retrieval_score_cutoff
        ]
        citations = [self._row_to_citation(index, row) for index, row in enumerate(accepted_rows, start=1)]
        elapsed_ms = round((self._clock() - started) * 1000, 1)
        candidate_count = int(rows[0].get("candidate_count") or 0) if rows else 0
        grounded = bool(citations)
        over_budget = self._clock() > effective_deadline
        log_completed = _logger.warning if over_budget else _logger.info
        log_completed(
            "LLMOps scoped retrieval completed: scope_count=%d candidates=%d results=%d elapsed_ms=%.1f over_budget=%s",
            len(pairs),
            candidate_count,
            len(citations),
            elapsed_ms,
            over_budget,
        )
        return ScopedRetrievalResult(
            citations=citations,
            metadata=RetrievalMetadata(
                trace_id=uuid.uuid4(),
                scope=[RetrievalScope(doc_id=doc_id, revision_id=revision_id) for doc_id, revision_id in pairs],
                result_count=len(citations),
                candidate_count=candidate_count,
                grounded=grounded,
                decision="answerable" if grounded else "insufficient_evidence",
                timings_ms={"embedding": embedding_ms, "database": db_ms},
                elapsed_ms=elapsed_ms,
                over_budget=over_budget,
            ),
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
            "application_name": "automation_smb_scoped_retrieval",
            "options": (f"-c default_transaction_read_only=on -c statement_timeout={effective_statement_timeout_ms}"),
        }

    def _ensure_budget(self, deadline: float, started: float) -> None:
        if self._clock() >= deadline:
            raise self._budget_error(started)

    def _remaining_statement_timeout_ms(self, deadline: float, started: float) -> int:
        now = self._clock()
        if now >= deadline:
            raise self._budget_error(started)
        return min(
            self._settings.llmops_db_query_timeout_ms,
            max(1, math.floor((deadline - now) * 1000)),
        )

    def _budget_error(self, started: float) -> LlmopsSearchError:
        return LlmopsSearchError(
            "llmops_retrieval_budget_exhausted",
            "선택 문서 검색 시간 예산이 소진됐습니다.",
            round((self._clock() - started) * 1000, 1),
        )

    def _row_to_citation(self, index: int, row: dict[str, Any]) -> DocumentCitation:
        content = str(row.get("content") or "").strip()
        if len(content) > self._settings.llmops_chunk_max_chars:
            content = f"{content[: self._settings.llmops_chunk_max_chars]}…"
        raw_location = row.get("citation_anchor")
        location = {
            str(key): value
            for key, value in (raw_location.items() if isinstance(raw_location, dict) else [])
            if str(key) in self._LOCATION_KEYS
        }
        return DocumentCitation(
            index=index,
            doc_id=row["doc_id"],
            revision_id=row["revision_id"],
            chunk_id=row["chunk_id"],
            title=str(row.get("title") or "문서"),
            section_path=[str(value) for value in (row.get("section_path") or [])],
            location=location,
            excerpt=content,
            scores=RetrievalScores(
                vector=self._finite_or_none(row.get("vector_score")),
                lexical=self._finite_or_none(row.get("lexical_score")),
                trigram=self._finite_or_none(row.get("trigram_score")),
                rrf=round(float(row.get("rrf_score") or 0.0), 8),
            ),
        )

    @staticmethod
    def _finite_or_none(value: Any) -> float | None:
        if value is None:
            return None
        number = float(value)
        return round(number, 8) if math.isfinite(number) else None

    def close(self) -> None:
        """재사용 embedding 연결을 닫는다."""

        self._embedding_client.close()
