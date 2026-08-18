"""의존성 주입만으로 생성되는 최소 LangGraph flow."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from .contracts import BotDependencies, BotGraphState, BotRoute, BotStepResult
from .routing import route_request


def _append_trace(state: BotGraphState, node: str) -> tuple[str, ...]:
    return (*state.get("node_trace", ()), node)


def _merge_timing(state: BotGraphState, name: str, elapsed_ms: float) -> dict[str, float]:
    return {**state.get("timings_ms", {}), name: round(max(0.0, elapsed_ms), 3)}


def _timed(
    clock: Callable[[], float],
    operation: Callable[[], BotStepResult],
    *,
    timeout_code: str,
) -> tuple[BotStepResult, float]:
    started = clock()
    try:
        result = operation()
    except TimeoutError:
        result = BotStepResult(safe_error=timeout_code)
    return result, max(0.0, (clock() - started) * 1000)


def create_bot_graph(dependencies: BotDependencies) -> Any:
    """client·settings·DB를 만들지 않고 주입된 의존성으로 graph를 compile한다."""

    def router_node(state: BotGraphState) -> BotGraphState:
        started = dependencies.clock()
        route = route_request(state.get("message", ""), state.get("selected_ids", ()))
        elapsed_ms = (dependencies.clock() - started) * 1000
        return {
            "route": route,
            "citation_ids": state.get("citation_ids", ()),
            "safe_error": state.get("safe_error", ""),
            "timings_ms": _merge_timing(state, "router", elapsed_ms),
            "node_trace": _append_trace(state, "router"),
        }

    def document_qa_node(state: BotGraphState) -> BotGraphState:
        retrieval, retrieval_ms = _timed(
            dependencies.clock,
            lambda: dependencies.retriever.retrieve(
                state.get("message", ""),
                state.get("selected_ids", ()),
            ),
            timeout_code="retrieval_timeout",
        )
        timings = _merge_timing(state, "retriever", retrieval_ms)
        citation_ids = tuple(dict.fromkeys(retrieval.citation_ids))
        safe_error = retrieval.safe_error
        if not safe_error and not citation_ids:
            safe_error = "insufficient_evidence"
        elif not safe_error:
            model, model_ms = _timed(
                dependencies.clock,
                lambda: dependencies.model.generate(state.get("message", ""), citation_ids),
                timeout_code="model_timeout",
            )
            timings = {**timings, "model": round(max(0.0, model_ms), 3)}
            safe_error = model.safe_error
        return {
            "citation_ids": citation_ids,
            "safe_error": safe_error,
            "timings_ms": timings,
            "node_trace": _append_trace(state, "document_qa"),
        }

    def metadata_node(state: BotGraphState) -> BotGraphState:
        result, elapsed_ms = _timed(
            dependencies.clock,
            lambda: dependencies.metadata.read(state.get("selected_ids", ())),
            timeout_code="tool_timeout",
        )
        return {
            "citation_ids": tuple(dict.fromkeys(result.citation_ids)),
            "safe_error": result.safe_error,
            "timings_ms": _merge_timing(state, "metadata", elapsed_ms),
            "node_trace": _append_trace(state, "metadata"),
        }

    def unsupported_node(state: BotGraphState) -> BotGraphState:
        return {
            "citation_ids": (),
            "safe_error": "unsupported_request",
            "node_trace": _append_trace(state, "unsupported"),
        }

    def finalize_node(state: BotGraphState) -> BotGraphState:
        elapsed_ms = round(sum(state.get("timings_ms", {}).values()), 3)
        return {
            "elapsed_ms": elapsed_ms,
            "over_budget": elapsed_ms > dependencies.budget_ms,
            "node_trace": _append_trace(state, "finalize"),
        }

    def select_route(state: BotGraphState) -> BotRoute:
        return state.get("route", "unsupported")

    builder = StateGraph(BotGraphState)
    builder.add_node("router", router_node)
    builder.add_node("document_qa", document_qa_node)
    builder.add_node("metadata", metadata_node)
    builder.add_node("unsupported", unsupported_node)
    builder.add_node("finalize", finalize_node)
    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        select_route,
        {
            "document_qa": "document_qa",
            "metadata": "metadata",
            "unsupported": "unsupported",
        },
    )
    builder.add_edge("document_qa", "finalize")
    builder.add_edge("metadata", "finalize")
    builder.add_edge("unsupported", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()
