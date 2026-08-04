"""선택 문서 scoped retrieval과 grounded 응답 계약을 검증한다."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from smb_finder.config import Settings
from smb_finder.llmops_retrieval import LlmopsScopedRetriever, ScopedRetrievalResult
from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.models import DocumentCitation, RetrievalMetadata, RetrievalScope, RetrievalScores
from smb_finder.playground.document_chat import DocumentChatService
from smb_finder.playground.document_models import ChatRequest, SelectedFileContext


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_query(self, query: str) -> list[float]:
        self.queries.append(query)
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
        "llm_model": "qwen3:30b-a3b",
        "ollama_base_url": "http://127.0.0.1:11434",
    }
    if tmp_path is not None:
        payload["playground_skills_dir"] = str(tmp_path / "skills")
    return Settings(**payload)


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
    retriever = LlmopsScopedRetriever(_settings(), embedding_client=embedding, connect=connect)
    result = retriever.retrieve("synthetic overview", [(str(doc_id), str(revision_id))])

    assert embedding.queries == ["synthetic overview"]
    assert result.metadata.grounded is True
    assert result.metadata.scope == [RetrievalScope(doc_id=doc_id, revision_id=revision_id)]
    assert result.citations[0].doc_id == doc_id
    assert result.citations[0].revision_id == revision_id
    assert result.citations[0].location == {"page": 2, "cell_range": "A1:B3"}
    assert "source_uri" not in result.citations[0].model_dump_json()
    assert "must-not-leak" not in result.citations[0].model_dump_json()
    assert "default_transaction_read_only=on" in str(captured["options"])

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

    def retrieve(self, query: str, selections: list[tuple[str, str]]) -> ScopedRetrievalResult:
        self.calls.append((query, selections))
        return self.result


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


def test_insufficient_evidence_never_invokes_llm(monkeypatch, tmp_path):
    doc_id = uuid4()
    revision_id = uuid4()
    retriever = FakeRetriever(_retrieval_result(doc_id, revision_id, citations=False))
    agent = DocumentChatService(_settings(tmp_path))
    llm_calls = 0

    def fail_if_called(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal llm_calls
        llm_calls += 1
        raise AssertionError("근거가 없을 때 LLM을 호출하면 안 됩니다.")

    monkeypatch.setattr(agent, "_chat_json", fail_if_called)
    response = agent.run(_chat_request(doc_id, revision_id), {}, scoped_retriever=retriever)

    assert response.error_code == ""
    assert response.retrieval is not None
    assert response.retrieval.decision == "insufficient_evidence"
    assert response.citations == []
    assert llm_calls == 0


def test_grounded_response_returns_citations_retrieval_and_artifact_links(monkeypatch, tmp_path):
    doc_id = uuid4()
    revision_id = uuid4()
    retriever = FakeRetriever(_retrieval_result(doc_id, revision_id, citations=True))
    agent = DocumentChatService(_settings(tmp_path))
    monkeypatch.setattr(agent, "_chat_json", lambda *_args, **_kwargs: {"answer": "Grounded answer [1]."})

    response = agent.run(_chat_request(doc_id, revision_id), {}, scoped_retriever=retriever)

    assert response.assistant_message == "Grounded answer [1]."
    assert response.retrieval is not None and response.retrieval.grounded is True
    assert response.citations[0].doc_id == doc_id
    assert response.artifacts[0].preview_url.endswith("/artifacts/preview")
    assert response.artifacts[0].canonical_url.endswith("/artifacts/canonical")
    assert response.artifacts[0].graph_url == f"/api/playground/files/{doc_id}/graph"
    assert all("s3://" not in artifact.model_dump_json() for artifact in response.artifacts)
