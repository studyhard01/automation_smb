"""검사 보고서 tool 공통 계약 모델."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ReportKind = Literal["cytogenetics", "ngs"]
KaryotypeSummaryKind = Literal["cytogenetics_karyotype_summary"]


class CytogeneticsKaryotypeSummaryRequest(BaseModel):
    """API에서 선택할 LLM과 ISCN 핵형을 전달하는 입력."""

    iscn: str = Field(description="ISCN 핵형 문자열. 환자 식별정보는 포함하지 않는다.")
    provider: Literal["local", "openai"] = "local"
    local_base_url: str = ""
    model: str = ""


class ReportCandidateFile(BaseModel):
    """로컬 내용 인덱스에서 찾은 보고서/템플릿 후보."""

    name: str = Field(description="파일 이름")
    path: str = Field(description="공유 루트 기준 상대 경로")
    ext: str = Field(default="", description="확장자")
    score: float = Field(default=0.0, description="검색 관련도 점수")


class ReportChecklistItem(BaseModel):
    """보고서 작성 전 확인할 항목."""

    title: str
    detail: str
    required: bool = True


class ReportToolResponse(BaseModel):
    """보고서 tool 실행 결과."""

    report_kind: ReportKind
    display_name: str
    query: str
    candidate_files: list[ReportCandidateFile] = Field(default_factory=list)
    checklist: list[ReportChecklistItem] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    over_budget: bool = False
    indexed_files: int = 0


class CytogeneticsKaryotypeSummaryResponse(BaseModel):
    """선택한 LLM이 생성한 ISCN 핵형 요약 결과."""

    summary_kind: KaryotypeSummaryKind = "cytogenetics_karyotype_summary"
    display_name: str = "핵형분석요약"
    karyotype_summary: str
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: float = Field(default=0.0, ge=0)
    over_budget: bool = False
