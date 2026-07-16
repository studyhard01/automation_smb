"""네트워크나 judge 없이 재현 가능한 문서 검색 지표."""

from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import mean
from typing import Any

from .models import AggregateMetrics, CaseEvaluationResult, GoldenCase, RetrievedChunk


def _normalize_key(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/").casefold()


def _safe_document_key(hit: Any) -> str:
    """내부 전체 경로 대신 artifact에 기록 가능한 파일명 key만 반환한다."""

    file_name = str(getattr(hit, "file_name", "") or "").strip()
    if file_name:
        return file_name
    file_path = str(getattr(hit, "file_path", "") or "").replace("\\", "/").rstrip("/")
    return file_path.rsplit("/", maxsplit=1)[-1]


def build_retrieved_chunks(hits: Iterable[Any]) -> list[RetrievedChunk]:
    """RAG hit를 경로·본문 비노출 평가 결과로 정규화한다."""

    results: list[RetrievedChunk] = []
    for rank, hit in enumerate(hits, start=1):
        document_key = _safe_document_key(hit)
        chunk_index = int(getattr(hit, "chunk_index", 0))
        results.append(
            RetrievedChunk(
                document_key=document_key,
                chunk_key=f"{document_key}#{chunk_index}",
                rank=rank,
                similarity=float(getattr(hit, "similarity", 0.0)),
            )
        )
    return results


def score_retrieval_case(
    case: GoldenCase,
    retrieved: list[RetrievedChunk],
    *,
    actual_tool_calls: list[str],
) -> dict[str, float | None]:
    """Hit@K, Recall@K, MRR, tool exact match와 no-answer 정확도를 계산한다."""

    expected_chunks = {_normalize_key(value) for value in case.expectations.expected_chunk_keys if value.strip()}
    expected_documents = {_normalize_key(value) for value in case.expectations.expected_document_keys if value.strip()}
    ranked_values = [
        _normalize_key(item.chunk_key if expected_chunks else item.document_key)
        for item in retrieved
    ]
    relevant = expected_chunks or expected_documents

    if relevant:
        matched = relevant.intersection(ranked_values)
        hit_at_k = float(bool(matched))
        recall_at_k = len(matched) / len(relevant)
        first_rank = next((rank for rank, value in enumerate(ranked_values, start=1) if value in relevant), None)
        reciprocal_rank = 1.0 / first_rank if first_rank else 0.0
    else:
        hit_at_k = None
        recall_at_k = None
        reciprocal_rank = None

    expected_tools = [call.name for call in case.expectations.expected_tool_calls]
    tool_exact_match = float(actual_tool_calls == expected_tools)
    no_answer_correct = None if case.expectations.should_answer else float(not retrieved)
    return {
        "hit_at_k": hit_at_k,
        "recall_at_k": recall_at_k,
        "reciprocal_rank": reciprocal_rank,
        "tool_exact_match": tool_exact_match,
        "no_answer_correct": no_answer_correct,
    }


def _average(values: Iterable[float | None]) -> float:
    numbers = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return mean(numbers) if numbers else 0.0


def _percentile(values: Iterable[float], percentile: float) -> float:
    """외부 의존성 없이 선형 보간 percentile을 계산한다."""

    numbers = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not numbers:
        return 0.0
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    return numbers[lower] + (numbers[upper] - numbers[lower]) * (position - lower)


def aggregate_case_results(results: list[CaseEvaluationResult]) -> AggregateMetrics:
    """case 결과를 MLflow run 단위 metric으로 집계한다."""

    evaluated = [result for result in results if result.status != "skipped"]
    successful = [result for result in evaluated if result.status == "ok"]
    no_answer = [result.no_answer_correct for result in results if result.no_answer_correct is not None]
    evaluated_count = len(evaluated)
    total_count = len(results)
    error_count = sum(result.status == "error" for result in evaluated)
    return AggregateMetrics(
        total_cases=total_count,
        evaluated_cases=evaluated_count,
        skipped_cases=total_count - evaluated_count,
        successful_cases=len(successful),
        error_cases=error_count,
        hit_at_k=_average(result.hit_at_k for result in successful),
        recall_at_k=_average(result.recall_at_k for result in successful),
        mrr=_average(result.reciprocal_rank for result in successful),
        tool_exact_match=_average(result.tool_exact_match for result in results),
        no_answer_accuracy=_average(no_answer) if no_answer else None,
        success_rate=len(successful) / evaluated_count if evaluated_count else 0.0,
        error_rate=error_count / evaluated_count if evaluated_count else 0.0,
        over_budget_rate=(sum(result.over_budget for result in evaluated) / evaluated_count if evaluated_count else 0.0),
        embedding_p50_ms=_percentile((result.embedding_ms for result in successful), 0.50),
        embedding_p95_ms=_percentile((result.embedding_ms for result in successful), 0.95),
        db_p50_ms=_percentile((result.db_ms for result in successful), 0.50),
        db_p95_ms=_percentile((result.db_ms for result in successful), 0.95),
        retrieval_p50_ms=_percentile((result.elapsed_ms for result in successful), 0.50),
        retrieval_p95_ms=_percentile((result.elapsed_ms for result in successful), 0.95),
    )
