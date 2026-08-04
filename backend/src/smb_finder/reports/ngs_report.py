"""NGS 보고서 업무 보조 tool."""

from __future__ import annotations

from typing import Any

from smb_finder.config import Settings
from smb_finder.playground.models import ToolExecutionResult

from .common import ReportProfile, run_report_tool
from .models import ReportChecklistItem

_PROFILE = ReportProfile(
    kind="ngs",
    display_name="NGS 보고서",
    search_terms=("NGS", "panel", "variant", "CNV", "fusion", "coverage", "HGVS", "ACMG", "AMP", "보고서", "template"),
    checklist=(
        ReportChecklistItem(title="검체/패널 정보", detail="검체 종류, 검사 패널, 분석 버전, 접수일/보고일 확인"),
        ReportChecklistItem(title="QC 지표", detail="read depth, coverage, uniformity, tumor fraction 등 기관 필수 QC 기준 확인"),
        ReportChecklistItem(title="변이 표기", detail="gene, transcript, HGVS c./p., VAF, tier/classification 표기 일관성 확인"),
        ReportChecklistItem(title="해석 근거", detail="ACMG/AMP, OncoKB/CIViC 등 기관에서 승인한 근거 체계와 문구 확인"),
        ReportChecklistItem(title="한계/주의", detail="검출 한계, 낮은 coverage, CNV/fusion 별도 검증 필요 여부 명시"),
    ),
    next_actions=(
        "후보 문서/템플릿에서 같은 패널·질환군 보고서의 섹션 순서와 표 형식을 확인하세요.",
        "변이와 임상 해석은 분석 파이프라인 원본, IGV/검증 결과, 승인된 지식베이스 기준으로 대조하세요.",
        "최종 보고 전 책임자 검토와 LIS 업로드 형식을 확인하세요.",
    ),
)


def run_ngs_report(
    args: dict[str, Any],
    *,
    settings: Settings,
    content_searcher: Any | None,
) -> ToolExecutionResult:
    """NGS 보고서 작성에 필요한 로컬 후보 문서와 체크리스트를 반환한다."""
    return run_report_tool(args=args, settings=settings, content_searcher=content_searcher, profile=_PROFILE)
