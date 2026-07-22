"""Langflow 설치 없이 Playground의 MCP 등록·호출을 검증하는 합성 서버."""

from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field


server = FastMCP(
    name="synthetic-langflow-project",
    instructions="합성 문자열만 되돌려주는 Langflow MCP 대체 smoke 서버입니다.",
    host="127.0.0.1",
    port=8765,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


@server.tool(
    name="synthetic_echo_workflow",
    description="합성 문자열을 Langflow Flow처럼 가공해 반환합니다. 연결 테스트에만 사용합니다.",
)
def synthetic_echo_workflow(
    input_value: Annotated[str, Field(min_length=1, max_length=200)],
    uppercase: bool = False,
) -> str:
    """외부 호출이나 파일 접근 없이 합성 입력을 반환한다."""

    rendered = input_value.upper() if uppercase else input_value
    return f"synthetic-langflow-result: {rendered}"


if __name__ == "__main__":
    server.run(transport="streamable-http")
