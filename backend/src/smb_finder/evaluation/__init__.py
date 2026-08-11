"""합성 문서 챗봇과 기안 생성기의 오프라인 평가 도구."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AggregateMetrics": (".models", "AggregateMetrics"),
    "CaseEvaluationResult": (".models", "CaseEvaluationResult"),
    "CorpusSnapshot": (".models", "CorpusSnapshot"),
    "EvaluationReport": (".models", "EvaluationReport"),
    "EndToEndAggregateMetrics": (".models", "EndToEndAggregateMetrics"),
    "EndToEndCaseEvaluationResult": (".models", "EndToEndCaseEvaluationResult"),
    "EndToEndEvaluationReport": (".models", "EndToEndEvaluationReport"),
    "GoldenCase": (".models", "GoldenCase"),
    "GoldenDataset": (".models", "GoldenDataset"),
    "ProposalEvaluationReport": (".proposal_models", "ProposalEvaluationReport"),
    "capture_corpus_snapshot": (".document_chatbot", "capture_corpus_snapshot"),
    "load_candidate_dataset": (".datasets", "load_candidate_dataset"),
    "load_golden_dataset": (".datasets", "load_golden_dataset"),
    "load_proposal_dataset": (".proposal_runner", "load_proposal_dataset"),
    "run_end_to_end_evaluation": (".end_to_end", "run_end_to_end_evaluation"),
    "run_proposal_evaluation": (".proposal_runner", "run_proposal_evaluation"),
    "run_retrieval_evaluation": (".document_chatbot", "run_retrieval_evaluation"),
}

__all__ = [
    "AggregateMetrics",
    "CaseEvaluationResult",
    "CorpusSnapshot",
    "EvaluationReport",
    "EndToEndAggregateMetrics",
    "EndToEndCaseEvaluationResult",
    "EndToEndEvaluationReport",
    "GoldenCase",
    "GoldenDataset",
    "ProposalEvaluationReport",
    "capture_corpus_snapshot",
    "load_candidate_dataset",
    "load_golden_dataset",
    "load_proposal_dataset",
    "run_proposal_evaluation",
    "run_retrieval_evaluation",
    "run_end_to_end_evaluation",
]


def __getattr__(name: str) -> Any:
    """선택한 평가 surface만 import해 독립 evaluator가 무거운 agent 의존성을 피하게 한다."""

    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
