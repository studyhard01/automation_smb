"""현재 제품 경계의 FastAPI 계약을 검증한다."""

from __future__ import annotations

import asyncio
import logging

from smb_finder import api
from smb_finder.config import Settings


def test_internal_client_loggers_do_not_emit_connection_metadata_at_info() -> None:
    """HTTP·SMB client가 내부 endpoint와 사용자명을 INFO에 남기지 않는다."""

    for logger_name in ("httpx", "httpcore", "smbprotocol", "smbclient"):
        assert logging.getLogger(logger_name).getEffectiveLevel() >= logging.WARNING


def test_openapi_exposes_only_document_vertical_slice() -> None:
    schema = api.app.openapi()
    operations = schema["paths"]

    assert operations["/health"]["get"]["operationId"] == "health_check"
    assert operations["/api/playground/files/search"]["post"]["operationId"] == "search_playground_files"
    assert (
        operations["/api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}"]["get"][
            "operationId"
        ]
        == "read_playground_file_artifact"
    )
    assert operations["/api/playground/files/{doc_id}/graph"]["get"]["operationId"] == "read_playground_file_graph"
    assert operations["/api/playground/stores/status"]["get"]["operationId"] == "get_playground_stores_status"
    assert operations["/api/playground/chat"]["post"]["operationId"] == "run_document_chat"

    serialized = str(schema).casefold()
    for removed in ("/find", "/search-content", "/mcp", "skills", "/attachments", "tool-draft", "karyotype"):
        assert removed not in serialized


def test_health_does_not_expose_addresses_or_credentials() -> None:
    payload = asyncio.run(api.health())

    assert set(payload) == {"status", "ready", "adapters"}
    serialized = str(payload).casefold()
    assert "password" not in serialized
    assert "postgres_host" not in serialized
    assert "minio_endpoint" not in serialized
    assert "neo4j_uri" not in serialized


def test_lifespan_owns_one_reused_model_gateway(monkeypatch) -> None:
    created = []

    class FakeGateway:
        def __init__(self, _settings: Settings) -> None:
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    async def exercise() -> None:
        async with api.lifespan(api.app):
            first = api._runtime().model_gateway
            second = api._runtime().model_gateway
            assert first is second is created[0]
            assert created[0].closed is False

        assert created[0].closed is True
        assert api._state == {}

    monkeypatch.setattr(api, "_settings", Settings(_env_file=None))
    monkeypatch.setattr(api, "OllamaModelGateway", FakeGateway)
    api._state.clear()

    asyncio.run(exercise())
