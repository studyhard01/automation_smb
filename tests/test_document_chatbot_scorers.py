from __future__ import annotations

from smb_finder.evaluation.models import CaseEvaluationResult, GoldenCase, RetrievedChunk
from smb_finder.evaluation.scorers import aggregate_case_results, score_retrieval_case


def _case() -> GoldenCase:
    return GoldenCase.model_validate(
        {
            "case_id": "synthetic-score-001",
            "inputs": {"question": "합성 질문", "top_k": 3},
            "expectations": {
                "expected_document_keys": ["alpha.md", "beta.md"],
                "expected_chunk_keys": ["alpha.md#1", "beta.md#2"],
                "expected_tool_calls": [{"name": "search_rag_chunks"}],
                "reference_answer": "합성 답변",
                "should_answer": True,
            },
            "provenance": {
                "review_status": "candidate",
                "source_kind": "corpus",
                "generation_method": "corpus-derived",
            },
        }
    )


def test_retrieval_scorers_match_manual_calculation():
    retrieved = [
        RetrievedChunk(document_key="noise.md", chunk_key="noise.md#0", rank=1, similarity=0.8),
        RetrievedChunk(document_key="BETA.md", chunk_key="BETA.md#2", rank=2, similarity=0.7),
    ]

    scores = score_retrieval_case(_case(), retrieved, actual_tool_calls=["search_rag_chunks"])

    assert scores == {
        "hit_at_k": 1.0,
        "recall_at_k": 0.5,
        "reciprocal_rank": 0.5,
        "tool_exact_match": 1.0,
        "no_answer_correct": None,
    }


def test_aggregate_metrics_include_linear_p50_p95_and_error_rate():
    results = [
        CaseEvaluationResult(
            case_id="ok-1",
            status="ok",
            question="q1",
            top_k=5,
            hit_at_k=1,
            recall_at_k=0.5,
            reciprocal_rank=1,
            tool_exact_match=1,
            embedding_ms=10,
            db_ms=20,
            elapsed_ms=30,
        ),
        CaseEvaluationResult(
            case_id="ok-2",
            status="ok",
            question="q2",
            top_k=5,
            hit_at_k=0,
            recall_at_k=0,
            reciprocal_rank=0,
            tool_exact_match=1,
            embedding_ms=30,
            db_ms=40,
            elapsed_ms=70,
            over_budget=True,
        ),
        CaseEvaluationResult(
            case_id="error-3",
            status="error",
            question="q3",
            top_k=5,
            tool_exact_match=1,
            error_code="synthetic_error",
        ),
    ]

    aggregate = aggregate_case_results(results)

    assert aggregate.hit_at_k == 0.5
    assert aggregate.recall_at_k == 0.25
    assert aggregate.mrr == 0.5
    assert aggregate.error_rate == 1 / 3
    assert aggregate.over_budget_rate == 1 / 3
    assert aggregate.embedding_p50_ms == 20
    assert aggregate.embedding_p95_ms == 29
    assert aggregate.retrieval_p95_ms == 68
