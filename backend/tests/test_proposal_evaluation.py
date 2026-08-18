from __future__ import annotations

import hashlib
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from smb_finder.evaluation.proposal_models import ProposalXlsxStatus
from smb_finder.evaluation.proposal_runner import (
    run_proposal_evaluation,
    write_proposal_evaluation_report,
)
from smb_finder.evaluation.proposal_scoring import score_xlsx_result


ARTIFACT_REFERENCE = f"art-{'a' * 20}"
ARTIFACT_TARGET = f"art-{'b' * 20}"
ARTIFACT_FORBIDDEN = f"art-{'c' * 20}"
CHUNK_REFERENCE = f"chk-{'1' * 16}"
CHUNK_TARGET = f"chk-{'2' * 16}"
CHUNK_FORBIDDEN = f"chk-{'3' * 16}"
CASE_ID = f"prp-{'1' * 20}"
GROUP_ID = f"grp-{'1' * 16}"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _artifact(artifact_id: str, chunk_id: str, text: str) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "extraction_status": "ok",
        "chunks": [{"chunk_id": chunk_id, "text": text}],
    }


def _case(
    *,
    case_id: str = CASE_ID,
    group_id: str = GROUP_ID,
    reference_ids: list[str] | None = None,
    body: str = "1. 목적\n합성 장비 도입",
) -> dict[str, object]:
    return {
        "case_id": case_id,
        "group_id": group_id,
        "target_artifact_id": ARTIFACT_TARGET,
        "reference_artifact_ids": reference_ids if reference_ids is not None else [ARTIFACT_REFERENCE],
        "excluded_post_event_artifact_ids": [ARTIFACT_FORBIDDEN],
        "review_status": "candidate",
        "evaluation_scope": "current_tool",
        "expected": {
            "source_sheet": "기안지",
            "title": "합성 장비 도입",
            "approval_request": "검토 후 재가하여 주시기 바랍니다.",
            "body": body,
            "body_lines": body.splitlines(),
            "body_sections": [
                {"semantic_role": "purpose", "heading": "1. 목적", "lines": body.splitlines()}
            ],
            "current_tool_compatible": True,
        },
    }


def _dataset(tmp_path: Path, *, reference_text: str = "합성 장비 도입 근거") -> tuple[Path, Path]:
    documents = tmp_path / "documents.jsonl"
    cases = tmp_path / "proposal_cases.jsonl"
    _write_jsonl(
        documents,
        [
            _artifact(ARTIFACT_REFERENCE, CHUNK_REFERENCE, reference_text),
            _artifact(ARTIFACT_TARGET, CHUNK_TARGET, "합성 정답 본체"),
            _artifact(ARTIFACT_FORBIDDEN, CHUNK_FORBIDDEN, "합성 사후 자료"),
        ],
    )
    _write_jsonl(cases, [_case()])
    return documents, cases


def _workbook(path: Path, *, title: str, approval: str, body: str) -> None:
    cells = [
        f'<c r="C8" t="inlineStr"><is><t>{escape(title)}</t></is></c>',
        f'<c r="A10" t="inlineStr"><is><t>{escape(approval)}</t></is></c>',
    ]
    for index, line in enumerate(body.splitlines(), start=15):
        cells.append(f'<c r="A{index}" t="inlineStr"><is><t>{escape(line)}</t></is></c>')
    sheet = (
        f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="8">'
        f'{cells[0]}</row><row r="10">{cells[1]}</row>'
        + "".join(f'<row r="{15 + index}">{cell}</row>' for index, cell in enumerate(cells[2:]))
        + "</sheetData></worksheet>"
    )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="기안지" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr(
            "xl/styles.xml",
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _v2_workbook(
    path: Path,
    *,
    title: str,
    approval: str,
    heading: str,
    headers: list[str],
    rows: list[list[str]],
    table_sheet: str = "기안지",
    wrap: bool = True,
    merge: bool = True,
    row_heights: bool = True,
    border: bool = True,
    fake_pipe: bool = False,
) -> None:
    body_wrap_xml = "" if wrap else '<alignment wrapText="1"/>'
    table_wrap_xml = '<alignment wrapText="1"/>' if wrap else ""
    border_id = "1" if border else "0"
    styles = (
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<borders count="2"><border/><border><left style="thin"/><right style="thin"/>'
        '<top style="thin"/><bottom style="thin"/></border></borders>'
        '<cellXfs count="3"><xf borderId="0"/><xf borderId="0" applyAlignment="1">'
        f'{body_wrap_xml}</xf><xf borderId="{border_id}" applyAlignment="1">{table_wrap_xml}</xf></cellXfs>'
        "</styleSheet>"
    )

    def cell(reference: str, value: str, style: int) -> str:
        return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'

    def row(number: int, cells: str, *, explicit_height: bool) -> str:
        height = ' ht="30" customHeight="1"' if explicit_height else ""
        return f'<row r="{number}"{height}>{cells}</row>'

    main_rows = [
        '<row r="8">' + cell("C8", title, 0) + "</row>",
        '<row r="10">' + cell("A10", approval, 0) + "</row>",
        row(15, cell("A15", heading, 1), explicit_height=not row_heights),
    ]
    appendix_rows: list[str] = []
    table_start = 16 if table_sheet == "기안지" else 1
    table_rows = [headers, *rows]
    for row_offset, values in enumerate(table_rows):
        cells = "".join(
            cell(f"{chr(ord('A') + column_offset)}{table_start + row_offset}", value, 2)
            for column_offset, value in enumerate(values)
        )
        target = main_rows if table_sheet == "기안지" else appendix_rows
        target.append(row(table_start + row_offset, cells, explicit_height=row_heights))
    if fake_pipe:
        main_rows.append(row(table_start, cell(f"A{table_start}", " | ".join(headers), 1), explicit_height=False))
    merge_xml = "" if merge else '<mergeCells count="1"><mergeCell ref="A15:Z15"/></mergeCells>'
    main_sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(main_rows)
        + f"</sheetData>{merge_xml}</worksheet>"
    )
    include_appendix = table_sheet == "세부내용"
    appendix_sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(appendix_rows)
        + "</sheetData></worksheet>"
    )
    sheets = '<sheet name="기안지" sheetId="1" r:id="rId1"/>'
    relationships = (
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
    )
    if include_appendix:
        sheets += '<sheet name="세부내용" sheetId="2" r:id="rId2"/>'
        relationships += (
            '<Relationship Id="rId2" Target="worksheets/sheet2.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
        )
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{relationships}</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", main_sheet)
        if include_appendix:
            archive.writestr("xl/worksheets/sheet2.xml", appendix_sheet)


def _prediction(*, workbook_name: str, evidence_artifacts: list[str] | None = None) -> dict[str, object]:
    return {
        "case_id": CASE_ID,
        "status": "ok",
        "fields": {
            "title": "합성 장비 도입",
            "approval_request": "검토 후 재가하여 주시기 바랍니다.",
            "body": "1. 목적\n합성 장비 도입",
        },
        "document": {
            "schema_version": "proposal-document-v2",
            "title": "합성 장비 도입",
            "approval_request": "검토 후 재가하여 주시기 바랍니다.",
            "sections": [
                {
                    "heading": "1. 목적",
                    "semantic_role": "purpose",
                    "citations": ["E001"],
                    "blocks": [{"type": "paragraph", "text": "1. 목적\n합성 장비 도입"}],
                    "missing_information": [],
                }
            ],
            "missing_information": [],
        },
        "evidence_artifact_ids": evidence_artifacts or [ARTIFACT_REFERENCE],
        "evidence_chunk_ids": [CHUNK_REFERENCE],
        "content_judge": {
            "schema_version": "proposal-content-judge-v1",
            "rubric_version": "proposal-content-rubric-v1",
            "status": "completed",
            "judge_model": "synthetic-judge",
            "scores": {
                "grounded_accuracy": 12,
                "decision_completeness": 9,
                "purpose_and_necessity": 7,
                "actionability_and_feasibility": 6,
                "logical_structure": 5,
                "business_writing": 4,
                "conciseness_and_readability": 2,
            },
            "unsupported_material_claim_count": 0,
            "missing_critical_item_count": 0,
            "contradiction_count": 0,
            "confidence": 1,
        },
        "citation_map": {"E001": CHUNK_REFERENCE},
        "context": {"estimated_tokens": 100, "context_sha256": "a" * 64},
        "context_usage": {
            "schema_version": "proposal-context-usage-v1",
            "source_citation_count": 1,
            "packed_citation_count": 1,
            "source_document_count": 1,
            "packed_document_count": 1,
            "context_budget_chars": 1_000,
            "context_chars": 200,
            "estimated_input_tokens": 100,
        },
        "projection": {
            "source_line_count": 2,
            "output_line_count": 2,
            "omitted_line_count": 0,
            "truncated": False,
        },
        "tool_events": [
            {"stage": "evidence", "status": "ok", "elapsed_ms": 1},
            {"stage": "llm", "status": "ok", "elapsed_ms": 2},
            {"stage": "workbook", "status": "ok", "elapsed_ms": 3},
            {"stage": "save", "status": "ok", "elapsed_ms": 4},
        ],
        "workbook_path": workbook_name,
        "timings_ms": {"evidence_ms": 1, "llm_ms": 2, "workbook_ms": 3, "save_ms": 4, "total_ms": 10},
    }


def _table_prediction(
    *,
    workbook_name: str,
    headers: list[str],
    rows: list[list[str]],
) -> dict[str, object]:
    prediction = _prediction(workbook_name=workbook_name)
    prediction["xlsx_contract"] = "proposal-xlsx-v2"
    prediction["document"]["sections"][0]["blocks"] = [  # type: ignore[index]
        {"type": "table", "headers": headers, "rows": rows}
    ]
    return prediction


def _revision_case_and_prediction(workbook_name: str) -> tuple[dict[str, object], dict[str, object], str]:
    prediction = _prediction(workbook_name=workbook_name)
    base_document = deepcopy(prediction["document"])
    base_document["proposal_type"] = None  # type: ignore[index]
    revised_document = deepcopy(base_document)
    revised_document["title"] = "합성 수정 장비 도입"  # type: ignore[index]
    base_hash = _canonical_sha256(base_document)
    revised_hash = _canonical_sha256(revised_document)
    case = _case()
    case["expected"]["title"] = "합성 수정 장비 도입"  # type: ignore[index]
    case["revision_expectation"] = {
        "feedback_sha256": hashlib.sha256("제목을 수정해 주세요".encode()).hexdigest(),
        "base_document_sha256": base_hash,
        "expected_revised_document_sha256": revised_hash,
        "expected_changed_fields": ["title"],
        "expected_preserved_fields": ["approval_request", "proposal_type", "sections", "missing_information"],
    }
    prediction["fields"]["title"] = "합성 수정 장비 도입"  # type: ignore[index]
    prediction["document"] = revised_document
    prediction["revision_of_draft_id"] = "11111111-1111-4111-8111-111111111111"
    prediction["revision_number"] = "1.1"
    prediction["revision_summary"] = "합성 제목 수정 완료"
    prediction["revision_base_document"] = base_document
    prediction["revision_base_document_sha256_before"] = base_hash
    prediction["revision_base_document_sha256_after"] = base_hash
    prediction["revision_base_workbook_sha256_before"] = "d" * 64
    prediction["revision_base_workbook_sha256_after"] = "d" * 64
    return case, prediction, base_hash


def test_perfect_prediction_scores_100_points(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "result.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [_prediction(workbook_name=workbook.name)])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)

    assert report.cases[0].scores.model_dump() == {
        "evidence_context": 25.0,
        "llm_content": 45.0,
        "xlsx_result": 20.0,
        "tool_flow": 10.0,
        "raw_total": 100.0,
        "final_total": 100.0,
    }
    assert report.cases[0].hard_gates.passed is True
    assert report.aggregate.content_judge.average_total_score == 45
    assert report.aggregate.content_judge.completed_case_count == 1
    assert report.cases[0].status == "passed"
    assert report.cases[0].profile.label_status == "not_labeled"
    sanitized = report.model_dump_json()
    assert "합성 장비 도입" not in sanitized
    assert "result.xlsx" not in sanitized
    assert "proposal-document-v2" not in sanitized


def test_xlsx_score_is_zero_when_workbook_was_not_generated() -> None:
    """레이아웃 기본값이 참이어도 파일이 없으면 XLSX 점수를 주지 않는다."""

    status = ProposalXlsxStatus(contract_version="proposal-xlsx-v2")

    assert status.generated is False
    assert score_xlsx_result(status) == 0


def test_evidence_leakage_hard_gate_caps_score(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "result.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(
        predictions,
        [_prediction(workbook_name=workbook.name, evidence_artifacts=[ARTIFACT_REFERENCE, ARTIFACT_TARGET])],
    )

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.hard_gates.evidence_leakage_free is False
    assert result.scores.raw_total > 80
    assert result.scores.final_total == 49
    assert result.status == "failed"


def test_evidence_filter_scores_exclusion_recall_without_reporting_reason_names(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "filtered.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    prediction = _prediction(workbook_name=workbook.name)
    prediction["evidence_filter"] = {
        "schema_version": "proposal-evidence-filter-v1",
        "input_document_count": 2,
        "included_document_count": 1,
        "excluded_document_count": 1,
        "input_citation_count": 2,
        "included_citation_count": 1,
        "excluded_citation_count": 1,
        "excluded_reason_counts": {"SENSITIVE_SYNTHETIC_REASON": 1},
        "fallback_used": False,
    }
    prediction["excluded_evidence_artifact_ids"] = [ARTIFACT_FORBIDDEN]
    prediction["excluded_evidence_chunk_ids"] = [CHUNK_FORBIDDEN]
    predictions = tmp_path / "filtered.jsonl"
    _write_jsonl(predictions, [prediction])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)
    result = report.cases[0]

    assert result.evidence_filter.status == "passed"
    assert result.evidence_filter.excluded_artifact_recall == 1
    assert result.evidence_filter.false_exclusion_count == 0
    assert result.evidence_filter.excluded_citation_context_leakage_count == 0
    assert result.scores.evidence_context == 25
    assert result.hard_gates.evidence_filter_valid is True
    assert report.aggregate.evidence_filter.evaluated_case_count == 1
    assert report.aggregate.evidence_filter.excluded_artifact_recall == 1
    assert "SENSITIVE_SYNTHETIC_REASON" not in report.model_dump_json()


def test_evidence_filter_false_exclusion_and_excluded_citation_leakage_fail_gate(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "filter-failed.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    prediction = _prediction(
        workbook_name=workbook.name,
        evidence_artifacts=[ARTIFACT_REFERENCE, ARTIFACT_FORBIDDEN],
    )
    prediction["evidence_chunk_ids"] = [CHUNK_REFERENCE, CHUNK_FORBIDDEN]
    prediction["citation_map"] = {"E001": CHUNK_FORBIDDEN}
    prediction["context_usage"]["packed_citation_count"] = 2  # type: ignore[index]
    prediction["evidence_filter"] = {
        "schema_version": "proposal-evidence-filter-v1",
        "input_document_count": 4,
        "included_document_count": 2,
        "excluded_document_count": 2,
        "input_citation_count": 4,
        "included_citation_count": 2,
        "excluded_citation_count": 2,
        "excluded_reason_counts": {"synthetic": 2},
        "fallback_used": False,
    }
    prediction["excluded_evidence_artifact_ids"] = [ARTIFACT_FORBIDDEN, ARTIFACT_REFERENCE]
    prediction["excluded_evidence_chunk_ids"] = [CHUNK_FORBIDDEN, CHUNK_REFERENCE]
    predictions = tmp_path / "filter-failed.jsonl"
    _write_jsonl(predictions, [prediction])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)
    result = report.cases[0]

    assert result.evidence_filter.status == "failed"
    assert result.evidence_filter.false_exclusion_count == 1
    assert result.evidence_filter.excluded_citation_context_leakage_count > 0
    assert result.hard_gates.evidence_filter_valid is False
    assert result.hard_gates.evidence_leakage_free is False
    assert result.scores.final_total == 49
    assert report.aggregate.evidence_filter.false_exclusion_count == 1
    assert report.aggregate.evidence_filter.excluded_citation_context_leakage_case_count == 1


def test_v2_table_and_list_structure_are_scored(tmp_path: Path) -> None:
    body = "1. 목적\n구분 | 금액\n장비 | 100\n- 검토"
    documents = tmp_path / "documents.jsonl"
    cases = tmp_path / "proposal_cases.jsonl"
    _write_jsonl(
        documents,
        [
            _artifact(ARTIFACT_REFERENCE, CHUNK_REFERENCE, "합성 구조 근거"),
            _artifact(ARTIFACT_TARGET, CHUNK_TARGET, "합성 본체"),
            _artifact(ARTIFACT_FORBIDDEN, CHUNK_FORBIDDEN, "합성 사후 자료"),
        ],
    )
    case = _case(body=body)
    case["expected"]["body_rows"] = [  # type: ignore[index]
        {"source_row": 15, "text": "구분 | 금액", "cells": [{"value": "구분"}, {"value": "금액"}]},
        {"source_row": 16, "text": "장비 | 100", "cells": [{"value": "장비"}, {"value": "100"}]},
        {"source_row": 17, "text": "- 검토", "cells": [{"value": "- 검토"}]},
    ]
    case["expected"]["body_sections"] = [  # type: ignore[index]
        {
            "semantic_role": "purpose",
            "heading": "1. 목적",
            "lines": body.splitlines(),
            "source_line_start": 1,
            "source_line_end": 4,
        }
    ]
    _write_jsonl(cases, [case])
    workbook = tmp_path / "structured.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body=body,
    )
    prediction = _prediction(workbook_name=workbook.name)
    prediction["fields"]["body"] = body  # type: ignore[index]
    prediction["document"]["sections"][0]["blocks"] = [  # type: ignore[index]
        {"type": "table", "headers": ["구분", "금액"], "rows": [["장비", "100"]]},
        {"type": "list", "ordered": False, "items": ["검토"]},
    ]
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.scores.llm_content == 45
    assert result.structure.structure_fidelity_score == 14
    assert result.structure.expected_table_row_count == result.structure.predicted_table_row_count == 2
    assert result.structure.expected_list_item_count == result.structure.predicted_list_item_count == 1


def test_v2_xlsx_contract_requires_real_table_geometry_and_layout(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "v2.xlsx"
    headers = ["item", "amount"]
    rows = [["device", "100"]]
    prediction = _table_prediction(workbook_name=workbook.name, headers=headers, rows=rows)
    _v2_workbook(
        workbook,
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        heading=prediction["document"]["sections"][0]["heading"],  # type: ignore[index]
        headers=headers,
        rows=rows,
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.xlsx.layout_status == "passed"
    assert result.xlsx.table_geometry_valid is True
    assert result.xlsx.table_border_valid is True
    assert result.xlsx.fake_pipe_table_absent is True
    assert result.xlsx.inline_table_count == 1
    assert result.scores.xlsx_result == 20
    assert result.hard_gates.xlsx_render_contract_valid is True


def test_v2_fake_pipe_table_fails_render_hard_gate(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "fake-table.xlsx"
    headers = ["item", "amount"]
    rows = [["device", "100"]]
    prediction = _table_prediction(workbook_name=workbook.name, headers=headers, rows=rows)
    _v2_workbook(
        workbook,
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        heading=prediction["document"]["sections"][0]["heading"],  # type: ignore[index]
        headers=headers,
        rows=rows,
        fake_pipe=True,
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.xlsx.fake_pipe_table_absent is False
    assert result.xlsx.table_geometry_valid is False
    assert result.xlsx.layout_status == "failed"
    assert result.hard_gates.xlsx_render_contract_valid is False
    assert result.scores.final_total == 49


def test_v2_wide_table_requires_valid_appendix_matrix(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "appendix.xlsx"
    headers = [f"column-{index}" for index in range(7)]
    rows = [[f"value-{index}" for index in range(7)]]
    prediction = _table_prediction(workbook_name=workbook.name, headers=headers, rows=rows)
    _v2_workbook(
        workbook,
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        heading=prediction["document"]["sections"][0]["heading"],  # type: ignore[index]
        headers=headers,
        rows=rows,
        table_sheet="세부내용",
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.xlsx.appendix_status == "valid"
    assert result.xlsx.appendix_table_count == 1
    assert result.xlsx.inline_table_count == 0
    assert result.xlsx.layout_status == "passed"


def test_v2_wide_table_without_appendix_fails_render_hard_gate(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "appendix-missing.xlsx"
    headers = [f"column-{index}" for index in range(7)]
    rows = [[f"value-{index}" for index in range(7)]]
    prediction = _table_prediction(workbook_name=workbook.name, headers=headers, rows=rows)
    _v2_workbook(
        workbook,
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        heading=prediction["document"]["sections"][0]["heading"],  # type: ignore[index]
        headers=headers,
        rows=rows,
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.xlsx.appendix_status == "missing"
    assert result.xlsx.table_geometry_valid is False
    assert result.xlsx.layout_status == "failed"
    assert result.hard_gates.xlsx_render_contract_valid is False


def test_v2_body_layout_and_table_border_are_diagnostic_failures(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "bad-layout.xlsx"
    headers = ["item", "amount"]
    rows = [["device", "100"]]
    prediction = _table_prediction(workbook_name=workbook.name, headers=headers, rows=rows)
    _v2_workbook(
        workbook,
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        heading=prediction["document"]["sections"][0]["heading"],  # type: ignore[index]
        headers=headers,
        rows=rows,
        wrap=False,
        merge=False,
        row_heights=False,
        border=False,
    )
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.xlsx.body_text_single_line_valid is False
    assert result.xlsx.body_editable_unmerged_valid is False
    assert result.xlsx.body_default_height_valid is False
    assert result.xlsx.table_wrap_valid is False
    assert result.xlsx.table_explicit_row_height_valid is False
    assert result.xlsx.table_border_valid is False
    assert result.xlsx.layout_status == "failed"


def test_labeled_proposal_type_and_relative_section_order_are_scored(tmp_path: Path) -> None:
    documents, _ = _dataset(tmp_path)
    cases = tmp_path / "proposal_cases.jsonl"
    labeled_case = _case()
    labeled_case["proposal_type_label"] = "purchase"
    labeled_case["expected_section_order"] = ["purpose", "budget"]
    _write_jsonl(cases, [labeled_case])
    workbook = tmp_path / "profile.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )

    matching = _prediction(workbook_name=workbook.name)
    matching["document"]["proposal_type"] = "purchase"  # type: ignore[index]
    matching["requested_proposal_type"] = "purchase"
    matching["resolved_proposal_type"] = "purchase"
    matching["proposal_type_source"] = "user"
    matching_path = tmp_path / "matching.jsonl"
    _write_jsonl(matching_path, [matching])
    matching_result = run_proposal_evaluation(
        documents, cases, mode="predictions", predictions_path=matching_path
    ).cases[0]

    mismatching = _prediction(workbook_name=workbook.name)
    mismatching["document"]["proposal_type"] = "general"  # type: ignore[index]
    mismatching["requested_proposal_type"] = "purchase"
    mismatching["resolved_proposal_type"] = "general"
    mismatching["proposal_type_source"] = "fallback"
    sections = mismatching["document"]["sections"]  # type: ignore[index]
    sections.insert(
        0,
        {
            "heading": "예산",
            "semantic_role": "budget",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "100"}],
            "missing_information": [],
        },
    )
    mismatching_path = tmp_path / "mismatching.jsonl"
    _write_jsonl(mismatching_path, [mismatching])
    mismatching_result = run_proposal_evaluation(
        documents, cases, mode="predictions", predictions_path=mismatching_path
    ).cases[0]

    assert matching_result.profile.profile_score == 2
    assert matching_result.profile.resolution_status == "matched"
    assert matching_result.profile.section_order_status == "matched"
    assert mismatching_result.profile.profile_score == 0.5
    assert mismatching_result.profile.resolution_status == "mismatched"
    assert mismatching_result.profile.source_status == "invalid"
    assert mismatching_result.profile.section_order_status == "violated"
    assert mismatching_result.structure.structure_fidelity_score < matching_result.structure.structure_fidelity_score


def test_event_attendance_requires_three_compact_headings_in_order(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "event.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    prediction = _prediction(workbook_name=workbook.name)
    prediction["resolved_proposal_type"] = "event_attendance"
    prediction["proposal_type_source"] = "rule"
    prediction["requested_proposal_type"] = "auto"
    prediction["document"]["proposal_type"] = "event_attendance"  # type: ignore[index]
    prediction["document"]["sections"] = [  # type: ignore[index]
        {
            "heading": heading,
            "semantic_role": role,
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": f"{index}번 합성 내용"}],
            "missing_information": [],
        }
        for index, (heading, role) in enumerate(
            (("참가 목적", "purpose"), ("참가 내용", "details"), ("행사 주요 내용", "details")),
            start=1,
        )
    ]
    predictions = tmp_path / "event.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.event_attendance.status == "passed"
    assert result.event_attendance.present_required_heading_count == 3
    assert result.event_attendance.order_valid is True
    assert result.hard_gates.event_attendance_structure_valid is True


def test_event_attendance_forbids_separate_overview_and_full_schedule(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    workbook = tmp_path / "event-invalid.xlsx"
    _workbook(
        workbook,
        title="합성 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    prediction = _prediction(workbook_name=workbook.name)
    prediction["resolved_proposal_type"] = "event_attendance"
    prediction["document"]["proposal_type"] = "event_attendance"  # type: ignore[index]
    headings = ["행사 개요", "참가 목적", "전체 일정", "참가 내용", "행사 주요 내용"]
    prediction["document"]["sections"] = [  # type: ignore[index]
        {
            "heading": heading,
            "semantic_role": "details",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "합성 내용"}],
            "missing_information": [],
        }
        for heading in headings
    ]
    predictions = tmp_path / "event-invalid.jsonl"
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.event_attendance.status == "failed"
    assert result.event_attendance.forbidden_heading_count == 2
    assert result.event_attendance.extra_heading_count == 2
    assert result.event_attendance.order_valid is False
    assert result.hard_gates.event_attendance_structure_valid is False
    assert result.scores.final_total == 49


def test_revision_applies_feedback_preserves_fields_and_keeps_base_hashes(tmp_path: Path) -> None:
    documents, _ = _dataset(tmp_path)
    workbook = tmp_path / "revision.xlsx"
    _workbook(
        workbook,
        title="합성 수정 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    case, prediction, _ = _revision_case_and_prediction(workbook.name)
    cases = tmp_path / "revision-cases.jsonl"
    predictions = tmp_path / "revision.jsonl"
    _write_jsonl(cases, [case])
    _write_jsonl(predictions, [prediction])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)
    result = report.cases[0]

    assert result.revision.status == "passed"
    assert result.revision.feedback_application_status == "matched"
    assert result.revision.preservation_status == "matched"
    assert result.revision.base_document_hash_status == "matched"
    assert result.revision.base_workbook_hash_status == "matched"
    assert result.revision.applied_changed_field_count == 1
    assert result.revision.preserved_field_count == 4
    assert result.revision.revision_minor == 1
    assert result.revision.revision_summary_length == len("합성 제목 수정 완료")
    assert result.hard_gates.revision_valid is True
    assert report.aggregate.supplemental.revision_passed_case_count == 1
    assert "합성 제목 수정 완료" not in report.model_dump_json()
    assert "제목을 수정해 주세요" not in report.model_dump_json()


def test_revision_preservation_and_base_hash_mutation_fail_gate(tmp_path: Path) -> None:
    documents, _ = _dataset(tmp_path)
    workbook = tmp_path / "revision-invalid.xlsx"
    _workbook(
        workbook,
        title="합성 수정 장비 도입",
        approval="검토 후 재가하여 주시기 바랍니다.",
        body="1. 목적\n합성 장비 도입",
    )
    case, prediction, _ = _revision_case_and_prediction(workbook.name)
    prediction["document"]["sections"][0]["blocks"][0]["text"] = "보존 위반"  # type: ignore[index]
    case["revision_expectation"]["expected_revised_document_sha256"] = _canonical_sha256(  # type: ignore[index]
        prediction["document"]
    )
    prediction["revision_base_document_sha256_after"] = "e" * 64
    prediction["revision_base_workbook_sha256_after"] = "e" * 64
    cases = tmp_path / "revision-invalid-cases.jsonl"
    predictions = tmp_path / "revision-invalid.jsonl"
    _write_jsonl(cases, [case])
    _write_jsonl(predictions, [prediction])

    result = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions).cases[0]

    assert result.revision.feedback_application_status == "matched"
    assert result.revision.preservation_status == "mismatched"
    assert result.revision.base_document_hash_status == "mismatched"
    assert result.revision.base_workbook_hash_status == "mismatched"
    assert result.revision.status == "failed"
    assert result.hard_gates.revision_valid is False
    assert result.scores.final_total == 49


def test_preflight_buckets_runtime_budget_model_limit_and_reference_skip(tmp_path: Path) -> None:
    documents = tmp_path / "documents.jsonl"
    cases = tmp_path / "proposal_cases.jsonl"
    artifact_small = f"art-{'d' * 20}"
    artifact_medium = f"art-{'e' * 20}"
    artifact_huge = f"art-{'f' * 20}"
    _write_jsonl(
        documents,
        [
            _artifact(artifact_small, f"chk-{'4' * 16}", "가" * 100),
            _artifact(artifact_medium, f"chk-{'5' * 16}", "나" * 20_000),
            _artifact(artifact_huge, f"chk-{'6' * 16}", "다" * 140_000),
        ],
    )
    _write_jsonl(
        cases,
        [
            _case(case_id=f"prp-{'4' * 20}", group_id=f"grp-{'4' * 16}", reference_ids=[artifact_small]),
            _case(case_id=f"prp-{'5' * 20}", group_id=f"grp-{'5' * 16}", reference_ids=[artifact_medium]),
            _case(case_id=f"prp-{'6' * 20}", group_id=f"grp-{'6' * 16}", reference_ids=[artifact_huge]),
            _case(case_id=f"prp-{'7' * 20}", group_id=f"grp-{'7' * 16}", reference_ids=[ARTIFACT_REFERENCE]),
        ],
    )

    report = run_proposal_evaluation(
        documents,
        cases,
        mode="preflight",
        runtime_context_window_tokens=16_384,
        reserved_prompt_tokens=0,
        reserved_output_tokens=0,
        model_context_limit_tokens=262_144,
    )

    assert report.context.bucket_counts == {
        "lte_8k": 1,
        "8k_to_16k": 1,
        "16k_to_32k": 0,
        "32k_to_64k": 0,
        "gt_64k": 1,
    }
    assert report.context.runtime_over_budget_count == 1
    assert report.context.model_over_limit_count == 0
    assert report.context.skipped_reference_missing_count == 1
    assert report.model_context_limit_tokens == 262_144
    assert report.context.packed_chars_max == 26_214
    assert report.context.packed_tokens_max == 16_384


def test_report_is_sanitized_and_dataset_reads_are_non_mutating(tmp_path: Path) -> None:
    sensitive_marker = "SENSITIVE-SYNTHETIC-MARKER"
    documents, cases = _dataset(tmp_path, reference_text=sensitive_marker)
    before = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (documents, cases)
    }

    report = run_proposal_evaluation(documents, cases, mode="preflight")
    output = tmp_path / "report.json"
    write_proposal_evaluation_report(output, report)

    payload = output.read_text(encoding="utf-8")
    after = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (documents, cases)
    }
    assert before == after
    assert sensitive_marker not in payload
    assert str(documents) not in payload
    lowered_keys = {str(key).casefold() for key in _all_keys(json.loads(payload))}
    assert lowered_keys.isdisjoint({"text", "path", "relative_path", "file_name", "prompt", "response", "body"})


def _all_keys(value: object):  # noqa: ANN202
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _all_keys(nested)


def test_observed_context_limit_failures_keep_only_ids_and_lengths(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    predictions = tmp_path / "predictions.jsonl"
    _write_jsonl(
        predictions,
        [
            {
                "case_id": CASE_ID,
                "status": "error",
                "failure_code": "proposal_context_limit_exceeded",
                "evidence_artifact_ids": [ARTIFACT_REFERENCE],
                "evidence_chunk_ids": [CHUNK_REFERENCE],
                "context": {"packed_chars": 18_000, "estimated_tokens": 9_000},
                "tool_events": [
                    {"stage": "evidence", "status": "ok"},
                    {"stage": "llm", "status": "error"},
                    {"stage": "workbook", "status": "skipped"},
                    {"stage": "save", "status": "skipped"},
                ],
            }
        ],
    )

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)

    failures = report.aggregate.context_failures
    assert failures.failure_count == 1
    assert failures.case_ids == [CASE_ID]
    assert failures.group_ids == [GROUP_ID]
    assert failures.packed_estimated_tokens_min == 9_000
    assert failures.packed_estimated_tokens_max == 9_000


def test_baseline_does_not_require_prediction_or_external_service(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)

    report = run_proposal_evaluation(documents, cases, mode="baseline")

    assert report.mode == "baseline"
    assert report.evaluation_kind == "oracle_contract_check"
    assert report.deprecated_baseline_alias_used is True
    assert report.product_quality_eligible is False
    assert report.prediction_fingerprint is None
    assert report.context.ready_case_count == 1
    assert report.aggregate.evaluated_case_count == 1
    assert report.cases[0].xlsx.contract_version == "proposal-xlsx-v2"
    assert report.cases[0].xlsx.layout_status == "passed"


def test_oracle_contract_check_is_never_product_quality_eligible(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)

    report = run_proposal_evaluation(documents, cases, mode="oracle_contract_check")

    assert report.mode == "oracle_contract_check"
    assert report.evaluation_kind == "oracle_contract_check"
    assert report.deprecated_baseline_alias_used is False
    assert report.product_quality_eligible is False


def test_legacy_line_fact_labels_are_scored_only_as_diagnostic(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path, reference_text="SUPPORTED-FACT reference")
    case = _case()
    case.update(
        {
            "review_status": "reviewed",
            "split": "test",
            "required_fact_labels": ["SUPPORTED-FACT"],
            "unsupported_addition_labels": ["UNSUPPORTED-CLAIM"],
        }
    )
    _write_jsonl(cases, [case])
    prediction = _prediction(workbook_name="reviewed.xlsx")
    prediction["fields"]["body"] = "SUPPORTED-FACT"  # type: ignore[index]
    prediction["document"]["sections"][0]["blocks"] = [  # type: ignore[index]
        {"type": "paragraph", "text": "SUPPORTED-FACT"}
    ]
    workbook = tmp_path / "reviewed.xlsx"
    _workbook(
        workbook,
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        body="SUPPORTED-FACT",
    )
    predictions = tmp_path / "reviewed.jsonl"
    _write_jsonl(predictions, [prediction])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)
    result = report.cases[0]

    assert result.coverage.product_quality_eligible is False
    assert result.coverage.scoring_basis == "diagnostic"
    assert result.factual.status == "scored"
    assert result.factual.supported_fact_count == 1
    assert result.factual.omitted_fact_count == 0
    assert result.factual.unsupported_addition_count == 0
    assert result.factual.normalized_scored_points == 100
    assert report.aggregate.coverage.factual_normalized_scored_points == 100
    assert report.product_quality_eligible is False


def test_reviewed_fact_expectations_score_key_values_instead_of_whole_lines(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path, reference_text="합성 행사 참가비는 1,755,000원입니다.")
    case = _case(body="참가 목적\n합성 행사 참가비 1,755,000원")
    case.update(
        {
            "review_status": "reviewed",
            "split": "test",
            "instruction": "합성 행사 참가 승인을 요청합니다.",
            "fact_expectations": [
                {
                    "fact_key": "participation_fee",
                    "expectation": "include",
                    "expected_values": ["1,755,000 원"],
                    "aliases": ["1755000원"],
                    "source_artifact_ids": [ARTIFACT_REFERENCE],
                    "missing_action": "ask",
                },
                {
                    "fact_key": "full_timetable",
                    "expectation": "exclude",
                    "expected_values": ["시간대별 전체 일정"],
                    "missing_action": "omit",
                },
            ],
        }
    )
    _write_jsonl(cases, [case])
    prediction = _prediction(workbook_name="structured-facts.xlsx")
    prediction["fields"]["body"] = "참가비는 1,755,000원이며 참가 승인을 요청합니다."  # type: ignore[index]
    prediction["document"]["sections"][0]["blocks"] = [  # type: ignore[index]
        {"type": "paragraph", "text": "참가비는 1,755,000원이며 참가 승인을 요청합니다."}
    ]
    _workbook(
        tmp_path / "structured-facts.xlsx",
        title=prediction["fields"]["title"],  # type: ignore[index]
        approval=prediction["fields"]["approval_request"],  # type: ignore[index]
        body=prediction["fields"]["body"],  # type: ignore[index]
    )
    predictions = tmp_path / "structured-facts.jsonl"
    _write_jsonl(predictions, [prediction])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)
    factual = report.cases[0].factual

    assert report.cases[0].coverage.product_quality_eligible is True
    assert report.cases[0].coverage.scoring_basis == "reviewed_test"
    assert report.product_quality_eligible is True
    assert factual.status == "scored"
    assert factual.required_fact_count == 1
    assert factual.supported_fact_count == 1
    assert factual.unsupported_label_count == 1
    assert factual.unsupported_addition_count == 0
    assert factual.normalized_scored_points == 100


def test_expected_clarification_passes_without_creating_or_saving_workbook(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path, reference_text="합성 행사 참가 안내")
    case = _case(body="참가비 1,755,000원")
    case.update(
        {
            "review_status": "reviewed",
            "split": "test",
            "instruction": "합성 행사 참가 기안을 작성해 줘",
            "fact_expectations": [
                {
                    "fact_key": "participation_fee",
                    "expectation": "include",
                    "expected_values": ["1,755,000원"],
                    "source_artifact_ids": [ARTIFACT_REFERENCE],
                    "missing_action": "ask",
                }
            ],
        }
    )
    _write_jsonl(cases, [case])
    prediction = _prediction(workbook_name="unused.xlsx")
    prediction.pop("workbook_path")
    prediction["completion"] = {
        "schema_version": "proposal-completion-v1",
        "status": "needs_clarification",
        "question_field_keys": ["participation_fee"],
        "supported_claim_count": 1,
        "derived_claim_count": 0,
        "omitted_claim_count": 1,
        "conflicting_claim_count": 0,
    }
    prediction["tool_events"] = [
        {"stage": "evidence", "status": "ok", "elapsed_ms": 1},
        {"stage": "llm", "status": "ok", "elapsed_ms": 2},
        {"stage": "workbook", "status": "skipped", "elapsed_ms": 0},
        {"stage": "save", "status": "skipped", "elapsed_ms": 0},
    ]
    predictions = tmp_path / "clarification.jsonl"
    _write_jsonl(predictions, [prediction])

    report = run_proposal_evaluation(documents, cases, mode="predictions", predictions_path=predictions)
    result = report.cases[0]

    assert result.factual.supported_fact_count == 1
    assert result.factual.normalized_scored_points == 100
    assert result.hard_gates.xlsx_valid is True
    assert result.hard_gates.required_tool_stages_succeeded is True
    assert result.hard_gates.passed is True


def test_baseline_skips_reference_missing_case(tmp_path: Path) -> None:
    documents = tmp_path / "documents.jsonl"
    cases = tmp_path / "proposal_cases.jsonl"
    _write_jsonl(documents, [_artifact(ARTIFACT_TARGET, CHUNK_TARGET, "합성 본체")])
    _write_jsonl(cases, [_case(reference_ids=[ARTIFACT_REFERENCE])])

    report = run_proposal_evaluation(documents, cases, mode="baseline")

    assert report.cases[0].status == "skipped_reference_missing"
    assert report.aggregate.skipped_case_count == 1


@pytest.mark.parametrize("runtime_window,reserved", [(1_000, 1_000), (4_096, 5_000)])
def test_invalid_runtime_context_budget_is_rejected(
    tmp_path: Path,
    runtime_window: int,
    reserved: int,
) -> None:
    documents, cases = _dataset(tmp_path)
    with pytest.raises(ValueError, match="runtime_context_budget_invalid"):
        run_proposal_evaluation(
            documents,
            cases,
            mode="preflight",
            runtime_context_window_tokens=runtime_window,
            reserved_prompt_tokens=reserved,
            reserved_output_tokens=0,
        )
