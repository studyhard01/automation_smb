"""MLflow built-in LLM judge를 코드 지표와 격리해 fail-open으로 실행한다."""

from __future__ import annotations

import importlib
import importlib.util
import os
from typing import Any

from .models import (
    EndToEndEvaluationReport,
    GoldenDataset,
    JudgeEvaluationSummary,
    JudgeScorerResult,
)


def build_answer_judge_rows(
    dataset: GoldenDataset,
    report: EndToEndEvaluationReport,
) -> list[dict[str, Any]]:
    """Correctness와 RelevanceToQuery가 읽는 inputs/outputs/expectations 행을 만든다."""

    case_by_id = {case.case_id: case for case in dataset.cases}
    rows: list[dict[str, Any]] = []
    for result in report.cases:
        if result.status != "ok":
            continue
        case = case_by_id.get(result.case_id)
        if case is None:
            continue
        # MLflow Correctness는 expected_facts와 expected_response를 동시에 받지 않는다.
        # 구조화된 fact가 있으면 이를 우선하고, 없는 case만 reference 답변을 사용한다.
        expectations = (
            {"expected_facts": case.expectations.expected_facts}
            if case.expectations.expected_facts
            else {"expected_response": case.expectations.reference_answer}
        )
        rows.append(
            {
                "inputs": {"question": case.inputs.question},
                "outputs": result.answer,
                "expectations": expectations,
                "tags": {"case_id": case.case_id, "synthetic": "true"},
            }
        )
    return rows


def _trace_case_id(trace: Any) -> str:
    data = getattr(trace, "data", None)
    spans = list(getattr(data, "spans", []) or [])
    for span in spans:
        attributes = getattr(span, "attributes", {}) or {}
        case_id = attributes.get("evaluation.case_id")
        if case_id:
            return str(case_id)
    return ""


def _groundedness_traces(mlflow: Any, run_id: str, dataset: GoldenDataset) -> list[Any]:
    """RAG 호출이 기대되는 case의 content 포함 trace만 고른다."""

    rag_case_ids = {
        case.case_id
        for case in dataset.cases
        if any(call.name == "search_rag_chunks" for call in case.expectations.expected_tool_calls)
    }
    traces = mlflow.search_traces(run_id=run_id, return_type="list", flush=True)
    return [trace for trace in traces if _trace_case_id(trace) in rag_case_ids]


def _zero_retry_policy() -> Any:
    """외부 가격표 조회 없이 LiteLLM의 모든 transient retry를 비활성화한다."""

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    from litellm import RetryPolicy

    return RetryPolicy(
        TimeoutErrorRetries=0,
        RateLimitErrorRetries=0,
        InternalServerErrorRetries=0,
        ContentPolicyViolationErrorRetries=0,
        BadRequestErrorRetries=0,
        AuthenticationErrorRetries=0,
    )


def run_llm_judges(
    mlflow: Any,
    *,
    dataset: GoldenDataset,
    report: EndToEndEvaluationReport,
    run_id: str,
    model: str,
    max_output_tokens: int = 256,
    request_timeout_seconds: float = 30.0,
) -> JudgeEvaluationSummary:
    """세 judge를 독립 실행하고 하나가 실패해도 나머지와 코드 지표를 보존한다."""

    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")
    if importlib.util.find_spec("litellm") is None:
        error = "직접 provider judge 실행에 필요한 litellm이 설치되지 않았습니다."
        return JudgeEvaluationSummary(
            enabled=True,
            status="failed",
            model=model,
            scorers=[
                JudgeScorerResult(name=name, status="failed", error=error)
                for name in ("correctness", "relevance_to_query", "retrieval_groundedness")
            ],
        )

    scorers_module = importlib.import_module("mlflow.genai.scorers")
    answer_rows = build_answer_judge_rows(dataset, report)
    results: list[JudgeScorerResult] = []
    inference_params = {
        "temperature": 0.0,
        "max_tokens": max(1, max_output_tokens),
        "timeout": max(1.0, request_timeout_seconds),
        # MLflow 기본값(일시 오류 10회 재시도)은 단일 scorer를 수분 지연시킬 수 있다.
        # 평가 전체는 scorer별 fail-open이므로 각 외부 요청은 한 번만 시도한다.
        "retry_policy": _zero_retry_policy(),
    }

    answer_scorers = (
        ("correctness", scorers_module.Correctness(model=model, inference_params=inference_params)),
        (
            "relevance_to_query",
            scorers_module.RelevanceToQuery(model=model, inference_params=inference_params),
        ),
    )
    for name, scorer in answer_scorers:
        if not answer_rows:
            results.append(JudgeScorerResult(name=name, status="skipped", error="성공한 답변 case가 없습니다."))
            continue
        try:
            mlflow.genai.evaluate(data=answer_rows, scorers=[scorer])
            results.append(JudgeScorerResult(name=name, status="completed"))
        except Exception as exc:  # judge 오류는 deterministic 평가를 중단하지 않는다.
            results.append(JudgeScorerResult(name=name, status="failed", error=str(exc)[:500]))

    try:
        traces = _groundedness_traces(mlflow, run_id, dataset)
        if not traces:
            results.append(
                JudgeScorerResult(
                    name="retrieval_groundedness",
                    status="skipped",
                    error="RAG content trace를 찾지 못했습니다.",
                )
            )
        else:
            scorer = scorers_module.RetrievalGroundedness(
                model=model,
                inference_params=inference_params,
            )
            mlflow.genai.evaluate(data=traces, scorers=[scorer])
            results.append(JudgeScorerResult(name="retrieval_groundedness", status="completed"))
    except Exception as exc:  # judge 오류는 deterministic 평가를 중단하지 않는다.
        results.append(
            JudgeScorerResult(name="retrieval_groundedness", status="failed", error=str(exc)[:500])
        )

    completed = sum(result.status == "completed" for result in results)
    failed = sum(result.status == "failed" for result in results)
    if completed == len(results):
        status = "completed"
    elif completed:
        status = "partial"
    elif failed:
        status = "failed"
    else:
        status = "skipped"
    return JudgeEvaluationSummary(enabled=True, status=status, model=model, scorers=results)


def judge_metrics(summary: JudgeEvaluationSummary) -> dict[str, float]:
    """judge 상태를 답변 품질 점수와 분리된 운영 metric으로 변환한다."""

    return {
        "judge_enabled": float(summary.enabled),
        "judge_completed_count": float(sum(result.status == "completed" for result in summary.scorers)),
        "judge_failed_count": float(sum(result.status == "failed" for result in summary.scorers)),
        "judge_skipped_count": float(sum(result.status == "skipped" for result in summary.scorers)),
    }
