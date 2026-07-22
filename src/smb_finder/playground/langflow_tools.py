"""Langflow 프로젝트 MCP tool 등록, 동기화, 호출 지원."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from smb_finder.config import Settings

from .models import (
    LangflowToolSnapshot,
    LangflowToolSource,
    LangflowToolSourceRequest,
    ToolDefinition,
    ToolExecutionResult,
)


class LangflowToolError(RuntimeError):
    """Langflow tool 등록 또는 호출의 공개 가능한 오류."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RemoteToolContract:
    """MCP tools/list에서 읽은 최소 tool 계약."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class RemoteToolResult:
    """MCP tools/call 결과의 직렬화 가능한 표현."""

    is_error: bool
    content: list[dict[str, Any]]
    structured_content: dict[str, Any] | None


class LangflowToolStore:
    """Langflow 연결과 동기화된 tool 계약을 Git 제외 JSON에 보관한다."""

    _file_lock = threading.RLock()

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = Path(settings.playground_langflow_tools_path)

    def list_sources(self) -> list[LangflowToolSource]:
        """등록된 연결을 source_id 순서로 반환한다."""

        sources = self._load()
        configured = bool(self.settings.langflow_mcp_api_key.strip())
        return [source.model_copy(update={"api_key_configured": configured}) for source in sources]

    def get_source(self, source_id: str) -> LangflowToolSource:
        """source_id로 등록된 연결을 찾는다."""

        for source in self.list_sources():
            if source.source_id == source_id:
                return source
        raise LangflowToolError("langflow_source_not_found", "등록된 Langflow 연결을 찾지 못했습니다.")

    def synchronized_tools(self) -> list[tuple[LangflowToolSource, LangflowToolSnapshot]]:
        """활성 source의 캐시된 tool만 반환하며 네트워크를 호출하지 않는다."""

        return [
            (source, snapshot)
            for source in self.list_sources()
            if source.enabled
            for snapshot in source.tools
            if snapshot.definition.enabled
        ]

    def save_sync(
        self,
        request: LangflowToolSourceRequest,
        contracts: list[RemoteToolContract],
        elapsed_ms: float,
    ) -> LangflowToolSource:
        """검증된 연결과 동기화된 tool 스냅샷을 원자적으로 저장한다."""

        self.validate_url(request.mcp_url)
        tools = [self._snapshot(request, contract) for contract in contracts]
        source = LangflowToolSource(
            **request.model_dump(),
            tools=tools,
            last_synced_at=datetime.now(UTC).isoformat(),
            last_sync_elapsed_ms=round(elapsed_ms, 1),
            last_error="",
            api_key_configured=bool(self.settings.langflow_mcp_api_key.strip()),
        )
        with self._file_lock:
            sources = [item for item in self._load_unlocked() if item.source_id != request.source_id]
            sources.append(source)
            self._write_unlocked(sources)
        return source

    def mark_sync_error(self, source_id: str, message: str) -> None:
        """기존 tool 스냅샷은 유지하고 마지막 동기화 오류만 기록한다."""

        with self._file_lock:
            sources = self._load_unlocked()
            changed = False
            updated: list[LangflowToolSource] = []
            for source in sources:
                if source.source_id == source_id:
                    source = source.model_copy(update={"last_error": message[:300]})
                    changed = True
                updated.append(source)
            if changed:
                self._write_unlocked(updated)

    def delete(self, source_id: str) -> None:
        """등록 연결과 해당 tool 스냅샷을 삭제한다."""

        with self._file_lock:
            sources = self._load_unlocked()
            updated = [source for source in sources if source.source_id != source_id]
            if len(updated) == len(sources):
                raise LangflowToolError("langflow_source_not_found", "등록된 Langflow 연결을 찾지 못했습니다.")
            self._write_unlocked(updated)

    def validate_url(self, value: str) -> str:
        """outbound MCP URL을 scheme·userinfo·host allowlist 기준으로 검증한다."""

        url = value.strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LangflowToolError("langflow_url_invalid", "Langflow MCP URL은 http 또는 https 전체 주소여야 합니다.")
        if parsed.username or parsed.password:
            raise LangflowToolError("langflow_url_userinfo_forbidden", "URL에 자격증명을 넣을 수 없습니다.")
        if parsed.query or parsed.fragment:
            raise LangflowToolError("langflow_url_invalid", "Langflow MCP URL에는 query 또는 fragment를 넣을 수 없습니다.")
        if parsed.hostname.lower() not in self.settings.playground_langflow_allowed_host_set:
            raise LangflowToolError(
                "langflow_host_not_allowed",
                "허용되지 않은 Langflow 호스트입니다. PLAYGROUND_LANGFLOW_ALLOWED_HOSTS를 확인하세요.",
            )
        return url

    def _snapshot(self, request: LangflowToolSourceRequest, contract: RemoteToolContract) -> LangflowToolSnapshot:
        tool_id = _playground_tool_id(request.source_id, contract.name)
        return LangflowToolSnapshot(
            remote_name=contract.name,
            definition=ToolDefinition(
                id=tool_id,
                display_name=contract.name,
                description=contract.description or f"{request.display_name}의 Langflow Flow tool",
                category="workflow",
                permission="read",
                execution_type="code",
                enabled=request.enabled,
                default_selected=False,
                timeout_ms=request.timeout_ms,
                input_schema=contract.input_schema,
                origin="langflow",
                source_id=request.source_id,
            ),
        )

    def _load(self) -> list[LangflowToolSource]:
        with self._file_lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> list[LangflowToolSource]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_sources = payload.get("sources", []) if isinstance(payload, dict) else []
            return [LangflowToolSource.model_validate(item) for item in raw_sources]
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise LangflowToolError(
                "langflow_store_invalid",
                "Langflow tool 등록 파일을 읽지 못했습니다. 파일 형식을 확인하세요.",
            ) from exc

    def _write_unlocked(self, sources: list[LangflowToolSource]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            payload = {
                "version": 1,
                "sources": [source.model_dump(exclude={"api_key_configured"}) for source in sorted(sources, key=lambda x: x.source_id)],
            }
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
        except OSError as exc:
            raise LangflowToolError(
                "langflow_store_write_failed",
                "Langflow tool 등록 파일을 저장하지 못했습니다.",
            ) from exc


class LangflowMcpGateway:
    """공식 MCP SDK로 Langflow Streamable HTTP 서버를 호출한다."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = LangflowToolStore(settings)

    def list_tools(self, source: LangflowToolSourceRequest | LangflowToolSource) -> tuple[list[RemoteToolContract], float]:
        """Langflow MCP tools/list를 호출하고 지연을 반환한다."""

        started = time.perf_counter()
        try:
            tools = asyncio.run(self._list_tools(source))
        except LangflowToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 내부 transport 예외를 공개 응답에서 숨긴다.
            raise LangflowToolError(
                "langflow_sync_failed",
                "Langflow MCP tool 목록을 가져오지 못했습니다. 주소, 인증키, Flow 공개 상태를 확인하세요.",
            ) from exc
        return tools, round((time.perf_counter() - started) * 1000, 1)

    def call_tool(self, source: LangflowToolSource, remote_name: str, arguments: dict[str, Any]) -> ToolExecutionResult:
        """등록된 원격 tool을 실행하고 Playground 공통 결과로 변환한다."""

        started = time.perf_counter()
        argument_keys = ",".join(sorted(str(key) for key in arguments)) or "-"
        try:
            result = asyncio.run(self._call_tool(source, remote_name, arguments))
        except Exception as exc:  # noqa: BLE001 - transport 세부·원격 응답 원문을 공개하지 않는다.
            code = exc.code if isinstance(exc, LangflowToolError) else "langflow_tool_call_failed"
            message = (
                exc.message
                if isinstance(exc, LangflowToolError)
                else "Langflow tool 호출에 실패했습니다. 연결 상태와 Flow 실행 로그를 확인하세요."
            )
            return ToolExecutionResult(
                status="error",
                result_text=message,
                error_code=code,
                arguments_summary=(
                    f"source_id={source.source_id} argument_keys={argument_keys} "
                    f"elapsed_ms={round((time.perf_counter() - started) * 1000, 1)}"
                ),
            )

        result_text = _result_text(result, self.settings.playground_langflow_result_chars)
        payload = _result_payload(result, self.settings.playground_langflow_result_chars)
        return ToolExecutionResult(
            status="error" if result.is_error else "ok",
            result_text=result_text,
            observation_text=result_text,
            result_payload={
                "source_id": source.source_id,
                "remote_tool_name": remote_name,
                **payload,
            },
            error_code="langflow_tool_error" if result.is_error else "",
            arguments_summary=(
                f"source_id={source.source_id} argument_keys={argument_keys} "
                f"elapsed_ms={round((time.perf_counter() - started) * 1000, 1)}"
            ),
        )

    @asynccontextmanager
    async def _session(
        self,
        source: LangflowToolSourceRequest | LangflowToolSource,
    ) -> AsyncIterator[ClientSession]:
        url = self.store.validate_url(source.mcp_url)
        timeout_seconds = source.timeout_ms / 1000
        headers = {"User-Agent": "automation-smb-playground/0.1"}
        if self.settings.langflow_mcp_api_key.strip():
            headers["x-api-key"] = self.settings.langflow_mcp_api_key.strip()
        timeout = httpx.Timeout(timeout_seconds)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=timeout_seconds),
                ) as session:
                    await session.initialize()
                    yield session

    async def _list_tools(
        self,
        source: LangflowToolSourceRequest | LangflowToolSource,
    ) -> list[RemoteToolContract]:
        contracts: list[RemoteToolContract] = []
        async with self._session(source) as session:
            cursor: str | None = None
            while True:
                response = await session.list_tools(cursor=cursor)
                for tool in response.tools:
                    schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
                    contracts.append(
                        RemoteToolContract(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=schema,
                        )
                    )
                cursor = response.nextCursor
                if not cursor:
                    break
        return contracts

    async def _call_tool(
        self,
        source: LangflowToolSource,
        remote_name: str,
        arguments: dict[str, Any],
    ) -> RemoteToolResult:
        async with self._session(source) as session:
            response = await session.call_tool(remote_name, arguments=arguments)
        content = [item.model_dump(mode="json", by_alias=True) for item in response.content]
        structured = response.structuredContent if isinstance(response.structuredContent, dict) else None
        return RemoteToolResult(
            is_error=bool(response.isError),
            content=content,
            structured_content=structured,
        )


def _playground_tool_id(source_id: str, remote_name: str) -> str:
    """원격 이름 충돌과 특수문자를 피한 안정적인 Playground tool ID를 만든다."""

    source_slug = re.sub(r"[^a-z0-9]+", "_", source_id.lower()).strip("_")[:40] or "source"
    remote_slug = re.sub(r"[^a-z0-9]+", "_", remote_name.lower()).strip("_")[:40] or "tool"
    digest = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:8]
    return f"langflow_{source_slug}_{remote_slug}_{digest}"


def _result_text(result: RemoteToolResult, limit: int) -> str:
    texts = [str(item.get("text", "")).strip() for item in result.content if item.get("type") == "text"]
    text = "\n".join(value for value in texts if value)
    if not text and result.structured_content is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False, indent=2)
    if not text:
        content_types = sorted({str(item.get("type", "result")) for item in result.content})
        text = f"Langflow tool이 비텍스트 결과를 반환했습니다: {', '.join(content_types) or 'empty'}"
    if len(text) > limit:
        return f"{text[:limit]}\n...[truncated {len(text) - limit} chars]"
    return text


def _result_payload(result: RemoteToolResult, limit: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": result.content}
    if result.structured_content is not None:
        payload["structured_content"] = result.structured_content
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if len(serialized) <= limit:
        return payload
    return {"preview": serialized[:limit], "truncated": True}
