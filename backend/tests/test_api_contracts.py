"""현재 제품 경계의 FastAPI 계약을 검증한다."""

from __future__ import annotations

import asyncio

from smb_finder import api


def test_openapi_exposes_only_document_vertical_slice() -> None:
    schema = api.app.openapi()
    operations = schema["paths"]

    assert operations["/health"]["get"]["operationId"] == "health_check"
    assert operations["/api/playground/files/search"]["post"]["operationId"] == "search_playground_files"
    assert (
        operations["/api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}"]["get"]
        ["operationId"]
        == "read_playground_file_artifact"
    )
    assert operations["/api/playground/files/{doc_id}/graph"]["get"]["operationId"] == "read_playground_file_graph"
    assert operations["/api/playground/stores/status"]["get"]["operationId"] == "get_playground_stores_status"
    assert operations["/api/playground/chat"]["post"]["operationId"] == "run_document_chat"

    serialized = str(schema).casefold()
    for removed in ("/find", "/search-content", "/mcp", "skills", "attachments", "tool-draft", "karyotype"):
        assert removed not in serialized


def test_health_does_not_expose_addresses_or_credentials() -> None:
    payload = asyncio.run(api.health())

    assert set(payload) == {"status", "ready", "adapters"}
    serialized = str(payload).casefold()
    assert "password" not in serialized
    assert "postgres_host" not in serialized
    assert "minio_endpoint" not in serialized
    assert "neo4j_uri" not in serialized
