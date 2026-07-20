"""Phase 2 agent 평가와 MLflow manual tracing 계약 테스트."""

from __future__ import annotations

from typing import Any

from smb_finder.evaluation.end_to_end import run_end_to_end_evaluation
from smb_finder.evaluation.models import CorpusSnapshot, GoldenDataset
from smb_finder.evaluation.tracing import MlflowTraceObserver
from smb_finder.playground.models import ChatResponse, TokenUsage, ToolCallTrace


class FakeSpan:
    def __init__(self, name: str, parent: str, run_id: str) -> None:
        self.name = name
        self.parent = parent
        self.run_id = run_id
        self.inputs: Any = None
        self.outputs: Any = None
        self.attributes: dict[str, Any] = {}

    def set_inputs(self, inputs: Any) -> None:
        self.inputs = inputs

    def set_outputs(self, outputs: Any) -> None:
        self.outputs = outputs

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class FakeSpanContext:
    def __init__(self, client: "FakeMlflow", span: FakeSpan) -> None:
        self.client = client
        self.span = span

    def __enter__(self) -> FakeSpan:
        self.client.stack.append(self.span.name)
        self.client.spans.append(self.span)
        return self.span

    def __exit__(self, *_args: Any) -> None:
        self.client.stack.pop()


class FakeMlflow:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.stack: list[str] = []
        self.spans: list[FakeSpan] = []

    def start_span(
        self,
        *,
        name: str,
        span_type: str,  # noqa: ARG002
        attributes: dict[str, Any] | None,
        run_id: str | None,
    ) -> FakeSpanContext:
        if self.fail_start:
            raise RuntimeError("trace offline")
        span = FakeSpan(name, self.stack[-1] if self.stack else "", run_id or "")
        span.attributes.update(attributes or {})
        return FakeSpanContext(self, span)


def _dataset() -> GoldenDataset:
    return GoldenDataset.model_validate(
        {
            "name": "synthetic-phase2",
            "version": "v1",
            "fingerprint": "phase2",
            "cases": [
                {
                    "case_id": "synthetic-phase2-001",
                    "inputs": {"question": "Alpha 일정은?", "top_k": 5},
                    "expectations": {
                        "expected_document_keys": ["alpha.md"],
                        "expected_chunk_keys": ["alpha.md#3"],
                        "expected_tool_calls": [{"name": "search_rag_chunks"}],
                        "expected_facts": ["7월"],
                        "reference_answer": "Alpha 일정은 7월입니다.",
                    },
                    "provenance": {
                        "review_status": "candidate",
                        "source_kind": "corpus",
                        "generation_method": "corpus-derived",
                    },
                }
            ],
        }
    )


def test_mlflow_observer_preserves_nested_span_hierarchy_and_run_id():
    mlflow = FakeMlflow()
    observer = MlflowTraceObserver(mlflow, run_id="run-123")

    with observer.span(name="root", span_type="WORKFLOW", inputs={"case": "1"}) as root:
        root.set_attributes({"ok": True})
        with observer.span(name="child", span_type="TOOL", inputs={"query_length": 5}) as child:
            child.set_outputs({"result_count": 1})

    assert [(span.name, span.parent) for span in mlflow.spans] == [("root", ""), ("child", "root")]
    assert mlflow.spans[0].run_id == "run-123"
    assert mlflow.spans[1].run_id == ""
    assert mlflow.spans[0].inputs == {"case": "1"}
    assert mlflow.spans[1].outputs == {"result_count": 1}
    assert observer.errors == []


def test_mlflow_observer_fails_open_when_trace_backend_is_unavailable():
    observer = MlflowTraceObserver(FakeMlflow(fail_start=True))
    executed = False

    with observer.span(name="root", span_type="WORKFLOW"):
        executed = True

    assert executed is True
    assert observer.errors and observer.errors[0].startswith("span_start:root")


def test_end_to_end_runner_scores_actual_agent_response():
    class FakeAgent:
        def run(self, request, registry, openai_api_key: str = "", request_id: str = ""):  # noqa: ANN001, ANN201, ARG002
            return ChatResponse(
                request_id=request_id,
                session_id=request.session_id,
                provider_used="openai",
                model_used="gpt-4.1-mini",
                assistant_message="alpha.md 근거에 따르면 Alpha 일정은 7월입니다.",
                tool_calls=[
                    ToolCallTrace(
                        tool_id="search_rag_chunks",
                        tool_name="벡터 기반 Chunk 검색",
                        result_payload={
                            "hits": [
                                {
                                    "file_name": "alpha.md",
                                    "file_path": "C:/hidden/alpha.md",
                                    "chunk_index": 3,
                                    "similarity": 0.9,
                                }
                            ]
                        },
                    )
                ],
                elapsed_ms=123,
                token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, calls=1),
            )

    report = run_end_to_end_evaluation(
        _dataset(),
        FakeAgent(),  # type: ignore[arg-type]
        {},
        corpus=CorpusSnapshot(fingerprint="corpus"),
        provider="openai",
        model="gpt-4.1-mini",
    )

    assert report.mode == "end-to-end"
    assert report.aggregate.success_rate == 1
    assert report.aggregate.hit_at_k == 1
    assert report.aggregate.tool_exact_match == 1
    assert report.aggregate.fact_coverage == 1
    assert report.aggregate.citation_match == 1
    assert report.aggregate.total_tokens == 15
    assert "hidden" not in report.model_dump_json()
