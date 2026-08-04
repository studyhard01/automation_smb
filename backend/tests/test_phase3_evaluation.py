"""Phase 3 dataset 분리·등록과 LLM judge fail-open 계약 테스트."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from smb_finder.evaluation.datasets import GoldenDatasetError, load_candidate_dataset, load_golden_dataset
from smb_finder.evaluation.judges import build_answer_judge_rows, run_llm_judges
from smb_finder.evaluation.managed_datasets import build_managed_dataset_records, register_managed_dataset
from smb_finder.evaluation.models import (
    CorpusSnapshot,
    EndToEndCaseEvaluationResult,
    EndToEndEvaluationReport,
    GoldenDataset,
)


def _dataset() -> GoldenDataset:
    return GoldenDataset.model_validate(
        {
            "name": "synthetic-golden.jsonl",
            "version": "v2",
            "fingerprint": "golden123",
            "tier": "golden",
            "cases": [
                {
                    "case_id": "synthetic-phase3-001",
                    "inputs": {"question": "Alpha 일정은?", "top_k": 5},
                    "expectations": {
                        "expected_document_keys": ["alpha.md"],
                        "expected_chunk_keys": ["alpha.md#1"],
                        "expected_tool_calls": [{"name": "search_rag_chunks"}],
                        "expected_facts": ["7월"],
                        "reference_answer": "Alpha 일정은 7월입니다.",
                    },
                    "tags": {"synthetic": "true", "category": "single-hop", "difficulty": "easy"},
                    "provenance": {
                        "review_status": "validated",
                        "source_kind": "corpus",
                        "generation_method": "manual",
                        "source_chunk_hashes": {"alpha.md#1": "a" * 64},
                    },
                }
            ],
        }
    )


def _report() -> EndToEndEvaluationReport:
    return EndToEndEvaluationReport(
        dataset_name="synthetic-golden.jsonl",
        dataset_version="v2",
        dataset_fingerprint="golden123",
        corpus=CorpusSnapshot(fingerprint="corpus123"),
        provider="openai",
        model="gpt-4.1-mini",
        skill_id="rag-grounded-answer",
        cases=[
            EndToEndCaseEvaluationResult(
                case_id="synthetic-phase3-001",
                status="ok",
                question="Alpha 일정은?",
                answer="Alpha 일정은 7월입니다.",
            )
        ],
        aggregate={"total_cases": 1, "successful_cases": 1, "error_cases": 0},
    )


def test_managed_dataset_records_use_mlflow_reserved_expectation_keys():
    records = build_managed_dataset_records(_dataset())

    assert records[0]["inputs"] == {"question": "Alpha 일정은?", "top_k": 5}
    assert records[0]["expectations"]["expected_facts"] == ["7월"]
    assert records[0]["expectations"]["expected_response"] == "Alpha 일정은 7월입니다."
    assert records[0]["tags"]["review_status"] == "validated"
    assert records[0]["tags"]["review_decision"] == ""


def test_repository_golden_and_candidate_files_are_physically_separated():
    golden = load_golden_dataset("data/evaluation/document_chatbot_golden.jsonl")
    candidates = load_candidate_dataset("data/evaluation/document_chatbot_candidates.jsonl")

    assert len(golden.cases) == 15
    assert len(candidates.cases) == 25
    assert {case.provenance.review_status for case in golden.cases} == {"validated"}
    assert {case.provenance.review_status for case in candidates.cases} == {"candidate"}
    with pytest.raises(GoldenDatasetError, match="review_status=validated"):
        load_golden_dataset("data/evaluation/document_chatbot_candidates.jsonl")


def test_register_managed_dataset_creates_and_merges_records():
    class FakeManagedDataset:
        dataset_id = "d-local"

        def __init__(self) -> None:
            self.records: list[dict[str, Any]] = []

        def merge_records(self, records: list[dict[str, Any]]) -> None:
            self.records = records

        def to_df(self) -> list[dict[str, Any]]:
            return self.records

    class FakeDatasetsApi:
        def __init__(self) -> None:
            self.managed = FakeManagedDataset()
            self.created_tags: dict[str, str] = {}

        def search_datasets(self, **_kwargs: Any) -> list[Any]:
            return []

        def create_dataset(self, *, tags: dict[str, str], **_kwargs: Any) -> FakeManagedDataset:
            self.created_tags = tags
            return self.managed

    datasets_api = FakeDatasetsApi()
    fake_mlflow = SimpleNamespace(
        genai=SimpleNamespace(datasets=datasets_api),
        set_tracking_uri=lambda _uri: None,
        set_experiment=lambda _name: SimpleNamespace(experiment_id="1"),
    )

    result = register_managed_dataset(
        fake_mlflow,
        tracking_uri="http://127.0.0.1:5000",
        experiment_name="evaluation",
        dataset_name="golden",
        dataset=_dataset(),
        corpus=CorpusSnapshot(fingerprint="corpus123", document_count=1, chunk_count=1),
    )

    assert result.dataset_id == "d-local"
    assert result.record_count == 1
    assert datasets_api.created_tags["tier"] == "golden"


def test_judge_rows_and_runner_keep_scorer_failure_isolated(monkeypatch):
    rows = build_answer_judge_rows(_dataset(), _report())
    assert rows[0]["outputs"] == "Alpha 일정은 7월입니다."
    assert rows[0]["expectations"] == {"expected_facts": ["7월"]}

    class NamedScorer:
        def __init__(self, name: str, **_kwargs: Any) -> None:
            self.name = name
            self.kwargs = _kwargs

    scorers = SimpleNamespace(
        Correctness=lambda **kwargs: NamedScorer("correctness", **kwargs),
        RelevanceToQuery=lambda **kwargs: NamedScorer("relevance_to_query", **kwargs),
        RetrievalGroundedness=lambda **kwargs: NamedScorer("retrieval_groundedness", **kwargs),
    )
    monkeypatch.setattr(
        "smb_finder.evaluation.judges._zero_retry_policy",
        lambda: SimpleNamespace(TimeoutErrorRetries=0, RateLimitErrorRetries=0),
    )
    monkeypatch.setattr("smb_finder.evaluation.judges.importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr("smb_finder.evaluation.judges.importlib.import_module", lambda _name: scorers)

    calls: list[str] = []

    def evaluate(*, data: list[Any], scorers: list[NamedScorer]) -> None:
        calls.append(scorers[0].name)
        assert data
        inference_params = scorers[0].kwargs["inference_params"]
        assert inference_params["temperature"] == 0.0
        assert inference_params["max_tokens"] == 256
        assert inference_params["timeout"] == 30.0
        assert inference_params["retry_policy"].TimeoutErrorRetries == 0
        assert inference_params["retry_policy"].RateLimitErrorRetries == 0
        if scorers[0].name == "relevance_to_query":
            raise RuntimeError("synthetic judge failure")

    trace = SimpleNamespace(
        data=SimpleNamespace(
            spans=[SimpleNamespace(attributes={"evaluation.case_id": "synthetic-phase3-001"})]
        )
    )
    fake_mlflow = SimpleNamespace(
        genai=SimpleNamespace(evaluate=evaluate),
        search_traces=lambda **_kwargs: [trace],
    )

    summary = run_llm_judges(
        fake_mlflow,
        dataset=_dataset(),
        report=_report(),
        run_id="run-local",
        model="openai:/gpt-4.1-mini",
    )

    assert calls == ["correctness", "relevance_to_query", "retrieval_groundedness"]
    assert summary.status == "partial"
    assert [result.status for result in summary.scorers] == ["completed", "failed", "completed"]


def test_judge_runner_fails_fast_when_litellm_is_missing(monkeypatch):
    monkeypatch.setattr("smb_finder.evaluation.judges.importlib.util.find_spec", lambda _name: None)

    summary = run_llm_judges(
        SimpleNamespace(),
        dataset=_dataset(),
        report=_report(),
        run_id="run-local",
        model="openai:/gpt-4.1-mini",
    )

    assert summary.status == "failed"
    assert [result.status for result in summary.scorers] == ["failed", "failed", "failed"]
    assert all("litellm" in result.error for result in summary.scorers)
