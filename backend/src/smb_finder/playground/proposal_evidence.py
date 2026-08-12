"""기안 유형에 맞지 않는 첨부 문서를 LLM 입력 전에 제외한다."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from smb_finder.models import DocumentCitation

ProposalEvidenceType = Literal["purchase", "event_attendance", "general"]


class ProposalEvidenceFilterSummary(BaseModel):
    """파일명·경로·식별자를 제외한 공개용 근거 선별 집계."""

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


@dataclass(frozen=True)
class FilteredProposalEvidence:
    """LLM에 전달할 근거와 공개 가능한 집계."""

    citations: list[DocumentCitation]
    summary: ProposalEvidenceFilterSummary


_RECEIPT_TERMS = (
    "영수증",
    "카드 전표",
    "카드전표",
    "승인 전표",
    "승인전표",
)
_PAYMENT_CONFIRMATION_TERMS = (
    "결제 내역",
    "결제내역",
    "결제 확인",
    "결제확인",
    "결제 완료",
    "결제완료",
    "승인 내역",
    "승인내역",
    "거래 명세서",
    "거래명세서",
    "세금 계산서",
    "세금계산서",
    "입금 확인",
    "입금확인",
)
_ATTENDANCE_CONFIRMATION_TERMS = (
    "참석 확인증",
    "참석확인증",
    "참가 확인증",
    "참가확인증",
    "출석 확인",
    "출석확인",
    "수료증",
    "이수증",
    "참석 증명",
    "참석증명",
    "출입증",
    "명찰",
)
_POST_EVENT_REPORT_TERMS = (
    "행사 결과 보고",
    "행사결과보고",
    "참가 결과 보고",
    "참가결과보고",
    "출장 결과 보고",
    "출장결과보고",
    "정산 보고",
    "정산보고",
)
_PURCHASE_COMPLETION_TERMS = (
    "납품 확인서",
    "납품확인서",
    "납품 완료",
    "납품완료",
    "검수 확인서",
    "검수확인서",
    "검수 완료",
    "검수완료",
)
_STRONG_POST_ACTION_TERMS = (
    _RECEIPT_TERMS
    + _PAYMENT_CONFIRMATION_TERMS
    + _ATTENDANCE_CONFIRMATION_TERMS
    + _POST_EVENT_REPORT_TERMS
    + _PURCHASE_COMPLETION_TERMS
)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _mentions_any(value: str, terms: tuple[str, ...]) -> bool:
    compact = value.replace(" ", "")
    return any(_normalized(term) in value or _normalized(term).replace(" ", "") in compact for term in terms)


def _exclusion_reason(
    proposal_type: ProposalEvidenceType,
    instruction: str,
    document_text: str,
) -> str | None:
    """명시적 사용자 요구는 존중하되 사후 증빙은 기본 본문 근거에서 제외한다."""

    if proposal_type == "general":
        return None
    if proposal_type == "event_attendance":
        if _mentions_any(document_text, _RECEIPT_TERMS) and not _mentions_any(instruction, _RECEIPT_TERMS):
            return "receipt"
        if _mentions_any(document_text, _PAYMENT_CONFIRMATION_TERMS) and not _mentions_any(
            instruction, _PAYMENT_CONFIRMATION_TERMS
        ):
            return "payment_confirmation"
        if _mentions_any(document_text, _ATTENDANCE_CONFIRMATION_TERMS) and not _mentions_any(
            instruction, _ATTENDANCE_CONFIRMATION_TERMS
        ):
            return "attendance_confirmation"
        if _mentions_any(document_text, _POST_EVENT_REPORT_TERMS) and not _mentions_any(
            instruction, _POST_EVENT_REPORT_TERMS
        ):
            return "post_event_report"
    if proposal_type == "purchase":
        if _mentions_any(document_text, _RECEIPT_TERMS) and not _mentions_any(instruction, _RECEIPT_TERMS):
            return "receipt"
        if _mentions_any(document_text, _PAYMENT_CONFIRMATION_TERMS) and not _mentions_any(
            instruction, _PAYMENT_CONFIRMATION_TERMS
        ):
            return "payment_confirmation"
        if _mentions_any(document_text, _PURCHASE_COMPLETION_TERMS) and not _mentions_any(
            instruction, _PURCHASE_COMPLETION_TERMS
        ):
            return "purchase_completion"
    return None


def is_strong_post_action_metadata(value: str) -> bool:
    """자동 유형 판정에서 제외할 명확한 사후자료 제목인지 확인한다."""

    return _mentions_any(_normalized(value), _STRONG_POST_ACTION_TERMS)


def filter_proposal_evidence(
    evidence: list[DocumentCitation],
    *,
    proposal_type: ProposalEvidenceType,
    instruction: str,
) -> FilteredProposalEvidence:
    """같은 문서의 모든 citation을 함께 포함하거나 제외해 근거 누출을 막는다."""

    grouped: dict[tuple[UUID, UUID], list[DocumentCitation]] = defaultdict(list)
    for citation in evidence:
        grouped[(citation.doc_id, citation.revision_id)].append(citation)

    included: list[DocumentCitation] = []
    excluded_reasons: Counter[str] = Counter()
    normalized_instruction = _normalized(instruction)
    for citations in grouped.values():
        document_metadata = _normalized(
            " ".join(
                [
                    *(citation.title for citation in citations),
                    *(" ".join(citation.section_path) for citation in citations),
                ]
            )
        )
        reason = _exclusion_reason(proposal_type, normalized_instruction, document_metadata)
        if reason is None:
            included.extend(citations)
        else:
            excluded_reasons[reason] += 1

    included_keys = {(item.doc_id, item.revision_id) for item in included}
    summary = ProposalEvidenceFilterSummary(
        input_document_count=len(grouped),
        included_document_count=len(included_keys),
        excluded_document_count=len(grouped) - len(included_keys),
        input_citation_count=len(evidence),
        included_citation_count=len(included),
        excluded_citation_count=len(evidence) - len(included),
        excluded_reason_counts=dict(sorted(excluded_reasons.items())),
        fallback_used=False,
    )
    return FilteredProposalEvidence(citations=included, summary=summary)
