"""보고서 tool 공통 실행/포맷 로직."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from smb_finder.config import Settings
from smb_finder.models import ContentSearchRequest
from smb_finder.playground.models import ToolExecutionResult

from .models import ReportCandidateFile, ReportChecklistItem, ReportKind, ReportToolResponse


@dataclass(frozen=True)
class ReportProfile:
    """검사 보고서 tool별 고정 프로필."""

    kind: ReportKind
    display_name: str
    search_terms: tuple[str, ...]
    checklist: tuple[ReportChecklistItem, ...]
    next_actions: tuple[str, ...]


def run_report_tool(
    *,
    args: dict[str, Any],
    settings: Settings,
    content_searcher: Any | None,
    profile: ReportProfile,
) -> ToolExecutionResult:
    """로컬 내용 인덱스에서 보고서 후보를 찾고 검사별 작성 체크리스트를 반환한다."""
    if content_searcher is None:
        return ToolExecutionResult(
            status="error",
            result_text="내용 검색 인덱스가 아직 준비되지 않아 보고서 tool을 실행할 수 없습니다.",
            observation_text="보고서 tool 실행 실패: 내용 검색 인덱스가 준비되지 않았습니다.",
            error_code="content_searcher_not_ready",
        )

    started = time.perf_counter()
    query = _text_arg(args, "query") or _text_arg(args, "report_query")
    context = _text_arg(args, "context") or _text_arg(args, "case_context")
    limit = _positive_int(args.get("limit"), settings.content_default_limit)
    search_query = _build_search_query(query=query, context=context, terms=profile.search_terms)

    response = content_searcher.search(ContentSearchRequest(query=search_query, limit=limit))
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    candidates = [
        ReportCandidateFile(
            name=str(getattr(hit, "name", "")),
            path=str(getattr(hit, "path", "")),
            ext=str(getattr(hit, "ext", "")),
            score=float(getattr(hit, "score", 0.0) or 0.0),
        )
        for hit in list(getattr(response, "hits", []))
    ]
    over_budget = bool(getattr(response, "over_budget", False)) or elapsed_ms > settings.content_search_budget_ms
    data = ReportToolResponse(
        report_kind=profile.kind,
        display_name=profile.display_name,
        query=search_query,
        candidate_files=candidates,
        checklist=list(profile.checklist),
        next_actions=list(profile.next_actions),
        safety_notes=[
            "이 tool은 로컬 SMB/내용 인덱스 기반 업무 보조이며, 보고서 확정이나 진단 판정을 자동 수행하지 않습니다.",
            "환자 식별자와 원문 검사 데이터는 외부 LLM이나 외부 tracing으로 보내지 않는 운영을 기본으로 합니다.",
            "최종 보고 전 LIS/원내 시스템 원본, 검사 책임자 검토, 기관 표준 문구를 확인하세요.",
        ],
        elapsed_ms=elapsed_ms,
        over_budget=over_budget,
        indexed_files=int(getattr(response, "indexed_files", 0) or 0),
    )
    return ToolExecutionResult(
        result_text=_format_report_response(data),
        observation_text=_format_safe_observation(data),
        result_payload=data.model_dump(),
        arguments_summary=(
            f"query_len={len(query)} context_len={len(context)} candidates={len(candidates)} elapsed_ms={elapsed_ms}"
        ),
    )


def _text_arg(args: dict[str, Any], key: str) -> str:
    value = args.get(key, "")
    return str(value or "").strip()


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, fallback)
    return max(1, min(parsed, 20))


def _build_search_query(*, query: str, context: str, terms: tuple[str, ...]) -> str:
    pieces = [query, context, " ".join(terms)]
    return " ".join(piece for piece in pieces if piece).strip()


def _format_report_response(data: ReportToolResponse) -> str:
    lines = [
        f"{data.display_name} 보조 결과 ({data.elapsed_ms}ms)",
        "",
        f"검색 후보 {len(data.candidate_files)}건 / 내용 인덱스 {data.indexed_files}파일",
    ]
    if data.over_budget:
        lines.append("주의: 시간 예산을 초과해 일부 결과일 수 있습니다.")
    if data.candidate_files:
        lines.append("")
        lines.append("후보 문서/템플릿:")
        for index, hit in enumerate(data.candidate_files, start=1):
            ext = f" ({hit.ext})" if hit.ext else ""
            lines.append(f"{index}. {hit.name}{ext} - {hit.path}")
    else:
        lines.append("후보 문서/템플릿을 찾지 못했습니다. 내용 DB화 범위와 검색 단서를 확인하세요.")

    lines.append("")
    lines.append("작성 전 체크리스트:")
    for item in data.checklist:
        required = "필수" if item.required else "권장"
        lines.append(f"- [{required}] {item.title}: {item.detail}")

    lines.append("")
    lines.append("다음 행동:")
    for action in data.next_actions:
        lines.append(f"- {action}")

    lines.append("")
    lines.append("보안/품질 메모:")
    for note in data.safety_notes:
        lines.append(f"- {note}")
    return "\n".join(lines)


def _format_safe_observation(data: ReportToolResponse) -> str:
    """외부 LLM에 전달해도 경로/본문이 빠진 최소 관측값."""
    checklist = ", ".join(item.title for item in data.checklist[:5])
    return (
        f"{data.display_name} tool 실행 완료. 후보 문서 {len(data.candidate_files)}건, "
        f"elapsed_ms={data.elapsed_ms}, over_budget={data.over_budget}. "
        f"체크리스트 항목: {checklist}. "
        "구체적인 파일명/경로/검사 데이터는 외부 LLM observation에서 제외했습니다."
    )
