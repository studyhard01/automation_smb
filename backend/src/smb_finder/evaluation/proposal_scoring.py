"""기안 생성 결과를 100점으로 재현 가능하게 채점하는 결정론 규칙."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from statistics import mean

from .proposal_models import (
    ProposalCaseEvaluation,
    ProposalContextCaseStats,
    ProposalContextFailureAggregate,
    ProposalDatasetArtifact,
    ProposalDatasetCaseInput,
    ProposalEvaluationAggregate,
    ProposalHardGates,
    ProposalPrediction,
    ProposalPredictionTimings,
    ProposalScoreBreakdown,
    ProposalStructureDiagnostics,
    ProposalToolFlowStatus,
    ProposalXlsxStatus,
)


PASS_THRESHOLD = 80.0
HARD_GATE_SCORE_CAP = 49.0
_STAGE_ORDER = ("evidence", "llm", "workbook", "save")
_SUCCESS_STAGE_STATUSES = {"ok", "simulated"}


def _normalize_text(value: str) -> str:
    """유니코드·공백 차이만 제거하고 실제 문구 차이는 유지한다."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _similarity(expected: str, actual: str) -> float:
    expected_value = _normalize_text(expected)
    actual_value = _normalize_text(actual)
    if not expected_value or not actual_value:
        return 0.0
    return SequenceMatcher(a=expected_value, b=actual_value, autojunk=False).ratio()


def _round_score(value: float) -> float:
    return round(max(0.0, value), 4)


def _expected_structure(case: ProposalDatasetCaseInput) -> tuple[list[str], list[str], set[str], int, int]:
    if case.expected is None:
        return [], [], set(), 0, 0
    roles = ["other" if section.semantic_role == "preamble" else section.semantic_role for section in case.expected.body_sections]
    headings = [
        _normalize_text(section.heading or (section.lines[0] if section.lines else ""))
        for section in case.expected.body_sections
    ]
    headings = [heading for heading in headings if heading]
    heading_values = {
        _normalize_text(section.heading or (section.lines[0] if section.lines else ""))
        for section in case.expected.body_sections
    }
    table_rows = sum(len(row.cells) > 1 for row in case.expected.body_rows)
    list_items = sum(
        bool(re.match(r"^\s*(?:\d+[.)]|[-•])\s+", line)) and _normalize_text(line) not in heading_values
        for line in case.expected.body_lines
    )
    block_types: set[str] = set()
    if table_rows:
        block_types.add("table")
    if list_items:
        block_types.add("list")
    if any(
        len(row.cells) <= 1
        and (
            not re.match(r"^\s*(?:\d+[.)]|[-•])\s+", row.text)
            or _normalize_text(row.text) in heading_values
        )
        for row in case.expected.body_rows
    ):
        block_types.add("paragraph")
    if not block_types and case.expected.body:
        block_types.add("paragraph")
    return roles, headings, block_types, table_rows, list_items


def _ratio(expected: int, actual: int) -> float:
    if expected == actual == 0:
        return 1.0
    if expected == 0 or actual == 0:
        return 0.0
    return min(expected, actual) / max(expected, actual)


def _structure_and_citations(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
) -> tuple[float, float, ProposalStructureDiagnostics]:
    expected_roles, expected_headings, expected_types, expected_table_rows, expected_list_items = _expected_structure(case)
    if prediction is None or prediction.document is None:
        return 0.0, 0.0, ProposalStructureDiagnostics(
            expected_section_count=len(expected_roles),
            predicted_section_count=0,
            expected_table_row_count=expected_table_rows,
            predicted_table_row_count=0,
            expected_list_item_count=expected_list_items,
            predicted_list_item_count=0,
            expected_block_type_count=len(expected_types),
            predicted_block_type_count=0,
            valid_citation_count=0,
            invalid_citation_count=0,
            structure_fidelity_score=0,
        )
    document = prediction.document
    predicted_roles = [section.semantic_role for section in document.sections]
    role_score = SequenceMatcher(a=expected_roles, b=predicted_roles, autojunk=False).ratio() if expected_roles else 1.0
    predicted_heading_text = {_normalize_text(section.heading) for section in document.sections}
    heading_score = (
        sum(heading in predicted_heading_text for heading in expected_headings) / len(expected_headings)
        if expected_headings
        else 1.0
    )
    predicted_types = {block.type for section in document.sections for block in section.blocks}
    type_union = expected_types | predicted_types
    type_score = len(expected_types & predicted_types) / len(type_union) if type_union else 1.0
    predicted_table_rows = sum(
        len(block.rows) + 1
        for section in document.sections
        for block in section.blocks
        if block.type == "table"
    )
    predicted_list_items = sum(
        len(block.items)
        for section in document.sections
        for block in section.blocks
        if block.type == "list"
    )
    section_score = 4.0 * role_score + 3.0 * heading_score
    block_score = (
        3.0 * type_score
        + 2.0 * _ratio(expected_table_rows, predicted_table_rows)
        + 2.0 * _ratio(expected_list_items, predicted_list_items)
    )
    structure_score = min(14.0, section_score + block_score)

    citation_ids = [citation for section in document.sections for citation in section.citations]
    packed_count = prediction.context_usage.packed_citation_count if prediction.context_usage is not None else 0
    syntactically_valid = [
        citation
        for citation in citation_ids
        if re.fullmatch(r"E\d{3}", citation) is not None and 1 <= int(citation[1:]) <= packed_count
    ]
    syntax_ratio = len(syntactically_valid) / len(citation_ids) if citation_ids else 0.0
    valid_reference_chunks = set(prediction.evidence_chunk_ids)
    mapped_valid = [
        citation
        for citation in syntactically_valid
        if prediction.citation_map.get(citation) in valid_reference_chunks
    ]
    grounding_ratio = len(mapped_valid) / len(citation_ids) if citation_ids else 0.0
    citation_score = 3.0 * syntax_ratio + 2.0 * grounding_ratio
    fully_valid = len(mapped_valid)
    diagnostics = ProposalStructureDiagnostics(
        expected_section_count=len(expected_roles),
        predicted_section_count=len(document.sections),
        expected_table_row_count=expected_table_rows,
        predicted_table_row_count=predicted_table_rows,
        expected_list_item_count=expected_list_items,
        predicted_list_item_count=predicted_list_items,
        expected_block_type_count=len(expected_types),
        predicted_block_type_count=len(predicted_types),
        valid_citation_count=fully_valid,
        invalid_citation_count=max(0, len(citation_ids) - fully_valid),
        structure_fidelity_score=_round_score(structure_score),
    )
    return structure_score, citation_score, diagnostics


def score_llm_content(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
) -> tuple[float, ProposalStructureDiagnostics]:
    """3필드 16점과 V2 section·block 14점, citation 5점을 채점한다."""

    structure_score, citation_score, diagnostics = _structure_and_citations(case, prediction)
    if case.expected is None or prediction is None or prediction.status != "ok" or prediction.fields is None:
        return 0.0, diagnostics
    fields = prediction.fields
    score = (
        5.0 * _similarity(case.expected.title, fields.title)
        + 4.0 * _similarity(case.expected.approval_request, fields.approval_request)
        + 7.0 * _similarity(case.expected.body, fields.body)
        + structure_score
        + citation_score
    )
    return _round_score(min(score, 35.0)), diagnostics


def score_evidence_context(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
    artifacts: Mapping[str, ProposalDatasetArtifact],
    context: ProposalContextCaseStats,
    *,
    effective_input_budget_tokens: int,
) -> tuple[float, bool, bool]:
    """reference recall 10·chunk 정합 5·누출 방지 5·context 예산 5점을 계산한다."""

    if prediction is None:
        return 0.0, True, True
    expected_artifacts = set(case.reference_artifact_ids)
    actual_artifacts = set(prediction.evidence_artifact_ids)
    artifact_recall = len(expected_artifacts & actual_artifacts) / len(expected_artifacts) if expected_artifacts else 0.0

    valid_chunk_ids = {
        chunk.chunk_id
        for artifact_id in expected_artifacts
        if (artifact := artifacts.get(artifact_id)) is not None
        for chunk in artifact.chunks
    }
    actual_chunks = set(prediction.evidence_chunk_ids)
    chunk_precision = len(valid_chunk_ids & actual_chunks) / len(actual_chunks) if actual_chunks else 0.0

    forbidden_artifacts = {case.target_artifact_id, *case.excluded_post_event_artifact_ids}
    forbidden_chunks = {
        chunk.chunk_id
        for artifact_id in forbidden_artifacts
        if (artifact := artifacts.get(artifact_id)) is not None
        for chunk in artifact.chunks
    }
    leakage_free = not bool((actual_artifacts & forbidden_artifacts) or (actual_chunks & forbidden_chunks))

    if prediction.context_usage is not None:
        observed_tokens = prediction.context_usage.estimated_input_tokens
    elif prediction.context is not None and prediction.context.estimated_tokens is not None:
        observed_tokens = prediction.context.estimated_tokens
    else:
        observed_tokens = context.packed_estimated_tokens
    within_budget = observed_tokens <= effective_input_budget_tokens
    score = 10.0 * artifact_recall + 5.0 * chunk_precision + 5.0 * float(leakage_free) + 5.0 * float(within_budget)
    return _round_score(min(score, 25.0)), leakage_free, within_budget


def score_xlsx_result(status: ProposalXlsxStatus) -> float:
    """생성 3·ZIP 5·필수 OOXML 4·세 출력 필드 4/4/5점으로 채점한다."""

    return _round_score(
        3.0 * float(status.generated)
        + 5.0 * float(status.zip_valid)
        + 4.0 * float(status.required_parts_present)
        + 4.0 * float(status.title_written)
        + 4.0 * float(status.approval_written)
        + 5.0 * float(status.body_written)
    )


def assess_tool_flow(prediction: ProposalPrediction | None) -> tuple[ProposalToolFlowStatus, float, bool]:
    """네 stage 성공 각 3점과 순서 3점을 계산한다."""

    if prediction is None:
        return ProposalToolFlowStatus(), 0.0, False
    statuses: dict[str, str] = {}
    observed_order: list[str] = []
    duplicate = False
    for event in prediction.tool_events:
        if event.stage in statuses:
            duplicate = True
        else:
            observed_order.append(event.stage)
        statuses[event.stage] = event.status
    ordered = not duplicate and observed_order == list(_STAGE_ORDER)
    score = sum(3.0 for stage in _STAGE_ORDER if statuses.get(stage) in _SUCCESS_STAGE_STATUSES)
    score += 3.0 * float(ordered)
    succeeded = ordered and all(statuses.get(stage) in _SUCCESS_STAGE_STATUSES for stage in _STAGE_ORDER)
    flow = ProposalToolFlowStatus(
        evidence=statuses.get("evidence", "missing"),
        llm=statuses.get("llm", "missing"),
        workbook=statuses.get("workbook", "missing"),
        save=statuses.get("save", "missing"),
        ordered=ordered,
        stage_count=len(prediction.tool_events),
    )
    return flow, _round_score(min(score, 15.0)), succeeded


def _zero_scores() -> ProposalScoreBreakdown:
    return ProposalScoreBreakdown(
        evidence_context=0,
        llm_content=0,
        xlsx_result=0,
        tool_flow=0,
        raw_total=0,
        final_total=0,
    )


def build_skipped_case(
    case: ProposalDatasetCaseInput,
    context: ProposalContextCaseStats,
) -> ProposalCaseEvaluation:
    """참고 문서가 없으면 모델 호출·채점 없이 skip한다."""

    gates = ProposalHardGates(
        references_available=False,
        prediction_succeeded=False,
        output_schema_valid=False,
        structured_document_valid=False,
        evidence_leakage_free=True,
        context_within_runtime_budget=True,
        xlsx_valid=False,
        required_tool_stages_succeeded=False,
        passed=False,
    )
    return ProposalCaseEvaluation(
        case_id=case.case_id,
        group_id=case.group_id,
        status="skipped_reference_missing",
        scope=case.evaluation_scope,
        scores=_zero_scores(),
        hard_gates=gates,
        context=context,
        observed_context_estimated_tokens=None,
        structure=_structure_and_citations(case, None)[2],
        xlsx=ProposalXlsxStatus(),
        tool_flow=ProposalToolFlowStatus(),
        timings_ms=ProposalPredictionTimings(),
        failure_code="reference_missing",
    )


def score_proposal_case(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
    prediction_sha256: str | None,
    artifacts: Mapping[str, ProposalDatasetArtifact],
    context: ProposalContextCaseStats,
    xlsx: ProposalXlsxStatus,
    *,
    effective_input_budget_tokens: int,
    pass_threshold: float = PASS_THRESHOLD,
    hard_gate_score_cap: float = HARD_GATE_SCORE_CAP,
) -> ProposalCaseEvaluation:
    """case 하나를 채점하고 hard gate 실패 시 최종 점수를 cap한다."""

    if context.status == "skipped_reference_missing":
        return build_skipped_case(case, context)

    evidence_score, leakage_free, within_budget = score_evidence_context(
        case,
        prediction,
        artifacts,
        context,
        effective_input_budget_tokens=effective_input_budget_tokens,
    )
    llm_score, structure = score_llm_content(case, prediction)
    xlsx_score = score_xlsx_result(xlsx)
    tool_flow, tool_score, tool_succeeded = assess_tool_flow(prediction)
    prediction_succeeded = prediction is not None and prediction.status == "ok"
    output_schema_valid = prediction_succeeded and prediction.fields is not None
    structured_document_valid = prediction_succeeded and prediction.document is not None
    xlsx_valid = all(
        (
            xlsx.generated,
            xlsx.zip_valid,
            xlsx.required_parts_present,
            xlsx.title_written,
            xlsx.approval_written,
            xlsx.body_written,
        )
    )
    hard_gate_passed = all(
        (
            prediction_succeeded,
            output_schema_valid,
            structured_document_valid,
            leakage_free,
            within_budget,
            xlsx_valid,
            tool_succeeded,
        )
    )
    raw_total = _round_score(evidence_score + llm_score + xlsx_score + tool_score)
    final_total = raw_total if hard_gate_passed else min(raw_total, hard_gate_score_cap)
    passed = hard_gate_passed and final_total >= pass_threshold
    gates = ProposalHardGates(
        references_available=True,
        prediction_succeeded=prediction_succeeded,
        output_schema_valid=output_schema_valid,
        structured_document_valid=structured_document_valid,
        evidence_leakage_free=leakage_free,
        context_within_runtime_budget=within_budget,
        xlsx_valid=xlsx_valid,
        required_tool_stages_succeeded=tool_succeeded,
        passed=hard_gate_passed,
    )
    status = "passed" if passed else ("missing_prediction" if prediction is None else "failed")
    failure_code = "prediction_missing" if prediction is None else prediction.failure_code
    return ProposalCaseEvaluation(
        case_id=case.case_id,
        group_id=case.group_id,
        status=status,
        scope=case.evaluation_scope,
        prediction_sha256=prediction_sha256,
        scores=ProposalScoreBreakdown(
            evidence_context=evidence_score,
            llm_content=llm_score,
            xlsx_result=xlsx_score,
            tool_flow=tool_score,
            raw_total=raw_total,
            final_total=_round_score(final_total),
        ),
        hard_gates=gates,
        context=context,
        observed_context_estimated_tokens=(
            prediction.context_usage.estimated_input_tokens
            if prediction is not None and prediction.context_usage is not None
            else prediction.context.estimated_tokens
            if prediction is not None and prediction.context is not None
            else None
        ),
        structure=structure,
        xlsx=xlsx,
        tool_flow=tool_flow,
        timings_ms=prediction.timings_ms if prediction is not None else ProposalPredictionTimings(),
        failure_code=failure_code,
    )


def _percentile(values: Iterable[float], percentile: float) -> float:
    numbers = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not numbers:
        return 0.0
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    return numbers[lower] + (numbers[upper] - numbers[lower]) * (position - lower)


def aggregate_proposal_results(results: list[ProposalCaseEvaluation]) -> ProposalEvaluationAggregate:
    """skip을 분모에서 제외하고 점수·hard gate·context 실패를 집계한다."""

    evaluated = [result for result in results if result.status != "skipped_reference_missing"]
    passed = [result for result in evaluated if result.status == "passed"]
    failed = [result for result in evaluated if result.status != "passed"]
    context_failed = [result for result in evaluated if result.failure_code == "proposal_context_limit_exceeded"]
    failure_tokens = [
        result.observed_context_estimated_tokens
        if result.observed_context_estimated_tokens is not None
        else result.context.packed_estimated_tokens
        for result in context_failed
    ]
    gates = (
        "references_available",
        "prediction_succeeded",
        "output_schema_valid",
        "structured_document_valid",
        "evidence_leakage_free",
        "context_within_runtime_budget",
        "xlsx_valid",
        "required_tool_stages_succeeded",
    )
    gate_failures = {
        gate: sum(not bool(getattr(result.hard_gates, gate)) for result in evaluated)
        for gate in gates
    }

    def average(attribute: str) -> float:
        return mean(float(getattr(result.scores, attribute)) for result in evaluated) if evaluated else 0.0

    return ProposalEvaluationAggregate(
        total_case_count=len(results),
        evaluated_case_count=len(evaluated),
        skipped_case_count=len(results) - len(evaluated),
        passed_case_count=len(passed),
        failed_case_count=len(failed),
        pass_rate=len(passed) / len(evaluated) if evaluated else 0.0,
        average_total_score=_round_score(average("final_total")),
        evidence_context_average=_round_score(average("evidence_context")),
        llm_content_average=_round_score(average("llm_content")),
        xlsx_result_average=_round_score(average("xlsx_result")),
        tool_flow_average=_round_score(average("tool_flow")),
        latency_p50_ms=_round_score(_percentile((item.timings_ms.total_ms for item in evaluated), 0.50)),
        latency_p95_ms=_round_score(_percentile((item.timings_ms.total_ms for item in evaluated), 0.95)),
        hard_gate_failure_counts=gate_failures,
        context_failures=ProposalContextFailureAggregate(
            failure_count=len(context_failed),
            case_ids=sorted(item.case_id for item in context_failed),
            group_ids=sorted({item.group_id for item in context_failed}),
            packed_estimated_tokens_min=min(failure_tokens, default=0),
            packed_estimated_tokens_max=max(failure_tokens, default=0),
            packed_estimated_tokens_p50=int(round(_percentile(failure_tokens, 0.50))),
        ),
    )
