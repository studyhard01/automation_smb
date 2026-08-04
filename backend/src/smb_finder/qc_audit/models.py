"""QC 보고서 감사 도메인 모델."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuditStatus = Literal["PASS", "WARNING", "FAIL", "UNVERIFIABLE"]
RuleValueType = Literal["number", "text"]
DraftReviewStatus = Literal["DRAFT"]


class AttachmentMetadata(BaseModel):
    """로컬에 저장한 Playground 첨부파일의 공개 메타데이터."""

    id: str
    filename: str
    media_type: str
    extension: str
    size_bytes: int = Field(ge=1)
    sha256: str
    created_at: str


class ExtractedDocument(BaseModel):
    """PDF/Markdown에서 추출한 로컬 텍스트와 진단 정보."""

    attachment: AttachmentMetadata
    text: str = ""
    status: Literal["ok", "empty", "unsupported", "error"] = "ok"
    detail: str = ""
    text_chars: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0


class SopRule(BaseModel):
    """합성 SOP의 감사 규칙 한 건."""

    id: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    value_type: RuleValueType
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    warning_margin: float = Field(default=0.0, ge=0.0)
    accepted_values: list[str] = Field(default_factory=list)
    criterion: str
    evidence: str


class SopSearchHit(BaseModel):
    """SOP 검색 결과 한 건."""

    rule: SopRule
    score: float = 0.0


class AuditFinding(BaseModel):
    """문서의 한 QC 항목에 대한 감사 판정."""

    rule_id: str
    title: str
    status: AuditStatus
    observed_value: str = ""
    criterion: str
    evidence: str
    reason: str


class AuditResult(BaseModel):
    """첨부 문서 전체 감사 결과."""

    attachment: AttachmentMetadata
    status: AuditStatus
    findings: list[AuditFinding]
    counts: dict[str, int]
    extraction_ms: float = 0.0
    sop_search_ms: float = 0.0
    audit_ms: float = 0.0
    elapsed_ms: float = 0.0


class QcReportDraftRequest(BaseModel):
    """LLM 기반 합성 QC 보고서 초안 생성 입력."""

    model_config = ConfigDict(extra="forbid")

    temperature_c: float = Field(allow_inf_nan=False, description="Aurora chamber temperature 합성 측정값")
    recovery_rate_pct: float = Field(allow_inf_nan=False, description="Nova recovery rate 합성 측정값")
    self_check_status: str = Field(
        min_length=1,
        max_length=40,
        pattern=r"^[^\r\n|]+$",
        description="Orion self-check 합성 상태값",
    )
    operator_notes: str = Field(default="", max_length=1000, description="LLM 서술부에 참고할 선택적 합성 메모")


class QcDraftObservation(BaseModel):
    """초안에 코드가 고정해 넣는 합성 관찰값."""

    rule_id: str
    title: str
    value: str
    unit: str = ""


class QcDraftNarrative(BaseModel):
    """LLM이 생성하는 비수치 서술부."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    interpretation: str = Field(min_length=1, max_length=2000)
    follow_up: list[str] = Field(default_factory=list, max_length=5)


class QcReportDraftResult(BaseModel):
    """결정론적 사실과 LLM 서술을 조립한 QC 보고서 초안."""

    draft_kind: Literal["qc_report_draft"] = "qc_report_draft"
    review_status: DraftReviewStatus = "DRAFT"
    deterministic_status: AuditStatus
    verification_status: AuditStatus
    observations: list[QcDraftObservation]
    findings: list[AuditFinding]
    markdown: str
    provider: str
    model: str
    warnings: list[str] = Field(default_factory=list)
    llm_ms: float = Field(default=0.0, ge=0)
    verification_ms: float = Field(default=0.0, ge=0)
    elapsed_ms: float = Field(default=0.0, ge=0)
    over_budget: bool = False
