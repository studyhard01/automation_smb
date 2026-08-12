"""기안 생성 도구의 오프라인 평가 입력과 비식별 보고 계약."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ContextBucket = Literal["lte_8k", "8k_to_16k", "16k_to_32k", "32k_to_64k", "gt_64k"]
EvaluationMode = Literal["preflight", "oracle_contract_check", "baseline", "predictions", "live"]
EvaluationScope = Literal["all", "current_tool", "structure_only"]
ProposalStage = Literal["evidence", "llm", "workbook", "save"]
StageStatus = Literal["ok", "error", "skipped", "simulated"]


class ProposalDatasetChunkAnchor(BaseModel):
    """필터의 문서 metadata에 사용할 수 있는 위치 표식만 적재한다."""

    model_config = ConfigDict(extra="ignore")

    page: int | None = Field(default=None, ge=1)
    slide: int | None = Field(default=None, ge=1)
    sheet: str | None = Field(default=None, max_length=200)
    cell: str | None = Field(default=None, max_length=50)
    paragraph: int | None = Field(default=None, ge=0)
    row: int | None = Field(default=None, ge=0)

    def section_path(self) -> list[str]:
        """원문 경로 없이 core filter가 이해하는 짧은 section metadata를 만든다."""

        values: list[str] = []
        if self.sheet:
            values.append(self.sheet)
        if self.cell:
            values.append(self.cell)
        if self.page is not None:
            values.append(f"page-{self.page}")
        if self.slide is not None:
            values.append(f"slide-{self.slide}")
        if self.paragraph is not None:
            values.append(f"paragraph-{self.paragraph}")
        if self.row is not None:
            values.append(f"row-{self.row}")
        return values


class ProposalDatasetChunk(BaseModel):
    """외부 documents.jsonl에서 평가에 필요한 최소 chunk 필드."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(pattern=r"^chk-[0-9a-f]{16}$")
    ordinal: int = Field(default=0, ge=0)
    text: str = Field(min_length=1)
    anchor: ProposalDatasetChunkAnchor = Field(default_factory=ProposalDatasetChunkAnchor)


class ProposalDatasetArtifact(BaseModel):
    """외부 artifact의 ID·hash·추출 chunk만 읽는 최소 계약."""

    model_config = ConfigDict(extra="ignore")

    artifact_id: str = Field(pattern=r"^art-[0-9a-f]{20}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_name: str = Field(default="", max_length=500)
    role: str = Field(default="", max_length=100)
    is_target: bool = False
    extraction_status: Literal["ok", "partial", "unsupported", "error"]
    chunks: list[ProposalDatasetChunk] = Field(default_factory=list)


class ProposalDatasetSection(BaseModel):
    """본문 구조 점수 계산에 필요한 의미 역할과 제목."""

    model_config = ConfigDict(extra="ignore")

    semantic_role: str = "other"
    heading: str = ""
    lines: list[str] = Field(default_factory=list)
    source_line_start: int = Field(default=1, ge=1)
    source_line_end: int = Field(default=1, ge=1)


class ProposalDatasetCell(BaseModel):
    """표 구조 판별에 필요한 셀 값."""

    model_config = ConfigDict(extra="ignore")

    value: str = ""


class ProposalDatasetBodyRow(BaseModel):
    """원본 행의 읽기용 문자열과 셀 개수."""

    model_config = ConfigDict(extra="ignore")

    source_row: int = Field(ge=1)
    text: str = ""
    cells: list[ProposalDatasetCell] = Field(default_factory=list)


class ProposalDatasetExpected(BaseModel):
    """기안 case의 정답 출력."""

    model_config = ConfigDict(extra="ignore")

    source_sheet: str = "기안지"
    title: str = Field(min_length=1, max_length=500)
    approval_request: str = Field(min_length=1, max_length=5_000)
    body: str = Field(min_length=1, max_length=200_000)
    body_lines: list[str] = Field(default_factory=list)
    body_rows: list[ProposalDatasetBodyRow] = Field(default_factory=list)
    body_sections: list[ProposalDatasetSection] = Field(default_factory=list)
    current_tool_compatible: bool = False


RevisionField = Literal["title", "approval_request", "proposal_type", "sections", "missing_information"]


class ProposalDatasetRevisionExpectation(BaseModel):
    """수정 피드백 원문 없이 hash와 변경·보존 필드만 두는 선택 평가 label."""

    model_config = ConfigDict(extra="ignore")

    feedback_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revised_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_changed_fields: list[RevisionField] = Field(default_factory=list)
    expected_preserved_fields: list[RevisionField] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_field_sets(self) -> ProposalDatasetRevisionExpectation:
        if set(self.expected_changed_fields) & set(self.expected_preserved_fields):
            raise ValueError("수정 기대 필드와 보존 기대 필드는 겹칠 수 없습니다.")
        return self


class ProposalDatasetFactExpectation(BaseModel):
    """전체 문장 대신 사실 키와 허용 값으로 검토자가 확정하는 평가 label."""

    model_config = ConfigDict(extra="forbid")

    fact_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    expectation: Literal["include", "exclude"]
    expected_values: list[str] = Field(min_length=1, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    source_artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    missing_action: Literal["omit", "ask"] = "omit"

    @model_validator(mode="after")
    def validate_values(self) -> ProposalDatasetFactExpectation:
        values = [value.strip() for value in [*self.expected_values, *self.aliases]]
        if any(not value or len(value) > 500 for value in values):
            raise ValueError("사실 기대값과 별칭은 1~500자의 비어 있지 않은 문자열이어야 합니다.")
        if len(values) != len(set(values)):
            raise ValueError("사실 기대값과 별칭은 중복될 수 없습니다.")
        return self


class ProposalDatasetCaseInput(BaseModel):
    """외부 proposal_cases.jsonl의 평가용 최소 계약."""

    model_config = ConfigDict(extra="ignore")

    case_id: str = Field(pattern=r"^prp-[0-9a-f]{20}$")
    group_id: str = Field(pattern=r"^grp-[0-9a-f]{16}$")
    target_artifact_id: str = Field(pattern=r"^art-[0-9a-f]{20}$")
    instruction: str = Field(default="", max_length=5_000)
    reference_artifact_ids: list[str] = Field(default_factory=list)
    excluded_post_event_artifact_ids: list[str] = Field(default_factory=list)
    expected: ProposalDatasetExpected | None = None
    review_status: Literal["candidate", "needs_review", "incomplete", "reviewed"] = "needs_review"
    split: Literal["train", "validation", "test", "unassigned"] = "unassigned"
    evaluation_scope: Literal["current_tool", "structure_only"] = "structure_only"
    proposal_type_label: Literal["purchase", "event_attendance", "general"] | None = None
    expected_section_order: list[str] = Field(default_factory=list)
    revision_expectation: ProposalDatasetRevisionExpectation | None = None
    fact_expectations: list[ProposalDatasetFactExpectation] = Field(default_factory=list, max_length=200)
    required_fact_labels: list[str] = Field(default_factory=list, max_length=200)
    unsupported_addition_labels: list[str] = Field(default_factory=list, max_length=200)


class ProposalPredictedFields(BaseModel):
    """평가 대상 LLM이 만든 세 필드. 실제 문자열은 보고서에 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    approval_request: str = Field(min_length=1, max_length=5_000)
    body: str = Field(min_length=1, max_length=200_000)


class ProposalPredictedParagraphBlock(BaseModel):
    """V2 설명 문단."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["paragraph"]
    text: str = Field(min_length=1, max_length=3_000)


class ProposalPredictedListBlock(BaseModel):
    """V2 순서·비순서 목록."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["list"]
    ordered: bool
    items: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_items(self) -> ProposalPredictedListBlock:
        if any(not item.strip() or len(item) > 1_000 for item in self.items):
            raise ValueError("list item은 1~1000자여야 합니다.")
        return self


class ProposalPredictedTableBlock(BaseModel):
    """V2 표 matrix."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["table"]
    headers: list[str] = Field(min_length=1, max_length=20)
    rows: list[list[str]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_width(self) -> ProposalPredictedTableBlock:
        width = len(self.headers)
        if any(not header.strip() or len(header) > 1_000 for header in self.headers):
            raise ValueError("table header는 1~1000자여야 합니다.")
        if any(len(row) != width for row in self.rows):
            raise ValueError("table row 폭이 headers와 다릅니다.")
        if any(len(cell) > 1_000 for row in self.rows for cell in row):
            raise ValueError("table cell은 1000자를 넘을 수 없습니다.")
        return self


ProposalPredictedBlock = Annotated[
    ProposalPredictedParagraphBlock | ProposalPredictedListBlock | ProposalPredictedTableBlock,
    Field(discriminator="type"),
]


class ProposalPredictedSection(BaseModel):
    """V2 의미 section과 근거 citation."""

    model_config = ConfigDict(extra="forbid")

    heading: str = Field(min_length=1, max_length=120)
    semantic_role: Literal[
        "purpose",
        "background",
        "request",
        "details",
        "budget",
        "schedule",
        "expected_effect",
        "attachments",
        "notes",
        "other",
    ]
    citations: list[str] = Field(min_length=1, max_length=30)
    blocks: list[ProposalPredictedBlock] = Field(min_length=1, max_length=50)
    missing_information: list[str] = Field(default_factory=list, max_length=20)


class ProposalPredictedDocument(BaseModel):
    """core 응답과 같은 proposal-document-v2."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-document-v2"]
    title: str = Field(min_length=1, max_length=80)
    approval_request: str = Field(min_length=1, max_length=1_000)
    proposal_type: Literal["purchase", "event_attendance", "general"] | None = None
    sections: list[ProposalPredictedSection] = Field(min_length=1, max_length=20)
    missing_information: list[str] = Field(default_factory=list, max_length=50)


class ProposalPredictionContextUsage(BaseModel):
    """core의 proposal-context-usage-v1을 그대로 받는 비식별 사용량."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-context-usage-v1"] = "proposal-context-usage-v1"
    source_citation_count: int = Field(default=0, ge=0)
    packed_citation_count: int = Field(default=0, ge=0)
    deduplicated_citation_count: int = Field(default=0, ge=0)
    source_document_count: int = Field(default=0, ge=0)
    packed_document_count: int = Field(default=0, ge=0)
    context_budget_chars: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    truncated: bool = False
    retry_count: int = Field(default=0, ge=0, le=1)
    first_attempt_context_chars: int | None = Field(default=None, ge=0)
    prompt_eval_count: int | None = Field(default=None, ge=0)


class ProposalPredictionProjection(BaseModel):
    """V2가 legacy XLSX 33행으로 투영될 때의 구조 손실 수치."""

    model_config = ConfigDict(extra="forbid")

    source_line_count: int = Field(default=0, ge=0)
    output_line_count: int = Field(default=0, ge=0)
    omitted_line_count: int = Field(default=0, ge=0)
    truncated: bool = False


class ProposalPredictionContext(BaseModel):
    """실행기가 관측한 context 크기. 원문이나 prompt는 허용하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    raw_chars: int | None = Field(default=None, ge=0)
    packed_chars: int | None = Field(default=None, ge=0)
    estimated_tokens: int | None = Field(default=None, ge=0)
    context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProposalToolStageEvent(BaseModel):
    """기안 생성 파이프라인의 고정 stage 상태."""

    model_config = ConfigDict(extra="forbid")

    stage: ProposalStage
    status: StageStatus
    elapsed_ms: float = Field(default=0.0, ge=0)


class ProposalPredictionTimings(BaseModel):
    """민감 문자열 없이 전달하는 단계별 시간."""

    model_config = ConfigDict(extra="forbid")

    evidence_ms: float = Field(default=0.0, ge=0)
    llm_ms: float = Field(default=0.0, ge=0)
    workbook_ms: float = Field(default=0.0, ge=0)
    save_ms: float = Field(default=0.0, ge=0)
    total_ms: float = Field(default=0.0, ge=0)


class ProposalPredictionEvidenceFilter(BaseModel):
    """core 공개 evidence filter 집계를 그대로 받되 보고서에는 원인 key를 복사하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-evidence-filter-v1"] = "proposal-evidence-filter-v1"
    input_document_count: int = Field(ge=0)
    included_document_count: int = Field(ge=0)
    excluded_document_count: int = Field(ge=0)
    input_citation_count: int = Field(ge=0)
    included_citation_count: int = Field(ge=0)
    excluded_citation_count: int = Field(ge=0)
    excluded_reason_counts: dict[str, int] = Field(default_factory=dict)
    fallback_used: bool = False

    @model_validator(mode="after")
    def validate_reason_counts(self) -> ProposalPredictionEvidenceFilter:
        if any(not reason.strip() or count < 0 for reason, count in self.excluded_reason_counts.items()):
            raise ValueError("evidence filter reason 집계가 올바르지 않습니다.")
        return self


class ProposalPredictionCompletion(BaseModel):
    """질문 원문 없이 live 생성의 근거 검증·확인 대기 상태만 보존한다."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-completion-v1"] = "proposal-completion-v1"
    status: Literal["completed", "needs_clarification", "completed_with_omissions"]
    question_field_keys: list[str] = Field(default_factory=list, max_length=3)
    supported_claim_count: int = Field(default=0, ge=0)
    derived_claim_count: int = Field(default=0, ge=0)
    omitted_claim_count: int = Field(default=0, ge=0)
    conflicting_claim_count: int = Field(default=0, ge=0)

    @field_validator("question_field_keys")
    @classmethod
    def validate_question_keys(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(re.fullmatch(r"[a-z][a-z0-9_]{1,63}", item) is None for item in value):
            raise ValueError("확인 질문 field key가 올바르지 않습니다.")
        return value


class ProposalPrediction(BaseModel):
    """외부 실행 결과 JSONL 한 줄. workbook 경로와 출력 문자열은 입력에서만 사용한다."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^prp-[0-9a-f]{20}$")
    status: Literal["ok", "error", "skipped"] = "ok"
    fields: ProposalPredictedFields | None = None
    document: ProposalPredictedDocument | None = None
    requested_proposal_type: Literal["auto", "purchase", "event_attendance", "general"] | None = None
    resolved_proposal_type: Literal["purchase", "event_attendance", "general"] | None = None
    proposal_type_source: Literal["user", "rule", "fallback"] | None = None
    evidence_filter: ProposalPredictionEvidenceFilter | None = None
    completion: ProposalPredictionCompletion | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    excluded_evidence_artifact_ids: list[str] = Field(default_factory=list)
    excluded_evidence_chunk_ids: list[str] = Field(default_factory=list)
    citation_map: dict[str, str] = Field(default_factory=dict)
    context: ProposalPredictionContext | None = None
    context_usage: ProposalPredictionContextUsage | None = None
    projection: ProposalPredictionProjection | None = None
    xlsx_contract: Literal["legacy-v1", "proposal-xlsx-v2"] = "legacy-v1"
    tool_events: list[ProposalToolStageEvent] = Field(default_factory=list)
    workbook_path: str | None = None
    workbook_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision_of_draft_id: UUID | None = None
    revision_number: str | None = Field(default=None, pattern=r"^1\.\d+$")
    revision_summary: str | None = Field(default=None, min_length=1, max_length=500)
    revision_base_document: ProposalPredictedDocument | None = None
    revision_base_document_sha256_before: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision_base_document_sha256_after: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision_base_workbook_sha256_before: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision_base_workbook_sha256_after: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    timings_ms: ProposalPredictionTimings = Field(default_factory=ProposalPredictionTimings)
    failure_code: Literal[
        "none",
        "reference_missing",
        "target_candidate_leakage",
        "proposal_context_limit_exceeded",
        "generation_error",
        "workbook_error",
        "tool_error",
        "unknown",
    ] = "none"

    @model_validator(mode="after")
    def validate_status_fields(self) -> ProposalPrediction:
        """성공 결과에는 출력 필드가 있어야 하고 실패에는 failure code가 있어야 한다."""

        if self.status == "ok" and (self.fields is None or self.document is None or self.context_usage is None):
            raise ValueError("status=ok prediction에는 fields, document, context_usage가 필요합니다.")
        if self.status == "error" and self.failure_code == "none":
            raise ValueError("status=error prediction에는 failure_code가 필요합니다.")
        return self


class ProposalContextCaseStats(BaseModel):
    """원문 없이 남기는 case별 context preflight 결과."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    group_id: str
    status: Literal["ready", "skipped_reference_missing"]
    reference_count: int = Field(ge=0)
    resolved_reference_count: int = Field(ge=0)
    missing_reference_artifact_ids: list[str] = Field(default_factory=list)
    available_chunk_count: int = Field(ge=0)
    packed_chunk_count: int = Field(ge=0)
    raw_chars: int = Field(ge=0)
    packed_chars: int = Field(ge=0)
    raw_estimated_tokens: int = Field(ge=0)
    packed_estimated_tokens: int = Field(ge=0)
    bucket: ContextBucket
    over_runtime_budget: bool = False
    over_model_limit: bool | None = None
    truncated: bool = False
    packed_context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProposalContextAggregate(BaseModel):
    """8k~64k 구간과 초과 case ID만 보존하는 context 통계."""

    model_config = ConfigDict(extra="forbid")

    total_case_count: int = Field(ge=0)
    ready_case_count: int = Field(ge=0)
    skipped_reference_missing_count: int = Field(ge=0)
    bucket_counts: dict[ContextBucket, int]
    runtime_over_budget_count: int = Field(ge=0)
    runtime_over_budget_case_ids: list[str] = Field(default_factory=list)
    runtime_over_budget_group_ids: list[str] = Field(default_factory=list)
    model_over_limit_count: int | None = Field(default=None, ge=0)
    model_over_limit_case_ids: list[str] = Field(default_factory=list)
    model_over_limit_group_ids: list[str] = Field(default_factory=list)
    skipped_reference_missing_case_ids: list[str] = Field(default_factory=list)
    skipped_reference_missing_group_ids: list[str] = Field(default_factory=list)
    raw_chars_min: int = Field(ge=0)
    raw_chars_max: int = Field(ge=0)
    raw_chars_p50: int = Field(ge=0)
    packed_chars_min: int = Field(ge=0)
    packed_chars_max: int = Field(ge=0)
    packed_chars_p50: int = Field(ge=0)
    raw_tokens_min: int = Field(ge=0)
    raw_tokens_max: int = Field(ge=0)
    raw_tokens_p50: int = Field(ge=0)
    packed_tokens_min: int = Field(ge=0)
    packed_tokens_max: int = Field(ge=0)
    packed_tokens_p50: int = Field(ge=0)


class ProposalScoreBreakdown(BaseModel):
    """고정 가중치 100점의 범주별 점수."""

    model_config = ConfigDict(extra="forbid")

    evidence_context: float = Field(ge=0, le=25)
    llm_content: float = Field(ge=0, le=35)
    xlsx_result: float = Field(ge=0, le=25)
    tool_flow: float = Field(ge=0, le=15)
    raw_total: float = Field(ge=0, le=100)
    final_total: float = Field(ge=0, le=100)


class ProposalHardGates(BaseModel):
    """총점과 별개로 반드시 통과해야 하는 생성 안전·완결성 조건."""

    model_config = ConfigDict(extra="forbid")

    references_available: bool
    prediction_succeeded: bool
    output_schema_valid: bool
    structured_document_valid: bool
    evidence_leakage_free: bool
    evidence_filter_valid: bool
    event_attendance_structure_valid: bool
    revision_valid: bool
    context_within_runtime_budget: bool
    xlsx_valid: bool
    xlsx_render_contract_valid: bool
    required_tool_stages_succeeded: bool
    passed: bool


class ProposalXlsxStatus(BaseModel):
    """XLSX 원문을 노출하지 않는 구조·셀 검증 상태."""

    model_config = ConfigDict(extra="forbid")

    generation_status: Literal["ok", "not_generated", "projection_invalid", "body_limit", "generation_error"] = (
        "not_generated"
    )
    contract_version: Literal["legacy-v1", "proposal-xlsx-v2"] = "legacy-v1"
    layout_status: Literal["not_applicable", "passed", "failed"] = "not_applicable"
    generated: bool = False
    zip_valid: bool = False
    required_parts_present: bool = False
    title_written: bool = False
    approval_written: bool = False
    body_written: bool = False
    body_text_single_line_valid: bool = True
    body_editable_unmerged_valid: bool = True
    body_default_height_valid: bool = True
    table_wrap_valid: bool = True
    table_explicit_row_height_valid: bool = True
    table_geometry_valid: bool = True
    table_border_valid: bool = True
    fake_pipe_table_absent: bool = True
    appendix_status: Literal["not_required", "valid", "missing", "invalid"] = "not_required"
    expected_table_count: int = Field(default=0, ge=0)
    rendered_table_count: int = Field(default=0, ge=0)
    inline_table_count: int = Field(default=0, ge=0)
    appendix_table_count: int = Field(default=0, ge=0)
    expected_content_block_count: int = Field(default=0, ge=0)
    rendered_content_block_count: int = Field(default=0, ge=0)
    omitted_content_block_count: int = Field(default=0, ge=0)
    content_omission_notified: bool = False
    output_line_count: int = Field(default=0, ge=0)
    projection_omitted_line_count: int = Field(default=0, ge=0)
    projection_truncated: bool = False
    workbook_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProposalStructureDiagnostics(BaseModel):
    """본문 없이 비교하는 V2 구조와 XLSX 평탄화 손실 통계."""

    model_config = ConfigDict(extra="forbid")

    expected_section_count: int = Field(ge=0)
    predicted_section_count: int = Field(ge=0)
    expected_table_row_count: int = Field(ge=0)
    predicted_table_row_count: int = Field(ge=0)
    expected_list_item_count: int = Field(ge=0)
    predicted_list_item_count: int = Field(ge=0)
    expected_block_type_count: int = Field(ge=0)
    predicted_block_type_count: int = Field(ge=0)
    valid_citation_count: int = Field(ge=0)
    invalid_citation_count: int = Field(ge=0)
    structure_fidelity_score: float = Field(ge=0, le=14)


class ProposalProfileDiagnostics(BaseModel):
    """원문 label 없이 유형 선택·해결·section 상대 순서를 나타내는 상태."""

    model_config = ConfigDict(extra="forbid")

    label_status: Literal["not_labeled", "labeled"] = "not_labeled"
    selection_status: Literal["not_labeled", "not_exposed", "auto", "matched", "mismatched"] = "not_labeled"
    resolution_status: Literal["not_labeled", "not_exposed", "matched", "mismatched"] = "not_labeled"
    source_status: Literal["not_labeled", "not_exposed", "valid", "invalid"] = "not_labeled"
    section_order_status: Literal["not_labeled", "matched", "violated"] = "not_labeled"
    expected_order_role_count: int = Field(default=0, ge=0)
    resolved_order_role_count: int = Field(default=0, ge=0)
    order_violation_count: int = Field(default=0, ge=0)
    profile_score: float = Field(default=2.0, ge=0, le=2)


class ProposalEvidenceFilterDiagnostics(BaseModel):
    """artifact ID의 정답 여부를 count/status로 축약한 근거 선별 진단."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_exposed", "passed", "failed"] = "not_exposed"
    summary_status: Literal["not_exposed", "valid", "invalid"] = "not_exposed"
    expected_excluded_artifact_count: int = Field(default=0, ge=0)
    reported_excluded_artifact_count: int = Field(default=0, ge=0)
    correctly_excluded_artifact_count: int = Field(default=0, ge=0)
    false_exclusion_count: int = Field(default=0, ge=0)
    excluded_artifact_recall: float = Field(default=1.0, ge=0, le=1)
    excluded_citation_context_leakage_count: int = Field(default=0, ge=0)
    fallback_used: bool = False


class ProposalEventAttendanceDiagnostics(BaseModel):
    """행사 참석 기안의 세 compact heading과 금지 section을 본문 없이 진단한다."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_applicable", "passed", "failed"] = "not_applicable"
    expected_heading_count: int = Field(default=0, ge=0)
    present_required_heading_count: int = Field(default=0, ge=0)
    missing_required_heading_count: int = Field(default=0, ge=0)
    extra_heading_count: int = Field(default=0, ge=0)
    forbidden_heading_count: int = Field(default=0, ge=0)
    order_valid: bool = True


class ProposalRevisionDiagnostics(BaseModel):
    """수정 내용은 버리고 feedback 반영·보존·원본 불변성만 hash/status로 남긴다."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_applicable", "passed", "failed"] = "not_applicable"
    label_status: Literal["not_labeled", "labeled"] = "not_labeled"
    metadata_status: Literal["not_exposed", "valid", "invalid"] = "not_exposed"
    feedback_application_status: Literal["not_labeled", "matched", "mismatched"] = "not_labeled"
    preservation_status: Literal["not_labeled", "matched", "mismatched"] = "not_labeled"
    base_document_hash_status: Literal["not_exposed", "matched", "mismatched"] = "not_exposed"
    base_workbook_hash_status: Literal["not_exposed", "matched", "mismatched"] = "not_exposed"
    expected_changed_field_count: int = Field(default=0, ge=0)
    applied_changed_field_count: int = Field(default=0, ge=0)
    expected_preserved_field_count: int = Field(default=0, ge=0)
    preserved_field_count: int = Field(default=0, ge=0)
    revision_minor: int | None = Field(default=None, ge=1)
    revision_summary_length: int = Field(default=0, ge=0)
    revision_summary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProposalToolFlowStatus(BaseModel):
    """고정 네 stage의 상태와 순서 검증 결과."""

    model_config = ConfigDict(extra="forbid")

    evidence: StageStatus | Literal["missing"] = "missing"
    llm: StageStatus | Literal["missing"] = "missing"
    workbook: StageStatus | Literal["missing"] = "missing"
    save: StageStatus | Literal["missing"] = "missing"
    ordered: bool = False
    stage_count: int = Field(default=0, ge=0)


class ProposalFactualDiagnostics(BaseModel):
    """검토된 명시 label이 있을 때만 사실 포함·누락·추가를 계산한다."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["not_scored", "scored"] = "not_scored"
    required_fact_count: int = Field(default=0, ge=0)
    supported_fact_count: int = Field(default=0, ge=0)
    omitted_fact_count: int = Field(default=0, ge=0)
    unsupported_label_count: int = Field(default=0, ge=0)
    unsupported_addition_count: int = Field(default=0, ge=0)
    scored_points: float = Field(default=0.0, ge=0)
    possible_points: float = Field(default=0.0, ge=0)
    normalized_scored_points: float | None = Field(default=None, ge=0, le=100)


class ProposalCaseCoverage(BaseModel):
    """case별 검토·split·label 범위와 제품 품질 사용 가능 여부."""

    model_config = ConfigDict(extra="forbid")

    review_status: Literal["candidate", "needs_review", "incomplete", "reviewed"] = "needs_review"
    split: Literal["train", "validation", "test", "unassigned"] = "unassigned"
    expected_label_status: Literal["labeled", "unlabeled"] = "unlabeled"
    factual_label_status: Literal["labeled", "unlabeled"] = "unlabeled"
    scoring_basis: Literal["diagnostic", "reviewed_test"] = "diagnostic"
    product_quality_eligible: bool = False


class ProposalCaseEvaluation(BaseModel):
    """민감 문자열을 제거한 case별 채점 결과."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    group_id: str
    status: Literal["passed", "failed", "skipped_reference_missing", "missing_prediction"]
    scope: Literal["current_tool", "structure_only"]
    prediction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scores: ProposalScoreBreakdown
    hard_gates: ProposalHardGates
    context: ProposalContextCaseStats
    observed_context_estimated_tokens: int | None = Field(default=None, ge=0)
    structure: ProposalStructureDiagnostics
    profile: ProposalProfileDiagnostics
    evidence_filter: ProposalEvidenceFilterDiagnostics
    event_attendance: ProposalEventAttendanceDiagnostics
    revision: ProposalRevisionDiagnostics
    xlsx: ProposalXlsxStatus
    tool_flow: ProposalToolFlowStatus
    timings_ms: ProposalPredictionTimings
    coverage: ProposalCaseCoverage = Field(default_factory=ProposalCaseCoverage)
    factual: ProposalFactualDiagnostics = Field(default_factory=ProposalFactualDiagnostics)
    failure_code: Literal[
        "none",
        "reference_missing",
        "prediction_missing",
        "target_candidate_leakage",
        "proposal_context_limit_exceeded",
        "generation_error",
        "workbook_error",
        "tool_error",
        "unknown",
    ] = "none"


class ProposalContextFailureAggregate(BaseModel):
    """실행 중 관측된 context 초과 길이 통계."""

    model_config = ConfigDict(extra="forbid")

    failure_count: int = Field(ge=0)
    case_ids: list[str] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    packed_estimated_tokens_min: int = Field(ge=0)
    packed_estimated_tokens_max: int = Field(ge=0)
    packed_estimated_tokens_p50: int = Field(ge=0)


class ProposalEvidenceFilterAggregate(BaseModel):
    """경로·파일명·원인 문자열 없는 evidence filter 전체 통계."""

    model_config = ConfigDict(extra="forbid")

    evaluated_case_count: int = Field(ge=0)
    passed_case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    expected_excluded_artifact_count: int = Field(ge=0)
    correctly_excluded_artifact_count: int = Field(ge=0)
    false_exclusion_count: int = Field(ge=0)
    excluded_citation_context_leakage_case_count: int = Field(ge=0)
    summary_invalid_case_count: int = Field(ge=0)
    excluded_artifact_recall: float = Field(ge=0, le=1)


class ProposalSupplementalAggregate(BaseModel):
    """행사 구조와 수정 계약의 적용/통과 건수만 보존한다."""

    model_config = ConfigDict(extra="forbid")

    event_evaluated_case_count: int = Field(ge=0)
    event_passed_case_count: int = Field(ge=0)
    event_failed_case_count: int = Field(ge=0)
    revision_evaluated_case_count: int = Field(ge=0)
    revision_passed_case_count: int = Field(ge=0)
    revision_failed_case_count: int = Field(ge=0)


class ProposalCoverageAggregate(BaseModel):
    """본문 없이 dataset 검토·split·label coverage와 정규화 점수 범위를 집계한다."""

    model_config = ConfigDict(extra="forbid")

    candidate_case_count: int = Field(default=0, ge=0)
    needs_review_case_count: int = Field(default=0, ge=0)
    incomplete_case_count: int = Field(default=0, ge=0)
    reviewed_case_count: int = Field(default=0, ge=0)
    unassigned_case_count: int = Field(default=0, ge=0)
    test_case_count: int = Field(default=0, ge=0)
    expected_labeled_case_count: int = Field(default=0, ge=0)
    factual_labeled_case_count: int = Field(default=0, ge=0)
    factual_scored_case_count: int = Field(default=0, ge=0)
    factual_not_scored_case_count: int = Field(default=0, ge=0)
    product_quality_eligible_case_count: int = Field(default=0, ge=0)
    factual_scored_points: float = Field(default=0.0, ge=0)
    factual_possible_points: float = Field(default=0.0, ge=0)
    factual_normalized_scored_points: float | None = Field(default=None, ge=0, le=100)


class ProposalEvaluationAggregate(BaseModel):
    """기안 평가 전체 집계."""

    model_config = ConfigDict(extra="forbid")

    total_case_count: int = Field(ge=0)
    evaluated_case_count: int = Field(ge=0)
    skipped_case_count: int = Field(ge=0)
    passed_case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    average_total_score: float = Field(ge=0, le=100)
    evidence_context_average: float = Field(ge=0, le=25)
    llm_content_average: float = Field(ge=0, le=35)
    xlsx_result_average: float = Field(ge=0, le=25)
    tool_flow_average: float = Field(ge=0, le=15)
    latency_p50_ms: float = Field(ge=0)
    latency_p95_ms: float = Field(ge=0)
    generated_completed_case_count: int = Field(default=0, ge=0)
    generated_completed_latency_p50_ms: float = Field(default=0.0, ge=0)
    generated_completed_latency_p95_ms: float = Field(default=0.0, ge=0)
    hard_gate_failure_counts: dict[str, int]
    context_failures: ProposalContextFailureAggregate
    evidence_filter: ProposalEvidenceFilterAggregate
    supplemental: ProposalSupplementalAggregate
    coverage: ProposalCoverageAggregate = Field(default_factory=ProposalCoverageAggregate)


class ProposalEvaluationReport(BaseModel):
    """경로·파일명·본문·prompt·응답이 없는 최종 평가 artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-evaluation-report-v1"] = "proposal-evaluation-report-v1"
    mode: EvaluationMode
    evaluation_kind: Literal[
        "preflight",
        "oracle_contract_check",
        "supplied_predictions",
        "live_predictions",
    ] = "preflight"
    deprecated_baseline_alias_used: bool = False
    product_quality_eligible: bool = False
    scope: EvaluationScope
    dataset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_context_window_tokens: int = Field(ge=1)
    reserved_prompt_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    effective_input_budget_tokens: int = Field(ge=1)
    model_context_limit_tokens: int | None = Field(default=None, ge=1)
    pass_threshold: float = Field(ge=0, le=100)
    hard_gate_score_cap: float = Field(ge=0, le=100)
    context: ProposalContextAggregate
    cases: list[ProposalCaseEvaluation]
    aggregate: ProposalEvaluationAggregate


class ProposalLiveArtifactReport(BaseModel):
    """경로·파일명 없이 live 선별 결과만 남기는 artifact 단위 보고."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^art-[0-9a-f]{20}$")
    candidate_role: Literal["reference", "expected_excluded"]
    filter_status: Literal["included", "excluded", "unavailable"]
    input_chunk_count: int = Field(ge=0)
    output_chunk_count: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProposalLiveCaseReport(BaseModel):
    """원문·오류문자열 없이 재개 가능한 live case 실행 상태를 보존한다."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^prp-[0-9a-f]{20}$")
    group_id: str = Field(pattern=r"^grp-[0-9a-f]{16}$")
    status: Literal["completed", "resumed", "failed", "skipped_reference_missing", "deadline_skipped"]
    resumed: bool = False
    reference_status: Literal["ready", "expected_skip", "unexpected_live_failure", "not_evaluated"] = (
        "not_evaluated"
    )
    failure_code: Literal[
        "none",
        "reference_missing",
        "instruction_missing",
        "candidate_missing",
        "target_candidate_leakage",
        "evidence_filter_contract_failed",
        "proposal_context_limit_exceeded",
        "generation_error",
        "workbook_error",
        "checkpoint_error",
        "deadline_exceeded",
    ] = "none"
    target_absent_from_generation_input: bool
    instruction_length: int = Field(ge=0)
    instruction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_artifact_count: int = Field(ge=0)
    included_artifact_count: int = Field(ge=0)
    excluded_artifact_count: int = Field(ge=0)
    input_citation_count: int = Field(ge=0)
    included_citation_count: int = Field(ge=0)
    workbook_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prediction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    elapsed_ms: float = Field(ge=0)
    artifacts: list[ProposalLiveArtifactReport] = Field(default_factory=list)


class ProposalLiveRunReport(BaseModel):
    """SMB 호출 없이 로컬에서 실행한 live prediction의 비식별 checkpoint 요약."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-live-run-report-v1"] = "proposal-live-run-report-v1"
    dataset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requested_case_count: int = Field(ge=0)
    completed_case_count: int = Field(ge=0)
    resumed_case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    skipped_reference_missing_case_count: int = Field(default=0, ge=0)
    expected_reference_skip_case_count: int = Field(default=0, ge=0)
    unexpected_live_reference_failure_case_count: int = Field(default=0, ge=0)
    deadline_skipped_case_count: int = Field(ge=0)
    max_cases: int | None = Field(default=None, ge=1)
    deadline_seconds: float | None = Field(default=None, gt=0)
    total_elapsed_ms: float = Field(ge=0)
    completed_latency_p50_ms: float = Field(default=0.0, ge=0)
    completed_latency_p95_ms: float = Field(default=0.0, ge=0)
    zero_smb_calls: Literal[True] = True
    cases: list[ProposalLiveCaseReport] = Field(default_factory=list)
