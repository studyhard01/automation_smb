"""기안 생성용 검색 근거를 재현 가능하고 제한된 LLM 컨텍스트로 압축한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smb_finder.models import DocumentCitation

_WORD_PATTERN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_NUMBER_OR_DATE_PATTERN = re.compile(
    r"(?:\d{4}[./-]\d{1,2}(?:[./-]\d{1,2})?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:원|만원|억|%|일|개월|년|개|명))"
)
_TABLE_HEADER_WORDS = (
    "구분",
    "항목",
    "내용",
    "금액",
    "단가",
    "수량",
    "합계",
    "일정",
    "기간",
    "담당",
    "비고",
)
_TOKEN_ESTIMATE_NUMERATOR = 5
_TOKEN_ESTIMATE_DENOMINATOR = 8


def estimate_proposal_tokens(char_count: int) -> int:
    """합성 실측에 여유를 둔 1.6자/token 비율로 입력 token을 추정한다."""

    normalized = max(0, char_count)
    return (normalized * _TOKEN_ESTIMATE_NUMERATOR + _TOKEN_ESTIMATE_DENOMINATOR - 1) // _TOKEN_ESTIMATE_DENOMINATOR


class ProposalContextUsage(BaseModel):
    """원문이나 경로를 노출하지 않는 기안 컨텍스트 사용량."""

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


@dataclass(frozen=True)
class PackedProposalContext:
    """LLM에 전달할 근거 문자열과 공개 가능한 사용량 메타데이터."""

    text: str
    citation_ids: tuple[str, ...]
    source_chunk_ids: tuple[str, ...]
    context_sha256: str
    usage: ProposalContextUsage


@dataclass(frozen=True)
class _RankedCitation:
    citation: DocumentCitation
    relevance: float
    identity: str


def _normalized_excerpt(value: str) -> str:
    return " ".join(value.casefold().split())


def _document_key(citation: DocumentCitation) -> tuple[str, str]:
    return str(citation.doc_id), str(citation.revision_id)


def _citation_identity(citation: DocumentCitation) -> str:
    payload = "\x1f".join(
        (
            str(citation.doc_id),
            str(citation.revision_id),
            str(citation.chunk_id),
            _normalized_excerpt(citation.excerpt),
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _query_terms(instruction: str) -> set[str]:
    return {match.group().casefold() for match in _WORD_PATTERN.finditer(instruction)}


def _relevance(citation: DocumentCitation, terms: set[str]) -> float:
    haystack = f"{citation.title} {' '.join(citation.section_path)} {citation.excerpt}".casefold()
    overlap = sum(1 for term in terms if term in haystack)
    return float(citation.scores.rrf) + overlap


def _is_table_header(line: str) -> bool:
    normalized = line.casefold()
    delimiter_count = line.count("|") + line.count("\t")
    header_hits = sum(1 for word in _TABLE_HEADER_WORDS if word in normalized)
    return delimiter_count >= 1 and header_hits >= 1 or header_hits >= 2


def _line_priority(line: str, terms: set[str]) -> int:
    normalized = line.casefold()
    if _is_table_header(line):
        return 4
    if _NUMBER_OR_DATE_PATTERN.search(line):
        return 3
    if any(term in normalized for term in terms):
        return 2
    return 1


def _compact_excerpt(excerpt: str, limit: int, terms: set[str]) -> str:
    """수치·날짜·표 머리글을 우선 보존한 뒤 원래 순서로 재조립한다."""

    normalized = excerpt.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return normalized[:limit]

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return normalized[:limit]
    ranked_indexes = sorted(range(len(lines)), key=lambda index: (-_line_priority(lines[index], terms), index))
    selected: set[int] = set()
    used = 0
    for index in ranked_indexes:
        available = limit - used - (1 if selected else 0)
        if available <= 0:
            break
        line = lines[index]
        if len(line) <= available:
            selected.add(index)
            used += len(line) + (1 if used else 0)
        elif not selected:
            selected.add(index)
            lines[index] = line[:available].rstrip()
            used += len(lines[index])
    compacted = "\n".join(lines[index] for index in sorted(selected))
    return compacted[:limit].rstrip()


def pack_proposal_context(
    instruction: str,
    citations: list[DocumentCitation],
    *,
    max_chars: int,
    per_document_chars: int,
    max_citations: int,
    retry_count: int = 0,
    first_attempt_context_chars: int | None = None,
) -> PackedProposalContext:
    """중복·문서 편중을 막고 관련도순으로 근거를 정해진 문자 예산에 넣는다."""

    if max_chars < 1 or per_document_chars < 1 or max_citations < 1:
        raise ValueError("기안 컨텍스트 제한은 1 이상이어야 합니다.")

    terms = _query_terms(instruction)
    ranked = sorted(
        (
            _RankedCitation(
                citation=citation,
                relevance=_relevance(citation, terms),
                identity=_citation_identity(citation),
            )
            for citation in citations
            if citation.excerpt.strip()
        ),
        key=lambda item: (
            -item.relevance,
            str(item.citation.doc_id),
            str(item.citation.revision_id),
            str(item.citation.chunk_id),
            item.identity,
        ),
    )
    unique: list[_RankedCitation] = []
    seen_excerpts: set[str] = set()
    for item in ranked:
        excerpt_key = _normalized_excerpt(item.citation.excerpt)
        if excerpt_key in seen_excerpts:
            continue
        seen_excerpts.add(excerpt_key)
        unique.append(item)

    fragments: list[str] = []
    citation_ids: list[str] = []
    source_chunk_ids: list[str] = []
    document_usage: dict[tuple[str, str], int] = {}
    packed_documents: set[tuple[str, str]] = set()
    excerpt_was_truncated = False
    for item in unique:
        if len(fragments) >= max_citations:
            break
        document_key = _document_key(item.citation)
        document_remaining = per_document_chars - document_usage.get(document_key, 0)
        global_remaining = max_chars - sum(len(fragment) for fragment in fragments) - max(0, len(fragments) * 2)
        if document_remaining <= 0 or global_remaining <= 0:
            continue
        citation_id = f"E{len(fragments) + 1:03d}"
        title = " ".join(item.citation.title.split())[:160]
        section = " > ".join(item.citation.section_path).strip()[:240]
        header = f"[{citation_id}]\n문서: {title}"
        if section:
            header += f"\n구역: {section}"
        header += "\n내용:\n"
        excerpt_limit = min(document_remaining, global_remaining) - len(header)
        if excerpt_limit <= 0:
            continue
        excerpt = _compact_excerpt(item.citation.excerpt, excerpt_limit, terms)
        if not excerpt:
            continue
        source_excerpt = item.citation.excerpt.replace("\r\n", "\n").replace("\r", "\n").strip()
        excerpt_was_truncated = excerpt_was_truncated or excerpt != source_excerpt
        fragment = f"{header}{excerpt}"
        fragments.append(fragment)
        citation_ids.append(citation_id)
        source_chunk_ids.append(str(item.citation.chunk_id))
        document_usage[document_key] = document_usage.get(document_key, 0) + len(fragment)
        packed_documents.add(document_key)

    text = "\n\n".join(fragments)
    source_documents = {_document_key(citation) for citation in citations}
    usage = ProposalContextUsage(
        source_citation_count=len(citations),
        packed_citation_count=len(fragments),
        deduplicated_citation_count=max(0, len(ranked) - len(unique)),
        source_document_count=len(source_documents),
        packed_document_count=len(packed_documents),
        context_budget_chars=max_chars,
        context_chars=len(text),
        estimated_input_tokens=estimate_proposal_tokens(len(instruction) + len(text)),
        truncated=excerpt_was_truncated or len(fragments) < len(unique),
        retry_count=retry_count,
        first_attempt_context_chars=first_attempt_context_chars,
    )
    return PackedProposalContext(
        text=text,
        citation_ids=tuple(citation_ids),
        source_chunk_ids=tuple(source_chunk_ids),
        context_sha256=sha256(text.encode("utf-8")).hexdigest(),
        usage=usage,
    )
