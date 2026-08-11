"""외부 기안 dataset을 읽기 전용으로 연결하는 preflight·baseline·채점 실행기."""

from __future__ import annotations

import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from xml.etree import ElementTree

from pydantic import BaseModel, ValidationError

from .proposal_models import (
    ContextBucket,
    EvaluationMode,
    EvaluationScope,
    ProposalContextAggregate,
    ProposalContextCaseStats,
    ProposalDatasetArtifact,
    ProposalDatasetCaseInput,
    ProposalEvaluationReport,
    ProposalPredictedDocument,
    ProposalPredictedFields,
    ProposalPredictedListBlock,
    ProposalPredictedParagraphBlock,
    ProposalPredictedSection,
    ProposalPredictedTableBlock,
    ProposalPrediction,
    ProposalPredictionContext,
    ProposalPredictionContextUsage,
    ProposalPredictionProjection,
    ProposalPredictionTimings,
    ProposalToolStageEvent,
    ProposalXlsxStatus,
)
from .proposal_scoring import (
    HARD_GATE_SCORE_CAP,
    PASS_THRESHOLD,
    aggregate_proposal_results,
    score_proposal_case,
)


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_FORBIDDEN_REPORT_KEYS = {
    "text",
    "relative_path",
    "path",
    "file_name",
    "filename",
    "instruction",
    "prompt",
    "response",
    "title",
    "approval_request",
    "body",
    "error_message",
}


class ProposalEvaluationError(ValueError):
    """원본 경로·본문·검증 예외를 노출하지 않는 평가 입력 오류."""


@dataclass(frozen=True)
class ProposalDatasetSnapshot:
    """읽기 전용으로 적재한 외부 dataset과 내용 fingerprint."""

    artifacts: dict[str, ProposalDatasetArtifact]
    cases: list[ProposalDatasetCaseInput]
    fingerprint: str


@dataclass(frozen=True)
class _ContextPlan:
    stats: ProposalContextCaseStats
    packed_chunk_ids: list[str]


@dataclass(frozen=True)
class _PredictionSnapshot:
    predictions: dict[str, ProposalPrediction]
    hashes: dict[str, str]
    fingerprint: str
    base_directory: Path


def _read_jsonl(path: Path, model: type[BaseModel], id_field: str) -> tuple[bytes, list[BaseModel]]:
    """파일을 byte 단위로 한 번만 읽고 쓰기 없이 Pydantic으로 검증한다."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProposalEvaluationError("dataset_read_failed") from exc
    records: list[BaseModel] = []
    seen: set[str] = set()
    try:
        decoded_lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ProposalEvaluationError("dataset_encoding_invalid") from exc
    for line_number, raw_line in enumerate(decoded_lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            record = model.model_validate_json(line)
        except (ValidationError, ValueError) as exc:
            raise ProposalEvaluationError(f"dataset_record_invalid:{line_number}") from exc
        record_id = str(getattr(record, id_field))
        if record_id in seen:
            raise ProposalEvaluationError(f"dataset_id_duplicated:{line_number}")
        seen.add(record_id)
        records.append(record)
    return raw, records


def load_proposal_dataset(
    documents_path: str | Path,
    proposal_cases_path: str | Path,
) -> ProposalDatasetSnapshot:
    """documents/proposal_cases JSONL을 읽기 전용으로 적재한다."""

    documents_raw, artifact_records = _read_jsonl(Path(documents_path), ProposalDatasetArtifact, "artifact_id")
    cases_raw, case_records = _read_jsonl(Path(proposal_cases_path), ProposalDatasetCaseInput, "case_id")
    fingerprint = hashlib.sha256(documents_raw + b"\x00proposal-cases\x00" + cases_raw).hexdigest()
    artifacts = {record.artifact_id: record for record in artifact_records if isinstance(record, ProposalDatasetArtifact)}
    cases = [record for record in case_records if isinstance(record, ProposalDatasetCaseInput)]
    return ProposalDatasetSnapshot(artifacts=artifacts, cases=cases, fingerprint=fingerprint)


def load_proposal_predictions(path: str | Path) -> _PredictionSnapshot:
    """prediction JSONL을 읽되 보고서에는 line hash만 전달한다."""

    prediction_path = Path(path)
    raw, records = _read_jsonl(prediction_path, ProposalPrediction, "case_id")
    predictions = {record.case_id: record for record in records if isinstance(record, ProposalPrediction)}
    hashes: dict[str, str] = {}
    for raw_line in raw.decode("utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            decoded = json.loads(line)
            case_id = str(decoded["case_id"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProposalEvaluationError("prediction_record_invalid") from exc
        hashes[case_id] = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return _PredictionSnapshot(
        predictions=predictions,
        hashes=hashes,
        fingerprint=hashlib.sha256(raw).hexdigest(),
        base_directory=prediction_path.resolve().parent,
    )


def conservative_estimated_tokens(char_count: int) -> int:
    """합성 실측에 여유를 둔 1.6자/token 비율로 token을 추정한다."""

    return math.ceil(max(0, char_count) * 5 / 8)


def context_bucket(estimated_tokens: int) -> ContextBucket:
    """모델 최대치와 무관하게 비교 가능한 고정 8k/16k/32k/64k 구간을 반환한다."""

    if estimated_tokens <= 8_192:
        return "lte_8k"
    if estimated_tokens <= 16_384:
        return "8k_to_16k"
    if estimated_tokens <= 32_768:
        return "16k_to_32k"
    if estimated_tokens <= 65_536:
        return "32k_to_64k"
    return "gt_64k"


def _fair_chunk_order(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
) -> list[tuple[str, str]]:
    """한 참고 문서가 budget을 독점하지 않도록 artifact별 chunk를 round-robin한다."""

    by_artifact = [artifacts[artifact_id].chunks for artifact_id in case.reference_artifact_ids if artifact_id in artifacts]
    ordered: list[tuple[str, str]] = []
    seen_text_hashes: set[str] = set()
    chunk_index = 0
    while any(chunk_index < len(chunks) for chunks in by_artifact):
        for chunks in by_artifact:
            if chunk_index >= len(chunks):
                continue
            chunk = chunks[chunk_index]
            text_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
            if text_hash in seen_text_hashes:
                continue
            seen_text_hashes.add(text_hash)
            ordered.append((chunk.chunk_id, chunk.text))
        chunk_index += 1
    return ordered


def _build_context_plan(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
    *,
    effective_input_budget_tokens: int,
    model_effective_input_limit_tokens: int | None,
) -> _ContextPlan:
    expected_ids = case.reference_artifact_ids
    missing_ids = [artifact_id for artifact_id in expected_ids if artifact_id not in artifacts]
    raw_chunks = [
        chunk
        for artifact_id in expected_ids
        if (artifact := artifacts.get(artifact_id)) is not None
        for chunk in artifact.chunks
    ]
    reference_missing = not expected_ids or bool(missing_ids) or not raw_chunks
    raw_chars = sum(len(chunk.text) for chunk in raw_chunks)
    raw_tokens = conservative_estimated_tokens(raw_chars)
    if reference_missing:
        return _ContextPlan(
            stats=ProposalContextCaseStats(
                case_id=case.case_id,
                group_id=case.group_id,
                status="skipped_reference_missing",
                reference_count=len(expected_ids),
                resolved_reference_count=len(expected_ids) - len(missing_ids),
                missing_reference_artifact_ids=missing_ids,
                available_chunk_count=len(raw_chunks),
                packed_chunk_count=0,
                raw_chars=raw_chars,
                packed_chars=0,
                raw_estimated_tokens=raw_tokens,
                packed_estimated_tokens=0,
                bucket=context_bucket(raw_tokens),
                over_runtime_budget=raw_tokens > effective_input_budget_tokens,
                over_model_limit=(
                    raw_tokens > model_effective_input_limit_tokens
                    if model_effective_input_limit_tokens is not None
                    else None
                ),
            ),
            packed_chunk_ids=[],
        )

    char_budget = effective_input_budget_tokens * 8 // 5
    packed_parts: list[bytes] = []
    packed_ids: list[str] = []
    packed_chars = 0
    for chunk_id, text in _fair_chunk_order(case, artifacts):
        remaining = char_budget - packed_chars
        if remaining <= 0:
            break
        selected = text[:remaining]
        if not selected:
            continue
        packed_parts.extend((chunk_id.encode("ascii"), b"\x00", selected.encode("utf-8"), b"\x00"))
        packed_ids.append(chunk_id)
        packed_chars += len(selected)
    packed_tokens = conservative_estimated_tokens(packed_chars)
    return _ContextPlan(
        stats=ProposalContextCaseStats(
            case_id=case.case_id,
            group_id=case.group_id,
            status="ready",
            reference_count=len(expected_ids),
            resolved_reference_count=len(expected_ids),
            available_chunk_count=len(raw_chunks),
            packed_chunk_count=len(packed_ids),
            raw_chars=raw_chars,
            packed_chars=packed_chars,
            raw_estimated_tokens=raw_tokens,
            packed_estimated_tokens=packed_tokens,
            bucket=context_bucket(raw_tokens),
            over_runtime_budget=raw_tokens > effective_input_budget_tokens,
            over_model_limit=(
                raw_tokens > model_effective_input_limit_tokens
                if model_effective_input_limit_tokens is not None
                else None
            ),
            truncated=packed_chars < raw_chars,
            packed_context_sha256=hashlib.sha256(b"".join(packed_parts)).hexdigest(),
        ),
        packed_chunk_ids=packed_ids,
    )


def _int_median(values: Iterable[int]) -> int:
    numbers = list(values)
    return int(round(median(numbers))) if numbers else 0


def _aggregate_context(plans: list[_ContextPlan], *, model_limit_supplied: bool) -> ProposalContextAggregate:
    stats = [plan.stats for plan in plans]
    ready = [item for item in stats if item.status == "ready"]
    skipped = [item for item in stats if item.status == "skipped_reference_missing"]
    runtime_over = [item for item in ready if item.over_runtime_budget]
    model_over = [item for item in ready if item.over_model_limit]
    bucket_counts: dict[ContextBucket, int] = {
        "lte_8k": 0,
        "8k_to_16k": 0,
        "16k_to_32k": 0,
        "32k_to_64k": 0,
        "gt_64k": 0,
    }
    for item in ready:
        bucket_counts[item.bucket] += 1
    raw_chars = [item.raw_chars for item in ready]
    packed_chars = [item.packed_chars for item in ready]
    raw_tokens = [item.raw_estimated_tokens for item in ready]
    packed_tokens = [item.packed_estimated_tokens for item in ready]
    return ProposalContextAggregate(
        total_case_count=len(stats),
        ready_case_count=len(ready),
        skipped_reference_missing_count=len(skipped),
        bucket_counts=bucket_counts,
        runtime_over_budget_count=len(runtime_over),
        runtime_over_budget_case_ids=sorted(item.case_id for item in runtime_over),
        runtime_over_budget_group_ids=sorted({item.group_id for item in runtime_over}),
        model_over_limit_count=len(model_over) if model_limit_supplied else None,
        model_over_limit_case_ids=sorted(item.case_id for item in model_over),
        model_over_limit_group_ids=sorted({item.group_id for item in model_over}),
        skipped_reference_missing_case_ids=sorted(item.case_id for item in skipped),
        skipped_reference_missing_group_ids=sorted({item.group_id for item in skipped}),
        raw_chars_min=min(raw_chars, default=0),
        raw_chars_max=max(raw_chars, default=0),
        raw_chars_p50=_int_median(raw_chars),
        packed_chars_min=min(packed_chars, default=0),
        packed_chars_max=max(packed_chars, default=0),
        packed_chars_p50=_int_median(packed_chars),
        raw_tokens_min=min(raw_tokens, default=0),
        raw_tokens_max=max(raw_tokens, default=0),
        raw_tokens_p50=_int_median(raw_tokens),
        packed_tokens_min=min(packed_tokens, default=0),
        packed_tokens_max=max(packed_tokens, default=0),
        packed_tokens_p50=_int_median(packed_tokens),
    )


def _scope_cases(cases: list[ProposalDatasetCaseInput], scope: EvaluationScope) -> list[ProposalDatasetCaseInput]:
    if scope == "all":
        return [case for case in cases if case.expected is not None]
    return [case for case in cases if case.expected is not None and case.evaluation_scope == scope]


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.findall(f".//{{{_MAIN_NS}}}t")) for item in root]


def _worksheet_path(workbook: zipfile.ZipFile, sheet_names: tuple[str, ...]) -> str:
    root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    sheets = root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet")
    selected = next((sheet for name in sheet_names for sheet in sheets if sheet.attrib.get("name") == name), None)
    if selected is None:
        raise ValueError("worksheet_missing")
    relationship_id = selected.attrib.get(f"{{{_OFFICE_REL_NS}}}id", "")
    relationships = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    relationship = next(
        (
            item
            for item in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
            if item.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if relationship is None:
        raise ValueError("worksheet_relationship_missing")
    target = relationship.attrib.get("Target", "").replace("\\", "/")
    return posixpath.normpath(target.lstrip("/") if target.startswith("xl/") else f"xl/{target.lstrip('/')}")


def _cell_values(workbook: zipfile.ZipFile, worksheet_path: str) -> dict[str, str]:
    shared = _shared_strings(workbook)
    root = ElementTree.fromstring(workbook.read(worksheet_path))
    values: dict[str, str] = {}
    for cell in root.findall(f".//{{{_MAIN_NS}}}c"):
        reference = cell.attrib.get("r", "")
        if not reference:
            continue
        data_type = cell.attrib.get("t", "")
        if data_type == "inlineStr":
            value = "".join(node.text or "" for node in cell.findall(f".//{{{_MAIN_NS}}}t"))
        else:
            node = cell.find(f"{{{_MAIN_NS}}}v")
            raw_value = node.text if node is not None and node.text is not None else ""
            if data_type == "s" and raw_value:
                value = shared[int(raw_value)]
            else:
                value = raw_value
        values[reference] = value.replace("\r\n", "\n").replace("\r", "\n")
    return values


def inspect_proposal_workbook(
    content: bytes,
    fields: ProposalPredictedFields,
    *,
    source_sheet: str,
    projection: ProposalPredictionProjection | None = None,
) -> ProposalXlsxStatus:
    """OOXML을 메모리에서 읽고 결과 문자열 대신 boolean과 hash만 반환한다."""

    status = ProposalXlsxStatus(
        generation_status="ok" if content else "not_generated",
        generated=bool(content),
        workbook_sha256=hashlib.sha256(content).hexdigest(),
        output_line_count=projection.output_line_count if projection is not None else len(fields.body.splitlines()),
        projection_omitted_line_count=projection.omitted_line_count if projection is not None else 0,
        projection_truncated=projection.truncated if projection is not None else False,
    )
    if not content.startswith(b"PK\x03\x04"):
        return status
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as workbook:
            status.zip_valid = workbook.testzip() is None
            required = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"}
            status.required_parts_present = required.issubset(workbook.namelist())
            worksheet_path = _worksheet_path(workbook, ("기안지", source_sheet))
            cells = _cell_values(workbook, worksheet_path)
            status.title_written = cells.get("C8", "").strip() == fields.title.strip()
            status.approval_written = cells.get("A10", "").strip() == fields.approval_request.strip()
            lines = fields.body.replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
            actual_lines = [cells.get(f"A{15 + index}", "") for index in range(len(lines))]
            status.body_written = actual_lines == lines
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return status
    return status


def _prediction_workbook_status(
    prediction: ProposalPrediction | None,
    base_directory: Path | None,
    case: ProposalDatasetCaseInput,
) -> ProposalXlsxStatus:
    if prediction is None or prediction.fields is None or not prediction.workbook_path or base_directory is None:
        return ProposalXlsxStatus()
    workbook_path = Path(prediction.workbook_path)
    resolved = workbook_path if workbook_path.is_absolute() else base_directory / workbook_path
    try:
        content = resolved.read_bytes()
    except OSError:
        return ProposalXlsxStatus()
    return inspect_proposal_workbook(
        content,
        prediction.fields,
        source_sheet=case.expected.source_sheet if case.expected else "기안지",
        projection=prediction.projection,
    )


def _canonical_prediction_hash(prediction: ProposalPrediction) -> str:
    payload = prediction.model_dump_json(exclude={"workbook_path"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _baseline_blocks(case: ProposalDatasetCaseInput, start: int, end: int):  # noqa: ANN202
    if case.expected is None:
        return []
    rows = case.expected.body_rows[start - 1 : end]
    blocks = []
    table_rows = [row for row in rows if len(row.cells) > 1]
    if len(table_rows) >= 2:
        headers = [(cell.value or "-")[:1_000] for cell in table_rows[0].cells]
        width = len(headers)
        matrix = []
        for row in table_rows[1:]:
            values = [cell.value[:1_000] for cell in row.cells]
            matrix.append((values + [""] * width)[:width])
        blocks.append(ProposalPredictedTableBlock(type="table", headers=headers, rows=matrix))
    list_items = []
    paragraph_lines = []
    for row in rows:
        if len(row.cells) > 1:
            if len(table_rows) < 2:
                paragraph_lines.append(row.text)
            continue
        line = row.text.strip()
        if not line:
            continue
        if line.startswith(("- ", "• ")):
            list_items.append(line[2:].strip()[:1_000])
        else:
            match = re.match(r"^\d+[.)]\s+(.*)$", line)
            if match:
                list_items.append(match.group(1)[:1_000])
            else:
                paragraph_lines.append(line)
    if paragraph_lines:
        blocks.append(ProposalPredictedParagraphBlock(type="paragraph", text="\n".join(paragraph_lines)[:3_000]))
    if list_items:
        blocks.append(ProposalPredictedListBlock(type="list", ordered=True, items=list_items[:30]))
    return blocks


def _baseline_document(case: ProposalDatasetCaseInput, citation_id: str) -> ProposalPredictedDocument:
    if case.expected is None:
        raise ProposalEvaluationError("baseline_expected_output_missing")
    sections = []
    for section in case.expected.body_sections[:20]:
        role = "other" if section.semantic_role == "preamble" else section.semantic_role
        blocks = _baseline_blocks(case, section.source_line_start, section.source_line_end)
        if not blocks:
            text = "\n".join(section.lines).strip()[:3_000] or "확인 필요"
            blocks = [ProposalPredictedParagraphBlock(type="paragraph", text=text)]
        sections.append(
            ProposalPredictedSection(
                heading=(section.heading or section.lines[0])[:120],
                semantic_role=role,
                citations=[citation_id],
                blocks=blocks,
                missing_information=[],
            )
        )
    if not sections:
        sections = [
            ProposalPredictedSection(
                heading="주요 내용",
                semantic_role="details",
                citations=[citation_id],
                blocks=[ProposalPredictedParagraphBlock(type="paragraph", text=case.expected.body[:3_000])],
                missing_information=[],
            )
        ]
    return ProposalPredictedDocument(
        schema_version="proposal-document-v2",
        title=case.expected.title[:80],
        approval_request=case.expected.approval_request[:1_000],
        sections=sections,
        missing_information=[],
    )


def _document_projection_line_count(document: ProposalPredictedDocument) -> int:
    count = 0
    for section in document.sections:
        count += 1
        for block in section.blocks:
            if block.type == "paragraph":
                count += 1
            elif block.type == "list":
                count += len(block.items)
            else:
                count += len(block.rows) + 1
        count += len(section.missing_information)
    return count + len(document.missing_information)


def _baseline_prediction(
    case: ProposalDatasetCaseInput,
    plan: _ContextPlan,
) -> tuple[ProposalPrediction, ProposalXlsxStatus]:
    if case.expected is None:
        raise ProposalEvaluationError("baseline_expected_output_missing")
    citation_id = "E001"
    document = _baseline_document(case, citation_id)
    fields = ProposalPredictedFields(
        title=case.expected.title,
        approval_request=case.expected.approval_request,
        body=case.expected.body,
    )
    source_line_count = _document_projection_line_count(document)
    projection = ProposalPredictionProjection(
        source_line_count=source_line_count,
        output_line_count=len(fields.body.splitlines()),
        omitted_line_count=0,
        truncated=False,
    )
    xlsx = ProposalXlsxStatus()
    try:
        from smb_finder.playground.proposal_draft import (
            ProposalDocumentV2,
            insert_proposal_fields,
            project_document_to_legacy_fields,
        )

        template_path = Path(__file__).resolve().parents[1] / "playground" / "templates" / "proposal_draft.xlsx"
        projected = project_document_to_legacy_fields(ProposalDocumentV2.model_validate(document.model_dump()))
        fields = ProposalPredictedFields.model_validate(projected.model_dump())
        output_line_count = len(fields.body.splitlines())
        projection = ProposalPredictionProjection(
            source_line_count=source_line_count,
            output_line_count=output_line_count,
            omitted_line_count=max(0, source_line_count - output_line_count),
            truncated=source_line_count > output_line_count or "생략" in fields.body,
        )
        generated = insert_proposal_fields(template_path.read_bytes(), projected)
        xlsx = inspect_proposal_workbook(
            generated,
            fields,
            source_sheet=case.expected.source_sheet,
            projection=projection,
        )
    except ValidationError:
        xlsx = ProposalXlsxStatus(generation_status="projection_invalid")
    except RuntimeError as exc:
        xlsx = ProposalXlsxStatus(
            generation_status="body_limit" if getattr(exc, "code", "") == "proposal_body_too_long" else "generation_error"
        )
    except (ImportError, OSError, ValueError):
        xlsx = ProposalXlsxStatus(generation_status="generation_error")
    workbook_succeeded = all(
        (xlsx.generated, xlsx.zip_valid, xlsx.required_parts_present, xlsx.title_written, xlsx.approval_written, xlsx.body_written)
    )
    prediction = ProposalPrediction(
        case_id=case.case_id,
        fields=fields,
        document=document,
        evidence_artifact_ids=case.reference_artifact_ids,
        evidence_chunk_ids=plan.packed_chunk_ids,
        citation_map={citation_id: plan.packed_chunk_ids[0]},
        context=ProposalPredictionContext(
            raw_chars=plan.stats.raw_chars,
            packed_chars=plan.stats.packed_chars,
            estimated_tokens=plan.stats.packed_estimated_tokens,
            context_sha256=plan.stats.packed_context_sha256,
        ),
        context_usage=ProposalPredictionContextUsage(
            source_citation_count=plan.stats.available_chunk_count,
            packed_citation_count=len(plan.packed_chunk_ids),
            source_document_count=plan.stats.reference_count,
            packed_document_count=plan.stats.resolved_reference_count,
            context_budget_chars=plan.stats.packed_chars,
            context_chars=plan.stats.packed_chars,
            estimated_input_tokens=plan.stats.packed_estimated_tokens,
            truncated=plan.stats.truncated,
        ),
        projection=projection,
        tool_events=[
            ProposalToolStageEvent(stage="evidence", status="simulated"),
            ProposalToolStageEvent(stage="llm", status="simulated"),
            ProposalToolStageEvent(stage="workbook", status="simulated" if workbook_succeeded else "error"),
            ProposalToolStageEvent(stage="save", status="simulated" if workbook_succeeded else "skipped"),
        ],
        timings_ms=ProposalPredictionTimings(),
        failure_code="none" if workbook_succeeded else "workbook_error",
    )
    return prediction, xlsx


def _empty_aggregate():
    return aggregate_proposal_results([])


def run_proposal_evaluation(
    documents_path: str | Path,
    proposal_cases_path: str | Path,
    *,
    mode: EvaluationMode,
    predictions_path: str | Path | None = None,
    scope: EvaluationScope = "all",
    runtime_context_window_tokens: int = 24_576,
    reserved_prompt_tokens: int = 1_024,
    reserved_output_tokens: int = 4_096,
    model_context_limit_tokens: int | None = None,
    pass_threshold: float = PASS_THRESHOLD,
    hard_gate_score_cap: float = HARD_GATE_SCORE_CAP,
) -> ProposalEvaluationReport:
    """SMB·외부 LLM 호출 없이 preflight, oracle baseline 또는 supplied prediction을 평가한다."""

    if runtime_context_window_tokens <= reserved_prompt_tokens + reserved_output_tokens:
        raise ProposalEvaluationError("runtime_context_budget_invalid")
    if model_context_limit_tokens is not None and model_context_limit_tokens <= reserved_prompt_tokens + reserved_output_tokens:
        raise ProposalEvaluationError("model_context_limit_invalid")
    if mode == "predictions" and predictions_path is None:
        raise ProposalEvaluationError("predictions_required")
    if mode != "predictions" and predictions_path is not None:
        raise ProposalEvaluationError("predictions_not_allowed")

    snapshot = load_proposal_dataset(documents_path, proposal_cases_path)
    cases = _scope_cases(snapshot.cases, scope)
    effective_input_budget = runtime_context_window_tokens - reserved_prompt_tokens - reserved_output_tokens
    model_effective_limit = (
        model_context_limit_tokens - reserved_prompt_tokens - reserved_output_tokens
        if model_context_limit_tokens is not None
        else None
    )
    plans = [
        _build_context_plan(
            case,
            snapshot.artifacts,
            effective_input_budget_tokens=effective_input_budget,
            model_effective_input_limit_tokens=model_effective_limit,
        )
        for case in cases
    ]
    context_aggregate = _aggregate_context(plans, model_limit_supplied=model_context_limit_tokens is not None)
    if mode == "preflight":
        report = ProposalEvaluationReport(
            mode=mode,
            scope=scope,
            dataset_fingerprint=snapshot.fingerprint,
            runtime_context_window_tokens=runtime_context_window_tokens,
            reserved_prompt_tokens=reserved_prompt_tokens,
            reserved_output_tokens=reserved_output_tokens,
            effective_input_budget_tokens=effective_input_budget,
            model_context_limit_tokens=model_context_limit_tokens,
            pass_threshold=pass_threshold,
            hard_gate_score_cap=hard_gate_score_cap,
            context=context_aggregate,
            cases=[],
            aggregate=_empty_aggregate(),
        )
        validate_sanitized_report(report)
        return report

    prediction_snapshot = load_proposal_predictions(predictions_path) if predictions_path is not None else None
    results = []
    for case, plan in zip(cases, plans, strict=True):
        if mode == "baseline" and plan.stats.status == "ready":
            prediction, xlsx = _baseline_prediction(case, plan)
            prediction_hash = _canonical_prediction_hash(prediction)
        elif mode == "predictions" and prediction_snapshot is not None:
            prediction = prediction_snapshot.predictions.get(case.case_id)
            prediction_hash = prediction_snapshot.hashes.get(case.case_id)
            xlsx = _prediction_workbook_status(prediction, prediction_snapshot.base_directory, case)
        else:
            prediction = None
            prediction_hash = None
            xlsx = ProposalXlsxStatus()
        results.append(
            score_proposal_case(
                case,
                prediction,
                prediction_hash,
                snapshot.artifacts,
                plan.stats,
                xlsx,
                effective_input_budget_tokens=effective_input_budget,
                pass_threshold=pass_threshold,
                hard_gate_score_cap=hard_gate_score_cap,
            )
        )
    report = ProposalEvaluationReport(
        mode=mode,
        scope=scope,
        dataset_fingerprint=snapshot.fingerprint,
        prediction_fingerprint=prediction_snapshot.fingerprint if prediction_snapshot is not None else None,
        runtime_context_window_tokens=runtime_context_window_tokens,
        reserved_prompt_tokens=reserved_prompt_tokens,
        reserved_output_tokens=reserved_output_tokens,
        effective_input_budget_tokens=effective_input_budget,
        model_context_limit_tokens=model_context_limit_tokens,
        pass_threshold=pass_threshold,
        hard_gate_score_cap=hard_gate_score_cap,
        context=context_aggregate,
        cases=results,
        aggregate=aggregate_proposal_results(results),
    )
    validate_sanitized_report(report)
    return report


def validate_sanitized_report(report: ProposalEvaluationReport) -> None:
    """보고서 계약에 민감 문자열용 필드가 다시 들어오면 저장 전에 실패시킨다."""

    def walk(value: object) -> None:
        if isinstance(value, dict):
            forbidden = _FORBIDDEN_REPORT_KEYS & {str(key).casefold() for key in value}
            if forbidden:
                raise ProposalEvaluationError("report_contains_forbidden_fields")
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(report.model_dump(mode="json"))


def write_proposal_evaluation_report(path: str | Path, report: ProposalEvaluationReport) -> None:
    """검증된 비식별 JSON만 명시한 로컬 경로에 기록한다."""

    validate_sanitized_report(report)
    output_path = Path(path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        raise ProposalEvaluationError("report_write_failed") from exc
