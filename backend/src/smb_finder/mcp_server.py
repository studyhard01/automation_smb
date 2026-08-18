"""문서 metadata 하나만 제공하는 MCP v2 Streamable HTTP bundle."""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError
from starlette.datastructures import Headers, URLPath
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .tooling import (
    GET_DOCUMENT_METADATA_TOOL_DESCRIPTION,
    GET_DOCUMENT_METADATA_TOOL_NAME,
    DocumentMetadataOutput,
    MetadataReader,
    ToolExecutionError,
    ToolExecutor,
)
from .tooling.contracts import GetDocumentMetadataInput


class SafeMCPServer(MCPServer[Any]):
    """tool 오류를 입력 원문 없는 구조화된 MCP 결과로 정규화한다."""

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any | None = None) -> Any:
        if name != GET_DOCUMENT_METADATA_TOOL_NAME:
            return _tool_error_result(ToolExecutionError("invalid_arguments", "허용되지 않은 도구입니다."))
        try:
            GetDocumentMetadataInput.model_validate(arguments)
        except ValidationError:
            return _tool_error_result(ToolExecutionError("invalid_arguments", "도구 입력이 올바르지 않습니다."))
        try:
            return await super().call_tool(name, arguments, context)
        except Exception as exc:
            cause: BaseException | None = exc
            while cause is not None:
                if isinstance(cause, ToolExecutionError):
                    return _tool_error_result(cause)
                cause = cause.__cause__
            return _tool_error_result(
                ToolExecutionError("internal_error", "메타데이터 도구를 실행할 수 없습니다.", retryable=True)
            )


@dataclass(frozen=True)
class McpBundle:
    """호스트 앱이 mount/lifespan/close를 연결할 최소 MCP 구성."""

    server: MCPServer[Any]
    app: ASGIApp
    executor: ToolExecutor

    def close(self) -> None:
        """호스트가 metadata adapter보다 먼저 worker를 종료한다."""

        self.executor.close()


class McpExactRoute(BaseRoute):
    """정확한 `/mcp`만 전달하고 trailing slash 자동 redirect는 404로 막는다."""

    def __init__(self, path: str, app: ASGIApp, *, name: str = "mcp") -> None:
        self.path = path
        self.app = app
        self.name = name

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and scope.get("path") in {self.path, f"{self.path}/"}:
            return Match.FULL, {}
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        if name == self.name and not path_params:
            return URLPath(self.path, protocol="http")
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("path") != self.path:
            await _http_error(send, 404, "not_found")
            return
        inner_scope = dict(scope)
        inner_scope["root_path"] = f"{scope.get('root_path', '')}{self.path}"
        inner_scope["path"] = "/"
        inner_scope["raw_path"] = b"/"
        await self.app(inner_scope, receive, send)


class McpAccessMiddleware:
    """MCP sub-app을 loopback client와 전용 bearer token으로 제한한다."""

    def __init__(self, app: ASGIApp, *, bearer_token: str) -> None:
        self._app = app
        self._bearer_token = bearer_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if not _is_loopback_client(scope):
            await _http_error(send, 403, "remote_client_forbidden")
            return
        authorization = Headers(scope=scope).get("authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(
                supplied_token,
                self._bearer_token,
            )
        ):
            await _http_error(send, 401, "authentication_required", headers={"WWW-Authenticate": "Bearer"})
            return
        await self._app(scope, receive, send)


def create_mcp_bundle(
    metadata_reader: MetadataReader,
    *,
    bearer_token: str,
    timeout_ms: int = 1500,
    max_concurrency: int = 2,
) -> McpBundle:
    """`/mcp` mount 전용 sub-app과 단일 metadata tool을 생성한다."""

    token = bearer_token.strip()
    if not token:
        raise RuntimeError("MCP 전용 bearer token이 필요합니다.")
    executor = ToolExecutor(
        metadata_reader,
        timeout_ms=timeout_ms,
        max_concurrency=max_concurrency,
    )
    server: MCPServer[Any] = SafeMCPServer(
        "automation-smb-metadata",
        instructions="서버가 발급한 UUID의 문서 revision 상태만 읽습니다.",
    )

    @server.tool(
        name=GET_DOCUMENT_METADATA_TOOL_NAME,
        description=GET_DOCUMENT_METADATA_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    async def get_document_metadata(
        doc_id: UUID,
        revision_id: UUID | None = None,
    ) -> DocumentMetadataOutput:
        """문서 UUID로 canonical revision 상태를 조회한다."""

        return await executor.execute_async({"doc_id": doc_id, "revision_id": revision_id})

    # 호스트는 `McpExactRoute("/mcp", bundle.app)`로 이 내부 `/` endpoint만 연결한다.
    app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
    )
    return McpBundle(server=server, app=McpAccessMiddleware(app, bearer_token=token), executor=executor)


def _tool_error_result(error: ToolExecutionError) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=f"{error.code}: {error.message}")],
        isError=True,
        _meta={
            "com.automation-smb/tool-error": {
                "code": error.code,
                "retryable": error.retryable,
                "elapsed_ms": max(0.0, error.elapsed_ms),
            }
        },
    )


def _is_loopback_client(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


async def _http_error(
    send: Send,
    status_code: int,
    code: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    response = JSONResponse({"error": code}, status_code=status_code, headers=headers)
    await response({"type": "http"}, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}
