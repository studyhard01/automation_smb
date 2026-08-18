"""MCP v2 단일 document metadata tool과 mount 보안 계약 테스트."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from mcp.server import MCPServer
from starlette.applications import Starlette

from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.mcp_server import McpExactRoute, create_mcp_bundle


class RecordingMetadataReader:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID | None, float]] = []

    def get_document_metadata(
        self,
        doc_id: UUID,
        revision_id: UUID | None = None,
        *,
        deadline: float,
    ) -> dict[str, object]:
        self.calls.append((doc_id, revision_id, deadline))
        return {
            "source": "llmops",
            "doc_id": doc_id,
            "revision_id": revision_id or uuid4(),
            "is_active": revision_id is None,
            "revision_status": "active" if revision_id is None else "superseded",
            "extension": ".pdf",
            "size_bytes": 1024,
            "modified_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
            "elapsed_ms": 3.0,
            "over_budget": False,
            "degraded_dependencies": [],
        }


def _mcp_credential() -> str:
    return "-".join(("test", "only", "placeholder"))


def _bundle():  # noqa: ANN202
    reader = RecordingMetadataReader()
    bundle = create_mcp_bundle(reader, bearer_token=_mcp_credential())
    return bundle, reader


def _mounted_app(bundle) -> Starlette:  # noqa: ANN001
    return Starlette(routes=[McpExactRoute("/mcp", bundle.app)])


def test_server_uses_official_v2_mcpserver_and_exposes_exactly_one_read_only_tool():
    bundle, _ = _bundle()
    try:
        tools = asyncio.run(bundle.server.list_tools())
    finally:
        bundle.close()

    assert isinstance(bundle.server, MCPServer)
    assert [tool.name for tool in tools] == ["get_document_metadata"]
    tool = tools[0]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False
    assert set(tool.input_schema["properties"]) == {"doc_id", "revision_id"}
    assert set(tool.output_schema["properties"]) == {
        "source",
        "doc_id",
        "revision_id",
        "is_active",
        "revision_status",
        "extension",
        "size_bytes",
        "modified_at",
        "elapsed_ms",
        "over_budget",
        "degraded_dependencies",
    }
    assert "find_folder" not in repr(tools)
    assert "search_content" not in repr(tools)


def test_mcp_tool_returns_structured_metadata_without_forbidden_fields():
    bundle, reader = _bundle()
    doc_id = uuid4()
    revision_id = uuid4()
    try:
        result = asyncio.run(
            bundle.server.call_tool(
                "get_document_metadata",
                {"doc_id": str(doc_id), "revision_id": str(revision_id)},
            )
        )
    finally:
        bundle.close()

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["doc_id"] == str(doc_id)
    assert result.structured_content["revision_id"] == str(revision_id)
    assert result.structured_content["is_active"] is False
    serialized = str(result.model_dump(mode="json", by_alias=True)).lower()
    for forbidden in ("filename", "title", "path", "uri", "key", "body", "snippet", "query"):
        assert forbidden not in serialized
    assert reader.calls[0][:2] == (doc_id, revision_id)


def test_invalid_uuid_is_not_reflected_by_mcpserver():
    bundle, reader = _bundle()
    private_input = "invalid-id-not-for-response"
    try:
        result = asyncio.run(bundle.server.call_tool("get_document_metadata", {"doc_id": private_input}))
    finally:
        bundle.close()

    assert result.is_error is True
    assert result.meta == {
        "com.automation-smb/tool-error": {"code": "invalid_arguments", "retryable": False, "elapsed_ms": 0.0}
    }
    assert private_input not in str(result)
    assert reader.calls == []


def test_unknown_tool_name_is_not_reflected_by_mcpserver():
    bundle, reader = _bundle()
    private_name = "unknown-private-tool-name"
    try:
        result = asyncio.run(bundle.server.call_tool(private_name, {}))
    finally:
        bundle.close()

    assert result.is_error is True
    assert result.meta["com.automation-smb/tool-error"]["code"] == "invalid_arguments"
    assert private_name not in str(result)
    assert reader.calls == []


def test_bundle_requires_nonempty_token():
    reader = RecordingMetadataReader()
    with pytest.raises(RuntimeError, match="bearer token"):
        create_mcp_bundle(reader, bearer_token="")


def test_default_host_app_keeps_mcp_unmounted_and_returns_404():
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=Starlette())
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as client:
            return await client.post("/mcp")

    response = asyncio.run(request())

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("client_address", "authorization", "status_code", "error_code"),
    [
        (("10.0.0.5", 1000), f"Bearer {_mcp_credential()}", 403, "remote_client_forbidden"),
        (("127.0.0.1", 1000), "", 401, "authentication_required"),
        (("127.0.0.1", 1000), "Bearer wrong-placeholder", 401, "authentication_required"),
    ],
)
def test_mcp_mount_rejects_remote_or_unauthenticated_requests(
    client_address,
    authorization,
    status_code,
    error_code,
):  # noqa: ANN001
    bundle, _ = _bundle()

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=bundle.app, client=client_address)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await client.post("/mcp", headers={"authorization": authorization})

    try:
        response = asyncio.run(request())
    finally:
        bundle.close()

    assert response.status_code == status_code
    assert response.json() == {"error": error_code}
    assert _mcp_credential() not in response.text


def test_mcp_mount_rejects_untrusted_host_with_valid_loopback_credentials():
    bundle, _ = _bundle()

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=_mounted_app(bundle), client=("127.0.0.1", 1000))
        async with bundle.server.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                return await client.post(
                    "/mcp",
                    headers={
                        "authorization": f"Bearer {_mcp_credential()}",
                        "content-type": "application/json",
                        "host": "untrusted.invalid",
                    },
                )

    try:
        response = asyncio.run(request())
    finally:
        bundle.close()

    assert response.status_code == 421
    assert _mcp_credential() not in response.text


def test_authorized_loopback_mount_serves_mcp_v2_initialize():
    bundle, _ = _bundle()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=_mounted_app(bundle), client=("127.0.0.1", 1000))
        async with bundle.server.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as client:
                return await client.post(
                    "/mcp",
                    headers={
                        "authorization": f"Bearer {_mcp_credential()}",
                        "accept": "application/json, text/event-stream",
                        "content-type": "application/json",
                    },
                    json=payload,
                )

    try:
        response = asyncio.run(request())
    finally:
        bundle.close()

    assert response.status_code == 200
    assert response.history == []
    assert response.json()["result"]["serverInfo"]["name"] == "automation-smb-metadata"


def test_mounted_boundary_handles_trailing_slash_and_keeps_unknown_path_404():
    bundle, _ = _bundle()

    async def request(path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=_mounted_app(bundle), client=("127.0.0.1", 1000))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8010",
            follow_redirects=False,
        ) as client:
            return await client.post(path, headers={"authorization": f"Bearer {_mcp_credential()}"})

    try:
        trailing = asyncio.run(request("/mcp/"))
        unknown = asyncio.run(request("/unknown"))
    finally:
        bundle.close()

    assert trailing.status_code == 404
    assert unknown.status_code == 404


def test_http_tools_call_returns_machine_readable_safe_error_meta():
    private_detail = "upstream-private-detail"

    class BudgetMetadataReader(RecordingMetadataReader):
        def get_document_metadata(self, doc_id, revision_id=None, *, deadline):  # noqa: ANN001, ANN201
            raise LlmopsSearchError("metadata_budget_exhausted", private_detail, 37.0)

    bundle = create_mcp_bundle(BudgetMetadataReader(), bearer_token=_mcp_credential())
    headers = {
        "authorization": f"Bearer {_mcp_credential()}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_document_metadata", "arguments": {"doc_id": str(uuid4())}},
    }

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=_mounted_app(bundle), client=("127.0.0.1", 1000))
        async with bundle.server.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as client:
                initialized = await client.post("/mcp", headers=headers, json=initialize)
                assert initialized.status_code == 200
                return await client.post("/mcp", headers=headers, json=call)

    try:
        response = asyncio.run(request())
    finally:
        bundle.close()

    payload = response.json()["result"]
    assert response.status_code == 200
    assert payload["isError"] is True
    assert payload["_meta"] == {
        "com.automation-smb/tool-error": {"code": "tool_timeout", "retryable": True, "elapsed_ms": 37.0}
    }
    assert "tool_timeout" in payload["content"][0]["text"]
    assert private_detail not in response.text
