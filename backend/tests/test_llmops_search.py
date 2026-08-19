"""LLMOps 문서 선택 검색 adapter의 읽기 전용 계약을 검증한다."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Event
from uuid import uuid4

import pytest

from smb_finder.model_gateway import ModelGatewayError, OllamaModelGateway
from smb_finder.config import Settings
from smb_finder.llmops_multistore_search import (
    LlmopsMultiStoreFileSearcher,
    LocalSearchQueryExpander,
    QueryExpansion,
)
from smb_finder.llmops_search import LlmopsFileSearcher, LlmopsSearchError
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

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, rows):  # noqa: ANN001
        self.cursor_instance = FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def cursor(self, **_kwargs):  # noqa: ANN003
        return self.cursor_instance


def _configured_settings(**overrides):  # noqa: ANN003, ANN202
    return Settings(
        _env_file=None,
        llmops_db_host="db.test",
        llmops_postgres_db="llmops_test",
        postgres_user="reader",
        postgres_password="synthetic-secret",
        **overrides,
    )


def test_document_metadata_resolves_active_revision_with_deadline_bounded_read_only_sql():
    doc_id = uuid4()
    revision_id = uuid4()
    now = [100.0]
    captured: dict[str, object] = {}
    connection = FakeConnection(
        [
            {
                "doc_id": doc_id,
                "revision_id": revision_id,
                "is_active": True,
                "revision_status": "ACTIVE",
                "extension": ".pdf",
                "file_size": 2048,
                "source_modified_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            }
        ]
    )

    def connect(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        now[0] = 100.4
        return connection

    settings = _configured_settings(llmops_db_connect_timeout_ms=5000, llmops_db_query_timeout_ms=5000)
    result = LlmopsFileSearcher(settings, connect=connect, clock=lambda: now[0]).get_document_metadata(
        doc_id,
        deadline=102.5,
    )

    assert result == {
        "source": "llmops",
        "doc_id": doc_id,
        "revision_id": revision_id,
        "is_active": True,
        "revision_status": "active",
        "extension": ".pdf",
        "size_bytes": 2048,
        "modified_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "elapsed_ms": 400.0,
        "over_budget": False,
        "degraded_dependencies": [],
    }
    assert captured["connect_timeout"] == 2
    assert "default_transaction_read_only=on" in str(captured["options"])
    assert "statement_timeout=2500" in str(captured["options"])
    configured_query_timeout = int(connection.cursor_instance.executions[0][1][0].removesuffix("ms"))
    assert 2099 <= configured_query_timeout <= 2100
    query, parameters = connection.cursor_instance.executions[1]
    sql_text = query.as_string().lower()
    assert parameters == (None, doc_id)
    assert "left join" in sql_text
    for forbidden in ("file_name", "title", "source_uri", "source_path", "source_item_id", "document_key", "body"):
        assert forbidden not in sql_text


def test_document_metadata_returns_requested_stale_revision_for_same_document():
    doc_id = uuid4()
    revision_id = uuid4()
    connection = FakeConnection(
        [
            {
                "doc_id": doc_id,
                "revision_id": revision_id,
                "is_active": False,
                "revision_status": "SUPERSEDED",
                "extension": ".txt",
                "file_size": None,
                "source_modified_at": None,
            }
        ]
    )
    searcher = LlmopsFileSearcher(_configured_settings(), connect=lambda **_kwargs: connection, clock=lambda: 10.0)

    result = searcher.get_document_metadata(doc_id, revision_id, deadline=12.0)

    assert result["revision_id"] == revision_id
    assert result["is_active"] is False
    assert result["revision_status"] == "superseded"
    assert connection.cursor_instance.executions[1][1] == (revision_id, doc_id)


@pytest.mark.parametrize("remaining", [0.0, 0.5])
def test_document_metadata_does_not_connect_without_enforceable_connect_budget(remaining: float):
    connect_calls = 0

    def connect(**_kwargs):  # noqa: ANN003
        nonlocal connect_calls
        connect_calls += 1
        return FakeConnection([])

    searcher = LlmopsFileSearcher(_configured_settings(), connect=connect, clock=lambda: 20.0)

    with pytest.raises(LlmopsSearchError) as captured:
        searcher.get_document_metadata(uuid4(), deadline=20.0 + remaining)

    assert captured.value.code == "metadata_budget_exhausted"
    assert connect_calls == 0


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([], "document_not_found"),
        ([{"doc_id": uuid4(), "revision_id": None}], "revision_not_found"),
    ],
)
def test_document_metadata_returns_stable_not_found_errors(rows, expected_code):  # noqa: ANN001
    searcher = LlmopsFileSearcher(
        _configured_settings(),
        connect=lambda **_kwargs: FakeConnection(rows),
        clock=lambda: 30.0,
    )

    with pytest.raises(LlmopsSearchError) as captured:
        searcher.get_document_metadata(uuid4(), deadline=32.0)

    assert captured.value.code == expected_code


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


@pytest.mark.parametrize("deadline", [100.0, 100.5])
def test_selection_validation_does_not_connect_without_enforceable_budget(deadline: float):
    connect_calls = 0

    def connect(**_kwargs):  # noqa: ANN003
        nonlocal connect_calls
        connect_calls += 1
        return FakeConnection([])

    searcher = LlmopsFileSearcher(
        Settings(_env_file=None),
        connect=connect,
        clock=lambda: 100.0,
    )

    with pytest.raises(LlmopsSearchError) as captured:
        searcher.validate_active_selections([("doc", "revision")], deadline=deadline)

    assert captured.value.code == "llmops_selection_validation_budget_exhausted"
    assert connect_calls == 0


def test_selection_validation_recomputes_statement_timeout_after_connection():
    doc_id = uuid4()
    revision_id = uuid4()
    now = [100.0]
    captured: dict[str, object] = {}
    connection = FakeConnection([(str(doc_id), str(revision_id))])

    def connect(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        now[0] = 101.0
        return connection

    searcher = LlmopsFileSearcher(
        Settings(
            _env_file=None,
            llmops_db_connect_timeout_ms=5000,
            llmops_db_query_timeout_ms=5000,
        ),
        connect=connect,
        clock=lambda: now[0],
    )

    valid = searcher.validate_active_selections(
        [(str(doc_id), str(revision_id))],
        deadline=103.0,
    )

    assert valid == {(str(doc_id), str(revision_id))}
    assert captured["connect_timeout"] == 3
    assert "statement_timeout=3000" in str(captured["options"])
    assert connection.cursor_instance.executions[0][1] == ("2000ms",)


def test_explicit_llmops_host_wins_over_environment_specific_fallback():
    settings = Settings(
        _env_file=None,
        llmops_db_host="explicit.test",
        dev_server="development.test",
        postgres_host="postgres",
    )

    assert settings.effective_llmops_db_host == "explicit.test"


class FakeHttpResponse:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload or {"message": {"content": '{"terms":["작업분류체계","일정표"]}'}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeHttpClient:
    def __init__(self, payload: dict | None = None) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict, float]] = []
        self.closed = False

    def post(self, url: str, *, json: dict, timeout: float) -> FakeHttpResponse:
        self.calls.append((url, json, timeout))
        return FakeHttpResponse(self.payload)

    def close(self) -> None:
        self.closed = True


def test_local_search_query_expander_uses_only_internal_ollama_query():
    client = FakeHttpClient()
    settings = Settings(
        _env_file=None,
        ollama_base_url="http://127.0.0.1:11434",
        llmops_chat_model="synthetic-model",
    )
    gateway = OllamaModelGateway(settings, client=client)
    expander = LocalSearchQueryExpander(settings, gateway=gateway)

    result = expander.expand("진검파트 WBS와 비슷한 일정 문서를 찾아줘")

    assert result.used is True
    assert result.warning == ""
    assert result.terms[-2:] == ["작업분류체계", "일정표"]
    assert "진검파트" in result.terms
    assert client.calls[0][0] == "http://127.0.0.1:11434/api/chat"
    serialized_payload = str(client.calls[0][1])
    assert "진검파트 WBS와 비슷한 일정 문서를 찾아줘" in serialized_payload
    assert "document_chunks" not in serialized_payload
    assert 0 < client.calls[0][2] <= 0.5


class TimeoutGateway:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_json(self, **_kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        raise ModelGatewayError("model_timeout", retryable=True, elapsed_ms=300)

    def close(self) -> None:
        return None


def test_query_expansion_timeout_immediately_falls_back_to_rule_terms():
    gateway = TimeoutGateway()
    expander = LocalSearchQueryExpander(
        Settings(
            _env_file=None,
            ollama_base_url="http://127.0.0.1:11434",
            llmops_chat_model="synthetic-model",
        ),
        gateway=gateway,
    )

    result = expander.expand("Synthetic Plan과 비슷한 자료를 찾아줘", deadline=time.monotonic() + 0.3)

    assert result.used is False
    assert result.terms[:2] == ["synthetic", "plan과"]
    assert result.warning == "search_llm_timeout"
    assert gateway.calls == 1


def test_query_expansion_missing_terms_falls_back_as_invalid_schema():
    settings = Settings(
        _env_file=None,
        ollama_base_url="http://127.0.0.1:11434",
        llmops_chat_model="synthetic-model",
    )
    gateway = OllamaModelGateway(
        settings,
        client=FakeHttpClient({"message": {"content": "{}"}}),
    )
    expander = LocalSearchQueryExpander(settings, gateway=gateway)

    result = expander.expand("Synthetic Plan과 비슷한 자료를 찾아줘")

    assert result.used is False
    assert result.terms[:2] == ["synthetic", "plan과"]
    assert result.warning == "search_llm_unavailable"


def test_simple_keyword_query_uses_zero_model_calls():
    gateway = TimeoutGateway()
    expander = LocalSearchQueryExpander(
        Settings(
            _env_file=None,
            ollama_base_url="http://127.0.0.1:11434",
            llmops_chat_model="synthetic-model",
        ),
        gateway=gateway,
    )

    result = expander.expand("synthetic aurora project document")

    assert result.used is False
    assert result.terms == ["synthetic", "aurora", "project", "document"]
    assert result.warning == ""
    assert gateway.calls == 0


class StaticExpander:
    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    def expand(self, _query: str, *, deadline: float | None = None) -> QueryExpansion:
        self.deadlines.append(deadline)
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
                update={
                    "matched_stores": ["postgresql", *sorted(refs[(str(self.hit.doc_id), str(self.hit.revision_id))])]
                },
            )
        ]

    def validate_active_selections(self, selections, *, deadline=None):  # noqa: ANN001, ANN202
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
    expander = StaticExpander()
    searcher = LlmopsMultiStoreFileSearcher(
        Settings(_env_file=None, llmops_file_search_budget_ms=8000),
        postgres_searcher=primary,
        artifact_reader=minio,
        graph_reader=neo4j,
        query_expander=expander,
    )

    response = searcher.search(DocumentSearchRequest(query="synthetic plan", limit=5))

    assert response.search_mode == "multistore"
    assert response.queried_stores == ["postgresql", "minio", "neo4j"]
    assert response.llm_expanded is True
    assert response.warnings == []
    assert response.hits[0].matched_stores == ["postgresql", "minio", "neo4j"]
    assert primary.search_terms == ["synthetic", "plan"]
    assert minio.calls and neo4j.calls
    assert expander.deadlines[0] is not None
    assert primary.hydrated_refs[(str(doc_id), str(revision_id))] == {"minio", "neo4j"}
    assert {"llm_query_expansion", "postgresql", "minio", "neo4j", "postgresql_hydrate"} <= set(response.timings_ms)


class BlockingExternalSearcher(FakeExternalSearcher):
    def __init__(self, pair: tuple[str, str], release: Event) -> None:
        super().__init__(pair)
        self.release = release
        self.active = Event()
        self.completed = Event()

    def search_document_refs(self, terms, *, limit):  # noqa: ANN001
        self.calls.append((list(terms), limit))
        self.active.set()
        try:
            self.release.wait(timeout=2)
            return {self.pair}
        finally:
            self.active.clear()
            self.completed.set()


def test_consecutive_search_preserves_postgresql_capacity_when_optional_workers_are_saturated():
    doc_id = uuid4()
    revision_id = uuid4()
    hit = DocumentSearchHit(
        doc_id=doc_id,
        revision_id=revision_id,
        file_name="synthetic-plan.md",
        title="Synthetic plan",
        score=1.0,
    )
    release = Event()
    minio = BlockingExternalSearcher((str(doc_id), str(revision_id)), release)
    neo4j = BlockingExternalSearcher((str(doc_id), str(revision_id)), release)
    searcher = LlmopsMultiStoreFileSearcher(
        Settings(_env_file=None, llmops_file_search_budget_ms=500),
        postgres_searcher=FakePrimarySearcher(hit),
        artifact_reader=minio,
        graph_reader=neo4j,
        query_expander=StaticExpander(),
    )

    started = time.monotonic()
    try:
        first_response = searcher.search(DocumentSearchRequest(query="synthetic plan", limit=5))
        first_elapsed = time.monotonic() - started
        worker_threads = tuple(searcher._executor._threads)  # noqa: SLF001
        second_started = time.monotonic()
        second_response = searcher.search(DocumentSearchRequest(query="synthetic plan", limit=5))
        second_elapsed = time.monotonic() - second_started

        assert first_elapsed < 0.8
        assert first_response.queried_stores == ["postgresql", "minio", "neo4j"]
        assert second_elapsed < 0.2
        assert second_response.result_count == 1
        assert second_response.queried_stores == ["postgresql"]
        assert {"minio_search_degraded", "neo4j_search_degraded"} <= set(second_response.warnings)
        assert minio.completed.is_set() is False
        assert neo4j.completed.is_set() is False
        assert minio.active.is_set() is True
        assert neo4j.active.is_set() is True
        assert len(minio.calls) == 1
        assert len(neo4j.calls) == 1
    finally:
        release.set()
        searcher.close()
    assert minio.completed.is_set() is True
    assert neo4j.completed.is_set() is True
    assert minio.active.is_set() is False
    assert neo4j.active.is_set() is False
    assert worker_threads and all(not thread.is_alive() for thread in worker_threads)


def test_multistore_search_does_not_wait_past_deadline_for_hydration():
    doc_id = uuid4()
    revision_id = uuid4()
    hit = DocumentSearchHit(
        doc_id=doc_id,
        revision_id=revision_id,
        file_name="synthetic-plan.md",
        title="Synthetic plan",
        score=1.0,
    )
    release = Event()

    class BlockingHydrationPrimary(FakePrimarySearcher):
        def __init__(self, target: DocumentSearchHit) -> None:
            super().__init__(target)
            self.completed = Event()

        def hydrate_document_refs(self, refs, *, limit):  # noqa: ANN001, ARG002
            self.hydrated_refs = refs
            release.wait(timeout=2)
            self.completed.set()
            return []

    primary = BlockingHydrationPrimary(hit)
    external = FakeExternalSearcher((str(doc_id), str(revision_id)))
    searcher = LlmopsMultiStoreFileSearcher(
        Settings(_env_file=None, llmops_file_search_budget_ms=500),
        postgres_searcher=primary,
        artifact_reader=external,
        graph_reader=None,
        query_expander=StaticExpander(),
    )

    started = time.monotonic()
    try:
        response = searcher.search(DocumentSearchRequest(query="synthetic plan", limit=5))
        elapsed = time.monotonic() - started

        assert elapsed < 0.8
        assert primary.completed.is_set() is False
        assert response.result_count == 1
        assert "postgresql_hydrate_degraded" in response.warnings
        assert "postgresql_hydrate" not in response.timings_ms
    finally:
        release.set()
        searcher.close()
    assert primary.completed.is_set() is True


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
