"""로컬 PostgreSQL/pgvector RAG 검색 서비스 테스트."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from smb_finder.config import Settings
from smb_finder.rag_search import OpenAICompatibleEmbeddingClient, RagSearchError, RagVectorSearcher


class FakeEmbeddingClient:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding
        self.queries: list[str] = []
        self.closed = False

    def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
        return self.embedding

    def close(self) -> None:
        self.closed = True


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql = ""
        self.params: tuple[Any, ...] = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self, **_kwargs) -> FakeCursor:
        return self._cursor


class FakeConnect:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor = FakeCursor(rows)
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs) -> FakeConnection:
        self.kwargs = kwargs
        return FakeConnection(self.cursor)


class RecordingSpan:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        self.record.setdefault("attributes", {}).update(attributes)

    def set_outputs(self, outputs: Any) -> None:
        self.record["outputs"] = outputs


class RecordingObserver:
    include_content = False

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    @contextmanager
    def span(
        self,
        *,
        name: str,
        span_type: str,
        inputs: Any | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[RecordingSpan]:
        record = {"name": name, "span_type": span_type, "inputs": inputs, "attributes": attributes or {}}
        self.records.append(record)
        yield RecordingSpan(record)


def _settings(**kwargs) -> Settings:
    return Settings(
        _env_file=None,
        rag_embedding_dimensions=3,
        rag_db_default_limit=1,
        rag_db_max_limit=2,
        rag_chunk_max_chars=100,
        **kwargs,
    )


def test_vector_search_uses_read_only_pgvector_query_and_clamps_limit():
    rows = [
        {
            "chunk_id": 10,
            "document_id": 2,
            "chunk_index": 4,
            "content": "1" * 103,
            "similarity": 0.87654321,
            "file_name": "wbs.md",
            "file_path": "docs/wbs.md",
            "section_path": "일정",
            "location_type": "heading",
            "location_label": "일정",
            "page_start": None,
            "page_end": None,
            "slide_start": None,
            "slide_end": None,
            "sheet_name": "",
        }
    ]
    embedding_client = FakeEmbeddingClient([0.1, 0.2, 0.3])
    connect = FakeConnect(rows)
    searcher = RagVectorSearcher(_settings(), embedding_client=embedding_client, connect=connect)

    response = searcher.search("프로젝트 일정은?", limit=99)

    assert embedding_client.queries == ["프로젝트 일정은?"]
    assert response.result_count == 1
    assert response.candidate_count == 1
    assert response.rejected_count == 0
    assert response.similarity_cutoff == 0.4
    assert response.top_similarity == 0.876543
    assert response.no_answer is False
    assert response.hits[0].content == f"{'1' * 100}…"
    assert response.hits[0].similarity == 0.876543
    assert response.embedding_ms >= 0
    assert response.db_ms >= 0
    assert response.elapsed_ms >= 0
    assert "ORDER BY c.embedding <=> %s::vector" in connect.cursor.sql
    assert connect.cursor.params[0] == connect.cursor.params[1]
    assert connect.cursor.params[2] == 2
    assert "default_transaction_read_only=on" in connect.kwargs["options"]
    assert "statement_timeout=1500" in connect.kwargs["options"]
    assert connect.kwargs["dbname"] == "rag_db_local_20260713"

    searcher.close()
    assert embedding_client.closed is True


def test_vector_search_filters_hits_below_similarity_cutoff():
    rows = [
        {
            "chunk_id": 10,
            "document_id": 2,
            "chunk_index": 1,
            "content": "충분한 합성 근거",
            "similarity": 0.61,
            "file_name": "alpha.md",
            "file_path": "docs/alpha.md",
        },
        {
            "chunk_id": 11,
            "document_id": 3,
            "chunk_index": 2,
            "content": "기준 미달 합성 후보",
            "similarity": 0.39,
            "file_name": "beta.md",
            "file_path": "docs/beta.md",
        },
    ]
    searcher = RagVectorSearcher(
        _settings(rag_similarity_cutoff=0.4),
        embedding_client=FakeEmbeddingClient([0.1, 0.2, 0.3]),
        connect=FakeConnect(rows),
    )

    response = searcher.search("합성 cutoff 질문", limit=2)

    assert [hit.chunk_id for hit in response.hits] == [10]
    assert response.result_count == 1
    assert response.candidate_count == 2
    assert response.rejected_count == 1
    assert response.top_similarity == 0.61
    assert response.no_answer is False


def test_vector_search_marks_no_answer_when_all_candidates_are_below_cutoff():
    rows = [
        {
            "chunk_id": 13,
            "document_id": 4,
            "chunk_index": 0,
            "content": "관련 없는 합성 후보",
            "similarity": 0.284252,
            "file_name": "unrelated.md",
            "file_path": "docs/unrelated.md",
        }
    ]
    searcher = RagVectorSearcher(
        _settings(rag_similarity_cutoff=0.4),
        embedding_client=FakeEmbeddingClient([0.1, 0.2, 0.3]),
        connect=FakeConnect(rows),
    )

    response = searcher.search("답이 없는 합성 질문")

    assert response.hits == []
    assert response.result_count == 0
    assert response.candidate_count == 1
    assert response.rejected_count == 1
    assert response.top_similarity == 0.284252
    assert response.no_answer is True


def test_vector_search_rejects_empty_query_before_db_connection():
    connect = FakeConnect([])
    searcher = RagVectorSearcher(_settings(), embedding_client=FakeEmbeddingClient([0.1, 0.2, 0.3]), connect=connect)

    with pytest.raises(RagSearchError, match="질문") as exc:
        searcher.search("  ")

    assert exc.value.code == "empty_query"
    assert connect.kwargs == {}


def test_vector_search_rejects_embedding_dimension_mismatch():
    searcher = RagVectorSearcher(_settings(), embedding_client=FakeEmbeddingClient([0.1, 0.2]), connect=FakeConnect([]))

    with pytest.raises(RagSearchError) as exc:
        searcher.search("차원 확인")

    assert exc.value.code == "embedding_dimension_mismatch"
    assert "기대 3" in exc.value.message


def test_vector_search_emits_retriever_embedding_and_db_spans_without_content():
    rows = [
        {
            "chunk_id": 10,
            "document_id": 2,
            "chunk_index": 4,
            "content": "trace에 기본 노출하지 않는 합성 본문",
            "similarity": 0.8,
            "file_name": "alpha.md",
            "file_path": "C:/hidden/alpha.md",
        }
    ]
    observer = RecordingObserver()
    searcher = RagVectorSearcher(
        _settings(),
        embedding_client=FakeEmbeddingClient([0.1, 0.2, 0.3]),
        connect=FakeConnect(rows),
        trace_observer=observer,
    )

    searcher.search("Alpha 질문")

    assert [record["name"] for record in observer.records] == [
        "rag.vector_search",
        "query_embedding",
        "rag.pgvector_query",
    ]
    retriever = observer.records[0]
    assert "query" not in retriever["inputs"]
    assert retriever["outputs"] == [{"doc_uri": "alpha.md", "chunk_id": 10, "similarity": 0.8}]
    assert retriever["attributes"]["retriever.similarity_cutoff"] == 0.4
    assert retriever["attributes"]["retriever.no_answer"] is False


def test_openai_compatible_client_adds_nomic_search_query_prefix(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    class FakeHttpClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        def post(self, path: str, *, json: dict[str, Any]):
            captured["path"] = path
            captured["json"] = json
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("smb_finder.rag_search.httpx.Client", FakeHttpClient)
    client = OpenAICompatibleEmbeddingClient(_settings())

    embedding = client.embed_query("로봇 경주 일정")

    assert embedding == [0.1, 0.2, 0.3]
    assert captured["path"] == "embeddings"
    assert captured["json"]["model"] == "nomic-embed-text-v2-moe"
    assert captured["json"]["input"] == "search_query: 로봇 경주 일정"
    client.close()
    assert captured["closed"] is True
