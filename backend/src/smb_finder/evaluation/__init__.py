"""합성 데이터 기반 문서 챗봇 오프라인 평가 도구."""

from .datasets import load_candidate_dataset, load_golden_dataset
from .document_chatbot import capture_corpus_snapshot, run_retrieval_evaluation
from .end_to_end import run_end_to_end_evaluation
from .models import (
    AggregateMetrics,
    CaseEvaluationResult,
    CorpusSnapshot,
    EvaluationReport,
    EndToEndAggregateMetrics,
    EndToEndCaseEvaluationResult,
    EndToEndEvaluationReport,
    GoldenCase,
    GoldenDataset,
)

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
    "capture_corpus_snapshot",
    "load_candidate_dataset",
    "load_golden_dataset",
    "run_retrieval_evaluation",
    "run_end_to_end_evaluation",
]
