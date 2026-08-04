"""세포유전 보고서 업무 보조 tool."""

from __future__ import annotations

from typing import Any

from smb_finder.config import Settings
from smb_finder.playground.models import ToolExecutionResult

from .common import ReportProfile, run_report_tool
from .models import ReportChecklistItem

_PROFILE = ReportProfile(
    kind="cytogenetics",
    display_name="세포유전 보고서",
    search_terms=("세포유전", "염색체", "핵형", "karyotype", "FISH", "ISCN", "보고서", "template"),
    checklist=(
        ReportChecklistItem(title="검체/검사명", detail="검체 종류, 의뢰 검사, 접수일/보고일이 원내 기록과 일치하는지 확인"),
        ReportChecklistItem(title="배양/분석 조건", detail="분석 세포 수, band level, FISH probe 등 검사 방법 핵심값 확인"),
        ReportChecklistItem(title="ISCN 표기", detail="핵형 또는 FISH 결과를 최신 기관 표준 표기법과 승인 템플릿에 맞춤"),
        ReportChecklistItem(title="판독 문구", detail="정상/이상 소견과 임상적 해석 문구를 책임자 검토 기준에 맞춤"),
        ReportChecklistItem(title="품질 제한", detail="검체 상태, 세포 수 부족, mosaicism 한계 등 제한사항이 있으면 명시"),
    ),
    next_actions=(
        "후보 문서/템플릿을 열어 같은 검사 유형의 승인 문구와 표 형식을 확인하세요.",
        "핵형/ISCN 결과값은 이 tool에 의존하지 말고 원내 판독 원본에서 복사·대조하세요.",
        "보고 전 검사 책임자 또는 판독자 승인 절차를 완료하세요.",
    ),
)


def run_cytogenetics_report(
    args: dict[str, Any],
    *,
    settings: Settings,
    content_searcher: Any | None,
) -> ToolExecutionResult:
    """세포유전 보고서 작성에 필요한 로컬 후보 문서와 체크리스트를 반환한다."""
    return run_report_tool(args=args, settings=settings, content_searcher=content_searcher, profile=_PROFILE)
