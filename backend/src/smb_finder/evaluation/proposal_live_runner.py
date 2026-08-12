"""실제 로컬 기안 생성기를 호출하되 SMB와 gold를 격리하는 live 평가 실행기."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from smb_finder.config import Settings, load_settings
from smb_finder.models import DocumentCitation, RetrievalScores
from smb_finder.playground.proposal_draft import (
    LocalProposalDraftGenerator,
    ProposalDocumentV2,
    ProposalDraftError,
    ProposalDraftResult,
    apply_proposal_evidence_verification,
    normalize_proposal_document,
    project_document_to_legacy_fields,
    render_proposal_workbook,
    resolve_proposal_type,
)
from smb_finder.playground.proposal_evidence import filter_proposal_evidence

from .proposal_models import (
    EvaluationScope,
    ProposalDatasetArtifact,
    ProposalDatasetCaseInput,
    ProposalLiveArtifactReport,
    ProposalLiveCaseReport,
    ProposalLiveRunReport,
    ProposalPredictedDocument,
    ProposalPredictedFields,
    ProposalPrediction,
    ProposalPredictionContext,
    ProposalPredictionContextUsage,
    ProposalPredictionCompletion,
    ProposalPredictionEvidenceFilter,
    ProposalPredictionProjection,
    ProposalPredictionTimings,
    ProposalToolStageEvent,
)
from .proposal_runner import (
    ProposalEvaluationError,
    load_proposal_dataset,
    proposal_reference_availability,
    validate_sanitized_report,
)


_UUID_NAMESPACE = UUID("fa14aa91-e3bc-4d40-9f39-c67df8072df7")
_PROCESS_RUN_ID = uuid4().hex
_PROCESS_STARTED_AT_NS = time.time_ns()
_HOST_SHA256 = hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()
_GENERATION_CONTRACT_VERSION = "proposal-live-generation-contract-v2"


class _ProposalGenerator(Protocol):
    def generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        proposal_type: str = "general",
    ) -> ProposalDraftResult: ...

    def close(self) -> None: ...


class _StageError(RuntimeError):
    """원문 예외를 보고서로 넘기지 않고 실패 stage와 실측 시간만 보존한다."""

    def __init__(
        self,
        cause: Exception,
        stage: str,
        timings: ProposalPredictionTimings,
    ) -> None:
        super().__init__(stage)
        self.cause = cause
        self.stage = stage
        self.timings = timings


def _stable_uuid(kind: str, stable_id: str) -> UUID:
    return uuid5(_UUID_NAMESPACE, f"{kind}:{stable_id}")


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _scope_cases(cases: list[ProposalDatasetCaseInput], scope: EvaluationScope) -> list[ProposalDatasetCaseInput]:
    if scope == "all":
        return cases
    return [case for case in cases if case.evaluation_scope == scope]


def _expected_reference_skip(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
) -> bool:
    """offline preflight와 같은 조건으로 dataset이 이미 예상한 reference skip을 판정한다."""

    ready, _ = proposal_reference_availability(case, artifacts)
    return not ready


def _percentile(values: list[float], percentile: float) -> float:
    numbers = sorted(value for value in values if math.isfinite(value))
    if not numbers:
        return 0.0
    position = (len(numbers) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return numbers[lower]
    return numbers[lower] + (numbers[upper] - numbers[lower]) * (position - lower)


def _candidate_citations(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
) -> tuple[list[DocumentCitation], dict[UUID, tuple[str, str]], list[str]]:
    """reference와 예상 제외 문서만 citation으로 만들고 target은 사전 차단한다."""

    candidate_ids = _ordered_unique(
        [*case.reference_artifact_ids, *case.excluded_post_event_artifact_ids]
    )
    references_ready, _ = proposal_reference_availability(case, artifacts)
    if not references_ready:
        raise ProposalEvaluationError("candidate_missing")
    if case.target_artifact_id in candidate_ids:
        raise ProposalEvaluationError("target_candidate_leakage")
    citations: list[DocumentCitation] = []
    citation_identity: dict[UUID, tuple[str, str]] = {}
    available_candidate_ids: list[str] = []
    reference_ids = set(case.reference_artifact_ids)
    for artifact_id in candidate_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None or artifact.extraction_status not in {"ok", "partial"} or not artifact.chunks:
            if artifact_id in reference_ids:
                raise ProposalEvaluationError("candidate_missing")
            continue
        if artifact.is_target:
            raise ProposalEvaluationError("target_candidate_leakage")
        available_candidate_ids.append(artifact_id)
        doc_id = _stable_uuid("document", artifact_id)
        revision_id = _stable_uuid("revision", f"{artifact_id}:{artifact.content_sha256}")
        title = artifact.file_name or artifact.role or artifact.artifact_id
        for chunk in sorted(artifact.chunks, key=lambda item: (item.ordinal, item.chunk_id)):
            chunk_uuid = _stable_uuid("chunk", chunk.chunk_id)
            citations.append(
                DocumentCitation(
                    index=len(citations) + 1,
                    doc_id=doc_id,
                    revision_id=revision_id,
                    chunk_id=chunk_uuid,
                    title=title,
                    section_path=chunk.anchor.section_path(),
                    location={},
                    excerpt=chunk.text,
                    scores=RetrievalScores(rrf=1.0 / (60 + len(citations) + 1)),
                )
            )
            citation_identity[chunk_uuid] = (artifact_id, chunk.chunk_id)
    return citations, citation_identity, available_candidate_ids


def _projection_line_count(document: ProposalPredictedDocument) -> int:
    count = len(document.missing_information)
    for section in document.sections:
        count += 1 + len(section.missing_information)
        for block in section.blocks:
            if block.type == "paragraph":
                count += 1
            elif block.type == "list":
                count += len(block.items)
            else:
                count += len(block.rows) + 1
    return count


def _prediction_line(prediction: ProposalPrediction) -> tuple[str, str]:
    line = prediction.model_dump_json(exclude_none=True)
    return line, hashlib.sha256(line.encode("utf-8")).hexdigest()


def _case_generation_fingerprint(case: ProposalDatasetCaseInput) -> str:
    payload = {
        "case_id": case.case_id,
        "instruction": case.instruction,
        "target_artifact_id": case.target_artifact_id,
        "reference_artifact_ids": case.reference_artifact_ids,
        "excluded_post_event_artifact_ids": case.excluded_post_event_artifact_ids,
        "requested_proposal_type": "auto",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _generation_config_fingerprint(
    settings: Settings,
    generator_type: type[object],
) -> str:
    """원문 endpoint/model을 저장하지 않고 생성 결과에 영향을 주는 설정을 결속한다."""

    implementation_sha256: str | None = None
    try:
        source_path = inspect.getsourcefile(generator_type)
        if source_path:
            implementation_sha256 = hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
    except OSError:
        implementation_sha256 = None
    identity = f"{generator_type.__module__}.{generator_type.__qualname__}"
    payload = {
        "contract_version": _GENERATION_CONTRACT_VERSION,
        "provider_identity_sha256": hashlib.sha256(b"ollama-local").hexdigest(),
        "endpoint_identity_sha256": hashlib.sha256(
            settings.ollama_base_url.strip().rstrip("/").encode("utf-8")
        ).hexdigest(),
        "model_identity_sha256": hashlib.sha256(
            settings.llmops_chat_model.strip().encode("utf-8")
        ).hexdigest(),
        "generator_identity_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "generator_implementation_sha256": implementation_sha256,
        "proposal_llm_num_ctx": settings.proposal_llm_num_ctx,
        "proposal_llm_max_tokens": settings.proposal_llm_max_tokens,
        "proposal_llm_timeout_ms": settings.proposal_llm_timeout_ms,
        "proposal_context_max_chars": settings.proposal_context_max_chars,
        "proposal_context_per_document_chars": settings.proposal_context_per_document_chars,
        "proposal_context_max_citations": settings.proposal_context_max_citations,
        "prompt_reserve_tokens": 1024,
        "effective_input_tokens": max(
            0,
            settings.proposal_llm_num_ctx - settings.proposal_llm_max_tokens - 1024,
        ),
        "temperature": 0,
        "think": False,
        "response_format": "json",
        "document_schema": "proposal-document-v2",
        "context_usage_schema": "proposal-context-usage-v1",
        "evidence_filter_schema": "proposal-evidence-filter-v1",
        "xlsx_contract": "proposal-xlsx-v2",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_manifest(
    *,
    dataset_fingerprint: str,
    scope: EvaluationScope,
    template_sha256: str,
    generation_config_fingerprint: str,
    cases: list[ProposalDatasetCaseInput],
) -> dict[str, object]:
    return {
        "schema_version": "proposal-live-checkpoint-manifest-v2",
        "dataset_fingerprint": dataset_fingerprint,
        "scope": scope,
        "template_sha256": template_sha256,
        "generation_config_fingerprint": generation_config_fingerprint,
        "case_generation_fingerprints": {
            case.case_id: _case_generation_fingerprint(case) for case in cases
        },
    }


def _initialize_or_validate_manifest(
    checkpoint: Path,
    manifest: dict[str, object],
    *,
    resume: bool,
) -> None:
    manifest_path = checkpoint.with_name(f"{checkpoint.name}.manifest.json")
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if checkpoint.exists() or manifest_path.exists():
        if not resume:
            raise ProposalEvaluationError("checkpoint_exists")
        if not checkpoint.exists() or not manifest_path.exists():
            raise ProposalEvaluationError("checkpoint_manifest_mismatch")
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProposalEvaluationError("checkpoint_manifest_mismatch") from exc
        if existing != manifest:
            raise ProposalEvaluationError("checkpoint_manifest_mismatch")
        return
    try:
        checkpoint.open("x", encoding="utf-8").close()
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.write("\n")
    except OSError as exc:
        raise ProposalEvaluationError("checkpoint_error") from exc


def _append_prediction(path: Path, prediction: ProposalPrediction) -> str:
    line, digest = _prediction_line(prediction)
    _append_prediction_line(path, line)
    return digest


def _append_prediction_line(path: Path, line: str) -> None:
    """journal의 검증된 원문 prediction 한 줄을 그대로 durable append한다."""

    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ProposalEvaluationError("checkpoint_error") from exc


def _load_resume_predictions(path: Path) -> dict[str, ProposalPrediction]:
    if not path.exists():
        return {}
    predictions: dict[str, ProposalPrediction] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        for line in lines:
            if not line.strip():
                continue
            prediction = ProposalPrediction.model_validate_json(line)
            if prediction.case_id in predictions:
                raise ProposalEvaluationError("checkpoint_error")
            predictions[prediction.case_id] = prediction
    except (OSError, ValidationError, ValueError) as exc:
        if isinstance(exc, ProposalEvaluationError):
            raise
        raise ProposalEvaluationError("checkpoint_error") from exc
    return predictions


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProposalEvaluationError("workbook_error") from exc


def _stage_workbook(path: Path, content: bytes) -> tuple[str, Path | None]:
    """checkpoint 전 workbook을 같은 디렉터리의 결정론적 pending 파일에 둔다."""

    digest = hashlib.sha256(content).hexdigest()
    pending = path.with_name(f".{path.name}.{digest}.pending")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _file_sha256(path) != digest:
                raise ProposalEvaluationError("workbook_error")
            return digest, None
        if pending.exists():
            if _file_sha256(pending) != digest:
                raise ProposalEvaluationError("workbook_error")
            return digest, pending
        with pending.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ProposalEvaluationError("workbook_error") from exc
    return digest, pending


def _publish_staged_workbook(path: Path, pending: Path | None, expected_sha256: str) -> None:
    """pending workbook을 hard-link로 신규 target에만 공개하고 hash를 다시 검증한다."""

    if path.exists():
        if _file_sha256(path) != expected_sha256:
            raise ProposalEvaluationError("workbook_error")
    elif pending is None or not pending.exists():
        raise ProposalEvaluationError("workbook_error")
    else:
        try:
            os.link(pending, path)
        except FileExistsError:
            if _file_sha256(path) != expected_sha256:
                raise ProposalEvaluationError("workbook_error")
        except OSError as exc:
            raise ProposalEvaluationError("workbook_error") from exc


def _manifest_sha256(manifest: dict[str, object]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _pending_journal_paths(checkpoint: Path, case_id: str) -> tuple[Path, Path]:
    stem = f".{checkpoint.name}.{case_id}.pending-journal"
    return checkpoint.with_name(f"{stem}.json"), checkpoint.with_name(f"{stem}.complete")


def _write_exclusive_fsynced(path: Path, content: str, error_code: str) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ProposalEvaluationError(error_code) from exc


def _write_pending_journal(
    checkpoint: Path,
    case: ProposalDatasetCaseInput,
    manifest: dict[str, object],
    prediction: ProposalPrediction,
) -> tuple[Path, str]:
    """LLM 성공 결과와 workbook hash를 checkpoint보다 먼저 외부 raw journal에 고정한다."""

    clarification_pending = prediction.completion is not None and prediction.completion.status == "needs_clarification"
    if prediction.status != "ok" or (not prediction.workbook_sha256 and not clarification_pending):
        raise ProposalEvaluationError("pending_journal_error")
    line, prediction_sha256 = _prediction_line(prediction)
    payload = {
        "schema_version": "proposal-live-pending-journal-v1",
        "case_id": case.case_id,
        "manifest_sha256": _manifest_sha256(manifest),
        "case_generation_fingerprint": _case_generation_fingerprint(case),
        "prediction_line": line,
        "prediction_sha256": prediction_sha256,
        "workbook_sha256": prediction.workbook_sha256,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    journal_path, _ = _pending_journal_paths(checkpoint, case.case_id)
    _write_exclusive_fsynced(journal_path, encoded, "pending_journal_error")
    return journal_path, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_pending_journal(
    checkpoint: Path,
    case: ProposalDatasetCaseInput,
    manifest: dict[str, object],
) -> tuple[Path, str, ProposalPrediction, str]:
    journal_path, _ = _pending_journal_paths(checkpoint, case.case_id)
    try:
        raw = journal_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        line = payload["prediction_line"]
        prediction = ProposalPrediction.model_validate_json(line)
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        raise ProposalEvaluationError("pending_journal_mismatch") from exc
    expected = {
        "schema_version": "proposal-live-pending-journal-v1",
        "case_id": case.case_id,
        "manifest_sha256": _manifest_sha256(manifest),
        "case_generation_fingerprint": _case_generation_fingerprint(case),
        "prediction_line": line,
        "prediction_sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
        "workbook_sha256": prediction.workbook_sha256,
    }
    if (
        not isinstance(payload, dict)
        or payload != expected
        or prediction.case_id != case.case_id
        or prediction.status != "ok"
        or (
            not prediction.workbook_sha256
            and not (prediction.completion is not None and prediction.completion.status == "needs_clarification")
        )
    ):
        raise ProposalEvaluationError("pending_journal_mismatch")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return journal_path, line, prediction, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mark_pending_journal_complete(checkpoint: Path, case_id: str, journal_sha256: str) -> None:
    _, complete_path = _pending_journal_paths(checkpoint, case_id)
    payload = json.dumps(
        {
            "schema_version": "proposal-live-pending-journal-complete-v1",
            "journal_sha256": journal_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if complete_path.exists():
        try:
            if complete_path.read_text(encoding="utf-8").strip() != payload:
                raise ProposalEvaluationError("pending_journal_mismatch")
        except OSError as exc:
            raise ProposalEvaluationError("pending_journal_mismatch") from exc
        return
    _write_exclusive_fsynced(complete_path, payload, "pending_journal_error")


def _recover_pending_prediction(
    checkpoint: Path,
    artifact_root: Path,
    case: ProposalDatasetCaseInput,
    manifest: dict[str, object],
    resumed: dict[str, ProposalPrediction],
) -> ProposalPrediction | None:
    """durable journal을 checkpoint와 XLSX로 publish하며 LLM 재호출을 막는다."""

    journal_path, complete_path = _pending_journal_paths(checkpoint, case.case_id)
    if not journal_path.exists():
        return None
    _, line, prediction, journal_sha256 = _load_pending_journal(checkpoint, case, manifest)
    existing = resumed.get(case.case_id)
    if existing is None:
        if complete_path.exists():
            raise ProposalEvaluationError("pending_journal_mismatch")
        _append_prediction_line(checkpoint, line)
        resumed[case.case_id] = prediction
    elif _prediction_line(existing)[0] != line:
        raise ProposalEvaluationError("pending_journal_mismatch")
    workbook_target = artifact_root / f"{case.case_id}.xlsx"
    expected_sha256 = prediction.workbook_sha256
    if expected_sha256:
        pending = workbook_target.with_name(f".{workbook_target.name}.{expected_sha256}.pending")
        try:
            _publish_staged_workbook(workbook_target, pending, expected_sha256)
        except ProposalEvaluationError as exc:
            raise ProposalEvaluationError("pending_publish_recovery_failed") from exc
    elif prediction.completion is None or prediction.completion.status != "needs_clarification":
        raise ProposalEvaluationError("pending_journal_mismatch")
    _mark_pending_journal_complete(checkpoint, case.case_id, journal_sha256)
    return prediction


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, _StageError):
        return _failure_code(exc.cause)
    if isinstance(exc, ProposalEvaluationError):
        code = str(exc).split(":", maxsplit=1)[0]
        if code == "candidate_missing":
            return "reference_missing"
        if code == "packed_context_metadata_invalid":
            return "generation_error"
        if code in {
            "instruction_missing",
            "target_candidate_leakage",
            "evidence_filter_contract_failed",
            "workbook_error",
            "checkpoint_error",
        }:
            return code
    if isinstance(exc, ProposalDraftError) and exc.code == "proposal_context_limit_exceeded":
        return "proposal_context_limit_exceeded"
    if isinstance(exc, (OSError, ValueError, ValidationError)):
        return "workbook_error"
    return "generation_error"


def _validated_packed_metadata(
    result: ProposalDraftResult,
    document: ProposalDocumentV2,
    usage: ProposalPredictionContextUsage,
    identity: dict[UUID, tuple[str, str]],
    filtered_citations: list[DocumentCitation],
) -> tuple[dict[str, str], str]:
    """core가 공개한 최종 retry 기준 packed map/hash를 정확히 stable ID로 변환한다."""

    raw_map = result.packed_citation_map
    context_sha256 = result.packed_context_sha256
    if (
        usage.packed_citation_count <= 0
        or len(raw_map) != usage.packed_citation_count
        or not context_sha256
        or len(context_sha256) != 64
        or any(character not in "0123456789abcdef" for character in context_sha256)
    ):
        raise ProposalEvaluationError("packed_context_metadata_invalid")
    filtered_ids = {item.chunk_id for item in filtered_citations}
    stable_map: dict[str, str] = {}
    for citation_id, raw_chunk_id in raw_map:
        try:
            chunk_uuid = UUID(raw_chunk_id)
        except ValueError as exc:
            raise ProposalEvaluationError("packed_context_metadata_invalid") from exc
        if citation_id in stable_map or chunk_uuid not in identity or chunk_uuid not in filtered_ids:
            raise ProposalEvaluationError("packed_context_metadata_invalid")
        stable_map[citation_id] = identity[chunk_uuid][1]
    document_citations = {
        citation_id
        for section in document.sections
        for citation_id in section.citations
    }
    if not document_citations or not document_citations <= set(stable_map):
        raise ProposalEvaluationError("packed_context_metadata_invalid")
    return stable_map, context_sha256


def _artifact_reports(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
    included_pairs: set[tuple[str, str]],
) -> list[ProposalLiveArtifactReport]:
    reports: list[ProposalLiveArtifactReport] = []
    for candidate_role, artifact_ids in (
        ("reference", case.reference_artifact_ids),
        ("expected_excluded", case.excluded_post_event_artifact_ids),
    ):
        for artifact_id in _ordered_unique(artifact_ids):
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                continue
            output_count = sum((artifact_id, chunk.chunk_id) in included_pairs for chunk in artifact.chunks)
            available = artifact.extraction_status in {"ok", "partial"} and bool(artifact.chunks)
            reports.append(
                ProposalLiveArtifactReport(
                    artifact_id=artifact_id,
                    candidate_role=candidate_role,
                    filter_status="included" if output_count else "excluded" if available else "unavailable",
                    input_chunk_count=len(artifact.chunks),
                    output_chunk_count=output_count,
                    content_sha256=artifact.content_sha256,
                )
            )
    return reports


def _error_prediction(
    case: ProposalDatasetCaseInput,
    exc: Exception,
    *,
    elapsed_ms: float,
) -> ProposalPrediction:
    """실패 stage와 시간만 담은 checkpoint prediction을 만든다."""

    failure_code = _failure_code(exc)
    skipped = failure_code == "reference_missing"
    if isinstance(exc, _StageError):
        timings = exc.timings
        failed_stage = exc.stage
        cause = exc.cause
    else:
        timings = ProposalPredictionTimings(total_ms=elapsed_ms)
        failed_stage = "evidence"
        cause = exc
    stages = ("evidence", "llm", "workbook", "save")
    failed_index = stages.index(failed_stage)
    events = [
        ProposalToolStageEvent(
            stage=stage,
            status="ok" if index < failed_index else "error" if index == failed_index else "skipped",
            elapsed_ms=float(getattr(timings, f"{stage}_ms")),
        )
        for index, stage in enumerate(stages)
    ]
    context_usage = None
    if isinstance(cause, ProposalDraftError) and cause.context_usage is not None:
        context_usage = ProposalPredictionContextUsage.model_validate(cause.context_usage.model_dump())
    return ProposalPrediction(
        case_id=case.case_id,
        status="skipped" if skipped else "error",
        context_usage=context_usage,
        tool_events=events,
        timings_ms=timings.model_copy(update={"total_ms": max(timings.total_ms, elapsed_ms)}),
        failure_code="reference_missing" if skipped else failure_code,
    )


def _build_prediction(
    case: ProposalDatasetCaseInput,
    artifacts: dict[str, ProposalDatasetArtifact],
    generator: _ProposalGenerator,
    template: bytes,
    workbook_path: Path,
    checkpoint_directory: Path,
) -> tuple[ProposalPrediction, ProposalLiveCaseReport, Path | None]:
    started = time.perf_counter()
    if not case.instruction.strip():
        raise ProposalEvaluationError("instruction_missing")
    evidence_started = time.perf_counter()
    citations, identity, candidate_ids = _candidate_citations(case, artifacts)
    resolution = resolve_proposal_type("auto", case.instruction, citations)
    filtered = filter_proposal_evidence(
        citations,
        proposal_type=resolution.proposal_type,
        instruction=case.instruction,
    )
    included_pairs = {identity[item.chunk_id] for item in filtered.citations}
    included_artifact_ids = {artifact_id for artifact_id, _ in included_pairs}
    evidence_ms = (time.perf_counter() - evidence_started) * 1000

    llm_started = time.perf_counter()
    try:
        result = generator.generate(
            case.instruction,
            filtered.citations,
            proposal_type=resolution.proposal_type,
        )
    except Exception as exc:
        llm_ms = (time.perf_counter() - llm_started) * 1000
        raise _StageError(
            exc,
            "llm",
            ProposalPredictionTimings(
                evidence_ms=evidence_ms,
                llm_ms=llm_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            ),
        ) from exc
    if result.document is None:
        raise ProposalEvaluationError("generation_error")
    document: ProposalDocumentV2 = normalize_proposal_document(result.document, resolution.proposal_type)
    usage = ProposalPredictionContextUsage.model_validate(result.context_usage.model_dump())
    try:
        citation_map, packed_context_sha256 = _validated_packed_metadata(
            result,
            document,
            usage,
            identity,
            filtered.citations,
        )
        completion = None
        assessor = getattr(generator, "assess", None)
        if callable(assessor):
            verification = assessor(
                case.instruction,
                document,
                filtered.citations,
                proposal_type=resolution.proposal_type,
                allow_questions=True,
            )
            document, generated_completion = apply_proposal_evidence_verification(
                document,
                verification,
                allow_questions=True,
                clarification_round=0,
            )
            completion = ProposalPredictionCompletion(
                status=generated_completion.status,
                question_field_keys=[question.field_key for question in generated_completion.questions],
                supported_claim_count=generated_completion.supported_claim_count,
                derived_claim_count=generated_completion.derived_claim_count,
                omitted_claim_count=generated_completion.omitted_claim_count,
                conflicting_claim_count=generated_completion.conflicting_claim_count,
            )
    except Exception as exc:
        llm_ms = (time.perf_counter() - llm_started) * 1000
        raise _StageError(
            exc,
            "llm",
            ProposalPredictionTimings(
                evidence_ms=evidence_ms,
                llm_ms=llm_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            ),
        ) from exc
    llm_ms = (time.perf_counter() - llm_started) * 1000
    fields = project_document_to_legacy_fields(document)
    predicted_document = ProposalPredictedDocument.model_validate(document.model_dump())
    predicted_fields = ProposalPredictedFields.model_validate(fields.model_dump())
    filtered_chunk_ids = [identity[item.chunk_id][1] for item in filtered.citations]
    excluded_pairs = set(identity.values()) - included_pairs
    excluded_chunk_ids = [chunk_id for _, chunk_id in sorted(excluded_pairs)]
    projection_line_count = _projection_line_count(predicted_document)
    output_line_count = len(predicted_fields.body.splitlines())

    if completion is not None and completion.status == "needs_clarification":
        elapsed_ms = (time.perf_counter() - started) * 1000
        timings = ProposalPredictionTimings(evidence_ms=evidence_ms, llm_ms=llm_ms, total_ms=elapsed_ms)
        prediction = ProposalPrediction(
            case_id=case.case_id,
            fields=predicted_fields,
            document=predicted_document,
            requested_proposal_type="auto",
            resolved_proposal_type=resolution.proposal_type,
            proposal_type_source=resolution.source,
            evidence_filter=ProposalPredictionEvidenceFilter.model_validate(filtered.summary.model_dump()),
            completion=completion,
            evidence_artifact_ids=sorted(included_artifact_ids),
            evidence_chunk_ids=filtered_chunk_ids,
            excluded_evidence_artifact_ids=sorted(set(candidate_ids) - included_artifact_ids),
            excluded_evidence_chunk_ids=excluded_chunk_ids,
            citation_map=citation_map,
            context=ProposalPredictionContext(
                raw_chars=sum(len(item.excerpt) for item in filtered.citations),
                packed_chars=usage.context_chars,
                estimated_tokens=usage.estimated_input_tokens,
                context_sha256=packed_context_sha256,
            ),
            context_usage=usage,
            projection=ProposalPredictionProjection(
                source_line_count=projection_line_count,
                output_line_count=output_line_count,
                omitted_line_count=max(0, projection_line_count - output_line_count),
                truncated=projection_line_count > output_line_count,
            ),
            xlsx_contract="proposal-xlsx-v2",
            tool_events=[
                ProposalToolStageEvent(stage="evidence", status="ok", elapsed_ms=evidence_ms),
                ProposalToolStageEvent(stage="llm", status="ok", elapsed_ms=llm_ms),
                ProposalToolStageEvent(stage="workbook", status="skipped"),
                ProposalToolStageEvent(stage="save", status="skipped"),
            ],
            timings_ms=timings,
        )
        _, prediction_sha256 = _prediction_line(prediction)
        return (
            prediction,
            ProposalLiveCaseReport(
                case_id=case.case_id,
                group_id=case.group_id,
                status="completed",
                reference_status="ready",
                target_absent_from_generation_input=True,
                instruction_length=len(case.instruction),
                instruction_sha256=hashlib.sha256(case.instruction.encode("utf-8")).hexdigest(),
                candidate_artifact_count=len(candidate_ids),
                included_artifact_count=len(included_artifact_ids),
                excluded_artifact_count=len(set(candidate_ids) - included_artifact_ids),
                input_citation_count=len(citations),
                included_citation_count=len(filtered.citations),
                prediction_sha256=prediction_sha256,
                elapsed_ms=elapsed_ms,
                artifacts=_artifact_reports(case, artifacts, included_pairs),
            ),
            None,
        )

    workbook_started = time.perf_counter()
    try:
        workbook = render_proposal_workbook(template, document)
    except Exception as exc:
        workbook_ms = (time.perf_counter() - workbook_started) * 1000
        raise _StageError(
            exc,
            "workbook",
            ProposalPredictionTimings(
                evidence_ms=evidence_ms,
                llm_ms=llm_ms,
                workbook_ms=workbook_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            ),
        ) from exc
    workbook_ms = (time.perf_counter() - workbook_started) * 1000
    save_started = time.perf_counter()
    try:
        workbook_sha256, pending_workbook = _stage_workbook(workbook_path, workbook)
    except Exception as exc:
        save_ms = (time.perf_counter() - save_started) * 1000
        raise _StageError(
            exc,
            "save",
            ProposalPredictionTimings(
                evidence_ms=evidence_ms,
                llm_ms=llm_ms,
                workbook_ms=workbook_ms,
                save_ms=save_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            ),
        ) from exc
    save_ms = (time.perf_counter() - save_started) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000

    timings = ProposalPredictionTimings(
        evidence_ms=evidence_ms,
        llm_ms=llm_ms,
        workbook_ms=workbook_ms,
        save_ms=save_ms,
        total_ms=elapsed_ms,
    )
    prediction = ProposalPrediction(
        case_id=case.case_id,
        fields=predicted_fields,
        document=predicted_document,
        requested_proposal_type="auto",
        resolved_proposal_type=resolution.proposal_type,
        proposal_type_source=resolution.source,
        evidence_filter=ProposalPredictionEvidenceFilter.model_validate(filtered.summary.model_dump()),
        completion=completion,
        evidence_artifact_ids=sorted(included_artifact_ids),
        evidence_chunk_ids=filtered_chunk_ids,
        excluded_evidence_artifact_ids=sorted(set(candidate_ids) - included_artifact_ids),
        excluded_evidence_chunk_ids=excluded_chunk_ids,
        citation_map=citation_map,
        context=ProposalPredictionContext(
            raw_chars=sum(len(item.excerpt) for item in filtered.citations),
            packed_chars=usage.context_chars,
            estimated_tokens=usage.estimated_input_tokens,
            context_sha256=packed_context_sha256,
        ),
        context_usage=usage,
        projection=ProposalPredictionProjection(
            source_line_count=projection_line_count,
            output_line_count=output_line_count,
            omitted_line_count=max(0, projection_line_count - output_line_count),
            truncated=projection_line_count > output_line_count,
        ),
        xlsx_contract="proposal-xlsx-v2",
        tool_events=[
            ProposalToolStageEvent(stage="evidence", status="ok", elapsed_ms=evidence_ms),
            ProposalToolStageEvent(stage="llm", status="ok", elapsed_ms=llm_ms),
            ProposalToolStageEvent(stage="workbook", status="ok", elapsed_ms=workbook_ms),
            ProposalToolStageEvent(stage="save", status="ok", elapsed_ms=save_ms),
        ],
        workbook_path=os.path.relpath(workbook_path, checkpoint_directory).replace("\\", "/"),
        workbook_sha256=workbook_sha256,
        timings_ms=timings,
    )
    _, prediction_sha256 = _prediction_line(prediction)
    case_report = ProposalLiveCaseReport(
        case_id=case.case_id,
        group_id=case.group_id,
        status="completed",
        reference_status="ready",
        target_absent_from_generation_input=True,
        instruction_length=len(case.instruction),
        instruction_sha256=hashlib.sha256(case.instruction.encode("utf-8")).hexdigest(),
        candidate_artifact_count=len(candidate_ids),
        included_artifact_count=len(included_artifact_ids),
        excluded_artifact_count=len(set(candidate_ids) - included_artifact_ids),
        input_citation_count=len(citations),
        included_citation_count=len(filtered.citations),
        workbook_sha256=workbook_sha256,
        prediction_sha256=prediction_sha256,
        elapsed_ms=elapsed_ms,
        artifacts=_artifact_reports(case, artifacts, included_pairs),
    )
    return prediction, case_report, pending_workbook


def _rebuild_resumed_case_report(
    case: ProposalDatasetCaseInput,
    prediction: ProposalPrediction,
    artifacts: dict[str, ProposalDatasetArtifact],
    artifact_root: Path,
) -> ProposalLiveCaseReport:
    """checkpoint prediction에서 원래 성공·실패·skip과 latency를 손실 없이 복원한다."""

    _, digest = _prediction_line(prediction)
    expected_skip = _expected_reference_skip(case, artifacts)
    failure_code = prediction.failure_code
    if prediction.status == "ok":
        status = "completed"
        failure_code = "none"
        workbook_target = artifact_root / f"{case.case_id}.xlsx"
        expected_hash = prediction.workbook_sha256
        pending = (
            workbook_target.with_name(f".{workbook_target.name}.{expected_hash}.pending")
            if expected_hash
            else None
        )
        if expected_hash:
            try:
                _publish_staged_workbook(workbook_target, pending, expected_hash)
            except ProposalEvaluationError as exc:
                raise ProposalEvaluationError("pending_publish_recovery_failed") from exc
        elif prediction.completion is None or prediction.completion.status != "needs_clarification":
            raise ProposalEvaluationError("pending_publish_recovery_failed")
    elif failure_code == "reference_missing":
        status = "skipped_reference_missing"
    else:
        status = "failed"
    reference_status = (
        "expected_skip"
        if expected_skip
        else "unexpected_live_failure"
        if failure_code == "reference_missing"
        else "ready"
    )
    return ProposalLiveCaseReport(
        case_id=case.case_id,
        group_id=case.group_id,
        status=status,
        resumed=True,
        reference_status=reference_status,
        failure_code=failure_code,
        target_absent_from_generation_input=failure_code != "target_candidate_leakage",
        instruction_length=len(case.instruction),
        instruction_sha256=(
            hashlib.sha256(case.instruction.encode("utf-8")).hexdigest() if case.instruction else None
        ),
        candidate_artifact_count=len(
            set(case.reference_artifact_ids) | set(case.excluded_post_event_artifact_ids)
        ),
        included_artifact_count=len(prediction.evidence_artifact_ids),
        excluded_artifact_count=len(prediction.excluded_evidence_artifact_ids),
        input_citation_count=(
            prediction.evidence_filter.input_citation_count if prediction.evidence_filter else 0
        ),
        included_citation_count=len(prediction.evidence_chunk_ids),
        workbook_sha256=prediction.workbook_sha256,
        prediction_sha256=digest,
        elapsed_ms=prediction.timings_ms.total_ms,
    )


def _run_live_proposal_predictions_locked(
    documents_path: str | Path,
    proposal_cases_path: str | Path,
    *,
    checkpoint_path: str | Path,
    artifact_directory: str | Path,
    scope: EvaluationScope = "all",
    max_cases: int | None = None,
    deadline_seconds: float | None = None,
    resume: bool = False,
    settings: Settings | None = None,
    generator: _ProposalGenerator | None = None,
    template_path: str | Path | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> ProposalLiveRunReport:
    """로컬 LLM prediction을 case별 checkpoint하며 SMB 없이 XLSX를 exclusive 저장한다."""

    if max_cases is not None and max_cases < 1:
        raise ProposalEvaluationError("max_cases_invalid")
    if deadline_seconds is not None and deadline_seconds <= 0:
        raise ProposalEvaluationError("deadline_invalid")
    snapshot = load_proposal_dataset(documents_path, proposal_cases_path)
    scoped_cases = _scope_cases(snapshot.cases, scope)
    cases = scoped_cases
    if max_cases is not None:
        cases = cases[:max_cases]
    checkpoint = Path(checkpoint_path).resolve()
    artifact_root = Path(artifact_directory).resolve()
    repository_root = Path(__file__).resolve().parents[4]
    if checkpoint.is_relative_to(repository_root) or artifact_root.is_relative_to(repository_root):
        raise ProposalEvaluationError("live_raw_output_must_be_outside_repository")
    template = Path(template_path).read_bytes() if template_path is not None else (
        Path(__file__).resolve().parents[1] / "playground" / "templates" / "proposal_draft.xlsx"
    ).read_bytes()
    effective_settings = settings or load_settings()
    manifest = _checkpoint_manifest(
        dataset_fingerprint=snapshot.fingerprint,
        scope=scope,
        template_sha256=hashlib.sha256(template).hexdigest(),
        generation_config_fingerprint=_generation_config_fingerprint(
            effective_settings,
            LocalProposalDraftGenerator,
        ),
        cases=scoped_cases,
    )
    try:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        artifact_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProposalEvaluationError("checkpoint_error") from exc
    _initialize_or_validate_manifest(checkpoint, manifest, resume=resume)
    resumed = _load_resume_predictions(checkpoint) if resume else {}
    owned_generator = generator is None
    live_generator = generator or LocalProposalDraftGenerator(effective_settings)
    started = clock()
    reports: list[ProposalLiveCaseReport] = []
    try:
        for case in cases:
            case_started = clock()
            journal_path, _ = _pending_journal_paths(checkpoint, case.case_id)
            if journal_path.exists() and not resume:
                raise ProposalEvaluationError("pending_journal_mismatch")
            recovered = _recover_pending_prediction(
                checkpoint,
                artifact_root,
                case,
                manifest,
                resumed,
            )
            if recovered is not None:
                reports.append(
                    _rebuild_resumed_case_report(case, recovered, snapshot.artifacts, artifact_root)
                )
                continue
            if case.case_id in resumed:
                prediction = resumed[case.case_id]
                reports.append(_rebuild_resumed_case_report(case, prediction, snapshot.artifacts, artifact_root))
                continue
            if deadline_seconds is not None and clock() - started >= deadline_seconds:
                reports.append(
                    ProposalLiveCaseReport(
                        case_id=case.case_id,
                        group_id=case.group_id,
                        status="deadline_skipped",
                        reference_status="not_evaluated",
                        failure_code="deadline_exceeded",
                        target_absent_from_generation_input=True,
                        instruction_length=len(case.instruction),
                        instruction_sha256=(
                            hashlib.sha256(case.instruction.encode("utf-8")).hexdigest() if case.instruction else None
                        ),
                        candidate_artifact_count=len(
                            set(case.reference_artifact_ids) | set(case.excluded_post_event_artifact_ids)
                        ),
                        included_artifact_count=0,
                        excluded_artifact_count=0,
                        input_citation_count=0,
                        included_citation_count=0,
                        elapsed_ms=(clock() - case_started) * 1000,
                    )
                )
                continue
            try:
                workbook_target = artifact_root / f"{case.case_id}.xlsx"
                prediction, case_report, pending_workbook = _build_prediction(
                    case,
                    snapshot.artifacts,
                    live_generator,
                    template,
                    workbook_target,
                    checkpoint.parent,
                )
                _, journal_sha256 = _write_pending_journal(
                    checkpoint,
                    case,
                    manifest,
                    prediction,
                )
                checkpoint_digest = _append_prediction(checkpoint, prediction)
                case_report = case_report.model_copy(update={"prediction_sha256": checkpoint_digest})
                if case_report.workbook_sha256:
                    try:
                        _publish_staged_workbook(workbook_target, pending_workbook, case_report.workbook_sha256)
                    except ProposalEvaluationError as exc:
                        raise ProposalEvaluationError("pending_publish_recovery_failed") from exc
                elif prediction.completion is None or prediction.completion.status != "needs_clarification":
                    raise ProposalEvaluationError("pending_publish_recovery_failed")
                _mark_pending_journal_complete(checkpoint, case.case_id, journal_sha256)
                reports.append(case_report)
            except Exception as exc:  # 보고서에는 분류 코드만 남긴다.
                if isinstance(exc, ProposalEvaluationError) and str(exc) in {
                    "pending_journal_error",
                    "pending_journal_mismatch",
                    "pending_publish_recovery_failed",
                }:
                    raise
                elapsed_ms = (clock() - case_started) * 1000
                failure_code = _failure_code(exc)
                prediction_digest = None
                if failure_code in {
                    "reference_missing",
                    "target_candidate_leakage",
                    "proposal_context_limit_exceeded",
                    "generation_error",
                    "workbook_error",
                }:
                    error_prediction = _error_prediction(case, exc, elapsed_ms=elapsed_ms)
                    prediction_digest = _append_prediction(checkpoint, error_prediction)
                reports.append(
                    ProposalLiveCaseReport(
                        case_id=case.case_id,
                        group_id=case.group_id,
                        status="skipped_reference_missing" if failure_code == "reference_missing" else "failed",
                        reference_status=(
                            "expected_skip"
                            if failure_code == "reference_missing"
                            and _expected_reference_skip(case, snapshot.artifacts)
                            else "unexpected_live_failure"
                            if failure_code == "reference_missing"
                            else "ready"
                            if isinstance(exc, _StageError)
                            else "not_evaluated"
                        ),
                        failure_code=failure_code,
                        target_absent_from_generation_input=(
                            case.target_artifact_id not in case.reference_artifact_ids
                            and case.target_artifact_id not in case.excluded_post_event_artifact_ids
                        ),
                        instruction_length=len(case.instruction),
                        instruction_sha256=(
                            hashlib.sha256(case.instruction.encode("utf-8")).hexdigest() if case.instruction else None
                        ),
                        candidate_artifact_count=len(
                            set(case.reference_artifact_ids) | set(case.excluded_post_event_artifact_ids)
                        ),
                        included_artifact_count=0,
                        excluded_artifact_count=0,
                        input_citation_count=0,
                        included_citation_count=0,
                        prediction_sha256=prediction_digest,
                        elapsed_ms=elapsed_ms,
                    )
                )
    finally:
        if owned_generator:
            live_generator.close()
    try:
        checkpoint_fingerprint = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProposalEvaluationError("checkpoint_error") from exc
    report = ProposalLiveRunReport(
        dataset_fingerprint=snapshot.fingerprint,
        checkpoint_fingerprint=checkpoint_fingerprint,
        requested_case_count=len(cases),
        completed_case_count=sum(item.status == "completed" for item in reports),
        resumed_case_count=sum(item.resumed for item in reports),
        failed_case_count=sum(item.status == "failed" for item in reports),
        skipped_reference_missing_case_count=sum(
            item.status == "skipped_reference_missing" for item in reports
        ),
        expected_reference_skip_case_count=sum(
            _expected_reference_skip(case, snapshot.artifacts) for case in cases
        ),
        unexpected_live_reference_failure_case_count=sum(
            item.reference_status == "unexpected_live_failure" for item in reports
        ),
        deadline_skipped_case_count=sum(item.status == "deadline_skipped" for item in reports),
        max_cases=max_cases,
        deadline_seconds=deadline_seconds,
        total_elapsed_ms=(clock() - started) * 1000,
        completed_latency_p50_ms=_percentile(
            [item.elapsed_ms for item in reports if item.status == "completed"], 0.50
        ),
        completed_latency_p95_ms=_percentile(
            [item.elapsed_ms for item in reports if item.status == "completed"], 0.95
        ),
        cases=reports,
    )
    validate_sanitized_report(report)
    return report


def _process_exists(pid: int) -> bool | None:
    """동일 host PID의 생존 여부를 보수적으로 판정하며 불명확하면 None을 반환한다."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False if ctypes.windll.kernel32.GetLastError() == 87 else None
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


def _lock_payload(run_id: str) -> dict[str, object]:
    return {
        "schema_version": "proposal-live-run-lock-v1",
        "host_sha256": _HOST_SHA256,
        "pid": os.getpid(),
        "process_started_at_ns": _PROCESS_STARTED_AT_NS,
        "process_run_id": _PROCESS_RUN_ID,
        "run_id": run_id,
    }


def _read_lock_payload(lock_path: Path) -> tuple[str, dict[str, object]]:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalEvaluationError("live_run_locked") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "schema_version",
            "host_sha256",
            "pid",
            "process_started_at_ns",
            "process_run_id",
            "run_id",
        }
        or payload["schema_version"] != "proposal-live-run-lock-v1"
        or not isinstance(payload["host_sha256"], str)
        or not isinstance(payload["pid"], int)
        or not isinstance(payload["process_started_at_ns"], int)
        or not isinstance(payload["process_run_id"], str)
        or not isinstance(payload["run_id"], str)
    ):
        raise ProposalEvaluationError("live_run_locked")
    return raw, payload


def _acquire_run_lock(lock_path: Path) -> tuple[IO[str], str]:
    """active/불명확 owner는 거부하고 same-host dead owner lock만 안전 복구한다."""

    run_id = uuid4().hex
    payload = _lock_payload(run_id)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    try:
        stream = lock_path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError:
        raw, existing = _read_lock_payload(lock_path)
        if existing["host_sha256"] != _HOST_SHA256:
            raise ProposalEvaluationError("live_run_locked")
        owner_state = _process_exists(existing["pid"])
        if owner_state is not False:
            raise ProposalEvaluationError("live_run_locked")
        try:
            if lock_path.read_text(encoding="utf-8").strip() != raw:
                raise ProposalEvaluationError("live_run_locked")
            lock_path.unlink()
            stream = lock_path.open("x", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ProposalEvaluationError("live_run_locked") from exc
    except OSError as exc:
        raise ProposalEvaluationError("live_run_locked") from exc
    try:
        stream.write(encoded)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    except OSError as exc:
        stream.close()
        raise ProposalEvaluationError("live_run_locked") from exc
    return stream, encoded


def _release_run_lock(lock_path: Path, stream: IO[str], encoded: str) -> None:
    """자신이 획득한 lock identity가 그대로일 때만 lock을 해제한다."""

    stream.close()
    try:
        if lock_path.read_text(encoding="utf-8").strip() == encoded:
            lock_path.unlink()
    except OSError:
        pass


def run_live_proposal_predictions(
    documents_path: str | Path,
    proposal_cases_path: str | Path,
    *,
    checkpoint_path: str | Path,
    artifact_directory: str | Path,
    scope: EvaluationScope = "all",
    max_cases: int | None = None,
    deadline_seconds: float | None = None,
    resume: bool = False,
    settings: Settings | None = None,
    generator: _ProposalGenerator | None = None,
    template_path: str | Path | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> ProposalLiveRunReport:
    """한 checkpoint에 하나의 live 실행만 허용하고 종료 시 lock을 해제한다."""

    checkpoint = Path(checkpoint_path).resolve()
    artifact_root = Path(artifact_directory).resolve()
    repository_root = Path(__file__).resolve().parents[4]
    if checkpoint.is_relative_to(repository_root) or artifact_root.is_relative_to(repository_root):
        raise ProposalEvaluationError("live_raw_output_must_be_outside_repository")
    try:
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        lock_path = checkpoint.with_name(f"{checkpoint.name}.run.lock")
    except OSError as exc:
        raise ProposalEvaluationError("live_run_locked") from exc
    lock_stream, lock_encoded = _acquire_run_lock(lock_path)
    try:
        return _run_live_proposal_predictions_locked(
            documents_path,
            proposal_cases_path,
            checkpoint_path=checkpoint,
            artifact_directory=artifact_root,
            scope=scope,
            max_cases=max_cases,
            deadline_seconds=deadline_seconds,
            resume=resume,
            settings=settings,
            generator=generator,
            template_path=template_path,
            clock=clock,
        )
    finally:
        _release_run_lock(lock_path, lock_stream, lock_encoded)


def write_proposal_live_report(path: str | Path, report: ProposalLiveRunReport) -> None:
    """live 보고서를 기존 파일을 덮어쓰지 않고 저장한다."""

    validate_sanitized_report(report)
    output = Path(path)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(report.model_dump_json(indent=2))
            stream.write("\n")
    except OSError as exc:
        raise ProposalEvaluationError("live_report_write_failed") from exc
