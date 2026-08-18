from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import smb_finder.evaluation.proposal_live_runner as live_module

from smb_finder.config import Settings
from smb_finder.evaluation.proposal_live_runner import run_live_proposal_predictions
from smb_finder.evaluation.proposal_runner import ProposalEvaluationError, run_proposal_evaluation
from smb_finder.playground.proposal_context import ProposalContextUsage
from smb_finder.playground.proposal_draft import (
    ProposalClaimVerdict,
    ProposalDocumentV2,
    ProposalDraftFields,
    ProposalDraftResult,
    ProposalDraftError,
    ProposalEvidenceVerification,
    ProposalParagraphBlock,
    ProposalQuestionCandidate,
    ProposalSectionV2,
)
from scripts.run_proposal_evaluation import main as evaluation_cli_main


REFERENCE_ID = f"art-{'1' * 20}"
EXCLUDED_ID = f"art-{'2' * 20}"
TARGET_ID = f"art-{'3' * 20}"
CASE_ID = f"prp-{'4' * 20}"
GROUP_ID = f"grp-{'5' * 16}"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def _artifact(
    artifact_id: str,
    *,
    file_name: str,
    content: str,
    is_target: bool = False,
) -> dict[str, object]:
    return {
        "artifact_id": artifact_id,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "file_name": file_name,
        "role": "reference",
        "is_target": is_target,
        "extraction_status": "ok",
        "chunks": [
            {
                "chunk_id": f"chk-{artifact_id[-16:]}",
                "ordinal": 0,
                "text": content,
                "anchor": {"sheet": "안내", "row": 1},
            }
        ],
    }


def _dataset(tmp_path: Path, *, target_in_candidates: bool = False) -> tuple[Path, Path]:
    documents = tmp_path / "documents.jsonl"
    cases = tmp_path / "proposal_cases.jsonl"
    _write_jsonl(
        documents,
        [
            _artifact(REFERENCE_ID, file_name="행사 안내.xlsx", content="행사 일정과 참가 목적 근거"),
            _artifact(EXCLUDED_ID, file_name="영수증.xlsx", content="행사 종료 뒤 결제 자료"),
            _artifact(TARGET_ID, file_name="정답 기안.xlsx", content="GOLD_SECRET", is_target=True),
        ],
    )
    references = [REFERENCE_ID, TARGET_ID] if target_in_candidates else [REFERENCE_ID]
    _write_jsonl(
        cases,
        [
            {
                "case_id": CASE_ID,
                "group_id": GROUP_ID,
                "target_artifact_id": TARGET_ID,
                "instruction": "교육 행사 참석 기안을 작성해 주세요.",
                "reference_artifact_ids": references,
                "excluded_post_event_artifact_ids": [EXCLUDED_ID],
                "review_status": "candidate",
                "split": "unassigned",
                "evaluation_scope": "current_tool",
                "expected": {
                    "title": "GOLD_SECRET",
                    "approval_request": "GOLD_SECRET",
                    "body": "GOLD_SECRET",
                },
            }
        ],
    )
    return documents, cases


class _FakeGenerator:
    def __init__(self, *, allow_excluded: bool = False) -> None:
        self.calls: list[tuple[str, list[object], str]] = []
        self.allow_excluded = allow_excluded

    def generate(self, instruction: str, evidence: list[object], *, proposal_type: str = "general") -> ProposalDraftResult:
        self.calls.append((instruction, evidence, proposal_type))
        assert "GOLD_SECRET" not in instruction
        assert all("GOLD_SECRET" not in item.excerpt for item in evidence)  # type: ignore[attr-defined]
        if not self.allow_excluded:
            assert all("영수증" not in item.title for item in evidence)  # type: ignore[attr-defined]
        document = ProposalDocumentV2(
            schema_version="proposal-document-v2",
            proposal_type="event_attendance",
            title="교육 행사 참석",
            approval_request="교육 행사 참석을 검토해 주시기 바랍니다.",
            sections=[
                ProposalSectionV2(
                    heading="참가 목적",
                    semantic_role="purpose",
                    citations=["E001"],
                    blocks=[ProposalParagraphBlock(type="paragraph", text="교육 행사에 참석합니다.")],
                    missing_information=[],
                ),
                ProposalSectionV2(
                    heading="참가 내용",
                    semantic_role="details",
                    citations=["E001"],
                    blocks=[ProposalParagraphBlock(type="paragraph", text="안내된 내용을 확인합니다.")],
                    missing_information=[],
                ),
                ProposalSectionV2(
                    heading="행사 주요 내용",
                    semantic_role="schedule",
                    citations=["E001"],
                    blocks=[ProposalParagraphBlock(type="paragraph", text="행사 일정에 따릅니다.")],
                    missing_information=[],
                ),
            ],
            missing_information=[],
        )
        fields = ProposalDraftFields(
            title=document.title,
            approval_request=document.approval_request,
            body="참가 목적\n교육 행사에 참석합니다.",
        )
        return ProposalDraftResult(
            fields=fields,
            model="synthetic-local",
            document=document,
            proposal_type="event_attendance",
            context_usage=ProposalContextUsage(
                source_citation_count=len(evidence),
                packed_citation_count=len(evidence),
                source_document_count=1,
                packed_document_count=1,
                context_budget_chars=2_000,
                context_chars=100,
                estimated_input_tokens=100,
            ),
            packed_citation_map=tuple(  # type: ignore[attr-defined]
                (f"E{index:03d}", str(item.chunk_id)) for index, item in enumerate(evidence, start=1)
            ),
            packed_context_sha256="a" * 64,
        )

    def close(self) -> None:
        return None


class _ClarificationGenerator(_FakeGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.assess_calls = 0

    def assess(self, instruction, document, evidence, **kwargs):  # noqa: ANN001, ANN003
        self.assess_calls += 1
        return ProposalEvidenceVerification(
            schema_version="proposal-evidence-verification-v1",
            claims=[
                ProposalClaimVerdict(claim_id="C0001", status="supported", action="keep", citations=["E001"]),
                ProposalClaimVerdict(claim_id="C0002", status="unsupported", action="ask", citations=[]),
                ProposalClaimVerdict(claim_id="C0003", status="supported", action="keep", citations=["E001"]),
            ],
            questions=[
                ProposalQuestionCandidate(
                    field_key="participation_fee",
                    prompt="행사 참가 승인 금액은 얼마인가요?",
                    reason="missing",
                    priority=1,
                )
            ],
        )


def test_live_runner_uses_real_filter_and_excludes_gold(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    generator = _FakeGenerator()
    checkpoint = tmp_path / "checkpoint.jsonl"
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=generator,
    )

    assert report.completed_case_count == 1
    assert report.failed_case_count == 0
    assert report.zero_smb_calls is True
    assert len(generator.calls) == 1
    assert generator.calls[0][2] == "event_attendance"
    assert len(generator.calls[0][1]) == 1
    case_report = report.cases[0]
    assert report.completed_latency_p50_ms == pytest.approx(case_report.elapsed_ms)
    assert report.completed_latency_p95_ms == pytest.approx(case_report.elapsed_ms)
    assert case_report.target_absent_from_generation_input is True
    assert case_report.included_artifact_count == 1
    assert case_report.excluded_artifact_count == 1
    assert {item.filter_status for item in case_report.artifacts} == {"included", "excluded"}
    prediction = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert prediction["evidence_artifact_ids"] == [REFERENCE_ID]
    assert prediction["excluded_evidence_artifact_ids"] == [EXCLUDED_ID]
    assert prediction["citation_map"] == {"E001": f"chk-{REFERENCE_ID[-16:]}"}
    assert prediction["context"]["context_sha256"] == "a" * 64
    assert TARGET_ID not in json.dumps(prediction)
    assert (tmp_path / "artifacts" / f"{CASE_ID}.xlsx").read_bytes().startswith(b"PK")
    sanitized = report.model_dump_json()
    for forbidden in ("GOLD_SECRET", "행사 안내.xlsx", "영수증.xlsx", "workbook_path"):
        assert forbidden not in sanitized
    evaluation = run_proposal_evaluation(documents, cases, mode="live", predictions_path=checkpoint)
    assert evaluation.aggregate.generated_completed_case_count == 1
    assert evaluation.aggregate.generated_completed_latency_p50_ms == pytest.approx(
        prediction["timings_ms"]["total_ms"]
    )


def test_live_runner_checkpoints_clarification_without_workbook_and_resumes_without_llm(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    generator = _ClarificationGenerator()
    checkpoint = tmp_path / "clarification-checkpoint.jsonl"
    artifact_directory = tmp_path / "clarification-artifacts"

    first = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifact_directory,
        generator=generator,
    )
    prediction = json.loads(checkpoint.read_text(encoding="utf-8").splitlines()[0])

    assert first.completed_case_count == 1
    assert generator.assess_calls == 1
    assert prediction["completion"]["status"] == "needs_clarification"
    assert prediction["completion"]["question_field_keys"] == ["participation_fee"]
    assert prediction.get("workbook_path") is None
    assert prediction.get("workbook_sha256") is None
    assert not (artifact_directory / f"{CASE_ID}.xlsx").exists()

    resumed = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifact_directory,
        generator=generator,
        resume=True,
    )

    assert resumed.resumed_case_count == 1
    assert generator.assess_calls == 1


def test_live_runner_resume_does_not_call_generator_or_overwrite(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    generator = _FakeGenerator()
    checkpoint = tmp_path / "checkpoint.jsonl"
    artifacts = tmp_path / "artifacts"
    first = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=generator,
    )
    checkpoint_before = checkpoint.read_bytes()
    workbook_before = (artifacts / f"{CASE_ID}.xlsx").read_bytes()
    resumed_generator = _FakeGenerator()
    second = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=resumed_generator,
        resume=True,
    )

    assert first.completed_case_count == 1
    assert second.resumed_case_count == 1
    assert second.completed_case_count == 1
    assert second.failed_case_count == 0
    assert second.cases[0].status == "completed"
    assert second.cases[0].resumed is True
    assert second.completed_latency_p50_ms == pytest.approx(first.completed_latency_p50_ms)
    assert resumed_generator.calls == []
    assert checkpoint.read_bytes() == checkpoint_before
    assert (artifacts / f"{CASE_ID}.xlsx").read_bytes() == workbook_before


def test_resume_reconstructs_success_failure_skip_and_completed_latency(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    base_case = json.loads(cases.read_text(encoding="utf-8"))
    failed_case = dict(base_case)
    failed_case.update(
        {
            "case_id": f"prp-{'a' * 20}",
            "group_id": f"grp-{'a' * 16}",
            "instruction": "FAIL",
        }
    )
    skipped_case = dict(base_case)
    skipped_case.update(
        {
            "case_id": f"prp-{'b' * 20}",
            "group_id": f"grp-{'b' * 16}",
            "reference_artifact_ids": [],
        }
    )
    _write_jsonl(cases, [base_case, failed_case, skipped_case])

    class MixedGenerator(_FakeGenerator):
        def generate(
            self,
            instruction: str,
            evidence: list[object],
            *,
            proposal_type: str = "general",
        ) -> ProposalDraftResult:
            if instruction == "FAIL":
                self.calls.append((instruction, evidence, proposal_type))
                raise ProposalDraftError("synthetic_generation", "synthetic", 500)
            return super().generate(instruction, evidence, proposal_type=proposal_type)

    checkpoint = tmp_path / "checkpoint.jsonl"
    artifacts = tmp_path / "artifacts"
    first = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=MixedGenerator(),
    )
    resumed_generator = MixedGenerator()
    second = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=resumed_generator,
        resume=True,
    )

    assert (first.completed_case_count, first.failed_case_count, first.skipped_reference_missing_case_count) == (1, 1, 1)
    assert (second.completed_case_count, second.failed_case_count, second.skipped_reference_missing_case_count) == (1, 1, 1)
    assert second.resumed_case_count == 3
    assert second.expected_reference_skip_case_count == 1
    assert second.unexpected_live_reference_failure_case_count == 0
    assert second.completed_latency_p50_ms == pytest.approx(first.completed_latency_p50_ms)
    assert [item.status for item in second.cases] == ["completed", "failed", "skipped_reference_missing"]
    assert resumed_generator.calls == []


def test_live_runner_rejects_target_candidate_before_generation(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path, target_in_candidates=True)
    generator = _FakeGenerator()
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        artifact_directory=tmp_path / "artifacts",
        generator=generator,
    )

    assert report.failed_case_count == 1
    assert report.cases[0].failure_code == "target_candidate_leakage"
    assert report.cases[0].target_absent_from_generation_input is False
    assert generator.calls == []
    checkpoint_prediction = json.loads((tmp_path / "checkpoint.jsonl").read_text(encoding="utf-8"))
    assert checkpoint_prediction["status"] == "error"
    assert checkpoint_prediction["failure_code"] == "target_candidate_leakage"
    evaluation = run_proposal_evaluation(
        documents,
        cases,
        mode="live",
        predictions_path=tmp_path / "checkpoint.jsonl",
    )
    assert evaluation.cases[0].hard_gates.evidence_leakage_free is False
    assert evaluation.cases[0].hard_gates.evidence_filter_valid is False


def test_live_runner_records_filter_miss_without_oracle_execution_gate(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    case = json.loads(cases.read_text(encoding="utf-8"))
    case["instruction"] = "일반 문서를 작성해 주세요."
    _write_jsonl(cases, [case])
    generator = _FakeGenerator(allow_excluded=True)
    checkpoint = tmp_path / "checkpoint.jsonl"
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=generator,
    )
    prediction = json.loads(checkpoint.read_text(encoding="utf-8"))

    assert report.completed_case_count == 1
    assert len(generator.calls[0][1]) == 2
    assert prediction["evidence_artifact_ids"] == sorted([REFERENCE_ID, EXCLUDED_ID])
    assert prediction["excluded_evidence_artifact_ids"] == []


def test_live_runner_checkpoint_is_exclusive_without_resume(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint_exists"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=tmp_path / "artifacts",
            generator=_FakeGenerator(),
        )


def test_live_runner_deadline_skips_without_generation(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    values = iter((0.0, 1.0, 2.0, 3.0, 4.0))
    generator = _FakeGenerator()
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        artifact_directory=tmp_path / "artifacts",
        generator=generator,
        deadline_seconds=0.5,
        clock=lambda: next(values),
    )

    assert report.deadline_skipped_case_count == 1
    assert report.cases[0].failure_code == "deadline_exceeded"
    assert generator.calls == []


def test_live_runner_checkpoints_context_failure_and_resume_preserves_it(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)

    class FailingGenerator(_FakeGenerator):
        def generate(
            self,
            instruction: str,
            evidence: list[object],
            *,
            proposal_type: str = "general",
        ) -> ProposalDraftResult:
            self.calls.append((instruction, evidence, proposal_type))
            raise ProposalDraftError(
                "proposal_context_limit_exceeded",
                "synthetic",
                422,
                context_usage=ProposalContextUsage(
                    source_citation_count=len(evidence),
                    source_document_count=1,
                    estimated_input_tokens=999,
                    truncated=True,
                ),
            )

    checkpoint = tmp_path / "checkpoint.jsonl"
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=FailingGenerator(),
    )
    prediction = json.loads(checkpoint.read_text(encoding="utf-8"))

    assert report.failed_case_count == 1
    assert report.cases[0].failure_code == "proposal_context_limit_exceeded"
    assert prediction["status"] == "error"
    assert prediction["failure_code"] == "proposal_context_limit_exceeded"
    assert prediction["context_usage"]["estimated_input_tokens"] == 999
    assert [item["status"] for item in prediction["tool_events"]] == ["ok", "error", "skipped", "skipped"]
    resumed = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=_FakeGenerator(),
        resume=True,
    )
    assert resumed.resumed_case_count == 1
    assert resumed.failed_case_count == 1
    assert resumed.completed_case_count == 0
    assert resumed.cases[0].elapsed_ms == pytest.approx(report.cases[0].elapsed_ms)


def test_live_runner_records_missing_reference_as_skipped_prediction(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    rows = [json.loads(line) for line in documents.read_text(encoding="utf-8").splitlines()]
    rows[0]["chunks"] = []
    _write_jsonl(documents, rows)
    checkpoint = tmp_path / "checkpoint.jsonl"
    generator = _FakeGenerator()
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=generator,
    )
    prediction = json.loads(checkpoint.read_text(encoding="utf-8"))

    assert report.skipped_reference_missing_case_count == 1
    assert report.cases[0].status == "skipped_reference_missing"
    assert prediction["status"] == "skipped"
    assert prediction["failure_code"] == "reference_missing"
    assert generator.calls == []


def test_live_report_separates_expected_and_unexpected_reference_skip(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    empty_id = f"art-{'6' * 20}"
    missing_id = f"art-{'7' * 20}"
    document_rows = [json.loads(line) for line in documents.read_text(encoding="utf-8").splitlines()]
    empty = _artifact(empty_id, file_name="빈 참고.xlsx", content="unused")
    empty["chunks"] = []
    document_rows.append(empty)
    _write_jsonl(documents, document_rows)
    base_case = json.loads(cases.read_text(encoding="utf-8"))
    expected_skip = dict(base_case)
    expected_skip.update(
        {
            "case_id": f"prp-{'8' * 20}",
            "group_id": f"grp-{'8' * 16}",
            "reference_artifact_ids": [missing_id],
        }
    )
    unexpected_skip = dict(base_case)
    unexpected_skip.update(
        {
            "case_id": f"prp-{'9' * 20}",
            "group_id": f"grp-{'9' * 16}",
            "reference_artifact_ids": [REFERENCE_ID, empty_id],
        }
    )
    _write_jsonl(cases, [expected_skip, unexpected_skip])

    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=tmp_path / "checkpoint.jsonl",
        artifact_directory=tmp_path / "artifacts",
        generator=_FakeGenerator(),
    )

    assert report.skipped_reference_missing_case_count == 2
    assert report.expected_reference_skip_case_count == 2
    assert report.unexpected_live_reference_failure_case_count == 0
    assert [item.reference_status for item in report.cases] == [
        "expected_skip",
        "expected_skip",
    ]
    preflight = run_proposal_evaluation(documents, cases, mode="preflight")
    assert preflight.context.skipped_reference_missing_count == 2
    assert set(preflight.context.skipped_reference_missing_case_ids) == {
        item.case_id for item in report.cases if item.reference_status == "expected_skip"
    }


def test_live_runner_rejects_raw_outputs_inside_repository(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    repository_root = Path(__file__).resolve().parents[2]
    with pytest.raises(ProposalEvaluationError, match="live_raw_output_must_be_outside_repository"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=repository_root / ".runtime" / "raw.jsonl",
            artifact_directory=tmp_path / "artifacts",
            generator=_FakeGenerator(),
        )


def test_live_resume_rejects_dataset_or_template_manifest_mismatch(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    artifacts = tmp_path / "artifacts"
    run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=_FakeGenerator(),
    )
    manifest = json.loads(
        checkpoint.with_name(f"{checkpoint.name}.manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["dataset_fingerprint"]
    assert manifest["template_sha256"]
    assert manifest["generation_config_fingerprint"]
    assert manifest["schema_version"] == "proposal-live-checkpoint-manifest-v2"
    assert manifest["case_generation_fingerprints"][CASE_ID]

    case = json.loads(cases.read_text(encoding="utf-8"))
    case["instruction"] = "변경된 생성 입력"
    _write_jsonl(cases, [case])
    with pytest.raises(ProposalEvaluationError, match="checkpoint_manifest_mismatch"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=artifacts,
            generator=_FakeGenerator(),
            resume=True,
        )
    _dataset(tmp_path)
    altered_template = tmp_path / "altered-template.xlsx"
    default_template = Path(live_module.__file__).resolve().parents[1] / "playground" / "templates" / "proposal_draft.xlsx"
    altered_template.write_bytes(default_template.read_bytes() + b"changed")
    with pytest.raises(ProposalEvaluationError, match="checkpoint_manifest_mismatch"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=artifacts,
            generator=_FakeGenerator(),
            template_path=altered_template,
            resume=True,
        )


def test_live_run_lock_rejects_concurrent_checkpoint_owner(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.with_name(f"{checkpoint.name}.run.lock").write_text("owned", encoding="utf-8")
    with pytest.raises(ProposalEvaluationError, match="live_run_locked"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=tmp_path / "artifacts",
            generator=_FakeGenerator(),
        )


def test_live_run_lock_rejects_valid_active_owner(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    lock_path = checkpoint.with_name(f"{checkpoint.name}.run.lock")
    lock_path.write_text(json.dumps(live_module._lock_payload("b" * 32)), encoding="utf-8")

    with pytest.raises(ProposalEvaluationError, match="live_run_locked"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=tmp_path / "artifacts",
            generator=_FakeGenerator(),
        )


def test_live_run_lock_recovers_only_confirmed_same_host_dead_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    lock_path = checkpoint.with_name(f"{checkpoint.name}.run.lock")
    payload = live_module._lock_payload("a" * 32)
    payload["pid"] = 999_999
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(live_module, "_process_exists", lambda _pid: False)

    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=_FakeGenerator(),
    )

    assert report.completed_case_count == 1
    assert not lock_path.exists()


def test_live_resume_rejects_generation_config_mismatch_without_raw_identity(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    endpoint = "http://private-runtime.invalid:11434"
    model = "private-model-name"
    settings = Settings(ollama_base_url=endpoint, llmops_chat_model=model)
    run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        settings=settings,
        generator=_FakeGenerator(),
    )
    manifest_text = checkpoint.with_name(f"{checkpoint.name}.manifest.json").read_text(encoding="utf-8")

    assert endpoint not in manifest_text
    assert model not in manifest_text
    assert "generation_config_fingerprint" in manifest_text
    with pytest.raises(ProposalEvaluationError, match="checkpoint_manifest_mismatch"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=tmp_path / "artifacts",
            settings=settings.model_copy(update={"proposal_llm_num_ctx": settings.proposal_llm_num_ctx + 1024}),
            generator=_FakeGenerator(),
            resume=True,
        )
    with pytest.raises(ProposalEvaluationError, match="checkpoint_manifest_mismatch"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=tmp_path / "artifacts",
            settings=settings.model_copy(update={"llmops_chat_model": "another-model"}),
            generator=_FakeGenerator(),
            resume=True,
        )
    with pytest.raises(ProposalEvaluationError, match="checkpoint_manifest_mismatch"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=tmp_path / "artifacts",
            settings=settings.model_copy(update={"ollama_base_url": "http://another-runtime.invalid:11434"}),
            generator=_FakeGenerator(),
            resume=True,
        )


def test_live_runner_rejects_missing_packed_metadata_as_generation_error(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)

    class MissingPackedMetadataGenerator(_FakeGenerator):
        def generate(
            self,
            instruction: str,
            evidence: list[object],
            *,
            proposal_type: str = "general",
        ) -> ProposalDraftResult:
            result = super().generate(instruction, evidence, proposal_type=proposal_type)
            return replace(result, packed_citation_map=(), packed_context_sha256=None)

    checkpoint = tmp_path / "checkpoint.jsonl"
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=tmp_path / "artifacts",
        generator=MissingPackedMetadataGenerator(),
    )
    prediction = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert report.failed_case_count == 1
    assert prediction["failure_code"] == "generation_error"
    assert not (tmp_path / "artifacts" / f"{CASE_ID}.xlsx").exists()


def test_checkpoint_append_failure_keeps_pending_for_safe_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    artifacts = tmp_path / "artifacts"
    original_append = live_module._append_prediction

    def fail_append(path: Path, prediction: object) -> str:
        raise ProposalEvaluationError("checkpoint_error")

    monkeypatch.setattr(live_module, "_append_prediction", fail_append)
    first_generator = _FakeGenerator()
    first = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=first_generator,
    )
    assert first.failed_case_count == 1
    assert not (artifacts / f"{CASE_ID}.xlsx").exists()
    assert list(artifacts.glob("*.pending"))

    monkeypatch.setattr(live_module, "_append_prediction", original_append)
    resumed_generator = _FakeGenerator()
    resumed = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=resumed_generator,
        resume=True,
    )
    assert resumed.completed_case_count == 1
    assert (artifacts / f"{CASE_ID}.xlsx").exists()
    assert resumed_generator.calls == []
    assert list(artifacts.glob("*.pending"))
    assert list(tmp_path.glob("*.pending-journal.complete"))


def test_publish_failure_requires_resume_recovery_before_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents, cases = _dataset(tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    artifacts = tmp_path / "artifacts"
    original_publish = live_module._publish_staged_workbook

    def fail_publish(path: Path, pending: Path | None, expected_sha256: str) -> None:
        raise ProposalEvaluationError("workbook_error")

    monkeypatch.setattr(live_module, "_publish_staged_workbook", fail_publish)
    with pytest.raises(ProposalEvaluationError, match="pending_publish_recovery_failed"):
        run_live_proposal_predictions(
            documents,
            cases,
            checkpoint_path=checkpoint,
            artifact_directory=artifacts,
            generator=_FakeGenerator(),
        )
    assert not (artifacts / f"{CASE_ID}.xlsx").exists()

    monkeypatch.setattr(live_module, "_publish_staged_workbook", original_publish)
    resumed_generator = _FakeGenerator()
    report = run_live_proposal_predictions(
        documents,
        cases,
        checkpoint_path=checkpoint,
        artifact_directory=artifacts,
        generator=resumed_generator,
        resume=True,
    )
    assert report.completed_case_count == 1
    assert resumed_generator.calls == []
    assert (artifacts / f"{CASE_ID}.xlsx").exists()


def test_live_cli_requires_sensitive_data_confirmation(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    args = [
        "--mode",
        "live",
        "--documents",
        str(documents),
        "--proposal-cases",
        str(cases),
        "--checkpoint",
        str(tmp_path / "raw.jsonl"),
        "--artifact-directory",
        str(tmp_path / "artifacts"),
        "--live-report",
        str(tmp_path / "live-report.json"),
        "--output",
        str(tmp_path / "evaluation.json"),
    ]
    assert evaluation_cli_main(args) == 2


def test_live_cli_rejects_in_repository_raw_paths(tmp_path: Path) -> None:
    documents, cases = _dataset(tmp_path)
    repository_root = Path(__file__).resolve().parents[2]
    assert (
        evaluation_cli_main(
            [
                "--mode",
                "live",
                "--documents",
                str(documents),
                "--proposal-cases",
                str(cases),
                "--checkpoint",
                str(repository_root / ".runtime" / "raw.jsonl"),
                "--artifact-directory",
                str(tmp_path / "artifacts"),
                "--live-report",
                str(tmp_path / "live-report.json"),
                "--output",
                str(tmp_path / "evaluation.json"),
                "--confirm-local-sensitive-data",
            ]
        )
        == 2
    )
