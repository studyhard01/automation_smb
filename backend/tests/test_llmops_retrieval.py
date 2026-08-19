"""선택 문서 scoped retrieval과 grounded 응답 계약을 검증한다."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from smb_finder.model_gateway import ModelGatewayError, ModelGatewayResult, ModelTokenUsage
from smb_finder.config import Settings
from smb_finder.llmops_retrieval import LlmopsScopedRetriever, OllamaQueryEmbeddingClient, ScopedRetrievalResult
from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.models import DocumentCitation, RetrievalMetadata, RetrievalScope, RetrievalScores
from smb_finder.playground.document_chat import DocumentChatService
from smb_finder.playground.document_models import ChatRequest, SelectedFileContext


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.deadlines: list[float | None] = []

    def embed_query(self, query: str, *, deadline: float | None = None) -> list[float]:
        self.queries.append(query)
        self.deadlines.append(deadline)
        return [0.1, 0.2, 0.3]

    def close(self) -> None:
        return None


class FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.executions: list[tuple[object, object | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def execute(self, query, parameters=None):  # noqa: ANN001
        self.executions.append((query, parameters))

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.cursor_instance = FakeCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):  # noqa: ANN002
        return None

    def cursor(self, **_kwargs):  # noqa: ANN003
        return self.cursor_instance


def _settings(tmp_path=None) -> Settings:  # noqa: ANN001
    payload = {
        "_env_file": None,
        "llmops_db_host": "db.test",
        "llmops_postgres_db": "llmops_test",
        "postgres_user": "reader",
        "postgres_password": "secret",
        "embedding_dim": 3,
        "llmops_retrieval_score_cutoff": 0.01,
        "llmops_chat_model": "server-chat-model",
        "ollama_base_url": "http://127.0.0.1:11434",
    }
    if tmp_path is not None:
        payload["playground_skills_dir"] = str(tmp_path / "skills")
    return Settings(**payload)


def test_default_local_model_budgets_cover_measured_cold_start() -> None:
    settings = Settings(_env_file=None)

    assert settings.llmops_embedding_timeout_ms == 8000
    assert settings.llm_timeout_ms == 30_000
    assert settings.playground_agent_budget_ms == 30_000


def _row(doc_id: UUID, revision_id: UUID, *, score: float = 0.02) -> dict:
    return {
        "doc_id": doc_id,
        "revision_id": revision_id,
        "chunk_id": uuid4(),
        "title": "Synthetic handbook",
        "section_path": ["Overview"],
        "citation_anchor": {
            "page": 2,
            "cell_range": "A1:B3",
            "source_uri": "s3://must-not-leak/private-key",
            "path": "must-not-leak/path",
        },
        "content": "Synthetic evidence only.",
        "vector_score": 0.91,
        "lexical_score": 0.73,
        "trigram_score": 0.42,
        "rrf_score": score,
        "candidate_count": 4,
    }


def test_scoped_retrieval_enforces_exact_pairs_and_safe_citations():
    doc_id = uuid4()
    revision_id = uuid4()
    connection = FakeConnection([_row(doc_id, revision_id)])
    captured: dict[str, object] = {}

    def connect(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return connection

    embedding = FakeEmbeddingClient()
    retriever = LlmopsScopedRetriever(
        _settings(),
        embedding_client=embedding,
        connect=connect,
        clock=lambda: 100.0,
    )
    result = retriever.retrieve(
        "synthetic overview",
        [(str(doc_id), str(revision_id))],
        deadline=101.25,
    )

    assert embedding.queries == ["synthetic overview"]
    assert embedding.deadlines == [101.25]
    assert result.metadata.grounded is True
    assert result.metadata.scope == [RetrievalScope(doc_id=doc_id, revision_id=revision_id)]
    assert result.citations[0].doc_id == doc_id
    assert result.citations[0].revision_id == revision_id
    assert result.citations[0].location == {"page": 2, "cell_range": "A1:B3"}
    assert "source_uri" not in result.citations[0].model_dump_json()
    assert "must-not-leak" not in result.citations[0].model_dump_json()
    assert "default_transaction_read_only=on" in str(captured["options"])
    assert "statement_timeout=1250" in str(captured["options"])

    _, parameters = connection.cursor_instance.executions[-1]
    assert parameters is not None
    assert parameters[0] == [str(doc_id)]
    assert parameters[1] == [str(revision_id)]
    sql_text = connection.cursor_instance.executions[-1][0].as_string()
    assert sql_text.count("JOIN selected AS s") == 4
    assert "active_revision_id = s.revision_id" in sql_text
    assert "INSERT " not in sql_text.upper()
    assert "UPDATE " not in sql_text.upper()
    assert "DELETE " not in sql_text.upper()


def test_scoped_retrieval_fails_closed_if_database_returns_out_of_scope_row():
    selected_doc_id = uuid4()
    selected_revision_id = uuid4()
    connection = FakeConnection([_row(uuid4(), uuid4())])
    retriever = LlmopsScopedRetriever(
        _settings(),
        embedding_client=FakeEmbeddingClient(),
        connect=lambda **_kwargs: connection,
    )

    with pytest.raises(LlmopsSearchError) as exc:
        retriever.retrieve("synthetic overview", [(str(selected_doc_id), str(selected_revision_id))])

    assert exc.value.code == "llmops_scope_violation"


def _retrieval_result(doc_id: UUID, revision_id: UUID, *, citations: bool) -> ScopedRetrievalResult:
    evidence = []
    if citations:
        evidence = [
            DocumentCitation(
                index=1,
                doc_id=doc_id,
                revision_id=revision_id,
                chunk_id=uuid4(),
                title="Synthetic handbook",
                section_path=["Overview"],
                location={"page": 2},
                excerpt="Synthetic evidence only.",
                scores=RetrievalScores(vector=0.91, lexical=0.73, trigram=0.42, rrf=0.02),
            )
        ]
    return ScopedRetrievalResult(
        citations=evidence,
        metadata=RetrievalMetadata(
            trace_id=uuid4(),
            scope=[RetrievalScope(doc_id=doc_id, revision_id=revision_id)],
            result_count=len(evidence),
            candidate_count=len(evidence),
            grounded=bool(evidence),
            decision="answerable" if evidence else "insufficient_evidence",
            elapsed_ms=12.5,
        ),
    )


class FakeRetriever:
    def __init__(self, result: ScopedRetrievalResult) -> None:
        self.result = result
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []
        self.deadlines: list[float | None] = []

    def retrieve(
        self,
        query: str,
        selections: list[tuple[str, str]],
        *,
        deadline: float | None = None,
    ) -> ScopedRetrievalResult:
        self.calls.append((query, selections))
        self.deadlines.append(deadline)
        return self.result


class FakeModelGateway:
    def __init__(self, payload: dict | None = None, error: ModelGatewayError | None = None) -> None:
        self.payload = payload or {"answer": "Grounded answer [1]."}
        self.error = error
        self.calls: list[dict] = []

    def invoke_json(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return ModelGatewayResult(
            payload=kwargs["response_model"].model_validate(self.payload),
            provider="ollama",
            model=kwargs["model"],
            usage=ModelTokenUsage(prompt_tokens=7, completion_tokens=3),
            elapsed_ms=4.0,
        )

    def close(self) -> None:
        return None


def _chat_request(doc_id: UUID, revision_id: UUID) -> ChatRequest:
    return ChatRequest(
        message="Summarize the selected synthetic document.",
        mode="document_qa",
        provider="local",
        selected_files=[
            SelectedFileContext(
                source="llmops",
                doc_id=doc_id,
                revision_id=revision_id,
                file_name="synthetic_handbook.md",
                title="Synthetic handbook",
            )
        ],
    )


def test_insufficient_evidence_never_invokes_llm(tmp_path):
    doc_id = uuid4()
    revision_id = uuid4()
    retriever = FakeRetriever(_retrieval_result(doc_id, revision_id, citations=False))
    gateway = FakeModelGateway()
    agent = DocumentChatService(_settings(tmp_path), gateway=gateway)
    response = agent.run(_chat_request(doc_id, revision_id), {}, scoped_retriever=retriever)

    assert response.error_code == ""
    assert response.retrieval is not None
    assert response.retrieval.decision == "insufficient_evidence"
    assert response.citations == []
    assert gateway.calls == []


def test_grounded_response_returns_citations_retrieval_and_artifact_links(tmp_path):
    doc_id = uuid4()
    revision_id = uuid4()
    retriever = FakeRetriever(_retrieval_result(doc_id, revision_id, citations=True))
    gateway = FakeModelGateway()
    agent = DocumentChatService(_settings(tmp_path), gateway=gateway)

    request = _chat_request(doc_id, revision_id).model_copy(update={"model": "client-model-must-be-ignored"})
    response = agent.run(request, {}, scoped_retriever=retriever)

    assert response.assistant_message == "Grounded answer [1]."
    assert response.retrieval is not None and response.retrieval.grounded is True
    assert response.citations[0].doc_id == doc_id
    assert response.artifacts[0].preview_url.endswith("/artifacts/preview")
    assert response.artifacts[0].canonical_url.endswith("/artifacts/canonical")
    assert response.artifacts[0].graph_url == f"/api/playground/files/{doc_id}/graph"
    assert all("s3://" not in artifact.model_dump_json() for artifact in response.artifacts)
    assert response.model_used == "server-chat-model"
    assert gateway.calls[0]["model"] == "server-chat-model"
    assert gateway.calls[0]["purpose"] == "document_answer"
    assert gateway.calls[0]["deadline"] == retriever.deadlines[0]
    assert gateway.calls[0]["response_model"].model_json_schema()["additionalProperties"] is False
    assert response.token_usage is not None and response.token_usage.total_tokens == 10


class FakeUploadManager:
    def __init__(self, result: ScopedRetrievalResult) -> None:
        self.result = result
        self.calls: list[tuple[str, list[tuple[UUID, UUID]]]] = []
        self.deadlines: list[float | None] = []

    def retrieve(
        self,
        query: str,
        selections: list[tuple[UUID, UUID]],
        *,
        deadline: float | None = None,
    ) -> ScopedRetrievalResult:
        self.calls.append((query, selections))
        self.deadlines.append(deadline)
        return self.result


def test_uploaded_file_can_answer_without_database_retriever(tmp_path):
    doc_id = uuid4()
    revision_id = uuid4()
    upload_manager = FakeUploadManager(_retrieval_result(doc_id, revision_id, citations=True))
    request = ChatRequest(
        message="Summarize the uploaded synthetic document.",
        selected_files=[
            SelectedFileContext(
                source="upload",
                doc_id=doc_id,
                revision_id=revision_id,
                file_name="synthetic-note.md",
                title="Synthetic note",
            )
        ],
    )
    agent = DocumentChatService(
        _settings(tmp_path),
        gateway=FakeModelGateway({"answer": "Uploaded evidence [1]."}),
    )

    response = agent.run(request, None, upload_manager=upload_manager)

    assert response.assistant_message == "Uploaded evidence [1]."
    assert response.citations[0].doc_id == doc_id
    assert response.artifacts == []
    assert upload_manager.calls == [(request.message, [(doc_id, revision_id)])]
    assert upload_manager.deadlines[0] is not None


def test_retrieval_does_not_start_embedding_or_database_after_deadline():
    embedding = FakeEmbeddingClient()
    connect_calls = 0

    def connect(**_kwargs):  # noqa: ANN003
        nonlocal connect_calls
        connect_calls += 1
        return FakeConnection([])

    retriever = LlmopsScopedRetriever(
        _settings(),
        embedding_client=embedding,
        connect=connect,
        clock=lambda: 100.0,
    )

    with pytest.raises(LlmopsSearchError) as captured:
        retriever.retrieve("synthetic overview", [(str(uuid4()), str(uuid4()))], deadline=100.0)

    assert captured.value.code == "llmops_retrieval_budget_exhausted"
    assert embedding.queries == []
    assert connect_calls == 0


def test_retrieval_does_not_start_database_when_embedding_consumes_deadline():
    now = [100.0]
    connect_calls = 0

    class BudgetConsumingEmbedding(FakeEmbeddingClient):
        def embed_query(self, query: str, *, deadline: float | None = None) -> list[float]:
            result = super().embed_query(query, deadline=deadline)
            now[0] = 100.25
            return result

    def connect(**_kwargs):  # noqa: ANN003
        nonlocal connect_calls
        connect_calls += 1
        return FakeConnection([])

    retriever = LlmopsScopedRetriever(
        _settings(),
        embedding_client=BudgetConsumingEmbedding(),
        connect=connect,
        clock=lambda: now[0],
    )

    with pytest.raises(LlmopsSearchError) as captured:
        retriever.retrieve("synthetic overview", [(str(uuid4()), str(uuid4()))], deadline=100.25)

    assert captured.value.code == "llmops_retrieval_budget_exhausted"
    assert connect_calls == 0


def test_retrieval_does_not_connect_when_less_than_one_second_remains():
    connect_calls = 0

    def connect(**_kwargs):  # noqa: ANN003
        nonlocal connect_calls
        connect_calls += 1
        return FakeConnection([])

    retriever = LlmopsScopedRetriever(
        _settings(),
        embedding_client=FakeEmbeddingClient(),
        connect=connect,
        clock=lambda: 100.0,
    )

    with pytest.raises(LlmopsSearchError) as captured:
        retriever.retrieve("synthetic overview", [(str(uuid4()), str(uuid4()))], deadline=100.5)

    assert captured.value.code == "llmops_retrieval_budget_exhausted"
    assert connect_calls == 0


def test_retrieval_recomputes_main_statement_timeout_after_connection():
    now = [100.0]
    captured: dict[str, object] = {}
    connection = FakeConnection([])

    def connect(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        now[0] = 101.0
        return connection

    settings = _settings().model_copy(
        update={
            "llmops_db_connect_timeout_ms": 5000,
            "llmops_db_query_timeout_ms": 5000,
        }
    )
    retriever = LlmopsScopedRetriever(
        settings,
        embedding_client=FakeEmbeddingClient(),
        connect=connect,
        clock=lambda: now[0],
    )

    retriever.retrieve("synthetic overview", [(str(uuid4()), str(uuid4()))], deadline=103.0)

    assert captured["connect_timeout"] == 3
    assert "statement_timeout=3000" in str(captured["options"])
    assert connection.cursor_instance.executions[1][1] == ("2000ms",)


def test_embedding_http_timeout_is_bounded_by_remaining_deadline():
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"embeddings": [[0.1, 0.2, 0.3]]}

    class FakeHttpClient:
        def __init__(self) -> None:
            self.timeout: float | None = None

        def post(self, _path: str, *, json: dict, timeout: float) -> FakeResponse:  # noqa: ARG002
            self.timeout = timeout
            return FakeResponse()

        def close(self) -> None:
            return None

    http_client = FakeHttpClient()
    embedding = OllamaQueryEmbeddingClient(_settings(), client=http_client, clock=lambda: 100.0)

    result = embedding.embed_query("synthetic overview", deadline=100.125)

    assert result == [0.1, 0.2, 0.3]
    assert http_client.timeout == pytest.approx(0.125)


def test_embedding_timeout_has_distinct_code_and_elapsed_time():
    now = [100.0]

    class TimeoutHttpClient:
        def post(self, _path: str, *, json: dict, timeout: float):  # noqa: ANN001, ANN201, ARG002
            assert timeout == pytest.approx(8.0)
            now[0] = 103.9
            raise httpx.ReadTimeout("synthetic private provider detail")

        def close(self) -> None:
            return None

    embedding = OllamaQueryEmbeddingClient(_settings(), client=TimeoutHttpClient(), clock=lambda: now[0])

    with pytest.raises(LlmopsSearchError) as captured:
        embedding.embed_query("synthetic overview", deadline=120.0)

    assert captured.value.code == "llmops_embedding_timeout"
    assert captured.value.elapsed_ms == pytest.approx(3900.0)
    assert "private provider detail" not in captured.value.message


def test_model_error_code_is_safe_and_preserves_server_citations(tmp_path):
    doc_id = uuid4()
    revision_id = uuid4()
    retriever = FakeRetriever(_retrieval_result(doc_id, revision_id, citations=True))
    gateway = FakeModelGateway(error=ModelGatewayError("model_timeout", retryable=True))
    service = DocumentChatService(_settings(tmp_path), gateway=gateway)

    response = service.run(_chat_request(doc_id, revision_id), scoped_retriever=retriever)

    assert response.error_code == "model_timeout"
    assert response.citations[0].doc_id == doc_id
    assert "internal" not in response.assistant_message.casefold()


def test_model_call_does_not_start_when_retrieval_consumes_deadline(tmp_path):
    now = [100.0]
    doc_id = uuid4()
    revision_id = uuid4()

    class BudgetConsumingRetriever(FakeRetriever):
        def retrieve(self, *args, **kwargs) -> ScopedRetrievalResult:  # noqa: ANN002, ANN003
            result = super().retrieve(*args, **kwargs)
            now[0] = 100.25
            return result

    retriever = BudgetConsumingRetriever(_retrieval_result(doc_id, revision_id, citations=True))
    gateway = FakeModelGateway()
    service = DocumentChatService(_settings(tmp_path), gateway=gateway, clock=lambda: now[0])

    response = service.run(
        _chat_request(doc_id, revision_id),
        scoped_retriever=retriever,
        deadline=100.25,
    )

    assert response.error_code == "model_budget_exhausted"
    assert response.citations[0].doc_id == doc_id
    assert gateway.calls == []
