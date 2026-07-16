"""로컬 PostgreSQL의 pgvector chunk를 검색하는 RAG 서비스."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from .config import Settings
from .telemetry import NoopTraceObserver, TraceObserver


class RagChunkHit(BaseModel):
    """벡터 검색으로 찾은 문서 chunk 한 건."""

    chunk_id: int
    document_id: int
    chunk_index: int
    content: str
    similarity: float
    file_name: str = ""
    file_path: str = ""
    section_path: str = ""
    location_type: str = ""
    location_label: str = ""
    page_start: int | None = None
    page_end: int | None = None
    slide_start: int | None = None
    slide_end: int | None = None
    sheet_name: str = ""


class RagSearchResponse(BaseModel):
    """RAG vector search 결과와 단계별 지연 측정값."""

    query: str
    hits: list[RagChunkHit] = Field(default_factory=list)
    result_count: int = 0
    embedding_model: str
    embedding_ms: float = 0.0
    db_ms: float = 0.0
    elapsed_ms: float = 0.0
    over_budget: bool = False


class RagSearchError(RuntimeError):
    """UI/tool 계층에서 안전하게 표시할 수 있는 RAG 검색 오류."""

    def __init__(self, code: str, message: str, elapsed_ms: float = 0.0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.elapsed_ms = elapsed_ms


class EmbeddingClient(Protocol):
    """테스트에서 교체 가능한 질의 임베딩 client 계약."""

    def embed_query(self, query: str) -> list[float]: ...

    def close(self) -> None: ...


class OpenAICompatibleEmbeddingClient:
    """OpenAI 호환 `/embeddings` endpoint를 재사용하는 로컬 client."""

    def __init__(self, settings: Settings) -> None:
        self._model = settings.rag_embedding_model.strip()
        self._dimensions = settings.rag_embedding_dimensions
        self._query_prefix = settings.rag_embedding_query_prefix
        base_url = f"{settings.rag_embedding_base_url.strip().rstrip('/')}/"
        api_key = settings.rag_embedding_api_key.strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=max(0.1, settings.rag_embedding_timeout_ms / 1000),
        )

    def embed_query(self, query: str) -> list[float]:
        """DB 적재 모델과 같은 query prefix로 1개 질의를 임베딩한다."""

        prefixed_query = query if query.startswith(self._query_prefix) else f"{self._query_prefix}{query}"
        try:
            response = self._client.post(
                "embeddings",
                json={"model": self._model, "input": prefixed_query, "encoding_format": "float"},
            )
            response.raise_for_status()
            payload = response.json()
            embedding = [float(value) for value in payload["data"][0]["embedding"]]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RagSearchError(
                "embedding_unavailable",
                "DB와 동일한 로컬 임베딩 endpoint에 연결할 수 없습니다. RAG_EMBEDDING_BASE_URL과 모델을 확인하세요.",
            ) from exc

        if len(embedding) != self._dimensions:
            raise RagSearchError(
                "embedding_dimension_mismatch",
                f"질의 임베딩 차원이 DB와 다릅니다(기대 {self._dimensions}, 실제 {len(embedding)}).",
            )
        if not all(math.isfinite(value) for value in embedding):
            raise RagSearchError("embedding_response_invalid", "질의 임베딩에 유효하지 않은 숫자가 포함되어 있습니다.")
        return embedding

    def close(self) -> None:
        """재사용 HTTP 연결을 종료한다."""

        self._client.close()


class RagVectorSearcher:
    """질의 임베딩을 만들고 pgvector cosine 검색을 실행한다."""

    _SEARCH_SQL = """
        SELECT
            c.id AS chunk_id,
            c.document_id,
            c.chunk_index,
            c.content,
            1 - (c.embedding <=> %s::vector) AS similarity,
            d.file_name,
            d.file_path,
            COALESCE(c.section_path, '') AS section_path,
            COALESCE(c.location_type, '') AS location_type,
            COALESCE(c.location_label, '') AS location_label,
            c.page_start,
            c.page_end,
            c.slide_start,
            c.slide_end,
            COALESCE(c.sheet_name, '') AS sheet_name
        FROM document_chunks AS c
        JOIN documents AS d ON d.id = c.document_id
        WHERE c.embedding IS NOT NULL
          AND d.deleted_at IS NULL
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """

    def __init__(
        self,
        settings: Settings,
        *,
        embedding_client: EmbeddingClient | None = None,
        connect: Callable[..., Any] | None = None,
        trace_observer: TraceObserver | None = None,
    ) -> None:
        self._settings = settings
        self._embedding_client = embedding_client or OpenAICompatibleEmbeddingClient(settings)
        self._connect = connect or psycopg.connect
        self._trace_observer = trace_observer or NoopTraceObserver()

    def search(self, query: str, limit: int | None = None) -> RagSearchResponse:
        """벡터 검색 전체를 retriever span으로 관측하고 민감한 본문은 기본적으로 제외한다."""

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise RagSearchError("empty_query", "DB에서 검색할 질문을 입력하세요.")
        result_limit = max(1, min(int(limit or self._settings.rag_db_default_limit), self._settings.rag_db_max_limit))
        trace_inputs: dict[str, Any] = {
            "query_length": len(normalized_query),
            "top_k": result_limit,
            "embedding_model": self._settings.rag_embedding_model,
        }
        if self._trace_observer.include_content:
            trace_inputs["query"] = normalized_query

        with self._trace_observer.span(
            name="rag.vector_search",
            span_type="RETRIEVER",
            inputs=trace_inputs,
            attributes={"retriever.top_k": result_limit},
        ) as span:
            response = self._search_impl(normalized_query, result_limit)
            documents = [
                {
                    "doc_uri": hit.file_name,
                    "chunk_id": hit.chunk_id,
                    "similarity": hit.similarity,
                    **({"page_content": hit.content} if self._trace_observer.include_content else {}),
                }
                for hit in response.hits
            ]
            span.set_attributes(
                {
                    "retriever.result_count": response.result_count,
                    "retriever.embedding_ms": response.embedding_ms,
                    "retriever.db_ms": response.db_ms,
                    "retriever.elapsed_ms": response.elapsed_ms,
                    "retriever.over_budget": response.over_budget,
                }
            )
            span.set_outputs(documents)
            return response

    def _search_impl(self, query: str, limit: int | None = None) -> RagSearchResponse:
        """자연어 질의와 가까운 chunk를 반환하고 단계별 소요 시간을 기록한다."""

        started = time.perf_counter()
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise RagSearchError("empty_query", "DB에서 검색할 질문을 입력하세요.")
        result_limit = max(1, min(int(limit or self._settings.rag_db_default_limit), self._settings.rag_db_max_limit))

        embedding_started = time.perf_counter()
        embedding_inputs: dict[str, Any] = {"query_length": len(normalized_query)}
        if self._trace_observer.include_content:
            embedding_inputs["query"] = normalized_query
        with self._trace_observer.span(
            name="query_embedding",
            span_type="EMBEDDING",
            inputs=embedding_inputs,
            attributes={"embedding.model": self._settings.rag_embedding_model},
        ) as embedding_span:
            try:
                embedding = self._embedding_client.embed_query(normalized_query)
            except RagSearchError as exc:
                exc.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                raise
            embedding_span.set_attributes({"embedding.dimensions": len(embedding)})
            embedding_span.set_outputs({"dimensions": len(embedding)})
        if len(embedding) != self._settings.rag_embedding_dimensions:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            raise RagSearchError(
                "embedding_dimension_mismatch",
                (
                    "질의 임베딩 차원이 DB와 다릅니다"
                    f"(기대 {self._settings.rag_embedding_dimensions}, 실제 {len(embedding)})."
                ),
                elapsed_ms,
            )
        if not all(math.isfinite(value) for value in embedding):
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            raise RagSearchError(
                "embedding_response_invalid",
                "질의 임베딩에 유효하지 않은 숫자가 포함되어 있습니다.",
                elapsed_ms,
            )
        embedding_ms = round((time.perf_counter() - embedding_started) * 1000, 1)
        vector_literal = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"

        db_started = time.perf_counter()
        with self._trace_observer.span(
            name="rag.pgvector_query",
            span_type="TASK",
            inputs={"top_k": result_limit},
            attributes={"db.system": "postgresql", "db.operation": "vector_search"},
        ) as db_span:
            try:
                connect_timeout_sec = max(1, math.ceil(self._settings.rag_db_connect_timeout_ms / 1000))
                options = (
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={self._settings.rag_db_query_timeout_ms}"
                )
                with self._connect(
                    host=self._settings.rag_db_host,
                    port=self._settings.rag_db_port,
                    dbname=self._settings.rag_db_name,
                    user=self._settings.rag_db_user,
                    password=self._settings.rag_db_password,
                    connect_timeout=connect_timeout_sec,
                    application_name="automation_smb_rag_search",
                    options=options,
                ) as connection:
                    with connection.cursor(row_factory=dict_row) as cursor:
                        cursor.execute(self._SEARCH_SQL, (vector_literal, vector_literal, result_limit))
                        rows = cursor.fetchall()
                db_span.set_attributes({"db.result_count": len(rows)})
                db_span.set_outputs({"result_count": len(rows)})
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                raise RagSearchError(
                    "rag_db_unavailable",
                    "로컬 RAG DB 벡터 검색에 실패했습니다. PostgreSQL 실행 상태와 RAG_DB_* 설정을 확인하세요.",
                    elapsed_ms,
                ) from exc
        db_ms = round((time.perf_counter() - db_started) * 1000, 1)

        hits = [self._row_to_hit(row) for row in rows]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        total_budget_ms = self._settings.rag_embedding_timeout_ms + self._settings.rag_db_query_timeout_ms
        return RagSearchResponse(
            query=normalized_query,
            hits=hits,
            result_count=len(hits),
            embedding_model=self._settings.rag_embedding_model,
            embedding_ms=embedding_ms,
            db_ms=db_ms,
            elapsed_ms=elapsed_ms,
            over_budget=elapsed_ms > total_budget_ms,
        )

    def _row_to_hit(self, row: dict[str, Any]) -> RagChunkHit:
        content = str(row.get("content") or "").strip()
        if len(content) > self._settings.rag_chunk_max_chars:
            content = f"{content[: self._settings.rag_chunk_max_chars]}…"
        return RagChunkHit(
            chunk_id=int(row["chunk_id"]),
            document_id=int(row["document_id"]),
            chunk_index=int(row["chunk_index"]),
            content=content,
            similarity=round(float(row.get("similarity") or 0.0), 6),
            file_name=str(row.get("file_name") or ""),
            file_path=str(row.get("file_path") or ""),
            section_path=str(row.get("section_path") or ""),
            location_type=str(row.get("location_type") or ""),
            location_label=str(row.get("location_label") or ""),
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            slide_start=row.get("slide_start"),
            slide_end=row.get("slide_end"),
            sheet_name=str(row.get("sheet_name") or ""),
        )

    def close(self) -> None:
        """질의 임베딩 HTTP 연결을 종료한다."""

        self._embedding_client.close()
