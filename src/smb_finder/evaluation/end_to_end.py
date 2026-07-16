"""실제 Playground agent 경로를 사용하는 문서 챗봇 Phase 2 평가."""

from __future__ import annotations

import re
import uuid
from typing import Any

from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.models import ChatRequest, ChatResponse
from smb_finder.playground.tools import ToolHandler
from smb_finder.telemetry import NoopTraceObserver, TraceObserver

from .models import (
    CorpusSnapshot,
    EndToEndCaseEvaluationResult,
    EndToEndEvaluationReport,
    GoldenCase,
    GoldenDataset,
)
from .scorers import aggregate_end_to_end_results, build_retrieved_chunks, score_retrieval_case


def _tool_hits(response: ChatResponse) -> list[Any]:
    hits: list[Any] = []
    for tool_call in response.tool_calls:
        if tool_call.tool_id != "search_rag_chunks" or not isinstance(tool_call.result_payload, dict):
            continue
        payload_hits = tool_call.result_payload.get("hits")
        if isinstance(payload_hits, list):
            hits.extend(payload_hits)
    return hits


def _text_fraction(answer: str, expected_values: list[str]) -> float | None:
    if not expected_values:
        return None
    normalized_answer = answer.casefold()
    return sum(value.casefold() in normalized_answer for value in expected_values) / len(expected_values)


def _fact_tokens(value: str) -> set[str]:
    """문장 표현 차이에 덜 민감하도록 합성 정답의 핵심 token을 정규화한다."""

    suffixes = ("입니다", "한다", "하며", "이며", "이다", "으로", "에서", "에게", "까지", "부터", "과", "와", "을", "를", "은", "는", "이", "가")
    tokens: set[str] = set()
    for raw_token in re.findall(r"[0-9a-zA-Z가-힣]+", value.casefold()):
        token = raw_token
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                token = token[: -len(suffix)]
                break
        if token:
            tokens.add(token)
    return tokens


def _fact_coverage(answer: str, expected_facts: list[str]) -> float | None:
    if not expected_facts:
        return None
    answer_tokens = _fact_tokens(answer)
    scores: list[float] = []
    for fact in expected_facts:
        expected_tokens = _fact_tokens(fact)
        scores.append(len(answer_tokens.intersection(expected_tokens)) / len(expected_tokens) if expected_tokens else 0.0)
    return sum(scores) / len(scores)


def _no_answer_correct(case: GoldenCase, answer: str) -> float | None:
    if case.expectations.should_answer:
        return None
    normalized = answer.casefold()
    abstention_markers = (
        "찾지 못",
        "정보가 없",
        "확인할 수 없",
        "검색 결과가 없",
        "문서가 없",
        "어떤 문서",
        "구체적으로",
        "명확하지",
    )
    return float(any(marker in normalized for marker in abstention_markers))


def _case_result(case: GoldenCase, response: ChatResponse) -> EndToEndCaseEvaluationResult:
    actual_tools = [tool_call.tool_id for tool_call in response.tool_calls]
    retrieved = build_retrieved_chunks(_tool_hits(response))
    retrieval_scores = score_retrieval_case(case, retrieved, actual_tool_calls=actual_tools)
    expected_documents = case.expectations.expected_document_keys
    usage = response.token_usage
    status = "error" if response.error_code else "ok"
    return EndToEndCaseEvaluationResult(
        case_id=case.case_id,
        status=status,
        question=case.inputs.question,
        answer=response.assistant_message,
        expected_tool_calls=[call.name for call in case.expectations.expected_tool_calls],
        actual_tool_calls=actual_tools,
        retrieved=retrieved,
        hit_at_k=retrieval_scores["hit_at_k"],
        recall_at_k=retrieval_scores["recall_at_k"],
        reciprocal_rank=retrieval_scores["reciprocal_rank"],
        tool_exact_match=float(retrieval_scores["tool_exact_match"] or 0.0),
        no_answer_correct=_no_answer_correct(case, response.assistant_message),
        fact_coverage=_fact_coverage(response.assistant_message, case.expectations.expected_facts),
        citation_match=_text_fraction(response.assistant_message, expected_documents),
        agent_steps=len(response.agent_steps),
        llm_calls=usage.calls if usage is not None else 0,
        prompt_tokens=usage.prompt_tokens if usage is not None else 0,
        completion_tokens=usage.completion_tokens if usage is not None else 0,
        total_tokens=usage.total_tokens if usage is not None else 0,
        elapsed_ms=response.elapsed_ms,
        over_budget=response.over_budget,
        error_code=response.error_code,
        error_message=response.assistant_message if response.error_code else "",
    )


def run_end_to_end_evaluation(
    dataset: GoldenDataset,
    agent: PlaygroundAgent,
    registry: dict[str, ToolHandler],
    *,
    corpus: CorpusSnapshot,
    provider: str,
    model: str,
    skill_id: str = "rag-grounded-answer",
    skill_fingerprint: str = "",
    openai_api_key: str = "",
    trace_observer: TraceObserver | None = None,
) -> EndToEndEvaluationReport:
    """각 golden case를 실제 ChatRequest로 실행하고 응답·검색·비용 지표를 계산한다."""

    observer = trace_observer or NoopTraceObserver()
    results: list[EndToEndCaseEvaluationResult] = []
    for case in dataset.cases:
        expected_tools = [call.name for call in case.expectations.expected_tool_calls]
        selected_skills = [skill_id] if "search_rag_chunks" in expected_tools and skill_id else []
        request_id = f"eval-{case.case_id}-{uuid.uuid4().hex[:8]}"
        root_inputs: dict[str, Any] = {
            "case_id": case.case_id,
            "question_length": len(case.inputs.question),
            "expected_tools": expected_tools,
        }
        if observer.include_content:
            root_inputs["question"] = case.inputs.question

        with observer.span(
            name="document_chatbot.evaluate_case",
            span_type="WORKFLOW",
            inputs=root_inputs,
            attributes={
                "evaluation.case_id": case.case_id,
                "evaluation.dataset": dataset.name,
                "evaluation.skill_fingerprint": skill_fingerprint,
                "evaluation.corpus_fingerprint": corpus.fingerprint,
            },
        ) as root_span:
            agent_inputs: dict[str, Any] = {
                "provider": provider,
                "model": model,
                "selected_tools": expected_tools,
                "selected_skills": selected_skills,
                "request_id": request_id,
            }
            if observer.include_content:
                agent_inputs["message"] = case.inputs.question
            with observer.span(
                name="playground.agent",
                span_type="AGENT",
                inputs=agent_inputs,
                attributes={
                    "agent.provider": provider,
                    "agent.model": model,
                    "agent.request_id": request_id,
                },
            ) as agent_span:
                response = agent.run(
                    ChatRequest(
                        message=case.inputs.question,
                        selected_tool_ids=expected_tools,
                        selected_skill_ids=selected_skills,
                        session_id=f"eval-{case.case_id}",
                        provider=provider,  # type: ignore[arg-type]
                        model=model,
                        debug_trace=True,
                    ),
                    registry,
                    openai_api_key=openai_api_key,
                    request_id=request_id,
                )
                agent_outputs: dict[str, Any] = {
                    "status": "error" if response.error_code else "ok",
                    "error_code": response.error_code,
                    "answer_length": len(response.assistant_message),
                    "tool_calls": [tool.tool_id for tool in response.tool_calls],
                    "elapsed_ms": response.elapsed_ms,
                }
                if observer.include_content:
                    agent_outputs["answer"] = response.assistant_message
                agent_span.set_outputs(agent_outputs)
                agent_span.set_attributes({"agent.elapsed_ms": response.elapsed_ms})

            case_result = _case_result(case, response)
            results.append(case_result)
            root_span.set_outputs(
                {
                    "status": case_result.status,
                    "error_code": case_result.error_code,
                    "tool_exact_match": case_result.tool_exact_match,
                    "hit_at_k": case_result.hit_at_k,
                    "elapsed_ms": case_result.elapsed_ms,
                }
            )

    trace_errors = list(getattr(observer, "errors", []))
    return EndToEndEvaluationReport(
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_fingerprint=dataset.fingerprint,
        corpus=corpus,
        provider=provider,
        model=model,
        skill_id=skill_id,
        skill_fingerprint=skill_fingerprint,
        cases=results,
        aggregate=aggregate_end_to_end_results(results),
        trace_errors=trace_errors,
    )
