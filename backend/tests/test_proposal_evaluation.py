from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from smb_finder.evaluation.proposal_runner import (
    run_proposal_evaluation,
    write_proposal_evaluation_report,
)


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
        "llm_content": 35.0,
        "xlsx_result": 25.0,
        "tool_flow": 15.0,
        "raw_total": 100.0,
        "final_total": 100.0,
    }
    assert report.cases[0].hard_gates.passed is True
    assert report.cases[0].status == "passed"
    sanitized = report.model_dump_json()
    assert "합성 장비 도입" not in sanitized
    assert "result.xlsx" not in sanitized
    assert "proposal-document-v2" not in sanitized


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

    assert result.scores.llm_content == 35
    assert result.structure.structure_fidelity_score == 14
    assert result.structure.expected_table_row_count == result.structure.predicted_table_row_count == 2
    assert result.structure.expected_list_item_count == result.structure.predicted_list_item_count == 1


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
    assert report.prediction_fingerprint is None
    assert report.context.ready_case_count == 1
    assert report.aggregate.evaluated_case_count == 1


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
