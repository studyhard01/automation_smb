"""LLMOps 문서 선택 검색 adapter의 읽기 전용 계약을 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from smb_finder.config import Settings
from smb_finder.llmops_multistore_search import (
    LlmopsMultiStoreFileSearcher,
    LocalSearchQueryExpander,
    QueryExpansion,
)
from smb_finder.llmops_search import LlmopsFileSearcher
from smb_finder.models import DocumentSearchHit, DocumentSearchRequest, DocumentSearchResponse, StoreConnectionState


class FakeCursor:
    def __init__(self, rows):  # noqa: ANN001
        self.rows = rows
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def execute(self, query, parameters):  # noqa: ANN001
        self.executions.append((query, parameters))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows):  # noqa: ANN001
        self.cursor_instance = FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def cursor(self, **_kwargs):  # noqa: ANN003
        return self.cursor_instance


def test_llmops_file_search_returns_active_document_metadata_without_content():
    doc_id = uuid4()
    revision_id = uuid4()
    rows = [
        {
            "doc_id": doc_id,
            "revision_id": revision_id,
            "file_name": "synthetic_wbs.xlsx",
            "title": "합성 WBS",
            "extension": ".xlsx",
            "file_size": 4096,
            "source_modified_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "score": 1.25,
            "match_source": "metadata",
        }
    ]
    captured = {}

    def connect(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return FakeConnection(rows)

    settings = Settings(
        _env_file=None,
        llmops_db_host="db.test",
        llmops_postgres_db="llmops_test",
        postgres_user="reader",
        postgres_password="secret",
    )
    response = LlmopsFileSearcher(settings, connect=connect).search(
        DocumentSearchRequest(query="합성 WBS 찾아줘", limit=5)
    )

    assert response.normalized_query == "합성 WBS"
    assert response.result_count == 1
    assert response.hits[0].doc_id == doc_id
    assert response.hits[0].revision_id == revision_id
    assert response.hits[0].file_name == "synthetic_wbs.xlsx"
    assert not hasattr(response.hits[0], "content")
    assert "default_transaction_read_only=on" in captured["options"]
    assert captured["application_name"] == "automation_smb_file_search"


def test_file_search_collapses_same_physical_file_but_preserves_same_name_in_other_paths():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    duplicate_uri = "smb://synthetic/share/team/report.xlsx"
    rows = [
        {
            "doc_id": uuid4(),
            "revision_id": uuid4(),
            "file_name": "report.xlsx",
            "title": "낮은 점수 중복",
            "extension": ".xlsx",
            "file_size": 10,
            "source_modified_at": now,
            "source_uri": duplicate_uri,
            "source_path": "team/report.xlsx",
            "source_item_id": "source-a",
            "score": 0.8,
            "match_source": "metadata",
        },
        {
            "doc_id": uuid4(),
            "revision_id": uuid4(),
            "file_name": "report.xlsx",
            "title": "높은 점수 대표",
            "extension": ".xlsx",
            "file_size": 10,
            "source_modified_at": now,
            "source_uri": duplicate_uri,
            "source_path": "team/report.xlsx",
            "source_item_id": "source-b",
            "score": 1.2,
            "match_source": "metadata",
        },
        {
            "doc_id": uuid4(),
            "revision_id": uuid4(),
            "file_name": "report.xlsx",
            "title": "다른 경로 1",
            "extension": ".xlsx",
            "file_size": 10,
            "source_modified_at": now,
            "source_uri": "",
            "source_path": "other-a",
            "source_item_id": "item-a",
            "score": 1.0,
            "match_source": "metadata",
        },
        {
            "doc_id": uuid4(),
            "revision_id": uuid4(),
            "file_name": "report.xlsx",
            "title": "다른 경로 2",
            "extension": ".xlsx",
            "file_size": 10,
            "source_modified_at": now,
            "source_uri": "",
            "source_path": "other-b",
            "source_item_id": "item-b",
            "score": 0.9,
            "match_source": "metadata",
        },
    ]
    connection = FakeConnection(rows)
    settings = Settings(_env_file=None, llmops_file_search_max_limit=20)

    response = LlmopsFileSearcher(settings, connect=lambda **_kwargs: connection).search(
        DocumentSearchRequest(query="report", limit=3)
    )

    assert response.result_count == 3
    assert [hit.title for hit in response.hits] == ["높은 점수 대표", "다른 경로 1", "다른 경로 2"]
    assert connection.cursor_instance.executions[0][1][-1] == 20
    serialized = response.model_dump_json()
    assert "source_uri" not in serialized
    assert "source_path" not in serialized
    assert "source_item_id" not in serialized


def test_external_hydration_collapses_same_physical_file_and_merges_store_matches():
    first_doc, first_revision = uuid4(), uuid4()
    second_doc, second_revision = uuid4(), uuid4()
    rows = [
        {
            "doc_id": first_doc,
            "revision_id": first_revision,
            "file_name": "same.pdf",
            "title": "첫 적재",
            "extension": ".pdf",
            "file_size": 10,
            "source_modified_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "source_uri": "smb://synthetic/share/same.pdf",
            "source_path": "share/same.pdf",
            "source_item_id": "item-1",
            "score": 0.75,
            "match_source": "metadata",
        },
        {
            "doc_id": second_doc,
            "revision_id": second_revision,
            "file_name": "same.pdf",
            "title": "두 번째 적재",
            "extension": ".pdf",
            "file_size": 10,
            "source_modified_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
            "source_uri": "smb://synthetic/share/same.pdf",
            "source_path": "share/same.pdf",
            "source_item_id": "item-2",
            "score": 0.75,
            "match_source": "metadata",
        },
    ]
    refs = {
        (str(first_doc), str(first_revision)): {"minio"},
        (str(second_doc), str(second_revision)): {"neo4j"},
    }
    searcher = LlmopsFileSearcher(Settings(_env_file=None), connect=lambda **_kwargs: FakeConnection(rows))

    hits = searcher.hydrate_document_refs(refs, limit=5)

    assert len(hits) == 1
    assert hits[0].title == "두 번째 적재"
    assert hits[0].matched_stores == ["postgresql", "minio", "neo4j"]


def test_explicit_llmops_host_wins_over_environment_specific_fallback():
    settings = Settings(
        _env_file=None,
        llmops_db_host="explicit.test",
        dev_server="development.test",
        postgres_host="postgres",
    )

    assert settings.effective_llmops_db_host == "explicit.test"


class FakeHttpResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": '{"terms":["작업분류체계","일정표"]}'}}


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url: str, *, json: dict) -> FakeHttpResponse:
        self.calls.append((url, json))
        return FakeHttpResponse()

    def close(self) -> None:
        self.closed = True


def test_local_search_query_expander_uses_only_internal_ollama_query():
    client = FakeHttpClient()
    settings = Settings(
        _env_file=None,
        ollama_base_url="http://127.0.0.1:11434",
        llmops_chat_model="synthetic-model",
    )
    expander = LocalSearchQueryExpander(settings, client=client)

    result = expander.expand("진검파트 WBS 찾아줘")

    assert result.used is True
    assert result.warning == ""
    assert result.terms == ["진검파트", "wbs", "작업분류체계", "일정표"]
    assert client.calls[0][0] == "http://127.0.0.1:11434/api/chat"
    serialized_payload = str(client.calls[0][1])
    assert "진검파트 WBS 찾아줘" in serialized_payload
    assert "document_chunks" not in serialized_payload


class StaticExpander:
    def expand(self, _query: str) -> QueryExpansion:
        return QueryExpansion(terms=["synthetic", "plan"], used=True, elapsed_ms=11.0)

    def close(self) -> None:
        return None


class FakePrimarySearcher:
    def __init__(self, hit: DocumentSearchHit) -> None:
        self.hit = hit
        self.search_terms: list[str] = []
        self.hydrated_refs: dict[tuple[str, str], set[str]] = {}

    def search(self, request: DocumentSearchRequest, *, search_terms):  # noqa: ANN001
        self.search_terms = list(search_terms)
        return DocumentSearchResponse(
            query=request.query,
            normalized_query="synthetic plan",
            hits=[self.hit],
            result_count=1,
            elapsed_ms=5,
            queried_stores=["postgresql"],
        )

    def hydrate_document_refs(self, refs, *, limit):  # noqa: ANN001, ARG002
        self.hydrated_refs = refs
        return [
            self.hit.model_copy(
                update={"matched_stores": ["postgresql", *sorted(refs[(str(self.hit.doc_id), str(self.hit.revision_id))])]},
            )
        ]

    def validate_active_selections(self, selections):  # noqa: ANN001
        return set(selections)

    def status(self) -> StoreConnectionState:
        return StoreConnectionState(configured=True, connected=True, message="connected")


class FakeExternalSearcher:
    def __init__(self, pair: tuple[str, str]) -> None:
        self.pair = pair
        self.calls: list[tuple[list[str], int]] = []

    def search_document_refs(self, terms, *, limit):  # noqa: ANN001
        self.calls.append((list(terms), limit))
        return {self.pair}


def test_multistore_search_uses_postgresql_minio_neo4j_and_llm_expansion():
    doc_id = uuid4()
    revision_id = uuid4()
    hit = DocumentSearchHit(
        doc_id=doc_id,
        revision_id=revision_id,
        file_name="synthetic-plan.md",
        title="Synthetic plan",
        score=1.0,
    )
    primary = FakePrimarySearcher(hit)
    minio = FakeExternalSearcher((str(doc_id), str(revision_id)))
    neo4j = FakeExternalSearcher((str(doc_id), str(revision_id)))
    searcher = LlmopsMultiStoreFileSearcher(
        Settings(_env_file=None),
        postgres_searcher=primary,
        artifact_reader=minio,
        graph_reader=neo4j,
        query_expander=StaticExpander(),
    )

    response = searcher.search(DocumentSearchRequest(query="synthetic plan", limit=5))

    assert response.search_mode == "multistore"
    assert response.queried_stores == ["postgresql", "minio", "neo4j"]
    assert response.llm_expanded is True
    assert response.warnings == []
    assert response.hits[0].matched_stores == ["postgresql", "minio", "neo4j"]
    assert primary.search_terms == ["synthetic", "plan"]
    assert minio.calls and neo4j.calls
    assert primary.hydrated_refs[(str(doc_id), str(revision_id))] == {"minio", "neo4j"}
    assert {"llm_query_expansion", "postgresql", "minio", "neo4j", "postgresql_hydrate"} <= set(
        response.timings_ms
    )


def test_multistore_collapses_same_physical_file_across_different_document_ids():
    first = DocumentSearchHit(
        doc_id=uuid4(),
        revision_id=uuid4(),
        file_name="same.xlsx",
        title="대표",
        score=1.2,
    )
    second = DocumentSearchHit(
        doc_id=uuid4(),
        revision_id=uuid4(),
        file_name="same.xlsx",
        title="중복",
        score=0.9,
    )
    first.set_physical_identity(source_uri="smb://synthetic/share/same.xlsx")
    second.set_physical_identity(source_uri="smb://synthetic/share/same.xlsx")

    class DuplicatePrimary(FakePrimarySearcher):
        def search(self, request: DocumentSearchRequest, *, search_terms):  # noqa: ANN001
            return DocumentSearchResponse(
                query=request.query,
                normalized_query=request.query,
                hits=[first, second],
                result_count=2,
                elapsed_ms=1,
                queried_stores=["postgresql"],
            )

    searcher = LlmopsMultiStoreFileSearcher(
        Settings(_env_file=None),
        postgres_searcher=DuplicatePrimary(first),
        artifact_reader=None,
        graph_reader=None,
        query_expander=StaticExpander(),
    )

    response = searcher.search(DocumentSearchRequest(query="same", limit=5))

    assert response.result_count == 1
    assert response.hits[0].title == "대표"
