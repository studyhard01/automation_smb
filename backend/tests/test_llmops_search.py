"""LLMOps 문서 선택 검색 adapter의 읽기 전용 계약을 검증한다."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from smb_finder.config import Settings
from smb_finder.llmops_search import LlmopsFileSearcher
from smb_finder.models import DocumentSearchRequest


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


def test_explicit_llmops_host_wins_over_environment_specific_fallback():
    settings = Settings(
        _env_file=None,
        llmops_db_host="explicit.test",
        dev_server="development.test",
        postgres_host="postgres",
    )

    assert settings.effective_llmops_db_host == "explicit.test"
