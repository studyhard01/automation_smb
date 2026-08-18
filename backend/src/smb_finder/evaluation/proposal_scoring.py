"""기안 생성 결과를 결정론 계약 55점과 온프레미스 LLM judge 45점으로 채점한다."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from statistics import mean

from .proposal_models import (
    ProposalCaseEvaluation,
    ProposalCaseCoverage,
    ProposalContentJudgeAggregate,
    ProposalContentJudgeDiagnostics,
    ProposalContextCaseStats,
    ProposalContextFailureAggregate,
    ProposalDatasetArtifact,
    ProposalDatasetCaseInput,
    ProposalEvaluationAggregate,
    ProposalHardGates,
    ProposalEvidenceFilterAggregate,
    ProposalEvidenceFilterDiagnostics,
    ProposalEventAttendanceDiagnostics,
    ProposalFactualDiagnostics,
    ProposalPrediction,
    ProposalPredictionTimings,
    ProposalProfileDiagnostics,
    ProposalRevisionDiagnostics,
    ProposalScoreBreakdown,
    ProposalStructureDiagnostics,
    ProposalToolFlowStatus,
    ProposalSupplementalAggregate,
    ProposalCoverageAggregate,
    ProposalXlsxStatus,
)


PASS_THRESHOLD = 80.0
HARD_GATE_SCORE_CAP = 49.0
_STAGE_ORDER = ("evidence", "llm", "workbook", "save")
_SUCCESS_STAGE_STATUSES = {"ok", "simulated"}
_PROFILE_SECTION_ORDER = {
    "purchase": ("purpose", "background", "request", "details", "budget", "schedule", "expected_effect", "attachments"),
    "event_attendance": ("purpose", "background", "details", "schedule", "budget", "expected_effect", "attachments"),
    "general": ("purpose", "background", "request", "details", "schedule", "expected_effect", "attachments", "notes", "other"),
}
_EVENT_HEADINGS = ("참가 목적", "참가 내용", "행사 주요 내용")
_EVENT_FORBIDDEN_HEADINGS = {"행사 개요", "전체 일정"}


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


def _case_coverage(case: ProposalDatasetCaseInput) -> ProposalCaseCoverage:
    # 구 line 전체 일치 label은 진단 호환용일 뿐 실제 기안 품질의 제품 점수로 승격하지 않는다.
    eligible = case.review_status == "reviewed" and case.split == "test" and bool(case.fact_expectations)
    return ProposalCaseCoverage(
        review_status=case.review_status,
        split=case.split,
        expected_label_status="labeled" if case.expected is not None else "unlabeled",
        factual_label_status=(
            "labeled"
            if case.fact_expectations or case.required_fact_labels or case.unsupported_addition_labels
            else "unlabeled"
        ),
        scoring_basis="reviewed_test" if eligible else "diagnostic",
        product_quality_eligible=eligible,
    )


def _prediction_content(prediction: ProposalPrediction | None) -> str:
    if prediction is None or prediction.status != "ok":
        return ""
    values: list[str] = []
    if prediction.fields is not None:
        values.extend((prediction.fields.title, prediction.fields.approval_request, prediction.fields.body))
    if prediction.document is not None:
        values.extend((prediction.document.title, prediction.document.approval_request))
        for section in prediction.document.sections:
            values.append(section.heading)
            for block in section.blocks:
                if block.type == "paragraph":
                    values.append(block.text)
                elif block.type == "list":
                    values.extend(block.items)
                else:
                    values.extend(block.headers)
                    values.extend(cell for row in block.rows for cell in row)
    return _normalize_text("\n".join(values))


def _normalize_fact_value(value: str) -> str:
    """날짜·금액의 구분 기호와 공백 차이를 허용하되 실제 문자·숫자는 보존한다."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _fact_value_present(values: list[str], content: str) -> bool:
    normalized_content = _normalize_fact_value(content)
    return any(
        len(normalized) >= 2 and normalized in normalized_content
        for value in values
        if (normalized := _normalize_fact_value(value))
    )


def _fact_evidence_content(
    case: ProposalDatasetCaseInput,
    source_artifact_ids: list[str],
    artifacts: Mapping[str, ProposalDatasetArtifact],
) -> str:
    source_ids = source_artifact_ids or case.reference_artifact_ids
    return "\n".join(
        [case.instruction]
        + [
            chunk.text
            for artifact_id in source_ids
            if (artifact := artifacts.get(artifact_id)) is not None
            for chunk in artifact.chunks
        ]
    )


def _expected_clarification_keys(
    case: ProposalDatasetCaseInput,
    artifacts: Mapping[str, ProposalDatasetArtifact],
) -> set[str]:
    """검토 label 중 사용자에게 물어야 하고 현재 입력에는 없는 사실 key를 찾는다."""

    result: set[str] = set()
    for expectation in case.fact_expectations:
        if expectation.expectation != "include" or expectation.missing_action != "ask":
            continue
        values = [*expectation.expected_values, *expectation.aliases]
        evidence = _fact_evidence_content(case, expectation.source_artifact_ids, artifacts)
        if not _fact_value_present(values, evidence):
            result.add(expectation.fact_key)
    return result


def assess_factual_labels(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
    artifacts: Mapping[str, ProposalDatasetArtifact],
) -> ProposalFactualDiagnostics:
    """reviewed test의 명시 label만으로 사실 포함·누락·금지 추가를 재현 가능하게 센다."""

    labels = [*case.fact_expectations, *case.required_fact_labels, *case.unsupported_addition_labels]
    if case.review_status != "reviewed" or case.split != "test" or not labels:
        return ProposalFactualDiagnostics()
    if case.fact_expectations:
        actual = _prediction_content(prediction)
        include_expectations = [item for item in case.fact_expectations if item.expectation == "include"]
        exclude_expectations = [item for item in case.fact_expectations if item.expectation == "exclude"]
        supported = 0
        for expectation in include_expectations:
            evidence = _fact_evidence_content(case, expectation.source_artifact_ids, artifacts)
            values = [*expectation.expected_values, *expectation.aliases]
            evidence_present = _fact_value_present(values, evidence)
            question_keys = set(prediction.completion.question_field_keys) if prediction and prediction.completion else set()
            if (_fact_value_present(values, actual) and evidence_present) or (
                not evidence_present
                and expectation.missing_action == "ask"
                and prediction is not None
                and prediction.completion is not None
                and prediction.completion.status == "needs_clarification"
                and expectation.fact_key in question_keys
            ):
                supported += 1
        unsupported_additions = sum(
            _fact_value_present([*expectation.expected_values, *expectation.aliases], actual)
            for expectation in exclude_expectations
        )
        omitted = len(include_expectations) - supported
        possible = float(len(case.fact_expectations))
        scored = float(supported + len(exclude_expectations) - unsupported_additions)
        return ProposalFactualDiagnostics(
            status="scored",
            required_fact_count=len(include_expectations),
            supported_fact_count=supported,
            omitted_fact_count=omitted,
            unsupported_label_count=len(exclude_expectations),
            unsupported_addition_count=unsupported_additions,
            scored_points=scored,
            possible_points=possible,
            normalized_scored_points=_round_score(scored / possible * 100) if possible else None,
        )
    actual = _prediction_content(prediction)
    evidence = _normalize_text(
        "\n".join(
            chunk.text
            for artifact_id in case.reference_artifact_ids
            if (artifact := artifacts.get(artifact_id)) is not None
            for chunk in artifact.chunks
        )
    )
    supported = 0
    omitted = 0
    for label in case.required_fact_labels:
        normalized = _normalize_text(label)
        if normalized and normalized in actual and normalized in evidence:
            supported += 1
        else:
            omitted += 1
    unsupported_additions = sum(
        bool((normalized := _normalize_text(label)) and normalized in actual)
        for label in case.unsupported_addition_labels
    )
    possible = float(len(labels))
    scored = float(supported + len(case.unsupported_addition_labels) - unsupported_additions)
    return ProposalFactualDiagnostics(
        status="scored",
        required_fact_count=len(case.required_fact_labels),
        supported_fact_count=supported,
        omitted_fact_count=omitted,
        unsupported_label_count=len(case.unsupported_addition_labels),
        unsupported_addition_count=unsupported_additions,
        scored_points=scored,
        possible_points=possible,
        normalized_scored_points=_round_score(scored / possible * 100) if possible else None,
    )


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


def _profile_score(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
) -> tuple[float, ProposalProfileDiagnostics]:
    label = case.proposal_type_label
    if label is None:
        return 2.0, ProposalProfileDiagnostics()
    expected_order = tuple(case.expected_section_order) or _PROFILE_SECTION_ORDER[label]
    if prediction is None or prediction.document is None:
        return 0.0, ProposalProfileDiagnostics(
            label_status="labeled",
            selection_status="not_exposed",
            resolution_status="not_exposed",
            source_status="not_exposed",
            section_order_status="matched",
            expected_order_role_count=len(expected_order),
            profile_score=0,
        )

    requested = prediction.requested_proposal_type
    if requested is None:
        selection_status = "not_exposed"
        selection_score = 0.0
    elif requested == label:
        selection_status = "matched"
        selection_score = 0.5
    elif requested == "auto":
        selection_status = "auto"
        selection_score = 0.25
    else:
        selection_status = "mismatched"
        selection_score = 0.0

    resolved = prediction.resolved_proposal_type or prediction.document.proposal_type
    if resolved is None:
        resolution_status = "not_exposed"
        resolution_score = 0.0
    elif resolved == label:
        resolution_status = "matched"
        resolution_score = 0.75
    else:
        resolution_status = "mismatched"
        resolution_score = 0.0

    if prediction.proposal_type_source is None:
        source_status = "not_exposed"
        source_score = 0.0
    else:
        source_valid = (
            requested == "auto" and prediction.proposal_type_source in {"rule", "fallback"}
        ) or (requested in {"purchase", "event_attendance", "general"} and prediction.proposal_type_source == "user")
        source_status = "valid" if source_valid else "invalid"
        source_score = 0.25 if source_valid else 0.0

    order_index = {role: index for index, role in enumerate(expected_order)}
    resolved_roles = [
        section.semantic_role
        for section in prediction.document.sections
        if section.semantic_role in order_index
    ]
    violations = sum(
        order_index[left] > order_index[right]
        for index, left in enumerate(resolved_roles)
        for right in resolved_roles[index + 1 :]
    )
    possible = len(resolved_roles) * (len(resolved_roles) - 1) // 2
    order_ratio = 1.0 - violations / possible if possible else 1.0
    order_score = 0.5 * order_ratio
    total = selection_score + resolution_score + source_score + order_score
    return total, ProposalProfileDiagnostics(
        label_status="labeled",
        selection_status=selection_status,
        resolution_status=resolution_status,
        source_status=source_status,
        section_order_status="matched" if violations == 0 else "violated",
        expected_order_role_count=len(expected_order),
        resolved_order_role_count=len(resolved_roles),
        order_violation_count=violations,
        profile_score=_round_score(total),
    )


def _structure_and_citations(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
) -> tuple[float, float, ProposalStructureDiagnostics, ProposalProfileDiagnostics]:
    expected_roles, expected_headings, expected_types, expected_table_rows, expected_list_items = _expected_structure(case)
    profile_score, profile = _profile_score(case, prediction)
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
        ), profile
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
    section_score = (
        4.0 * role_score + 3.0 * heading_score
        if case.proposal_type_label is None
        else 3.0 * role_score + 2.0 * heading_score + profile_score
    )
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
    return structure_score, citation_score, diagnostics, profile


def score_llm_content(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
) -> tuple[float, ProposalStructureDiagnostics, ProposalProfileDiagnostics]:
    """LLM judge와 별개로 기존 정답 문구·구조 진단을 계산한다."""

    structure_score, citation_score, diagnostics, profile = _structure_and_citations(case, prediction)
    if case.expected is None or prediction is None or prediction.status != "ok" or prediction.fields is None:
        return 0.0, diagnostics, profile
    fields = prediction.fields
    score = (
        5.0 * _similarity(case.expected.title, fields.title)
        + 4.0 * _similarity(case.expected.approval_request, fields.approval_request)
        + 7.0 * _similarity(case.expected.body, fields.body)
        + structure_score
        + citation_score
    )
    return _round_score(min(score, 35.0)), diagnostics, profile


def assess_event_attendance(
    prediction: ProposalPrediction | None,
) -> tuple[ProposalEventAttendanceDiagnostics, bool]:
    """행사 참석 문서만 세 compact heading의 정확한 구성과 순서를 검사한다."""

    if prediction is None or prediction.document is None:
        return ProposalEventAttendanceDiagnostics(), True
    resolved_type = prediction.resolved_proposal_type or prediction.document.proposal_type
    if resolved_type != "event_attendance":
        return ProposalEventAttendanceDiagnostics(), True
    headings = [_normalize_text(section.heading) for section in prediction.document.sections]
    expected = [_normalize_text(heading) for heading in _EVENT_HEADINGS]
    forbidden = {_normalize_text(heading) for heading in _EVENT_FORBIDDEN_HEADINGS}
    present_count = sum(heading in headings for heading in expected)
    missing_count = len(expected) - present_count
    forbidden_count = sum(heading in forbidden for heading in headings)
    extra_count = sum(heading not in set(expected) for heading in headings)
    order_valid = headings == expected
    valid = missing_count == 0 and forbidden_count == 0 and extra_count == 0 and order_valid
    return ProposalEventAttendanceDiagnostics(
        status="passed" if valid else "failed",
        expected_heading_count=len(expected),
        present_required_heading_count=present_count,
        missing_required_heading_count=missing_count,
        extra_heading_count=extra_count,
        forbidden_heading_count=forbidden_count,
        order_valid=order_valid,
    ), valid


def _document_sha256(document: object) -> str:
    if hasattr(document, "model_dump"):
        value = document.model_dump(mode="json")
    else:
        value = document
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assess_revision(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
) -> tuple[ProposalRevisionDiagnostics, bool]:
    """선택 revision label과 공개 metadata/hash로 피드백 반영 및 원본 불변성을 검사한다."""

    expectation = case.revision_expectation
    exposed = prediction is not None and any(
        (
            prediction.revision_of_draft_id,
            prediction.revision_number,
            prediction.revision_summary,
            prediction.revision_base_document,
            prediction.revision_base_document_sha256_before,
            prediction.revision_base_document_sha256_after,
            prediction.revision_base_workbook_sha256_before,
            prediction.revision_base_workbook_sha256_after,
        )
    )
    if expectation is None and not exposed:
        return ProposalRevisionDiagnostics(), True
    if prediction is None:
        return ProposalRevisionDiagnostics(status="failed", label_status="labeled"), False

    parsed_revision_minor = int(prediction.revision_number.split(".")[1]) if prediction.revision_number else None
    metadata_valid = bool(
        all((prediction.revision_of_draft_id, prediction.revision_number, prediction.revision_summary))
        and parsed_revision_minor is not None
        and parsed_revision_minor >= 1
    )
    revision_minor = parsed_revision_minor if parsed_revision_minor is not None and parsed_revision_minor >= 1 else None
    summary = prediction.revision_summary or ""
    base = prediction.revision_base_document
    revised = prediction.document
    computed_base_hash = _document_sha256(base) if base is not None else None
    revised_hash = _document_sha256(revised) if revised is not None else None

    base_document_hash_status = "not_exposed"
    if base is not None or prediction.revision_base_document_sha256_before or prediction.revision_base_document_sha256_after:
        base_document_hash_status = (
            "matched"
            if computed_base_hash is not None
            and computed_base_hash == prediction.revision_base_document_sha256_before
            and computed_base_hash == prediction.revision_base_document_sha256_after
            else "mismatched"
        )
    base_workbook_hash_status = "not_exposed"
    if prediction.revision_base_workbook_sha256_before or prediction.revision_base_workbook_sha256_after:
        base_workbook_hash_status = (
            "matched"
            if prediction.revision_base_workbook_sha256_before is not None
            and prediction.revision_base_workbook_sha256_before == prediction.revision_base_workbook_sha256_after
            else "mismatched"
        )

    feedback_status = "not_labeled"
    preservation_status = "not_labeled"
    applied_changed = 0
    preserved = 0
    if expectation is not None:
        feedback_status = (
            "matched" if revised_hash == expectation.expected_revised_document_sha256 else "mismatched"
        )
        base_payload = base.model_dump(mode="json") if base is not None else {}
        revised_payload = revised.model_dump(mode="json") if revised is not None else {}
        applied_changed = sum(
            base_payload.get(field) != revised_payload.get(field) for field in expectation.expected_changed_fields
        )
        preserved = sum(
            base_payload.get(field) == revised_payload.get(field) for field in expectation.expected_preserved_fields
        )
        preservation_status = (
            "matched"
            if applied_changed == len(expectation.expected_changed_fields)
            and preserved == len(expectation.expected_preserved_fields)
            else "mismatched"
        )
        if computed_base_hash != expectation.base_document_sha256:
            base_document_hash_status = "mismatched"

    valid = bool(metadata_valid)
    if expectation is not None:
        valid = valid and all(
            (
                feedback_status == "matched",
                preservation_status == "matched",
                base_document_hash_status == "matched",
                base_workbook_hash_status == "matched",
            )
        )
    else:
        valid = valid and base_document_hash_status != "mismatched" and base_workbook_hash_status != "mismatched"
    return ProposalRevisionDiagnostics(
        status="passed" if valid else "failed",
        label_status="labeled" if expectation is not None else "not_labeled",
        metadata_status="valid" if metadata_valid else "invalid",
        feedback_application_status=feedback_status,
        preservation_status=preservation_status,
        base_document_hash_status=base_document_hash_status,
        base_workbook_hash_status=base_workbook_hash_status,
        expected_changed_field_count=len(expectation.expected_changed_fields) if expectation is not None else 0,
        applied_changed_field_count=applied_changed,
        expected_preserved_field_count=len(expectation.expected_preserved_fields) if expectation is not None else 0,
        preserved_field_count=preserved,
        revision_minor=revision_minor,
        revision_summary_length=len(summary),
        revision_summary_sha256=hashlib.sha256(summary.encode("utf-8")).hexdigest() if summary else None,
    ), valid


def score_evidence_context(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction | None,
    artifacts: Mapping[str, ProposalDatasetArtifact],
    context: ProposalContextCaseStats,
    *,
    effective_input_budget_tokens: int,
) -> tuple[float, bool, bool, ProposalEvidenceFilterDiagnostics, bool]:
    """reference recall 10·chunk 정합 5·누출 방지 5·context 예산 5점을 계산한다."""

    if prediction is None:
        return 0.0, True, True, ProposalEvidenceFilterDiagnostics(), True
    if prediction.failure_code == "target_candidate_leakage":
        return (
            0.0,
            False,
            True,
            ProposalEvidenceFilterDiagnostics(status="failed", summary_status="not_exposed"),
            False,
        )
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
    citation_chunks = set(prediction.citation_map.values())
    leakage_count = len(actual_artifacts & forbidden_artifacts) + len((actual_chunks | citation_chunks) & forbidden_chunks)
    leakage_free = leakage_count == 0

    filter_exposed = prediction.evidence_filter is not None or bool(
        prediction.excluded_evidence_artifact_ids or prediction.excluded_evidence_chunk_ids
    )
    filter_diagnostics = ProposalEvidenceFilterDiagnostics()
    filter_valid = True
    excluded_recall = 1.0
    false_exclusion_count = 0
    if filter_exposed:
        expected_excluded = set(case.excluded_post_event_artifact_ids)
        reported_excluded = set(prediction.excluded_evidence_artifact_ids)
        correctly_excluded = expected_excluded & reported_excluded
        false_exclusions = expected_artifacts & reported_excluded
        false_exclusion_count = len(false_exclusions)
        excluded_recall = len(correctly_excluded) / len(expected_excluded) if expected_excluded else 1.0
        summary = prediction.evidence_filter
        summary_valid = summary is not None and all(
            (
                summary.input_document_count == summary.included_document_count + summary.excluded_document_count,
                summary.input_citation_count == summary.included_citation_count + summary.excluded_citation_count,
                sum(summary.excluded_reason_counts.values()) == summary.excluded_document_count,
                summary.included_document_count == len(actual_artifacts),
                summary.excluded_document_count == len(reported_excluded),
                summary.included_citation_count == len(actual_chunks),
                summary.excluded_citation_count == len(set(prediction.excluded_evidence_chunk_ids)),
            )
        )
        excluded_leakage_count = len(actual_artifacts & expected_excluded) + len(
            (actual_chunks | citation_chunks)
            & {
                chunk.chunk_id
                for artifact_id in expected_excluded
                if (artifact := artifacts.get(artifact_id)) is not None
                for chunk in artifact.chunks
            }
        )
        filter_valid = bool(
            summary_valid
            and excluded_recall == 1.0
            and false_exclusion_count == 0
            and excluded_leakage_count == 0
        )
        filter_diagnostics = ProposalEvidenceFilterDiagnostics(
            status="passed" if filter_valid else "failed",
            summary_status="valid" if summary_valid else "invalid",
            expected_excluded_artifact_count=len(expected_excluded),
            reported_excluded_artifact_count=len(reported_excluded),
            correctly_excluded_artifact_count=len(correctly_excluded),
            false_exclusion_count=false_exclusion_count,
            excluded_artifact_recall=excluded_recall,
            excluded_citation_context_leakage_count=excluded_leakage_count,
            fallback_used=summary.fallback_used if summary is not None else False,
        )

    if prediction.context_usage is not None:
        observed_tokens = prediction.context_usage.estimated_input_tokens
    elif prediction.context is not None and prediction.context.estimated_tokens is not None:
        observed_tokens = prediction.context.estimated_tokens
    else:
        observed_tokens = context.packed_estimated_tokens
    within_budget = observed_tokens <= effective_input_budget_tokens
    if filter_exposed:
        score = (
            8.0 * artifact_recall
            + 4.0 * chunk_precision
            + 3.0 * excluded_recall
            + 2.0 * float(false_exclusion_count == 0)
            + 3.0 * float(leakage_free)
            + 5.0 * float(within_budget)
        )
    else:
        score = 10.0 * artifact_recall + 5.0 * chunk_precision + 5.0 * float(leakage_free) + 5.0 * float(within_budget)
    return _round_score(min(score, 25.0)), leakage_free, within_budget, filter_diagnostics, filter_valid


def score_xlsx_result(status: ProposalXlsxStatus) -> float:
    """XLSX 무결성·편집성·레이아웃을 20점으로 채점한다."""

    if not status.generated:
        return 0.0
    if status.contract_version == "proposal-xlsx-v2":
        return _round_score(
            1.5 * float(status.generated)
            + 2.5 * float(status.zip_valid)
            + 1.5 * float(status.required_parts_present)
            + 1.5 * float(status.title_written)
            + 1.5 * float(status.approval_written)
            + 1.5 * float(status.body_written)
            + 1.0 * float(status.omitted_content_block_count == 0)
            + 1.5 * float(status.body_text_single_line_valid)
            + 1.5 * float(status.body_editable_unmerged_valid)
            + 1.5 * float(status.body_default_height_valid)
            + 0.75 * float(status.table_geometry_valid)
            + 0.75 * float(status.table_border_valid)
            + 0.75 * float(status.table_wrap_valid)
            + 0.75 * float(status.table_explicit_row_height_valid)
            + 0.75 * float(status.fake_pipe_table_absent)
            + 0.75 * float(status.appendix_status in {"not_required", "valid"})
        )
    return _round_score(
        2.5 * float(status.generated)
        + 4.0 * float(status.zip_valid)
        + 3.0 * float(status.required_parts_present)
        + 3.0 * float(status.title_written)
        + 3.0 * float(status.approval_written)
        + 4.5 * float(status.body_written)
    )


def assess_tool_flow(prediction: ProposalPrediction | None) -> tuple[ProposalToolFlowStatus, float, bool]:
    """네 stage 성공 각 2점과 순서 2점을 계산한다."""

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
    score = sum(2.0 for stage in _STAGE_ORDER if statuses.get(stage) in _SUCCESS_STAGE_STATUSES)
    score += 2.0 * float(ordered)
    succeeded = ordered and all(statuses.get(stage) in _SUCCESS_STAGE_STATUSES for stage in _STAGE_ORDER)
    flow = ProposalToolFlowStatus(
        evidence=statuses.get("evidence", "missing"),
        llm=statuses.get("llm", "missing"),
        workbook=statuses.get("workbook", "missing"),
        save=statuses.get("save", "missing"),
        ordered=ordered,
        stage_count=len(prediction.tool_events),
    )
    return flow, _round_score(min(score, 10.0)), succeeded


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
        evidence_filter_valid=True,
        event_attendance_structure_valid=True,
        revision_valid=True,
        context_within_runtime_budget=True,
        xlsx_valid=False,
        xlsx_render_contract_valid=False,
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
        profile=_structure_and_citations(case, None)[3],
        evidence_filter=ProposalEvidenceFilterDiagnostics(),
        event_attendance=ProposalEventAttendanceDiagnostics(),
        revision=ProposalRevisionDiagnostics(),
        xlsx=ProposalXlsxStatus(),
        tool_flow=ProposalToolFlowStatus(),
        timings_ms=ProposalPredictionTimings(),
        coverage=_case_coverage(case),
        factual=ProposalFactualDiagnostics(),
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

    evidence_score, leakage_free, within_budget, evidence_filter, evidence_filter_valid = score_evidence_context(
        case,
        prediction,
        artifacts,
        context,
        effective_input_budget_tokens=effective_input_budget_tokens,
    )
    _, structure, profile = score_llm_content(case, prediction)
    content_judge = (
        prediction.content_judge
        if prediction is not None and prediction.content_judge is not None
        else ProposalContentJudgeDiagnostics()
    )
    llm_score = _round_score(content_judge.total_score)
    event_attendance, event_attendance_valid = assess_event_attendance(prediction)
    revision, revision_valid = assess_revision(case, prediction)
    factual = assess_factual_labels(case, prediction, artifacts)
    xlsx_score = score_xlsx_result(xlsx)
    tool_flow, tool_score, tool_succeeded = assess_tool_flow(prediction)
    expected_question_keys = _expected_clarification_keys(case, artifacts)
    actual_question_keys = (
        set(prediction.completion.question_field_keys)
        if prediction is not None
        and prediction.completion is not None
        and prediction.completion.status == "needs_clarification"
        else set()
    )
    clarification_valid = bool(expected_question_keys) and actual_question_keys == expected_question_keys
    if clarification_valid:
        # 필수 답변 전 파일 생성·저장을 막은 것이 올바른 도구 동작이므로 두 후속 stage의 만점을 대체한다.
        xlsx_score = 20.0
        tool_score = 10.0
        tool_succeeded = True
        event_attendance_valid = True
    prediction_succeeded = prediction is not None and prediction.status == "ok"
    output_schema_valid = prediction_succeeded and prediction.fields is not None
    structured_document_valid = prediction_succeeded and prediction.document is not None
    xlsx_valid = clarification_valid or all(
        (
            xlsx.generated,
            xlsx.zip_valid,
            xlsx.required_parts_present,
            xlsx.title_written,
            xlsx.approval_written,
            xlsx.body_written,
        )
    )
    xlsx_render_contract_valid = clarification_valid or xlsx.layout_status in {"not_applicable", "passed"}
    hard_gate_passed = all(
        (
            prediction_succeeded,
            output_schema_valid,
            structured_document_valid,
            leakage_free,
            evidence_filter_valid,
            event_attendance_valid,
            revision_valid,
            within_budget,
            xlsx_valid,
            xlsx_render_contract_valid,
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
        evidence_filter_valid=evidence_filter_valid,
        event_attendance_structure_valid=event_attendance_valid,
        revision_valid=revision_valid,
        context_within_runtime_budget=within_budget,
        xlsx_valid=xlsx_valid,
        xlsx_render_contract_valid=xlsx_render_contract_valid,
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
        content_judge=content_judge,
        structure=structure,
        profile=profile,
        evidence_filter=evidence_filter,
        event_attendance=event_attendance,
        revision=revision,
        xlsx=xlsx,
        tool_flow=tool_flow,
        timings_ms=prediction.timings_ms if prediction is not None else ProposalPredictionTimings(),
        coverage=_case_coverage(case),
        factual=factual,
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
        "evidence_filter_valid",
        "event_attendance_structure_valid",
        "revision_valid",
        "context_within_runtime_budget",
        "xlsx_valid",
        "xlsx_render_contract_valid",
        "required_tool_stages_succeeded",
    )
    gate_failures = {
        gate: sum(not bool(getattr(result.hard_gates, gate)) for result in evaluated)
        for gate in gates
    }
    filter_evaluated = [item for item in evaluated if item.evidence_filter.status != "not_exposed"]
    filter_passed = [item for item in filter_evaluated if item.evidence_filter.status == "passed"]
    event_evaluated = [item for item in evaluated if item.event_attendance.status != "not_applicable"]
    revision_evaluated = [item for item in evaluated if item.revision.status != "not_applicable"]
    expected_excluded = sum(item.evidence_filter.expected_excluded_artifact_count for item in filter_evaluated)
    correctly_excluded = sum(item.evidence_filter.correctly_excluded_artifact_count for item in filter_evaluated)
    factual_scored = [item for item in evaluated if item.factual.status == "scored"]
    generated_completed = [
        item
        for item in evaluated
        if all(
            getattr(item.tool_flow, stage) == "ok"
            for stage in ("evidence", "llm", "workbook", "save")
        )
    ]
    factual_points = sum(item.factual.scored_points for item in factual_scored)
    factual_possible = sum(item.factual.possible_points for item in factual_scored)
    completed_judges = [
        item.content_judge for item in evaluated if item.content_judge.status in {"completed", "oracle"}
    ]

    def average(attribute: str) -> float:
        return mean(float(getattr(result.scores, attribute)) for result in evaluated) if evaluated else 0.0

    def judge_average(attribute: str) -> float:
        values = [float(getattr(item.scores, attribute)) for item in completed_judges if item.scores is not None]
        return mean(values) if values else 0.0

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
        generated_completed_case_count=len(generated_completed),
        generated_completed_latency_p50_ms=_round_score(
            _percentile((item.timings_ms.total_ms for item in generated_completed), 0.50)
        ),
        generated_completed_latency_p95_ms=_round_score(
            _percentile((item.timings_ms.total_ms for item in generated_completed), 0.95)
        ),
        hard_gate_failure_counts=gate_failures,
        context_failures=ProposalContextFailureAggregate(
            failure_count=len(context_failed),
            case_ids=sorted(item.case_id for item in context_failed),
            group_ids=sorted({item.group_id for item in context_failed}),
            packed_estimated_tokens_min=min(failure_tokens, default=0),
            packed_estimated_tokens_max=max(failure_tokens, default=0),
            packed_estimated_tokens_p50=int(round(_percentile(failure_tokens, 0.50))),
        ),
        evidence_filter=ProposalEvidenceFilterAggregate(
            evaluated_case_count=len(filter_evaluated),
            passed_case_count=len(filter_passed),
            failed_case_count=len(filter_evaluated) - len(filter_passed),
            expected_excluded_artifact_count=expected_excluded,
            correctly_excluded_artifact_count=correctly_excluded,
            false_exclusion_count=sum(item.evidence_filter.false_exclusion_count for item in filter_evaluated),
            excluded_citation_context_leakage_case_count=sum(
                item.evidence_filter.excluded_citation_context_leakage_count > 0 for item in filter_evaluated
            ),
            summary_invalid_case_count=sum(
                item.evidence_filter.summary_status == "invalid" for item in filter_evaluated
            ),
            excluded_artifact_recall=correctly_excluded / expected_excluded if expected_excluded else 1.0,
        ),
        content_judge=ProposalContentJudgeAggregate(
            completed_case_count=sum(item.content_judge.status == "completed" for item in evaluated),
            failed_case_count=sum(item.content_judge.status == "failed" for item in evaluated),
            not_run_case_count=sum(item.content_judge.status == "not_run" for item in evaluated),
            oracle_case_count=sum(item.content_judge.status == "oracle" for item in evaluated),
            average_total_score=_round_score(
                mean(item.total_score for item in completed_judges) if completed_judges else 0.0
            ),
            grounded_accuracy_average=_round_score(judge_average("grounded_accuracy")),
            decision_completeness_average=_round_score(judge_average("decision_completeness")),
            purpose_and_necessity_average=_round_score(judge_average("purpose_and_necessity")),
            actionability_and_feasibility_average=_round_score(
                judge_average("actionability_and_feasibility")
            ),
            logical_structure_average=_round_score(judge_average("logical_structure")),
            business_writing_average=_round_score(judge_average("business_writing")),
            conciseness_and_readability_average=_round_score(
                judge_average("conciseness_and_readability")
            ),
            unsupported_material_claim_count=sum(
                item.unsupported_material_claim_count for item in completed_judges
            ),
            missing_critical_item_count=sum(item.missing_critical_item_count for item in completed_judges),
            contradiction_count=sum(item.contradiction_count for item in completed_judges),
        ),
        supplemental=ProposalSupplementalAggregate(
            event_evaluated_case_count=len(event_evaluated),
            event_passed_case_count=sum(item.event_attendance.status == "passed" for item in event_evaluated),
            event_failed_case_count=sum(item.event_attendance.status == "failed" for item in event_evaluated),
            revision_evaluated_case_count=len(revision_evaluated),
            revision_passed_case_count=sum(item.revision.status == "passed" for item in revision_evaluated),
            revision_failed_case_count=sum(item.revision.status == "failed" for item in revision_evaluated),
        ),
        coverage=ProposalCoverageAggregate(
            candidate_case_count=sum(item.coverage.review_status == "candidate" for item in results),
            needs_review_case_count=sum(item.coverage.review_status == "needs_review" for item in results),
            incomplete_case_count=sum(item.coverage.review_status == "incomplete" for item in results),
            reviewed_case_count=sum(item.coverage.review_status == "reviewed" for item in results),
            unassigned_case_count=sum(item.coverage.split == "unassigned" for item in results),
            test_case_count=sum(item.coverage.split == "test" for item in results),
            expected_labeled_case_count=sum(item.coverage.expected_label_status == "labeled" for item in results),
            factual_labeled_case_count=sum(item.coverage.factual_label_status == "labeled" for item in results),
            factual_scored_case_count=len(factual_scored),
            factual_not_scored_case_count=sum(item.factual.status == "not_scored" for item in evaluated),
            product_quality_eligible_case_count=sum(item.coverage.product_quality_eligible for item in results),
            factual_scored_points=_round_score(factual_points),
            factual_possible_points=_round_score(factual_possible),
            factual_normalized_scored_points=(
                _round_score(factual_points / factual_possible * 100) if factual_possible else None
            ),
        ),
    )
