"""외부 기안 dataset을 읽기 전용으로 연결하는 preflight·oracle·채점 실행기."""

from __future__ import annotations

import hashlib
import io
import json
import math
import posixpath
import re
import unicodedata
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
    ProposalPredictionEvidenceFilter,
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


@dataclass(frozen=True)
class _RenderedCell:
    """본문을 노출하지 않고 OOXML 레이아웃을 검사하기 위한 메모리 셀."""

    reference: str
    row: int
    column: int
    value: str
    style_index: int


@dataclass(frozen=True)
class _WorksheetLayout:
    """시트별 셀, 병합, 명시적 행 높이의 최소 검사 표현."""

    cells: dict[tuple[int, int], _RenderedCell]
    merges: tuple[tuple[int, int, int, int], ...]
    explicit_height_rows: frozenset[int]


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


def proposal_reference_availability(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
) -> tuple[bool, list[str]]:
    """preflight·live·scorer가 공유하는 canonical generation reference eligibility."""

    missing_ids = [
        artifact_id
        for artifact_id in case.reference_artifact_ids
        if (artifact := artifacts.get(artifact_id)) is None
        or artifact.extraction_status not in {"ok", "partial"}
        or not artifact.chunks
    ]
    ready = bool(case.reference_artifact_ids) and not missing_ids
    return ready, missing_ids


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
    references_ready, missing_ids = proposal_reference_availability(case, artifacts)
    raw_chunks = [
        chunk
        for artifact_id in expected_ids
        if (artifact := artifacts.get(artifact_id)) is not None
        and artifact_id not in missing_ids
        for chunk in artifact.chunks
    ]
    reference_missing = not references_ready or not raw_chunks
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
    paths = _worksheet_paths(workbook)
    selected = next((paths[name] for name in sheet_names if name in paths), None)
    if selected is None:
        raise ValueError("worksheet_missing")
    return selected


def _worksheet_paths(workbook: zipfile.ZipFile) -> dict[str, str]:
    """workbook 관계를 해석해 외부 경로를 노출하지 않는 시트명→part 표를 만든다."""

    root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    sheets = root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet")
    relationships = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    targets = {
        item.attrib.get("Id", ""): item.attrib.get("Target", "")
        for item in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    paths: dict[str, str] = {}
    for sheet in sheets:
        name = sheet.attrib.get("name", "")
        relationship_id = sheet.attrib.get(f"{{{_OFFICE_REL_NS}}}id", "")
        target = targets.get(relationship_id, "").replace("\\", "/")
        if not name or not target:
            continue
        paths[name] = posixpath.normpath(
            target.lstrip("/") if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        )
    return paths


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


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _cell_coordinates(reference: str) -> tuple[int, int]:
    match = re.fullmatch(r"\$?([A-Za-z]+)\$?(\d+)", reference)
    if match is None:
        raise ValueError("cell_reference_invalid")
    return int(match.group(2)), _column_number(match.group(1))


def _worksheet_layout(workbook: zipfile.ZipFile, worksheet_path: str) -> _WorksheetLayout:
    shared = _shared_strings(workbook)
    root = ElementTree.fromstring(workbook.read(worksheet_path))
    cells: dict[tuple[int, int], _RenderedCell] = {}
    for cell in root.findall(f".//{{{_MAIN_NS}}}c"):
        reference = cell.attrib.get("r", "")
        if not reference:
            continue
        row, column = _cell_coordinates(reference)
        data_type = cell.attrib.get("t", "")
        if data_type == "inlineStr":
            value = "".join(node.text or "" for node in cell.findall(f".//{{{_MAIN_NS}}}t"))
        else:
            value_node = cell.find(f"{{{_MAIN_NS}}}v")
            raw_value = value_node.text if value_node is not None and value_node.text is not None else ""
            value = shared[int(raw_value)] if data_type == "s" and raw_value else raw_value
        cells[(row, column)] = _RenderedCell(
            reference=reference,
            row=row,
            column=column,
            value=value.replace("\r\n", "\n").replace("\r", "\n"),
            style_index=int(cell.attrib.get("s", "0")),
        )
    merges = []
    for merge in root.findall(f".//{{{_MAIN_NS}}}mergeCell"):
        cell_range = merge.attrib.get("ref", "")
        if ":" not in cell_range:
            continue
        start, end = cell_range.split(":", maxsplit=1)
        start_row, start_column = _cell_coordinates(start)
        end_row, end_column = _cell_coordinates(end)
        merges.append((start_row, start_column, end_row, end_column))
    explicit_height_rows = frozenset(
        int(row.attrib["r"])
        for row in root.findall(f".//{{{_MAIN_NS}}}row")
        if "r" in row.attrib and "ht" in row.attrib and row.attrib.get("customHeight", "1") not in {"0", "false"}
    )
    return _WorksheetLayout(
        cells=cells,
        merges=tuple(merges),
        explicit_height_rows=explicit_height_rows,
    )


def _style_capabilities(workbook: zipfile.ZipFile) -> tuple[frozenset[int], frozenset[int]]:
    root = ElementTree.fromstring(workbook.read("xl/styles.xml"))
    border_nodes = root.findall(f"{{{_MAIN_NS}}}borders/{{{_MAIN_NS}}}border")
    complete_border_ids = {
        index
        for index, border in enumerate(border_nodes)
        if all(
            (side := border.find(f"{{{_MAIN_NS}}}{name}")) is not None and bool(side.attrib.get("style"))
            for name in ("left", "right", "top", "bottom")
        )
    }
    wrapped: set[int] = set()
    bordered: set[int] = set()
    for index, style in enumerate(root.findall(f"{{{_MAIN_NS}}}cellXfs/{{{_MAIN_NS}}}xf")):
        alignment = style.find(f"{{{_MAIN_NS}}}alignment")
        if alignment is not None and alignment.attrib.get("wrapText", "0").casefold() in {"1", "true"}:
            wrapped.add(index)
        try:
            border_id = int(style.attrib.get("borderId", "0"))
        except ValueError:
            border_id = -1
        if border_id in complete_border_ids:
            bordered.add(index)
    return frozenset(wrapped), frozenset(bordered)


def _normalize_layout_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _contains_expected(cell_value: str, expected: str) -> bool:
    actual_value = _normalize_layout_text(cell_value)
    expected_value = _normalize_layout_text(expected)
    return bool(expected_value) and (actual_value == expected_value or actual_value.endswith(expected_value))


def _content_units(document: ProposalPredictedDocument) -> list[str]:
    units: list[str] = []
    for section in document.sections:
        units.append(section.heading)
        for block in section.blocks:
            if block.type == "paragraph":
                units.append(block.text)
            elif block.type == "list":
                units.extend(block.items)
        units.extend(section.missing_information)
    units.extend(document.missing_information)
    return [unit for unit in units if unit.strip()]


def _merged_across_columns(layout: _WorksheetLayout, cell: _RenderedCell) -> bool:
    return any(
        start_row <= cell.row <= end_row
        and start_column <= cell.column <= end_column
        and end_column > start_column
        for start_row, start_column, end_row, end_column in layout.merges
    )


def _matrix_cells(
    layout: _WorksheetLayout,
    headers: list[str],
    rows: list[list[str]],
    *,
    inline: bool,
) -> tuple[_RenderedCell, ...] | None:
    expected_matrix = [headers, *rows]
    width = len(headers)
    if width < 2:
        return None
    for (start_row, start_column), candidate in layout.cells.items():
        if not _contains_expected(candidate.value, headers[0]):
            continue
        patterns: list[list[tuple[int, int]]] = []
        if inline and start_column == 1 and width <= 6:
            base, remainder = divmod(26, width)
            spans = []
            current = 1
            for index in range(width):
                size = base + (1 if index < remainder else 0)
                spans.append((current, current + size - 1))
                current += size
            patterns.append(spans)
        patterns.append([(start_column + offset, start_column + offset) for offset in range(width)])
        for spans in patterns:
            matched: list[_RenderedCell] = []
            valid = True
            for row_offset, expected_row in enumerate(expected_matrix):
                row_number = start_row + row_offset
                for expected, (span_start, span_end) in zip(expected_row, spans, strict=True):
                    logical_cell = layout.cells.get((row_number, span_start))
                    if logical_cell is None or _normalize_layout_text(logical_cell.value) != _normalize_layout_text(expected):
                        valid = False
                        break
                    physical_cells = [layout.cells.get((row_number, column)) for column in range(span_start, span_end + 1)]
                    if any(cell is None for cell in physical_cells):
                        valid = False
                        break
                    if span_end > span_start and (
                        row_number,
                        span_start,
                        row_number,
                        span_end,
                    ) not in layout.merges:
                        valid = False
                        break
                    matched.extend(cell for cell in physical_cells if cell is not None)
                if not valid:
                    break
            if valid:
                return tuple(matched)
    return None


def inspect_proposal_workbook(
    content: bytes,
    fields: ProposalPredictedFields,
    *,
    source_sheet: str,
    projection: ProposalPredictionProjection | None = None,
    document: ProposalPredictedDocument | None = None,
    contract_version: str = "legacy-v1",
) -> ProposalXlsxStatus:
    """OOXML을 메모리에서 읽고 결과 문자열 대신 boolean과 hash만 반환한다."""

    status = ProposalXlsxStatus(
        generation_status="ok" if content else "not_generated",
        contract_version=contract_version,
        layout_status="failed" if contract_version == "proposal-xlsx-v2" else "not_applicable",
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
            if contract_version == "proposal-xlsx-v2" and document is not None:
                _inspect_v2_workbook(workbook, status, worksheet_path, document)
                return status
            lines = fields.body.replace("\r\n", "\n").replace("\r", "\n").strip().splitlines()
            actual_lines = [cells.get(f"A{15 + index}", "") for index in range(len(lines))]
            status.body_written = actual_lines == lines
    except (KeyError, OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return status
    return status


def _inspect_v2_workbook(
    workbook: zipfile.ZipFile,
    status: ProposalXlsxStatus,
    main_path: str,
    document: ProposalPredictedDocument,
) -> None:
    """V2 본문과 표가 실제 OOXML geometry로 보존됐는지 진단한다."""

    paths = _worksheet_paths(workbook)
    layouts = {name: _worksheet_layout(workbook, path) for name, path in paths.items()}
    main_name = next(name for name, path in paths.items() if path == main_path)
    main_layout = layouts[main_name]
    wrapped_styles, bordered_styles = _style_capabilities(workbook)

    units = _content_units(document)
    available_main_cells = [cell for cell in main_layout.cells.values() if cell.row >= 15 and cell.value.strip()]
    tables = [
        block
        for section in document.sections
        for block in section.blocks
        if block.type == "table"
    ]
    matched_table_cells: list[_RenderedCell] = []
    table_rows: set[tuple[str, int]] = set()
    inline_count = 0
    appendix_count = 0
    rendered_count = 0
    table_geometry_valid = True
    table_border_valid = True
    wide_table_expected = False
    for table in tables:
        width = len(table.headers)
        wide_table_expected = wide_table_expected or width > 6
        matched_name: str | None = None
        matched_cells: tuple[_RenderedCell, ...] | None = None
        candidate_names = ["세부내용"] if width > 6 else [main_name, "세부내용"]
        for name in candidate_names:
            layout = layouts.get(name)
            if layout is None:
                continue
            found = _matrix_cells(layout, table.headers, table.rows, inline=name == main_name)
            if found is not None:
                matched_name = name
                matched_cells = found
                break
        if matched_name is None or matched_cells is None:
            table_geometry_valid = False
            table_border_valid = False
            continue
        rendered_count += 1
        if matched_name == main_name:
            inline_count += 1
        else:
            appendix_count += 1
        layout = layouts[matched_name]
        matched_table_cells.extend(matched_cells)
        table_rows.update((matched_name, cell.row) for cell in matched_cells)
        if any(cell.style_index not in bordered_styles for cell in matched_cells):
            table_border_valid = False

    main_table_rows = {row for name, row in table_rows if name == main_name}
    editable_text_cells = [cell for cell in available_main_cells if cell.row not in main_table_rows]
    joined_text = " ".join(cell.value for cell in sorted(editable_text_cells, key=lambda item: (item.row, item.column)))
    rendered_content_count = sum(_normalize_layout_text(unit) in _normalize_layout_text(joined_text) for unit in units)

    fake_rows = {
        _normalize_layout_text(" | ".join(row))
        for table in tables
        for row in [table.headers, *table.rows]
    }
    status.fake_pipe_table_absent = not any(
        _normalize_layout_text(cell.value) in fake_rows and "|" in cell.value for cell in available_main_cells
    )
    omitted_content_count = max(0, len(units) - rendered_content_count)
    omission_cell = next(
        (cell for cell in editable_text_cells if "생략" in _normalize_layout_text(cell.value)),
        None,
    )
    omission_notified = bool(omitted_content_count and omission_cell is not None)
    content_rows = {(main_name, cell.row) for cell in editable_text_cells}
    status.body_text_single_line_valid = all(
        cell.style_index not in wrapped_styles and "\n" not in cell.value and "\r" not in cell.value
        for cell in editable_text_cells
    )
    status.body_editable_unmerged_valid = all(
        not _merged_across_columns(main_layout, cell) for cell in editable_text_cells
    )
    status.body_default_height_valid = all(
        row not in layouts[name].explicit_height_rows for name, row in content_rows
    )
    status.table_wrap_valid = all(cell.style_index in wrapped_styles for cell in matched_table_cells)
    status.table_explicit_row_height_valid = all(
        row in layouts[name].explicit_height_rows for name, row in table_rows
    )
    status.table_geometry_valid = table_geometry_valid
    status.table_border_valid = table_border_valid
    status.expected_table_count = len(tables)
    status.rendered_table_count = rendered_count
    status.inline_table_count = inline_count
    status.appendix_table_count = appendix_count
    status.expected_content_block_count = len(units)
    status.rendered_content_block_count = rendered_content_count
    status.omitted_content_block_count = omitted_content_count
    status.content_omission_notified = omission_notified
    status.body_written = (
        (omitted_content_count == 0 or omission_notified)
        and rendered_count == len(tables)
    )

    if wide_table_expected and "세부내용" not in layouts:
        status.appendix_status = "missing"
    elif wide_table_expected or appendix_count:
        status.appendix_status = "valid" if appendix_count and rendered_count == len(tables) else "invalid"
    else:
        status.appendix_status = "not_required"
    status.layout_status = (
        "passed"
        if all(
            (
                status.body_written,
                status.body_text_single_line_valid,
                status.body_editable_unmerged_valid,
                status.body_default_height_valid,
                status.table_geometry_valid,
                status.table_border_valid,
                status.table_wrap_valid,
                status.table_explicit_row_height_valid,
                status.fake_pipe_table_absent,
                status.appendix_status in {"not_required", "valid"},
            )
        )
        else "failed"
    )


def _prediction_workbook_status(
    prediction: ProposalPrediction | None,
    base_directory: Path | None,
    case: ProposalDatasetCaseInput,
) -> ProposalXlsxStatus:
    if prediction is None or prediction.fields is None or not prediction.workbook_path or base_directory is None:
        contract = prediction.xlsx_contract if prediction is not None else "legacy-v1"
        return ProposalXlsxStatus(
            contract_version=contract,
            layout_status="failed" if contract == "proposal-xlsx-v2" else "not_applicable",
        )
    workbook_path = Path(prediction.workbook_path)
    resolved = workbook_path if workbook_path.is_absolute() else base_directory / workbook_path
    try:
        content = resolved.read_bytes()
    except OSError:
        return ProposalXlsxStatus(
            contract_version=prediction.xlsx_contract,
            layout_status="failed" if prediction.xlsx_contract == "proposal-xlsx-v2" else "not_applicable",
        )
    return inspect_proposal_workbook(
        content,
        prediction.fields,
        source_sheet=case.expected.source_sheet if case.expected else "기안지",
        projection=prediction.projection,
        document=prediction.document,
        contract_version=prediction.xlsx_contract,
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
        proposal_type=case.proposal_type_label,
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
    artifacts: dict[str, ProposalDatasetArtifact],
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
    xlsx_contract = "legacy-v1"
    try:
        from smb_finder.playground import proposal_draft as proposal_module

        template_path = Path(__file__).resolve().parents[1] / "playground" / "templates" / "proposal_draft.xlsx"
        core_document = proposal_module.ProposalDocumentV2.model_validate(document.model_dump())
        if case.proposal_type_label == "event_attendance":
            core_document = proposal_module.normalize_proposal_document(core_document, "event_attendance")
            document = ProposalPredictedDocument.model_validate(core_document.model_dump())
            source_line_count = _document_projection_line_count(document)
        projected = proposal_module.project_document_to_legacy_fields(core_document)
        fields = ProposalPredictedFields.model_validate(projected.model_dump())
        output_line_count = len(fields.body.splitlines())
        projection = ProposalPredictionProjection(
            source_line_count=source_line_count,
            output_line_count=output_line_count,
            omitted_line_count=max(0, source_line_count - output_line_count),
            truncated=source_line_count > output_line_count or "생략" in fields.body,
        )
        renderer = getattr(proposal_module, "render_proposal_workbook", None)
        if renderer is None:
            generated = proposal_module.insert_proposal_fields(template_path.read_bytes(), projected)
        else:
            generated = renderer(template_path.read_bytes(), core_document)
            xlsx_contract = "proposal-xlsx-v2"
        xlsx = inspect_proposal_workbook(
            generated,
            fields,
            source_sheet=case.expected.source_sheet,
            projection=projection,
            document=document,
            contract_version=xlsx_contract,
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
        (
            xlsx.generated,
            xlsx.zip_valid,
            xlsx.required_parts_present,
            xlsx.title_written,
            xlsx.approval_written,
            xlsx.body_written,
            xlsx.layout_status in {"not_applicable", "passed"},
        )
    )
    excluded_chunks = [
        chunk.chunk_id
        for artifact_id in case.excluded_post_event_artifact_ids
        if (artifact := artifacts.get(artifact_id)) is not None
        for chunk in artifact.chunks
    ]
    prediction = ProposalPrediction(
        case_id=case.case_id,
        fields=fields,
        document=document,
        requested_proposal_type=case.proposal_type_label,
        resolved_proposal_type=case.proposal_type_label,
        proposal_type_source="user" if case.proposal_type_label is not None else None,
        evidence_filter=ProposalPredictionEvidenceFilter(
            input_document_count=len(case.reference_artifact_ids) + len(case.excluded_post_event_artifact_ids),
            included_document_count=len(case.reference_artifact_ids),
            excluded_document_count=len(case.excluded_post_event_artifact_ids),
            input_citation_count=len(plan.packed_chunk_ids) + len(excluded_chunks),
            included_citation_count=len(plan.packed_chunk_ids),
            excluded_citation_count=len(excluded_chunks),
            excluded_reason_counts={"post_event": len(case.excluded_post_event_artifact_ids)},
        ),
        evidence_artifact_ids=case.reference_artifact_ids,
        evidence_chunk_ids=plan.packed_chunk_ids,
        excluded_evidence_artifact_ids=case.excluded_post_event_artifact_ids,
        excluded_evidence_chunk_ids=excluded_chunks,
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
        xlsx_contract=xlsx_contract,
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
    """preflight, oracle 계약 검사 또는 저장된 prediction을 평가한다."""

    if runtime_context_window_tokens <= reserved_prompt_tokens + reserved_output_tokens:
        raise ProposalEvaluationError("runtime_context_budget_invalid")
    if model_context_limit_tokens is not None and model_context_limit_tokens <= reserved_prompt_tokens + reserved_output_tokens:
        raise ProposalEvaluationError("model_context_limit_invalid")
    prediction_mode = mode in {"predictions", "live"}
    oracle_mode = mode in {"oracle_contract_check", "baseline"}
    if prediction_mode and predictions_path is None:
        raise ProposalEvaluationError("predictions_required")
    if not prediction_mode and predictions_path is not None:
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
            evaluation_kind="preflight",
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
        if oracle_mode and plan.stats.status == "ready":
            prediction, xlsx = _baseline_prediction(case, plan, snapshot.artifacts)
            prediction_hash = _canonical_prediction_hash(prediction)
        elif prediction_mode and prediction_snapshot is not None:
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
        evaluation_kind=(
            "oracle_contract_check"
            if oracle_mode
            else "live_predictions"
            if mode == "live"
            else "supplied_predictions"
        ),
        deprecated_baseline_alias_used=mode == "baseline",
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
    report = report.model_copy(
        update={
            "product_quality_eligible": bool(results)
            and not oracle_mode
            and all(item.coverage.product_quality_eligible for item in results)
        }
    )
    validate_sanitized_report(report)
    return report


def validate_sanitized_report(report: BaseModel) -> None:
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


def write_proposal_evaluation_report(
    path: str | Path,
    report: ProposalEvaluationReport,
    *,
    exclusive: bool = False,
) -> None:
    """검증된 비식별 JSON만 명시한 로컬 경로에 기록한다."""

    validate_sanitized_report(report)
    output_path = Path(path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if exclusive:
            with output_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(report.model_dump_json(indent=2))
                stream.write("\n")
        else:
            output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        raise ProposalEvaluationError("report_write_failed") from exc
