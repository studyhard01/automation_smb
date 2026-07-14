"""읽기 전용 MCP Streamable HTTP endpoint 계약·보안 테스트."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from smb_finder import api
from smb_finder.config import Settings
from smb_finder.index import FolderIndex
from smb_finder.mcp_server import McpExactRoute, create_mcp_bundle, validate_mcp_settings


TOKEN = "local-mcp-test-token-0123456789"


class FakeFinder:
    def __init__(self) -> None:
        self.calls = 0

    def find(self, request):  # noqa: ANN001
        self.calls += 1
        return SimpleNamespace(
            query=request.query,
            normalized_query=request.query,
            hits=[SimpleNamespace(name="A", path="root/A", score=0.9, depth=2)],
            result_count=1,
            elapsed_ms=2.0,
            over_budget=False,
            source="index",
        )


class FakeSearcher:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, request):  # noqa: ANN001
        self.calls += 1
        return SimpleNamespace(
            query=request.query,
            terms=[request.query],
            hits=[
                SimpleNamespace(
                    name="result.txt",
                    path="root/result.txt",
                    ext=".txt",
                    score=1.1,
                    snippet="민감한 본문 snippet",
                    size=123,
                    mtime=1.0,
                )
            ],
            result_count=1,
            elapsed_ms=3.0,
            over_budget=False,
            indexed_files=10,
        )


def _bundle(**settings_kwargs):
    settings = Settings(
        _env_file=None,
        mcp_enabled=True,
        mcp_api_token=TOKEN,
        **settings_kwargs,
    )
    finder = FakeFinder()
    searcher = FakeSearcher()
    runtime = SimpleNamespace(settings=settings, finder=finder, content_searcher=searcher)
    return create_mcp_bundle(runtime, settings), finder, searcher


def _headers(**overrides: str) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {TOKEN}",
        "host": "127.0.0.1:8010",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    headers.update(overrides)
    return headers


def _initialize_payload() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }


async def _post(bundle, payload, *, headers=None, client=("127.0.0.1", 1234)):  # noqa: ANN001
    test_app = Starlette(routes=[McpExactRoute("/mcp", bundle.app)])
    transport = httpx.ASGITransport(app=test_app, client=client)
    async with bundle.server.session_manager.run():
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as http:
            return await http.post("/mcp", headers=headers or _headers(), json=payload)


def test_enabled_mcp_requires_separate_token():
    with pytest.raises(RuntimeError, match="MCP_API_TOKEN"):
        validate_mcp_settings(Settings(_env_file=None, mcp_enabled=True, mcp_api_token=""))

    with pytest.raises(RuntimeError, match="다른 값"):
        validate_mcp_settings(
            Settings(
                _env_file=None,
                mcp_enabled=True,
                mcp_api_token=TOKEN,
                admin_api_token=TOKEN,
            )
        )


def test_tools_list_is_exactly_two_read_only_tools():
    bundle, _, _ = _bundle()

    tools = asyncio.run(bundle.server.list_tools())

    assert [tool.name for tool in tools] == ["find_folder", "search_content"]
    assert all(tool.annotations.readOnlyHint is True for tool in tools)
    assert all(tool.annotations.destructiveHint is False for tool in tools)
    assert all(tool.annotations.idempotentHint is True for tool in tools)
    assert all(tool.annotations.openWorldHint is False for tool in tools)
    assert all(tool.outputSchema is not None for tool in tools)
    assert "indexed_files" not in tools[1].outputSchema["properties"]


def test_call_tool_executes_existing_search_once_and_redacts_payload():
    bundle, finder, searcher = _bundle()

    async def call_tools():
        folder_result = await bundle.server.call_tool("find_folder", {"query": "folder"})
        content_result = await bundle.server.call_tool("search_content", {"query": "민감 검색어"})
        return folder_result, content_result

    folder_result, content_result = asyncio.run(call_tools())
    serialized = repr((folder_result, content_result))

    assert finder.calls == 1
    assert searcher.calls == 1
    assert "root/A" in serialized
    assert "root/result.txt" in serialized
    assert "민감 검색어" not in serialized
    assert "민감한 본문 snippet" not in serialized
    assert "snippet" not in serialized
    assert "refresh_content" not in serialized


def test_invalid_tool_arguments_are_not_reflected_by_fastmcp():
    bundle, _, _ = _bundle()
    secret = "SECRET-PATIENT-IDENTIFIER"
    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "find_folder",
            "arguments": {"query": {"patient": secret}},
        },
    }

    response = asyncio.run(_post(bundle, payload))

    assert response.status_code == 200
    assert "invalid_arguments" in response.text
    assert secret not in response.text


def test_streamable_http_tools_list_uses_exact_mcp_path_without_redirect():
    bundle, _, _ = _bundle()

    async def request_tools():
        test_app = Starlette(routes=[McpExactRoute("/mcp", bundle.app)])
        transport = httpx.ASGITransport(app=test_app, client=("127.0.0.1", 1234))
        async with bundle.server.session_manager.run():
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as http:
                initialized = await http.post("/mcp", headers=_headers(), json=_initialize_payload())
                assert initialized.status_code == 200
                return await http.post(
                    "/mcp",
                    headers=_headers(),
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )

    response = asyncio.run(request_tools())
    names = [tool["name"] for tool in response.json()["result"]["tools"]]

    assert response.status_code == 200
    assert response.history == []
    assert names == ["find_folder", "search_content"]


@pytest.mark.parametrize(
    ("headers", "client", "status_code", "error_code"),
    [
        ({"authorization": ""}, ("127.0.0.1", 1), 401, "authentication_required"),
        ({"authorization": "Bearer wrong-token"}, ("127.0.0.1", 1), 401, "authentication_required"),
        ({"host": "evil.example"}, ("127.0.0.1", 1), 403, "host_forbidden"),
        ({"origin": "https://evil.example"}, ("127.0.0.1", 1), 403, "origin_forbidden"),
        ({}, ("10.20.30.40", 1), 403, "remote_client_forbidden"),
        ({"content-length": "70000"}, ("127.0.0.1", 1), 413, "request_too_large"),
    ],
)
def test_mcp_http_security_rejects_unsafe_requests(headers, client, status_code, error_code):
    bundle, _, _ = _bundle()
    request_headers = _headers(**headers)

    response = asyncio.run(_post(bundle, _initialize_payload(), headers=request_headers, client=client))

    assert response.status_code == status_code
    assert response.json() == {"error": error_code}
    assert TOKEN not in response.text


def test_chunked_body_over_64_kib_is_rejected():
    bundle, _, _ = _bundle()

    async def request_large_body():
        test_app = Starlette(routes=[McpExactRoute("/mcp", bundle.app)])
        transport = httpx.ASGITransport(app=test_app, client=("127.0.0.1", 1234))

        async def chunks():
            yield b"x" * 40_000
            yield b"y" * 40_000

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as http:
            return await http.post("/mcp", headers=_headers(), content=chunks())

    response = asyncio.run(request_large_body())

    assert response.status_code == 413
    assert response.json() == {"error": "request_too_large"}


def test_fastapi_lifespan_starts_mcp_session_and_closes_shared_runtime(monkeypatch):
    settings = Settings(_env_file=None, mcp_enabled=True, mcp_api_token=TOKEN)
    bundle = create_mcp_bundle(api._playground_runtime, settings)

    class FakeContentIndex:
        def __init__(self) -> None:
            self.closed = False

        def count(self) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    content_index = FakeContentIndex()

    async def fake_threadpool(function, *args):  # noqa: ANN001, ARG001
        if function is api.load_or_build:
            return FolderIndex([])
        if function is api.open_index:
            return content_index
        raise AssertionError("예상하지 않은 startup 함수")

    monkeypatch.setattr(api, "run_in_threadpool", fake_threadpool)
    monkeypatch.setattr(api, "_mcp_bundle", bundle)

    async def exercise_lifespan():
        async with api.lifespan(api.app):
            assert api._state["finder"] is not None
            assert api._state["content_searcher"] is not None
            test_app = Starlette(routes=[McpExactRoute("/mcp", bundle.app)])
            transport = httpx.ASGITransport(app=test_app, client=("127.0.0.1", 1234))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8010") as http:
                response = await http.post("/mcp", headers=_headers(), json=_initialize_payload())
                assert response.status_code == 200

    asyncio.run(exercise_lifespan())

    assert api._state == {}
    assert content_index.closed is True
