"""기존 검색 runtime을 읽기 전용 MCP Streamable HTTP endpoint로 노출한다."""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass
from typing import Any, Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.datastructures import Headers, URLPath
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .tooling import FindFolderOutput, SearchContentOutput, ToolExecutionError, ToolExecutor


@dataclass(frozen=True)
class McpBundle:
    """FastMCP server와 보안 wrapper가 적용된 ASGI app 묶음."""

    server: FastMCP
    app: ASGIApp


class McpExactRoute(BaseRoute):
    """하위 경로 redirect 없이 정확히 `/mcp`에서 ASGI app을 호출한다."""

    def __init__(self, path: str, app: ASGIApp, *, name: str = "mcp") -> None:
        self.path = path
        self.app = app
        self.name = name

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and scope.get("path") == self.path:
            return Match.FULL, {}
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> URLPath:
        if name == self.name and not path_params:
            return URLPath(self.path, protocol="http")
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


class SafeFastMCP(FastMCP):
    """FastMCP/Pydantic의 상세 오류가 입력 원문을 반사하지 않게 정규화한다."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            cause: BaseException | None = exc
            while cause is not None:
                if isinstance(cause, ToolExecutionError):
                    raise ToolError(f"{cause.code}: {cause.message}") from None
                if isinstance(cause, ValidationError):
                    raise ToolError("invalid_arguments: 도구 입력이 올바르지 않습니다.") from None
                cause = cause.__cause__
            raise ToolError("tool_error: 도구 실행에 실패했습니다.") from None


class McpSecurityMiddleware:
    """MCP 경로에만 token·loopback·Host·Origin·본문 크기 제한을 적용한다."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._token = settings.mcp_api_token
        self._allow_remote = settings.mcp_allow_remote
        self._allowed_hosts = settings.mcp_allowed_host_set
        self._allowed_origins = settings.mcp_allowed_origin_set
        self._max_body_bytes = settings.mcp_max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if not self._allow_remote and not _is_loopback_client(scope):
            await _http_error(send, 403, "remote_client_forbidden")
            return

        if _normalized_host(headers.get("host", "")) not in self._allowed_hosts:
            await _http_error(send, 403, "host_forbidden")
            return

        origin = headers.get("origin", "").strip().rstrip("/").lower()
        if origin and origin not in self._allowed_origins:
            await _http_error(send, 403, "origin_forbidden")
            return

        authorization = headers.get("authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(supplied_token, self._token):
            await _http_error(
                send,
                401,
                "authentication_required",
                headers={"WWW-Authenticate": "Bearer"},
            )
            return

        content_length = headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self._max_body_bytes:
                    await _http_error(send, 413, "request_too_large")
                    return
            except ValueError:
                await _http_error(send, 400, "invalid_content_length")
                return

        if scope.get("method", "GET").upper() == "POST":
            body = await _read_limited_body(receive, self._max_body_bytes)
            if body is None:
                await _http_error(send, 413, "request_too_large")
                return
            replayed = False

            async def replay_receive() -> Message:
                nonlocal replayed
                if replayed:
                    return {"type": "http.disconnect"}
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}

            receive = replay_receive

        await self.app(scope, receive, send)


def validate_mcp_settings(settings: Settings) -> None:
    """활성화된 MCP가 인증 없이 시작되거나 관리자 token을 재사용하지 않게 검증한다."""

    if not settings.mcp_enabled:
        return
    if not settings.mcp_api_token.strip():
        raise RuntimeError("MCP_ENABLED=true이면 별도의 MCP_API_TOKEN이 필요합니다.")
    if settings.admin_api_token and hmac.compare_digest(settings.mcp_api_token, settings.admin_api_token):
        raise RuntimeError("MCP_API_TOKEN은 ADMIN_API_TOKEN과 다른 값을 사용해야 합니다.")
    if not settings.mcp_allowed_host_set:
        raise RuntimeError("MCP_ALLOWED_HOSTS에는 하나 이상의 Host가 필요합니다.")


def create_mcp_bundle(runtime_provider: Any, settings: Settings) -> McpBundle:
    """검색 두 개만 등록한 stateless Streamable HTTP MCP app을 만든다."""

    validate_mcp_settings(settings)
    executor = ToolExecutor(runtime_provider)
    server = SafeFastMCP(
        name="automation-smb-search",
        instructions="사전 구축된 온프레미스 인덱스를 읽기 전용으로 검색합니다.",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
    )
    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @server.tool(
        name="find_folder",
        description="사전 구축된 인메모리 인덱스에서 공유 폴더를 찾습니다.",
        annotations=annotations,
    )
    async def find_folder(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        limit: Annotated[int | None, Field(ge=1, le=20)] = None,
    ) -> FindFolderOutput:
        result = await executor.execute_async(
            "find_folder",
            {"query": query, "limit": limit},
            surface="mcp",
        )
        return FindFolderOutput.model_validate(result)

    @server.tool(
        name="search_content",
        description="사전 구축된 로컬 내용 인덱스에서 파일을 찾습니다. 본문 snippet은 반환하지 않습니다.",
        annotations=annotations,
    )
    async def search_content(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        limit: Annotated[int | None, Field(ge=1, le=20)] = None,
    ) -> SearchContentOutput:
        result = await executor.execute_async(
            "search_content",
            {"query": query, "limit": limit},
            surface="mcp",
        )
        return SearchContentOutput.model_validate(result)

    secured_app = McpSecurityMiddleware(server.streamable_http_app(), settings)
    return McpBundle(server=server, app=secured_app)


def _is_loopback_client(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


def _normalized_host(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        closing = value.find("]")
        return value[: closing + 1] if closing >= 0 else value
    host, separator, port = value.rpartition(":")
    if separator and port.isdigit():
        return host
    return value


async def _read_limited_body(receive: Receive, maximum: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b""
        if message["type"] != "http.request":
            continue
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > maximum:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


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
