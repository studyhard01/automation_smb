"""Bot Core graph의 결정론적 route와 최소 상태 계약을 검증한다."""

from __future__ import annotations

import importlib

import httpx
import psycopg

from smb_finder.bot_core import (
    BotDependencies,
    BotStepResult,
    DeterministicMetadataFake,
    DeterministicModelFake,
    DeterministicRetrieverFake,
    create_bot_graph,
    route_request,
)
from smb_finder.bot_core import graph as graph_module

_SELECTED = (("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"),)
_ALLOWED_STATE = {
    "request_id",
    "session_id",
    "message",
    "selected_ids",
    "route",
    "citation_ids",
    "safe_error",
    "timings_ms",
    "elapsed_ms",
    "over_budget",
    "node_trace",
}


def _dependencies(
    *,
    retriever: DeterministicRetrieverFake | None = None,
    model: DeterministicModelFake | None = None,
    metadata: DeterministicMetadataFake | None = None,
) -> BotDependencies:
    return BotDependencies(
        retriever=retriever or DeterministicRetrieverFake(),
        model=model or DeterministicModelFake(),
        metadata=metadata or DeterministicMetadataFake(),
    )


def _input(message: str, selected_ids=_SELECTED):  # noqa: ANN001, ANN202
    return {
        "request_id": "request-synthetic",
        "session_id": "session-synthetic",
        "message": message,
        "selected_ids": selected_ids,
    }


def test_route_request_is_deterministic_without_model_calls():
    assert route_request("이 파일의 문서 정보를 알려줘", _SELECTED) == "metadata"
    assert route_request("선택한 문서를 요약해줘", _SELECTED) == "document_qa"
    assert route_request("일반 대화를 해줘", ()) == "unsupported"


def test_document_qa_route_calls_retriever_and_model_once_and_limits_state():
    retriever = DeterministicRetrieverFake(BotStepResult(citation_ids=("chunk-1", "chunk-1")))
    model = DeterministicModelFake()
    metadata = DeterministicMetadataFake()

    result = create_bot_graph(_dependencies(retriever=retriever, model=model, metadata=metadata)).invoke(
        _input("선택한 합성 문서를 요약해줘")
    )

    assert result["route"] == "document_qa"
    assert result["citation_ids"] == ("chunk-1",)
    assert result["safe_error"] == ""
    assert result["node_trace"] == ("router", "document_qa", "finalize")
    assert set(result) <= _ALLOWED_STATE
    assert set(result["timings_ms"]) == {"router", "retriever", "model"}
    assert retriever.calls == 1
    assert model.calls == 1
    assert metadata.calls == 0


def test_metadata_route_calls_only_metadata_dependency():
    retriever = DeterministicRetrieverFake()
    model = DeterministicModelFake()
    metadata = DeterministicMetadataFake()

    result = create_bot_graph(_dependencies(retriever=retriever, model=model, metadata=metadata)).invoke(
        _input("선택 파일의 metadata를 확인해줘")
    )

    assert result["route"] == "metadata"
    assert result["node_trace"] == ("router", "metadata", "finalize")
    assert retriever.calls == 0
    assert model.calls == 0
    assert metadata.calls == 1


def test_unsupported_route_does_not_call_dependencies():
    retriever = DeterministicRetrieverFake()
    model = DeterministicModelFake()
    metadata = DeterministicMetadataFake()

    result = create_bot_graph(_dependencies(retriever=retriever, model=model, metadata=metadata)).invoke(
        _input("일반 합성 대화", selected_ids=())
    )

    assert result["route"] == "unsupported"
    assert result["safe_error"] == "unsupported_request"
    assert result["citation_ids"] == ()
    assert result["node_trace"] == ("router", "unsupported", "finalize")
    assert retriever.calls == model.calls == metadata.calls == 0


def test_retriever_safe_error_stops_model_call():
    retriever = DeterministicRetrieverFake(BotStepResult(safe_error="retrieval_unavailable"))
    model = DeterministicModelFake()

    result = create_bot_graph(_dependencies(retriever=retriever, model=model)).invoke(
        _input("선택한 합성 문서를 확인해줘")
    )

    assert result["safe_error"] == "retrieval_unavailable"
    assert set(result["timings_ms"]) == {"router", "retriever"}
    assert retriever.calls == 1
    assert model.calls == 0


def test_no_evidence_stops_model_call_with_stable_error():
    retriever = DeterministicRetrieverFake()
    model = DeterministicModelFake()

    result = create_bot_graph(_dependencies(retriever=retriever, model=model)).invoke(
        _input("선택한 합성 문서를 확인해줘")
    )

    assert result["safe_error"] == "insufficient_evidence"
    assert result["citation_ids"] == ()
    assert retriever.calls == 1
    assert model.calls == 0


def test_retriever_timeout_is_safe_and_stops_model_call():
    retriever = DeterministicRetrieverFake(raise_timeout=True)
    model = DeterministicModelFake()

    result = create_bot_graph(_dependencies(retriever=retriever, model=model)).invoke(
        _input("선택한 합성 문서를 확인해줘")
    )

    assert result["safe_error"] == "retrieval_timeout"
    assert result["node_trace"] == ("router", "document_qa", "finalize")
    assert retriever.calls == 1
    assert model.calls == 0


def test_model_timeout_preserves_server_retrieval_citations():
    retriever = DeterministicRetrieverFake(BotStepResult(citation_ids=("chunk-1",)))
    model = DeterministicModelFake(raise_timeout=True)

    result = create_bot_graph(_dependencies(retriever=retriever, model=model)).invoke(
        _input("선택한 합성 문서를 확인해줘")
    )

    assert result["safe_error"] == "model_timeout"
    assert result["citation_ids"] == ("chunk-1",)
    assert retriever.calls == 1
    assert model.calls == 1


def test_metadata_timeout_returns_tool_timeout_without_other_calls():
    retriever = DeterministicRetrieverFake()
    model = DeterministicModelFake()
    metadata = DeterministicMetadataFake(raise_timeout=True)

    result = create_bot_graph(_dependencies(retriever=retriever, model=model, metadata=metadata)).invoke(
        _input("선택 파일의 메타데이터를 확인해줘")
    )

    assert result["safe_error"] == "tool_timeout"
    assert result["node_trace"] == ("router", "metadata", "finalize")
    assert retriever.calls == 0
    assert model.calls == 0
    assert metadata.calls == 1


def test_same_input_has_same_node_trace_and_dependency_order():
    call_log: list[str] = []
    retriever = DeterministicRetrieverFake(BotStepResult(citation_ids=("chunk-1",)), call_log=call_log)
    model = DeterministicModelFake(call_log=call_log)
    graph = create_bot_graph(_dependencies(retriever=retriever, model=model))

    first = graph.invoke(_input("선택한 합성 문서를 확인해줘"))
    second = graph.invoke(_input("선택한 합성 문서를 확인해줘"))

    assert first["route"] == second["route"] == "document_qa"
    assert first["node_trace"] == second["node_trace"] == ("router", "document_qa", "finalize")
    assert call_log == ["retriever", "model", "retriever", "model"]


def test_clock_controlled_timings_mark_over_budget():
    ticks = iter((0.000, 0.002, 0.002, 0.008, 0.008, 0.016))
    retriever = DeterministicRetrieverFake(BotStepResult(citation_ids=("chunk-1",)))
    dependencies = BotDependencies(
        retriever=retriever,
        model=DeterministicModelFake(),
        metadata=DeterministicMetadataFake(),
        budget_ms=15,
        clock=lambda: next(ticks),
    )

    result = create_bot_graph(dependencies).invoke(_input("선택한 합성 문서를 확인해줘"))

    assert result["timings_ms"] == {"router": 2.0, "retriever": 6.0, "model": 8.0}
    assert result["elapsed_ms"] == 16.0
    assert result["over_budget"] is True


def test_graph_import_and_compile_create_no_http_or_database_clients(monkeypatch):  # noqa: ANN001
    def fail(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("graph import/compile에서 client 또는 DB 연결을 만들면 안 됩니다.")

    monkeypatch.setattr(httpx, "Client", fail)
    monkeypatch.setattr(psycopg, "connect", fail)

    reloaded = importlib.reload(graph_module)
    compiled = reloaded.create_bot_graph(_dependencies())

    assert compiled is not None
    assert "ChatOpenAI" not in vars(reloaded)
    assert "Settings" not in vars(reloaded)
