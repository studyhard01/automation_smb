"""기안 생성 도구의 오프라인 평가 입력과 비식별 보고 계약."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ContextBucket = Literal["lte_8k", "8k_to_16k", "16k_to_32k", "32k_to_64k", "gt_64k"]
EvaluationMode = Literal["preflight", "baseline", "predictions"]
EvaluationScope = Literal["all", "current_tool", "structure_only"]
ProposalStage = Literal["evidence", "llm", "workbook", "save"]
StageStatus = Literal["ok", "error", "skipped", "simulated"]


class ProposalDatasetChunk(BaseModel):
    """외부 documents.jsonl에서 평가에 필요한 최소 chunk 필드."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(pattern=r"^chk-[0-9a-f]{16}$")
    text: str = Field(min_length=1)


class ProposalDatasetArtifact(BaseModel):
    """외부 artifact의 ID·hash·추출 chunk만 읽는 최소 계약."""

    model_config = ConfigDict(extra="ignore")

    artifact_id: str = Field(pattern=r"^art-[0-9a-f]{20}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
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


class ProposalDatasetCaseInput(BaseModel):
    """외부 proposal_cases.jsonl의 평가용 최소 계약."""

    model_config = ConfigDict(extra="ignore")

    case_id: str = Field(pattern=r"^prp-[0-9a-f]{20}$")
    group_id: str = Field(pattern=r"^grp-[0-9a-f]{16}$")
    target_artifact_id: str = Field(pattern=r"^art-[0-9a-f]{20}$")
    reference_artifact_ids: list[str] = Field(default_factory=list)
    excluded_post_event_artifact_ids: list[str] = Field(default_factory=list)
    expected: ProposalDatasetExpected | None = None
    review_status: Literal["candidate", "needs_review", "incomplete"] = "needs_review"
    evaluation_scope: Literal["current_tool", "structure_only"] = "structure_only"


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


class ProposalPrediction(BaseModel):
    """외부 실행 결과 JSONL 한 줄. workbook 경로와 출력 문자열은 입력에서만 사용한다."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^prp-[0-9a-f]{20}$")
    status: Literal["ok", "error", "skipped"] = "ok"
    fields: ProposalPredictedFields | None = None
    document: ProposalPredictedDocument | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    citation_map: dict[str, str] = Field(default_factory=dict)
    context: ProposalPredictionContext | None = None
    context_usage: ProposalPredictionContextUsage | None = None
    projection: ProposalPredictionProjection | None = None
    tool_events: list[ProposalToolStageEvent] = Field(default_factory=list)
    workbook_path: str | None = None
    timings_ms: ProposalPredictionTimings = Field(default_factory=ProposalPredictionTimings)
    failure_code: Literal[
        "none",
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
    context_within_runtime_budget: bool
    xlsx_valid: bool
    required_tool_stages_succeeded: bool
    passed: bool


class ProposalXlsxStatus(BaseModel):
    """XLSX 원문을 노출하지 않는 구조·셀 검증 상태."""

    model_config = ConfigDict(extra="forbid")

    generation_status: Literal["ok", "not_generated", "projection_invalid", "body_limit", "generation_error"] = (
        "not_generated"
    )
    generated: bool = False
    zip_valid: bool = False
    required_parts_present: bool = False
    title_written: bool = False
    approval_written: bool = False
    body_written: bool = False
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


class ProposalToolFlowStatus(BaseModel):
    """고정 네 stage의 상태와 순서 검증 결과."""

    model_config = ConfigDict(extra="forbid")

    evidence: StageStatus | Literal["missing"] = "missing"
    llm: StageStatus | Literal["missing"] = "missing"
    workbook: StageStatus | Literal["missing"] = "missing"
    save: StageStatus | Literal["missing"] = "missing"
    ordered: bool = False
    stage_count: int = Field(default=0, ge=0)


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
    xlsx: ProposalXlsxStatus
    tool_flow: ProposalToolFlowStatus
    timings_ms: ProposalPredictionTimings
    failure_code: Literal[
        "none",
        "reference_missing",
        "prediction_missing",
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
    hard_gate_failure_counts: dict[str, int]
    context_failures: ProposalContextFailureAggregate


class ProposalEvaluationReport(BaseModel):
    """경로·파일명·본문·prompt·응답이 없는 최종 평가 artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-evaluation-report-v1"] = "proposal-evaluation-report-v1"
    mode: EvaluationMode
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
