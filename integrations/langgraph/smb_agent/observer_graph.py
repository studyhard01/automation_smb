"""Playground의 안전한 실행 메타데이터를 Studio에서 확인하는 정적 그래프.

이 그래프는 LLM이나 SMB에 접근하지 않으며, 서버가 허용한 집계 필드만 상태로 기록한다.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class ObserverState(TypedDict, total=False):
    """Playground observer가 허용하는 고정 상태 계약."""

    schema_version: str
    event_type: str
    request_id: str
    provider: str
    model_id: str
    selected_tool_ids: list[str]
    called_tool_ids: list[str]
    tool_calls: list[dict[str, object]]
    outcome: str
    error_code: str
    warning_codes: list[str]
    tool_call_count: int
    tool_error_count: int
    agent_step_count: int
    warning_count: int
    elapsed_ms: float
    over_budget: bool
    token_total: int
    token_calls: int
    observer_stage: str
    observed: bool


def request_received(_state: ObserverState) -> ObserverState:
    """안전한 요청 메타데이터가 도착했음을 표시한다."""

    return {"observer_stage": "request_received"}


def tool_summary(_state: ObserverState) -> ObserverState:
    """고정 tool status/timing 필드가 Studio node에 보이게 한다."""

    return {"observer_stage": "tool_summary"}


def completed(_state: ObserverState) -> ObserverState:
    """부가 호출 없이 Studio run을 완료한다."""

    return {"observer_stage": "completed", "observed": True}


builder = StateGraph(ObserverState)
builder.add_node("request_received", request_received)
builder.add_node("tool_summary", tool_summary)
builder.add_node("completed", completed)
builder.add_edge(START, "request_received")
builder.add_edge("request_received", "tool_summary")
builder.add_edge("tool_summary", "completed")
builder.add_edge("completed", END)

graph = builder.compile()
