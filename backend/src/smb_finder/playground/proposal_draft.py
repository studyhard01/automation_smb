"""로컬 LLM 제목을 기안 XLSX에 넣고 새 파일로 보관하는 서비스."""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import math
import re
import threading
import time
import unicodedata
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol
from urllib.parse import urlparse
from uuid import UUID
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from smb_finder.config import Settings
from smb_finder.models import DocumentCitation

from .document_models import SelectedFileContext
from .proposal_context import PackedProposalContext, ProposalContextUsage, estimate_proposal_tokens, pack_proposal_context
from .proposal_evidence import (
    ProposalEvidenceFilterSummary,
    filter_proposal_evidence,
    is_strong_post_action_metadata,
)
from .upload_service import UploadError, UploadManager, build_versioned_filename

_logger = logging.getLogger(__name__)
_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "proposal_draft.xlsx"
_TEMPLATE_NAME = "기안지_초안.xlsx"
_PROPOSAL_PREFIX = "[기안] "
_TITLE_SHEET_NAME = "기안지"
_TITLE_CELL = "C8"
_APPROVAL_REQUEST_CELL = "A10"
_BODY_START_ROW = 15
_BODY_END_ROW = 47
_STRUCTURED_BODY_END_ROW = 120
_BODY_DEFAULT_STYLE = "7"
_APPENDIX_SHEET_NAME = "세부내용"
_BODY_FIRST_COLUMN = 1
_BODY_LAST_COLUMN = 26
_INLINE_TABLE_MAX_COLUMNS = 6
_WRAP_DISPLAY_WIDTH = 84
_MAX_ROW_HEIGHT = 390.0
_ROW_HEIGHT_PER_LINE = 16.5
_LEGACY_BODY_MAX_CHARS = 6000
_LEGACY_LINE_MAX_CHARS = 1000
_PROJECTION_OMISSION_NOTICE = "※ 구조화 초안의 일부 세부 행은 엑셀 표시 범위로 인해 생략되었습니다."
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_EXTENDED_PROPERTIES_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_DOC_PROPS_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_INVALID_TITLE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NUMBER_OR_DATE_PATTERN = re.compile(r"\d")
_MONEY_INTENT_PATTERN = re.compile(r"참가비|비용|금액|예산|결제|유료")
_MONEY_VALUE_PATTERN = re.compile(r"(?<!\d)\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)?\s*원|무료|무상")
_XML_ROOT_PATTERN = re.compile(r"<(?![?!])[^>\s]+(?:\s[^>]*)?>")
_XML_NAMESPACE_PATTERN = re.compile(r"\sxmlns(?::([A-Za-z_][\w.-]*))?=(['\"])(.*?)\2")

ProposalTypeRequest = Literal["auto", "purchase", "event_attendance", "general"]
ResolvedProposalType = Literal["purchase", "event_attendance", "general"]
ProposalTypeSource = Literal["user", "rule", "fallback"]


@dataclass(frozen=True)
class ProposalTypeResolution:
    """사용자 선택과 보수적 규칙으로 확정한 기안 유형."""

    proposal_type: ResolvedProposalType
    source: ProposalTypeSource


class ProposalDraftError(RuntimeError):
    """내부 경로와 원본 예외를 숨기고 공개할 수 있는 기안 초안 오류."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        context_usage: ProposalContextUsage | None = None,
        evidence_filter: ProposalEvidenceFilterSummary | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.context_usage = context_usage
        self.evidence_filter = evidence_filter


class ProposalDraftGenerateRequest(BaseModel):
    """선택 문서와 사용자의 기안 설명을 결합하기 위한 요청."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2000)
    selected_files: list[SelectedFileContext] = Field(min_length=1, max_length=5)
    proposal_type: ProposalTypeRequest = "auto"

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        """공백뿐인 LLM 지시는 API 경계에서 거부한다."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("작성할 기안에 대한 설명을 입력해 주세요.")
        return normalized


class ProposalDraftRevisionRequest(BaseModel):
    """기존 초안의 선택 문서 범위를 유지한 피드백 수정 요청."""

    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(min_length=1, max_length=2000)
    selected_files: list[SelectedFileContext] = Field(min_length=1, max_length=5)

    @field_validator("feedback")
    @classmethod
    def normalize_feedback(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("수정할 내용을 입력해 주세요.")
        return normalized


class ProposalClarificationAnswer(BaseModel):
    """확인 질문 하나에 대한 사용자 답변."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(pattern=r"^Q\d{3}$")
    answer: str = Field(min_length=1, max_length=500)

    @field_validator("answer")
    @classmethod
    def normalize_answer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("확인 질문에 대한 답변을 입력해 주세요.")
        return normalized


class ProposalDraftClarificationRequest(BaseModel):
    """보류된 기안의 필수 확인 질문을 한 번에 답하는 요청."""

    model_config = ConfigDict(extra="forbid")

    answers: list[ProposalClarificationAnswer] = Field(min_length=1, max_length=3)
    selected_files: list[SelectedFileContext] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_unique_questions(self) -> ProposalDraftClarificationRequest:
        question_ids = [answer.question_id for answer in self.answers]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("같은 확인 질문에는 한 번만 답할 수 있습니다.")
        return self


class ProposalDraftFields(BaseModel):
    """로컬 LLM과 XLSX 삽입이 공유하는 세 필드 계약."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    approval_request: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=6000)

    @field_validator("title", "approval_request", "body")
    @classmethod
    def normalize_text(cls, value: str, info) -> str:  # noqa: ANN001
        """XML에 넣을 수 없는 제어 문자와 빈 응답을 거부한다."""

        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError(f"{info.field_name} 값이 비어 있습니다.")
        if any(ord(char) < 32 and char not in {"\n", "\t"} for char in normalized):
            raise ValueError(f"{info.field_name} 값에 허용되지 않는 제어 문자가 있습니다.")
        if info.field_name == "title":
            normalized = _clean_title(normalized)
        return normalized


class ProposalParagraphBlock(BaseModel):
    """연속된 설명 문단."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["paragraph"]
    text: str = Field(min_length=1, max_length=3000)


class ProposalListBlock(BaseModel):
    """순서 또는 비순서 목록."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["list"]
    ordered: bool
    items: list[str] = Field(min_length=1, max_length=30)

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 1000 for item in normalized):
            raise ValueError("목록 항목은 1~1000자의 비어 있지 않은 문자열이어야 합니다.")
        return normalized


class ProposalTableBlock(BaseModel):
    """열 이름과 같은 폭의 행으로 구성한 표."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["table"]
    headers: list[str] = Field(min_length=1, max_length=20)
    rows: list[list[str]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_table_shape(self) -> ProposalTableBlock:
        width = len(self.headers)
        if any(not header.strip() for header in self.headers):
            raise ValueError("표 머리글은 비어 있을 수 없습니다.")
        if any(len(row) != width for row in self.rows):
            raise ValueError("표의 모든 행은 머리글과 같은 열 수를 가져야 합니다.")
        if any(len(cell) > 1000 for row in self.rows for cell in row):
            raise ValueError("표 셀은 1000자를 넘을 수 없습니다.")
        return self


ProposalBlock = Annotated[
    ProposalParagraphBlock | ProposalListBlock | ProposalTableBlock,
    Field(discriminator="type"),
]
ProposalSemanticRole = Literal[
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


class ProposalSectionV2(BaseModel):
    """평가 데이터셋의 의미 역할과 근거를 보존하는 기안 섹션."""

    model_config = ConfigDict(extra="forbid")

    heading: str = Field(min_length=1, max_length=120)
    semantic_role: ProposalSemanticRole
    citations: list[str] = Field(min_length=1, max_length=30)
    blocks: list[ProposalBlock] = Field(min_length=1, max_length=50)
    missing_information: list[str] = Field(max_length=20)

    @field_validator("citations")
    @classmethod
    def validate_citations(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(re.fullmatch(r"E\d{3}", item) is None for item in value):
            raise ValueError("citations는 중복 없는 E001 형식이어야 합니다.")
        return value


class ProposalDocumentV2(BaseModel):
    """LLM 출력과 도구 평가가 공유하는 엄격한 구조화 기안 문서."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-document-v2"]
    proposal_type: ResolvedProposalType | None = None
    title: str = Field(min_length=1, max_length=80)
    approval_request: str = Field(min_length=1, max_length=1000)
    sections: list[ProposalSectionV2] = Field(min_length=1, max_length=20)
    missing_information: list[str] = Field(max_length=50)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _clean_title(value)


class ProposalClarificationQuestion(BaseModel):
    """문서 근거만으로 채울 수 없는 필수 사실에 대한 짧은 확인 질문."""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(pattern=r"^Q\d{3}$")
    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    prompt: str = Field(min_length=1, max_length=200)
    reason: Literal["missing", "conflict"]


class ProposalCompletionSummary(BaseModel):
    """근거 검증과 사용자 확인이 끝났는지 공개하는 비식별 집계."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-completion-v1"] = "proposal-completion-v1"
    status: Literal["completed", "needs_clarification", "completed_with_omissions"] = "completed"
    questions: list[ProposalClarificationQuestion] = Field(default_factory=list, max_length=3)
    supported_claim_count: int = Field(default=0, ge=0)
    derived_claim_count: int = Field(default=0, ge=0)
    omitted_claim_count: int = Field(default=0, ge=0)
    conflicting_claim_count: int = Field(default=0, ge=0)
    clarification_round: int = Field(default=0, ge=0, le=1)


class ProposalClaimVerdict(BaseModel):
    """로컬 검증 LLM이 문장·목록 항목·표 행 하나에 내리는 내부 판정."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^C\d{4}$")
    status: Literal["supported", "derived", "unsupported", "conflicting"]
    action: Literal["keep", "omit", "ask"]
    citations: list[str] = Field(default_factory=list, max_length=30)


class ProposalQuestionCandidate(BaseModel):
    """최대 세 개로 압축하기 전의 내부 확인 질문 후보."""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    prompt: str = Field(min_length=1, max_length=200)
    reason: Literal["missing", "conflict"]
    priority: int = Field(ge=1, le=3)


class ProposalEvidenceVerification(BaseModel):
    """초안의 주장별 근거 판정과 꼭 필요한 질문만 담는 내부 LLM 계약."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proposal-evidence-verification-v1"]
    claims: list[ProposalClaimVerdict]
    # 로컬 모델이 지시보다 많은 후보를 내더라도 응답 전체를 버리지 않고 적용 단계에서 우선순위 상위 3개만 남긴다.
    questions: list[ProposalQuestionCandidate] = Field(default_factory=list, max_length=20)


def _legacy_document(fields: ProposalDraftFields, citation_id: str = "E001") -> ProposalDocumentV2:
    """기존 3필드 생성기 결과를 손실 없이 단일 섹션 V2 문서로 승격한다."""

    return ProposalDocumentV2(
        schema_version="proposal-document-v2",
        title=fields.title,
        approval_request=fields.approval_request,
        sections=[
            ProposalSectionV2(
                heading="주요 내용",
                semantic_role="details",
                citations=[citation_id],
                blocks=[ProposalParagraphBlock(type="paragraph", text=fields.body)],
                missing_information=[],
            )
        ],
        missing_information=[],
    )


def _single_excel_row(value: str) -> str:
    """block 내부의 모든 줄 경계와 탭을 Excel 한 행에 들어갈 공백으로 바꾼다."""

    return re.sub(r"[ \t]+", " ", " ".join(value.splitlines())).strip()


def _projection_lines(document: ProposalDocumentV2) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for section_index, section in enumerate(document.sections, start=1):
        lines.append((5, _single_excel_row(f"{section_index}. {section.heading}")))
        for block in section.blocks:
            if isinstance(block, ProposalParagraphBlock):
                lines.append((2, _single_excel_row(block.text)))
            elif isinstance(block, ProposalListBlock):
                for item_index, item in enumerate(block.items, start=1):
                    marker = f"{item_index})" if block.ordered else "-"
                    lines.append((2, _single_excel_row(f"{marker} {item}")))
            else:
                header = " | ".join(_single_excel_row(cell) for cell in block.headers)
                lines.append((4, header))
                for row in block.rows:
                    row_text = " | ".join(_single_excel_row(cell) for cell in row)
                    lines.append((3 if _NUMBER_OR_DATE_PATTERN.search(row_text) else 2, row_text))
        for missing in section.missing_information:
            lines.append((3, _single_excel_row(f"[확인 필요] {missing}")))
    for missing in document.missing_information:
        lines.append((3, _single_excel_row(f"[확인 필요] {missing}")))
    return [(priority, line) for priority, line in lines if line.strip()]


def _least_valuable_projection_position(selected: list[tuple[int, int, str]]) -> int:
    """우선순위가 낮고 긴 후반 행부터 제거할 위치를 결정한다."""

    return min(
        range(len(selected)),
        key=lambda position: (
            selected[position][1],
            -len(selected[position][2]),
            -selected[position][0],
        ),
    )


def project_document_to_legacy_fields(document: ProposalDocumentV2) -> ProposalDraftFields:
    """V2 문서를 기존 API·33행 엑셀 계약에 재현 가능하게 투영한다."""

    raw_candidates = _projection_lines(document)
    line_truncated = False
    candidates: list[tuple[int, str]] = []
    for priority, line in raw_candidates:
        if len(line) > _LEGACY_LINE_MAX_CHARS:
            line = f"{line[: _LEGACY_LINE_MAX_CHARS - 1].rstrip()}…"
            line_truncated = True
        candidates.append((priority, line))

    capacity = _BODY_END_ROW - _BODY_START_ROW + 1
    omitted = line_truncated or len(candidates) > capacity
    keep_count = capacity - 1 if omitted else capacity
    chosen_indexes = sorted(
        sorted(range(len(candidates)), key=lambda index: (-candidates[index][0], index))[:keep_count]
    )
    selected = [(index, candidates[index][0], candidates[index][1]) for index in chosen_indexes]

    def render() -> str:
        output = [line for _, _, line in selected]
        if omitted:
            output.append(_PROJECTION_OMISSION_NOTICE)
        return "\n".join(output)

    if len(render()) > _LEGACY_BODY_MAX_CHARS and not omitted:
        omitted = True
        if len(selected) >= capacity:
            selected.pop(_least_valuable_projection_position(selected))
    while len(render()) > _LEGACY_BODY_MAX_CHARS and selected:
        selected.pop(_least_valuable_projection_position(selected))

    body = render()
    return ProposalDraftFields(title=document.title, approval_request=document.approval_request, body=body)


class ProposalDraftGenerateResponse(BaseModel):
    """대화창의 생성 결과 카드에 필요한 공개 정보."""

    model_config = ConfigDict(extra="forbid")

    draft_id: UUID
    title: str
    fields: ProposalDraftFields
    document: ProposalDocumentV2
    proposal_type: ResolvedProposalType
    proposal_type_source: ProposalTypeSource
    evidence_filter: ProposalEvidenceFilterSummary
    context_usage: ProposalContextUsage
    completion: ProposalCompletionSummary = Field(default_factory=ProposalCompletionSummary)
    file_name: str | None = None
    download_url: str | None = None
    destination_label: str | None = None
    saved_to_smb: bool = True
    model_used: str
    elapsed_ms: float = Field(ge=0)
    timings_ms: dict[str, float] = Field(default_factory=dict)


class ProposalDraftRevisionResponse(ProposalDraftGenerateResponse):
    """원본을 보존한 새 수정본의 공개 응답."""

    revision_of_draft_id: UUID
    revision_number: str = Field(pattern=r"^1\.\d+$")
    revision_summary: str = Field(min_length=1, max_length=500)


class ProposalDraftClarificationResponse(ProposalDraftGenerateResponse):
    """확인 답변을 반영해 저장까지 마친 최종 기안 응답."""

    clarification_of_draft_id: UUID
    answered_question_count: int = Field(ge=1, le=3)


@dataclass(frozen=True)
class ProposalDraftResult:
    """LLM 호출에서 확정한 세 필드와 공개 가능한 메타데이터."""

    fields: ProposalDraftFields
    model: str
    document: ProposalDocumentV2 | None = None
    proposal_type: ResolvedProposalType | None = None
    context_usage: ProposalContextUsage = dataclass_field(default_factory=ProposalContextUsage)
    packed_citation_map: tuple[tuple[str, str], ...] = ()
    packed_context_sha256: str | None = None


@dataclass(frozen=True)
class ProposalDraftDownload:
    """브라우저 다운로드에 필요한 파일명과 XLSX 바이트."""

    file_name: str
    content: bytes


@dataclass(frozen=True)
class ProposalDraftRecord:
    """다운로드 바이트와 수정에 필요한 원본 snapshot을 함께 보존한다."""

    download: ProposalDraftDownload | None
    document: ProposalDocumentV2
    instruction: str
    selected_files: tuple[SelectedFileContext, ...]
    proposal_type: ResolvedProposalType
    proposal_type_source: ProposalTypeSource
    completion: ProposalCompletionSummary = dataclass_field(default_factory=ProposalCompletionSummary)
    revision_minor: int = 0
    root_draft_id: UUID | None = None


class ProposalDraftGenerator(Protocol):
    """테스트에서 LLM을 대체할 수 있는 구조화 기안 생성 계약."""

    def generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType = "general",
    ) -> ProposalDraftResult:
        """사용자 설명과 선택 문서 근거를 받아 세 필드를 반환한다."""

    def close(self) -> None:
        """재사용 중인 연결을 닫는다."""

    def revise(
        self,
        feedback: str,
        document: ProposalDocumentV2,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType,
    ) -> ProposalDraftResult:
        """기존 strict V2 문서를 선택 근거와 피드백 안에서 수정한다."""

    def assess(
        self,
        instruction: str,
        document: ProposalDocumentV2,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType,
        user_answers: list[str] | None = None,
        allow_questions: bool = True,
    ) -> ProposalEvidenceVerification:
        """초안의 각 주장에 근거가 있는지 판정하고 필수 질문만 반환한다."""


def _is_internal_http_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold()
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return address.is_private or address.is_loopback or address.is_link_local


def _clean_title(value: str) -> str:
    """LLM 응답을 엑셀 한 셀에 넣을 한 줄 제목으로 제한한다."""

    title = " ".join(value.replace("\r", "\n").splitlines()).strip().strip("`\"' ")
    title = re.sub(r"\s+", " ", title)
    if not title or any(ord(char) < 32 for char in title):
        raise ValueError("LLM 응답에 제목이 없습니다.")
    return title[:80].rstrip()


def _title_document_name(title: str) -> str:
    """제목을 기존 버전 파일명 규칙에 넣을 안전한 문서명으로 바꾼다."""

    normalized = _INVALID_TITLE_FILENAME_CHARS.sub(" ", title)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")[:80].rstrip(" .")
    return normalized or Path(_TEMPLATE_NAME).stem


def _revision_file_name(file_name: str, revision_minor: int) -> str:
    """기존 날짜·문서명을 보존하고 v1.x minor만 증가시킨다."""

    path = Path(file_name)
    stem = re.sub(r"_v1\.\d+$", "", path.stem)
    return f"{stem}_v1.{revision_minor}{path.suffix}"


def _revision_summary(before: ProposalDocumentV2, after: ProposalDocumentV2, revision_minor: int) -> str:
    """LLM 자유문구 없이 strict 문서 diff에서 수정 요약을 결정한다."""

    changed: list[str] = []
    if before.title != after.title:
        changed.append("제목")
    if before.approval_request != after.approval_request:
        changed.append("재가 문구")
    if before.sections != after.sections:
        changed.append("본문")
    if before.missing_information != after.missing_information:
        changed.append("확인 필요 항목")
    if not changed:
        return f"피드백을 반영했지만 구조화 문서의 내용 변경은 없는 v1.{revision_minor} 수정본입니다."
    return f"피드백을 반영한 v1.{revision_minor} 수정본입니다. 변경 항목: {', '.join(changed)}."


_CONTEXT_LIMIT_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
    "token limit",
    "num_ctx",
    "request too large",
    "request entity too large",
    "payload too large",
)
_PROPOSAL_PROMPT_RESERVE_TOKENS = 1024


def _evidence_context_budget_chars(settings: Settings, *, fixed_prompt_chars: int) -> int:
    """출력·프롬프트·수정 원문을 제외한 근거 컨텍스트 문자 예산을 계산한다."""

    input_tokens = max(
        1,
        settings.proposal_llm_num_ctx
        - settings.proposal_llm_max_tokens
        - _PROPOSAL_PROMPT_RESERVE_TOKENS,
    )
    runtime_input_chars = input_tokens * 8 // 5
    return max(1, min(settings.proposal_context_max_chars, runtime_input_chars - fixed_prompt_chars))


class _ContextLimitError(RuntimeError):
    """한 번만 축소 재시도하기 위해 내부에서만 쓰는 컨텍스트 제한 신호."""


def _is_context_limit_message(value: str) -> bool:
    normalized = value.casefold()
    return any(marker in normalized for marker in _CONTEXT_LIMIT_MARKERS)


def _is_context_limit_http_error(exc: httpx.HTTPError) -> bool:
    response = getattr(exc, "response", None)
    text = str(exc)
    if response is not None:
        if response.status_code == 413:
            return True
        try:
            text = f"{text} {response.text}"
        except (AttributeError, RuntimeError):
            pass
    return _is_context_limit_message(text)


def _decode_llm_payload(data: object) -> tuple[dict, int | None]:
    if not isinstance(data, dict):
        raise ValueError("LLM 응답이 JSON 객체가 아닙니다.")
    prompt_eval_count = data.get("prompt_eval_count")
    if not isinstance(prompt_eval_count, int) or prompt_eval_count < 0:
        prompt_eval_count = None
    error = data.get("error")
    if isinstance(error, str) and _is_context_limit_message(error):
        raise _ContextLimitError
    if "schema_version" in data or all(key in data for key in ("title", "approval_request", "body")):
        return data, prompt_eval_count
    message = data.get("message")
    raw_content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(raw_content, str):
        raise ValueError("LLM 응답에 기안 문서가 없습니다.")
    text = raw_content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    decoded = json.loads(text)
    if not isinstance(decoded, dict):
        raise ValueError("LLM 응답이 JSON 객체가 아닙니다.")
    return decoded, prompt_eval_count


def _validate_document_citations(document: ProposalDocumentV2, available: tuple[str, ...]) -> None:
    available_set = set(available)
    referenced = {citation for section in document.sections for citation in section.citations}
    if not referenced.issubset(available_set):
        raise ValueError("LLM 응답이 제공되지 않은 근거 ID를 인용했습니다.")


_EVENT_SUBJECT_TERMS = ("박람회", "전시회", "컨퍼런스", "학회", "세미나", "워크숍", "행사")
_EVENT_ACTION_TERMS = ("참석", "참가", "방문", "등록")
_PURCHASE_TERMS = ("구매", "구입", "도입", "교체", "견적", "발주", "비품", "장비")
_PROPOSAL_TYPE_PROMPTS: dict[ResolvedProposalType, str] = {
    "purchase": (
        "이 기안은 구매 유형입니다. 근거가 있는 항목만 사용해 목적, 배경·필요성, 구매 요청, 품목 세부표, "
        "예산, 납기·유지보수, 기대효과, 첨부 순서를 우선하세요. 품목·수량·단가·금액처럼 반복되는 값은 table block으로 "
        "구조화하세요."
    ),
    "event_attendance": (
        "이 기안은 박람회·행사 참석 유형입니다. section heading은 반드시 참가 목적, 참가 내용, 행사 주요 내용만 "
        "사용하고 이 순서를 지키세요. 행사 개요나 일정 section을 따로 만들지 마세요. 비용·일시·장소·참석자는 "
        "참가 내용 안에 간결하게 넣고, 시간대별 전체 일정은 넣지 마세요. 발표·전시 주제처럼 참석 판단에 필요한 핵심만 "
        "행사 주요 내용에 정리하세요. 영수증·결제 내역·참석 확인증 같은 사후 증빙의 상세 내용은 사용자가 명시적으로 "
        "요구하지 않으면 본문에 넣지 마세요."
    ),
    "general": (
        "이 기안은 일반 유형입니다. 근거가 있는 항목만 사용해 목적, 배경, 요청·세부내용, 일정, 기대효과, 첨부 순서를 "
        "우선하세요. 실제로 행과 열 관계가 있는 값에만 table block을 사용하세요."
    ),
}

_EVENT_SECTION_ORDER = ("참가 목적", "참가 내용", "행사 주요 내용")
_EVENT_SCHEDULE_TERMS = ("일정", "시간표", "타임테이블", "agenda", "schedule")
_EVENT_MAIN_CONTENT_TERMS = ("주요", "프로그램", "세션", "발표", "전시", "주제", "내용")


@dataclass
class _EventSectionAccumulator:
    blocks: list[ProposalBlock] = dataclass_field(default_factory=list)
    citations: list[str] = dataclass_field(default_factory=list)
    missing_information: list[str] = dataclass_field(default_factory=list)


def _event_section_heading(section: ProposalSectionV2) -> str | None:
    heading = section.heading.casefold()
    if section.heading in _EVENT_SECTION_ORDER:
        return section.heading
    if section.semantic_role == "schedule" or any(term in heading for term in _EVENT_SCHEDULE_TERMS):
        return None
    if section.semantic_role in {"purpose", "background", "expected_effect"}:
        return "참가 목적"
    if section.semantic_role in {"budget", "request"}:
        return "참가 내용"
    if any(term in heading for term in _EVENT_MAIN_CONTENT_TERMS):
        return "행사 주요 내용"
    return "참가 내용"


def normalize_proposal_document(document: ProposalDocumentV2, proposal_type: ResolvedProposalType) -> ProposalDocumentV2:
    """행사 참석 초안은 세 개의 간결한 본문 범주만 남기고 순서를 고정한다."""

    if proposal_type != "event_attendance":
        return document.model_copy(update={"proposal_type": proposal_type})

    grouped: dict[str, _EventSectionAccumulator] = {}
    for section in document.sections:
        heading = _event_section_heading(section)
        if heading is None:
            continue
        target = grouped.setdefault(heading, _EventSectionAccumulator())
        target.blocks.extend(section.blocks)
        for citation in section.citations:
            if citation not in target.citations:
                target.citations.append(citation)
        for missing in section.missing_information:
            if missing not in target.missing_information:
                target.missing_information.append(missing)

    sections = [
        ProposalSectionV2(
            heading=heading,
            semantic_role="purpose" if heading == "참가 목적" else "details",
            citations=grouped[heading].citations,
            blocks=grouped[heading].blocks,
            missing_information=grouped[heading].missing_information,
        )
        for heading in _EVENT_SECTION_ORDER
        if heading in grouped
    ]
    if not sections:
        raise ValueError("행사 참석 기안에 사용할 간결한 본문 섹션이 없습니다.")
    missing_information = [
        item
        for item in document.missing_information
        if not any(term in item.casefold() for term in _EVENT_SCHEDULE_TERMS)
    ]
    return document.model_copy(
        update={
            "proposal_type": proposal_type,
            "sections": sections,
            "missing_information": missing_information,
        }
    )


def resolve_proposal_type(
    requested: ProposalTypeRequest,
    instruction: str,
    evidence: list[DocumentCitation],
) -> ProposalTypeResolution:
    """명시 선택을 우선하고 불명확한 자동 판정은 일반 기안으로 되돌린다."""

    if requested != "auto":
        return ProposalTypeResolution(proposal_type=requested, source="user")

    def detect(candidate: str) -> tuple[ResolvedProposalType | None, bool]:
        normalized = candidate.casefold()
        event_hit = any(term in normalized for term in _EVENT_SUBJECT_TERMS) and any(
            term in normalized for term in _EVENT_ACTION_TERMS
        )
        purchase_hit = any(term in normalized for term in _PURCHASE_TERMS)
        if event_hit != purchase_hit:
            return ("event_attendance" if event_hit else "purchase"), True
        return None, event_hit or purchase_hit

    instruction_type, instruction_marked = detect(instruction)
    if instruction_type is not None:
        return ProposalTypeResolution(proposal_type=instruction_type, source="rule")
    if instruction_marked:
        return ProposalTypeResolution(proposal_type="general", source="fallback")
    reference_titles = " ".join(
        item.title for item in evidence if not is_strong_post_action_metadata(item.title)
    )
    reference_type, _ = detect(reference_titles)
    if reference_type is not None:
        return ProposalTypeResolution(proposal_type=reference_type, source="rule")
    return ProposalTypeResolution(proposal_type="general", source="fallback")


@dataclass(frozen=True)
class _ProposalClaim:
    """검증 결과를 원래 block 위치로 되돌리기 위한 내부 주장 단위."""

    claim_id: str
    section_index: int
    block_index: int
    item_index: int | None
    text: str


def _proposal_claims(document: ProposalDocumentV2) -> list[_ProposalClaim]:
    """문단, 목록 항목, 표 행을 각각 독립적으로 검증할 주장으로 펼친다."""

    claims: list[_ProposalClaim] = []
    for section_index, section in enumerate(document.sections):
        for block_index, block in enumerate(section.blocks):
            if isinstance(block, ProposalParagraphBlock):
                items = [(None, block.text)]
            elif isinstance(block, ProposalListBlock):
                items = list(enumerate(block.items))
            else:
                items = [
                    (row_index, " / ".join(f"{header}: {cell}" for header, cell in zip(block.headers, row, strict=True)))
                    for row_index, row in enumerate(block.rows)
                ]
            for item_index, text in items:
                claims.append(
                    _ProposalClaim(
                        claim_id=f"C{len(claims) + 1:04d}",
                        section_index=section_index,
                        block_index=block_index,
                        item_index=item_index,
                        text=text,
                    )
                )
    return claims


def _money_values(text: str) -> set[str]:
    """금액 표기 차이만 제거한 비교용 토큰을 반환한다."""

    return {re.sub(r"[\s,]", "", match.group()).casefold() for match in _MONEY_VALUE_PATTERN.finditer(text)}


def _canonical_question_field(field_key: str) -> str:
    """로컬 모델이 흔히 쓰는 동의 필드명을 평가 계약의 표준 키로 맞춘다."""

    return {
        "cost": "participation_fee",
        "event_fee": "participation_fee",
        "participation_cost": "participation_fee",
        "purchase_cost": "total_amount",
        "purchase_total": "total_amount",
        "total_cost": "total_amount",
    }.get(field_key, field_key)


def _enforce_required_fact_safety_net(
    verification: ProposalEvidenceVerification,
    claims: list[_ProposalClaim],
    *,
    instruction: str,
    evidence: list[DocumentCitation],
    user_answers: list[str],
    proposal_type: ResolvedProposalType,
    allow_questions: bool,
) -> ProposalEvidenceVerification:
    """명시적으로 금액 승인을 요구한 기안에서 금액 누락·환각을 결정론적으로 막는다."""

    source_text = "\n".join(
        [instruction, *user_answers]
        + [f"{citation.title}\n{citation.excerpt}" for citation in evidence]
    )
    source_money = _money_values(source_text)
    verdict_by_id = {verdict.claim_id: verdict for verdict in verification.claims}
    corrected_verdicts: list[ProposalClaimVerdict] = []
    for claim in claims:
        verdict = verdict_by_id.get(claim.claim_id)
        if verdict is None:
            continue
        unsupported_money = _money_values(claim.text) - source_money
        if unsupported_money and verdict.status == "supported":
            verdict = verdict.model_copy(update={"status": "unsupported", "action": "ask", "citations": []})
        corrected_verdicts.append(verdict)

    questions = [
        candidate.model_copy(update={"field_key": _canonical_question_field(candidate.field_key)})
        for candidate in verification.questions
    ]
    existing_fields = {_canonical_question_field(candidate.field_key) for candidate in questions}
    requires_money = proposal_type == "purchase" or (
        proposal_type == "event_attendance" and _MONEY_INTENT_PATTERN.search(instruction) is not None
    )
    if allow_questions and requires_money and not source_money:
        field_key = "total_amount" if proposal_type == "purchase" else "participation_fee"
        if field_key not in existing_fields:
            prompt = (
                "구매 승인에 필요한 총 금액은 얼마인가요?"
                if proposal_type == "purchase"
                else "참가비 승인에 필요한 금액과 인원 기준은 무엇인가요?"
            )
            questions.insert(
                0,
                ProposalQuestionCandidate(
                    field_key=field_key,
                    prompt=prompt,
                    reason="missing",
                    priority=1,
                ),
            )
    return verification.model_copy(update={"claims": corrected_verdicts, "questions": questions})


def _fallback_completion(document: ProposalDocumentV2) -> ProposalCompletionSummary:
    """assess 계약이 없는 구 테스트 생성기의 동작을 유지한다."""

    return ProposalCompletionSummary(supported_claim_count=len(_proposal_claims(document)))


def apply_proposal_evidence_verification(
    document: ProposalDocumentV2,
    verification: ProposalEvidenceVerification,
    *,
    allow_questions: bool,
    clarification_round: int,
) -> tuple[ProposalDocumentV2, ProposalCompletionSummary]:
    """근거 없는 선택 항목은 지우고 필수 누락은 최대 세 질문으로 바꾼다."""

    claims = _proposal_claims(document)
    expected_ids = {claim.claim_id for claim in claims}
    verdicts = {verdict.claim_id: verdict for verdict in verification.claims}
    if len(verdicts) != len(verification.claims) or set(verdicts) != expected_ids:
        raise ValueError("근거 검증 결과가 모든 주장과 일대일로 대응하지 않습니다.")

    question_candidates: list[ProposalQuestionCandidate] = []
    if allow_questions:
        seen_fields: set[str] = set()
        for candidate in sorted(verification.questions, key=lambda item: item.priority):
            if candidate.field_key in seen_fields:
                continue
            seen_fields.add(candidate.field_key)
            question_candidates.append(candidate)
            if len(question_candidates) == 3:
                break
    questions = [
        ProposalClarificationQuestion(
            question_id=f"Q{index:03d}",
            field_key=candidate.field_key,
            prompt=candidate.prompt,
            reason=candidate.reason,
        )
        for index, candidate in enumerate(question_candidates, start=1)
    ]

    claim_by_path = {
        (claim.section_index, claim.block_index, claim.item_index): claim.claim_id for claim in claims
    }

    def should_keep(claim_id: str) -> bool:
        verdict = verdicts[claim_id]
        return verdict.action == "keep" and verdict.status in {"supported", "derived"}

    sections: list[ProposalSectionV2] = []
    for section_index, section in enumerate(document.sections):
        blocks: list[ProposalBlock] = []
        for block_index, block in enumerate(section.blocks):
            if isinstance(block, ProposalParagraphBlock):
                claim_id = claim_by_path[(section_index, block_index, None)]
                if should_keep(claim_id):
                    blocks.append(block)
            elif isinstance(block, ProposalListBlock):
                items = [
                    item
                    for item_index, item in enumerate(block.items)
                    if should_keep(claim_by_path[(section_index, block_index, item_index)])
                ]
                if items:
                    blocks.append(block.model_copy(update={"items": items}))
            else:
                rows = [
                    row
                    for row_index, row in enumerate(block.rows)
                    if should_keep(claim_by_path[(section_index, block_index, row_index)])
                ]
                if rows:
                    blocks.append(block.model_copy(update={"rows": rows}))
        if blocks:
            sections.append(section.model_copy(update={"blocks": blocks, "missing_information": []}))

    if not sections and questions:
        source = document.sections[0]
        sections = [
            ProposalSectionV2(
                heading=source.heading,
                semantic_role=source.semantic_role,
                citations=source.citations,
                blocks=[ProposalParagraphBlock(type="paragraph", text="필수 정보를 확인한 뒤 기안 본문을 완성합니다.")],
                missing_information=[],
            )
        ]
    if not sections:
        raise ValueError("근거 검증 후 기안에 남길 수 있는 본문이 없습니다.")

    supported = sum(verdict.status == "supported" for verdict in verification.claims)
    derived = sum(verdict.status == "derived" for verdict in verification.claims)
    conflicting = sum(verdict.status == "conflicting" for verdict in verification.claims)
    omitted = sum(not should_keep(verdict.claim_id) for verdict in verification.claims)
    status: Literal["completed", "needs_clarification", "completed_with_omissions"]
    if questions:
        status = "needs_clarification"
    elif omitted:
        status = "completed_with_omissions"
    else:
        status = "completed"
    completion = ProposalCompletionSummary(
        status=status,
        questions=questions,
        supported_claim_count=supported,
        derived_claim_count=derived,
        omitted_claim_count=omitted,
        conflicting_claim_count=conflicting,
        clarification_round=clarification_round,
    )
    sanitized = document.model_copy(
        update={
            "sections": sections,
            "missing_information": [question.prompt for question in questions],
        }
    )
    return sanitized, completion


class LocalProposalDraftGenerator:
    """온프레미스 Ollama에서 strict V2 기안 문서를 생성한다."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=max(1.0, settings.proposal_llm_timeout_ms / 1000))

    def close(self) -> None:
        """재사용하던 HTTP 연결을 닫는다."""

        self._client.close()

    def generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType = "general",
    ) -> ProposalDraftResult:
        """외부 주소는 거부하고 선택 문서 근거로 strict V2 기안을 반환한다."""

        return self._generate(
            instruction,
            evidence,
            proposal_type=proposal_type,
            current_document=None,
        )

    def revise(
        self,
        feedback: str,
        document: ProposalDocumentV2,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType,
    ) -> ProposalDraftResult:
        """현재 문서와 사용자 피드백을 근거 범위 안에서 strict V2로 다시 작성한다."""

        return self._generate(
            feedback,
            evidence,
            proposal_type=proposal_type,
            current_document=document,
        )

    def assess(
        self,
        instruction: str,
        document: ProposalDocumentV2,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType,
        user_answers: list[str] | None = None,
        allow_questions: bool = True,
    ) -> ProposalEvidenceVerification:
        """문서 단위가 아닌 주장 단위로 근거를 다시 확인한다."""

        root_url = self._settings.ollama_base_url.strip().rstrip("/")
        model = self._settings.llmops_chat_model.strip()
        if not root_url or not model:
            raise ProposalDraftError("local_llm_not_configured", "로컬 LLM 주소 또는 모델이 구성되지 않았습니다.", 503)
        if not _is_internal_http_url(root_url):
            raise ProposalDraftError("local_llm_url_not_internal", "기안 내용은 온프레미스 LLM으로만 전송할 수 있습니다.", 503)
        if not evidence:
            raise ProposalDraftError("proposal_evidence_unavailable", "기안 근거를 다시 확인할 수 없습니다.", 422)

        claims = _proposal_claims(document)
        claim_payload = [
            {
                "claim_id": claim.claim_id,
                "section_heading": document.sections[claim.section_index].heading,
                "text": claim.text,
            }
            for claim in claims
        ]
        answers = user_answers or []
        essential_profile = {
            "purchase": (
                "purpose, purchase_item, quantity, unit_price, total_amount, requested_action 중 실제 승인에 필요한 항목"
            ),
            "event_attendance": (
                "participation_purpose, participation_content, event_main_content, participation_fee 중 실제 승인에 필요한 항목. "
                "event_overview와 full_timetable은 질문하지 않음"
            ),
            "general": "purpose와 requested_action",
        }[proposal_type]
        question_rule = (
            "필수 사실이 없거나 서로 충돌할 때만 우선순위 1부터 최대 3개 질문을 만드세요."
            if allow_questions
            else "questions는 반드시 빈 배열로 반환하고, 확인할 수 없는 주장은 모두 omit으로 판정하세요."
        )
        system_prompt = (
            "당신은 기안 초안의 근거 검증기입니다. 주장 문구가 참고 문서와 글자 그대로 같아야 하는 것은 아니며, 의미가 "
            "참고 근거나 사용자 입력으로 뒷받침되는지 판단하세요. 단순 합계처럼 근거 값에서 직접 계산되는 내용만 derived입니다. "
            "근거가 없지만 없어도 기안 목적이 유지되는 선택 정보는 unsupported/omit으로 판정하세요. 결재 판단에 꼭 필요한 "
            "정보가 없으면 unsupported/ask, 근거가 충돌하면 conflicting/ask로 판정하세요. supported와 derived만 keep할 수 "
            "있습니다. 각 claim_id를 정확히 한 번 반환하고 다른 claim_id를 만들지 마세요. citations에는 참고 문서 E001 형식과 "
            "사용자 입력 U001 형식만 사용할 수 있으며 supported/derived에는 하나 이상 넣으세요. 질문은 서로 겹치지 않게 한 "
            "문장으로 쓰고 사용자가 한 번에 답할 수 있어야 합니다. 선택 정보나 더 풍부한 문서를 위한 질문은 금지합니다. "
            "현재 초안의 missing_information도 필수 범위와 비교하고, 필수 사실이면 questions로 반환하세요. "
            f"이 유형의 필수 범위는 다음과 같습니다: {essential_profile}. {question_rule} 반환값은 마크다운 없는 JSON 객체이며 "
            "schema_version, claims, questions만 허용합니다. schema_version은 proposal-evidence-verification-v1입니다. claims는 "
            "claim_id, status(supported|derived|unsupported|conflicting), action(keep|omit|ask), citations 배열을 가집니다. "
            "questions는 field_key(영문 snake_case), prompt, reason(missing|conflict), priority(1~3)를 가집니다."
        )
        user_sources = [f"[U001] 최초 사용자 요청: {instruction.strip()}"]
        user_sources.extend(f"[U{index + 2:03d}] 확인 답변: {answer.strip()}" for index, answer in enumerate(answers))
        document_json = document.model_dump_json(exclude_none=True)
        claims_json = json.dumps(claim_payload, ensure_ascii=False, separators=(",", ":"))
        fixed_prompt_chars = len(system_prompt) + len(document_json) + len(claims_json) + sum(map(len, user_sources))
        evidence_budget_chars = _evidence_context_budget_chars(
            self._settings,
            fixed_prompt_chars=fixed_prompt_chars,
        )
        if evidence_budget_chars < 256:
            raise ProposalDraftError(
                "proposal_context_limit_exceeded",
                "초안 근거 검증이 로컬 LLM의 컨텍스트 제한을 초과했습니다.",
                422,
            )
        context = pack_proposal_context(
            instruction,
            evidence,
            max_chars=evidence_budget_chars,
            per_document_chars=min(self._settings.proposal_context_per_document_chars, evidence_budget_chars),
            max_citations=self._settings.proposal_context_max_citations,
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"사용자 입력:\n{chr(10).join(user_sources)}\n\n현재 초안:\n{document_json}\n\n"
                        f"검증할 주장:\n{claims_json}\n\n참고 문서 근거:\n{context.text}"
                    ),
                },
            ],
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": self._settings.proposal_llm_num_ctx,
                "num_predict": self._settings.proposal_llm_max_tokens,
            },
        }
        try:
            response = self._client.post(f"{root_url}/api/chat", json=payload)
            response.raise_for_status()
            decoded, _ = _decode_llm_payload(response.json())
            verification = ProposalEvidenceVerification.model_validate(decoded)
            verification = _enforce_required_fact_safety_net(
                verification,
                claims,
                instruction=instruction,
                evidence=evidence,
                user_answers=answers,
                proposal_type=proposal_type,
                allow_questions=allow_questions,
            )
            allowed_citations = set(context.citation_ids) | {f"U{index:03d}" for index in range(1, len(answers) + 2)}
            for verdict in verification.claims:
                if not set(verdict.citations).issubset(allowed_citations):
                    raise ValueError("근거 검증 결과가 제공되지 않은 근거 ID를 사용했습니다.")
                if verdict.status in {"supported", "derived"} and (
                    verdict.action != "keep" or not verdict.citations
                ):
                    raise ValueError("유지할 주장에는 supported/derived 판정과 근거가 필요합니다.")
                if verdict.status in {"unsupported", "conflicting"} and verdict.action == "keep":
                    raise ValueError("근거 없거나 충돌하는 주장은 유지할 수 없습니다.")
            return verification
        except _ContextLimitError as exc:
            raise ProposalDraftError(
                "proposal_context_limit_exceeded",
                "초안 근거 검증이 로컬 LLM의 컨텍스트 제한을 초과했습니다.",
                422,
                context_usage=context.usage,
            ) from exc
        except httpx.HTTPError as exc:
            if _is_context_limit_http_error(exc):
                raise ProposalDraftError(
                    "proposal_context_limit_exceeded",
                    "초안 근거 검증이 로컬 LLM의 컨텍스트 제한을 초과했습니다.",
                    422,
                    context_usage=context.usage,
                ) from exc
            _logger.warning("기안 근거 검증 LLM 연결 실패: failure_type=%s", type(exc).__name__)
            raise ProposalDraftError("proposal_verification_unavailable", "기안 근거를 확인할 수 없습니다.", 503) from exc
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            _logger.warning("기안 근거 검증 응답 실패: failure_type=%s", type(exc).__name__)
            raise ProposalDraftError(
                "proposal_verification_response_invalid",
                "로컬 LLM이 올바른 근거 검증 결과를 반환하지 않았습니다.",
                502,
            ) from exc

    def _generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType,
        current_document: ProposalDocumentV2 | None,
    ) -> ProposalDraftResult:
        """생성과 수정이 공유하는 bounded LLM 호출."""

        root_url = self._settings.ollama_base_url.strip().rstrip("/")
        model = self._settings.llmops_chat_model.strip()
        if not root_url or not model:
            raise ProposalDraftError("local_llm_not_configured", "로컬 LLM 주소 또는 모델이 구성되지 않았습니다.", 503)
        if not _is_internal_http_url(root_url):
            raise ProposalDraftError("local_llm_url_not_internal", "기안 내용은 온프레미스 LLM으로만 전송할 수 있습니다.", 503)

        if not evidence:
            raise ProposalDraftError(
                "proposal_evidence_unavailable",
                "선택한 문서에서 기안 작성에 사용할 근거를 찾지 못했습니다.",
                422,
            )
        system_prompt = (
            "사용자의 지시와 제공된 근거만 사용해 한국어 기안 초안을 작성하세요. 근거에 없는 사실은 만들지 말고 "
            "불명확한 값은 missing_information에 짧게 적으세요. 반환값은 마크다운 없이 JSON 객체 하나여야 합니다. "
            "최상위 키는 schema_version, title, approval_request, sections, missing_information만 허용합니다. "
            "schema_version은 proposal-document-v2입니다. 각 sections 항목은 heading, semantic_role, citations, blocks, "
            "missing_information만 가집니다. semantic_role은 purpose, background, request, details, budget, schedule, "
            "expected_effect, attachments, notes, other 중 하나입니다. citations에는 제공된 E001 형식 ID만 넣으세요. "
            "blocks는 {type:'paragraph',text:string}, {type:'list',ordered:boolean,items:string[]}, 또는 "
            "{type:'table',headers:string[],rows:string[][]} 중 하나이며 표의 모든 행 열 수는 headers와 같아야 합니다. "
            "section과 최상위 missing_information은 반드시 JSON 문자열 배열이며 확인할 내용이 없으면 문자열 '없음'이 "
            "아니라 빈 배열 []로 작성하세요. approval_request는 검토와 재가를 요청하는 1~2문장으로 작성하세요. "
            "모든 필드를 반드시 포함하세요. 빈 섹션을 억지로 만들지 마세요. 한 paragraph, 목록 항목, 표 행에는 가능한 "
            "한 가지 사실만 담아 뒤 단계에서 근거를 독립적으로 확인할 수 있게 하세요. "
            f"{_PROPOSAL_TYPE_PROMPTS[proposal_type]}"
        )
        if current_document is not None:
            system_prompt += (
                " 현재 strict V2 문서를 기준으로 사용자 피드백에 요청된 부분만 수정하세요. 요청하지 않은 내용은 가능한 "
                "한 유지하되, 다시 제공된 근거로 확인할 수 없는 사실은 제거하거나 missing_information으로 옮기세요."
            )
        current_document_json = (
            current_document.model_dump_json(exclude_none=True) if current_document is not None else ""
        )
        fixed_prompt_chars = len(system_prompt) + len(instruction) + len(current_document_json)
        evidence_budget_chars = _evidence_context_budget_chars(
            self._settings,
            fixed_prompt_chars=fixed_prompt_chars,
        )
        if evidence_budget_chars < 256:
            source_documents = {(citation.doc_id, citation.revision_id) for citation in evidence}
            raise ProposalDraftError(
                "proposal_context_limit_exceeded",
                "기존 초안과 수정 피드백이 로컬 LLM의 컨텍스트 제한을 초과했습니다.",
                422,
                context_usage=ProposalContextUsage(
                    source_citation_count=len(evidence),
                    source_document_count=len(source_documents),
                    context_budget_chars=evidence_budget_chars,
                    estimated_input_tokens=estimate_proposal_tokens(fixed_prompt_chars),
                    truncated=True,
                ),
            )
        context = pack_proposal_context(
            instruction,
            evidence,
            max_chars=evidence_budget_chars,
            per_document_chars=min(self._settings.proposal_context_per_document_chars, evidence_budget_chars),
            max_citations=self._settings.proposal_context_max_citations,
        )
        if not context.text:
            raise ProposalDraftError(
                "proposal_evidence_unavailable",
                "선택한 문서에서 기안 작성에 사용할 근거를 찾지 못했습니다.",
                422,
            )

        for attempt in range(2):
            payload = self._build_payload(
                model,
                system_prompt,
                instruction,
                context,
                current_document_json=current_document_json,
            )
            try:
                response = self._client.post(f"{root_url}/api/chat", json=payload)
                response.raise_for_status()
                decoded, prompt_eval_count = _decode_llm_payload(response.json())
                if decoded.get("schema_version") == "proposal-document-v2":
                    document = ProposalDocumentV2.model_validate(decoded)
                    _validate_document_citations(document, context.citation_ids)
                    document = normalize_proposal_document(document, proposal_type)
                    fields = project_document_to_legacy_fields(document)
                else:
                    # 배포 전환 중인 기존 로컬 모델 응답도 API 호환을 위해 단일 섹션으로 승격한다.
                    fields = ProposalDraftFields.model_validate(decoded)
                    document = _legacy_document(fields, context.citation_ids[0])
                    document = normalize_proposal_document(document, proposal_type)
                    fields = project_document_to_legacy_fields(document)
                usage = context.usage.model_copy(
                    update={
                        "estimated_input_tokens": estimate_proposal_tokens(
                            len(system_prompt) + len(instruction) + len(context.text) + len(current_document_json)
                        ),
                        "prompt_eval_count": prompt_eval_count,
                    }
                )
                _logger.info(
                    "Proposal LLM context: citations=%d documents=%d chars=%d estimated_tokens=%d "
                    "prompt_eval_count=%s truncated=%s retry_count=%d",
                    usage.packed_citation_count,
                    usage.packed_document_count,
                    usage.context_chars,
                    usage.estimated_input_tokens,
                    usage.prompt_eval_count,
                    usage.truncated,
                    usage.retry_count,
                )
                return ProposalDraftResult(
                    fields=fields,
                    document=document,
                    model=model,
                    proposal_type=proposal_type,
                    context_usage=usage,
                    packed_citation_map=tuple(zip(context.citation_ids, context.source_chunk_ids, strict=True)),
                    packed_context_sha256=context.context_sha256,
                )
            except _ContextLimitError as exc:
                context_error: Exception = exc
            except httpx.HTTPError as exc:
                if not _is_context_limit_http_error(exc):
                    _logger.warning("기안 로컬 LLM 연결 실패: failure_type=%s", type(exc).__name__)
                    raise ProposalDraftError(
                        "proposal_llm_unavailable",
                        "로컬 LLM에 연결할 수 없습니다.",
                        503,
                    ) from exc
                context_error = exc
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError) as exc:
                _logger.warning("기안 구조화 응답 생성 실패: failure_type=%s", type(exc).__name__)
                raise ProposalDraftError(
                    "proposal_llm_response_invalid",
                    "로컬 LLM이 올바른 기안 JSON을 반환하지 않았습니다.",
                    502,
                ) from exc

            if attempt == 1:
                raise ProposalDraftError(
                    "proposal_context_limit_exceeded",
                    "기안 참고 문서가 로컬 LLM의 컨텍스트 제한을 초과했습니다.",
                    422,
                    context_usage=context.usage,
                ) from context_error
            first_attempt_chars = context.usage.context_chars
            half_budget = max(1, context.usage.context_budget_chars // 2)
            context = pack_proposal_context(
                instruction,
                evidence,
                max_chars=half_budget,
                per_document_chars=min(
                    max(1, self._settings.proposal_context_per_document_chars // 2),
                    half_budget,
                ),
                max_citations=self._settings.proposal_context_max_citations,
                retry_count=1,
                first_attempt_context_chars=first_attempt_chars,
            )

        raise AssertionError("기안 LLM 재시도 횟수 경계를 벗어났습니다.")

    def _build_payload(
        self,
        model: str,
        system_prompt: str,
        instruction: str,
        context: PackedProposalContext,
        *,
        current_document_json: str = "",
    ) -> dict:
        """Ollama JSON 요청을 컨텍스트 예산과 출력 예산으로 제한한다."""

        current_document_part = (
            f"현재 기안 문서:\n{current_document_json}\n\n수정 피드백:\n" if current_document_json else "기안 설명:\n"
        )
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"{current_document_part}{instruction.strip()}\n\n선택 문서 근거:\n{context.text}",
                },
            ],
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": self._settings.proposal_llm_num_ctx,
                "num_predict": self._settings.proposal_llm_max_tokens,
            },
        }


class ProposalDraftRegistry:
    """다운로드와 수정 snapshot을 제한된 개수로 함께 보관한다."""

    def __init__(self, *, max_records: int = 25) -> None:
        self._max_records = max_records
        self._records: OrderedDict[UUID, ProposalDraftRecord] = OrderedDict()
        self._lock = threading.RLock()

    def register(self, record: ProposalDraftRecord) -> UUID:
        """추측하기 어려운 ID를 발급하고 오래된 메모리 항목을 제거한다."""

        draft_id = uuid.uuid4()
        with self._lock:
            self._records[draft_id] = record
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)
        return draft_id

    def find(self, draft_id: UUID) -> ProposalDraftRecord | None:
        """현재 프로세스에 남아 있는 생성 파일과 snapshot을 반환한다."""

        with self._lock:
            return self._records.get(draft_id)

    def next_revision_minor(self, draft_id: UUID) -> tuple[UUID, int]:
        """같은 기안 계보에 등록된 최신 minor 다음 번호를 계산한다."""

        with self._lock:
            base = self._records.get(draft_id)
            if base is None:
                raise KeyError(draft_id)
            root_id = base.root_draft_id or draft_id
            latest = max(
                (
                    record.revision_minor
                    for record_id, record in self._records.items()
                    if record_id == root_id or record.root_draft_id == root_id
                ),
                default=0,
            )
            return root_id, latest + 1


def _worksheet_path(workbook: zipfile.ZipFile, sheet_name: str) -> str:
    """시트 이름을 실제 OOXML worksheet 경로로 해석한다."""

    workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    sheet = next(
        (
            item
            for item in workbook_root.findall(f"{{{_XLSX_MAIN_NS}}}sheets/{{{_XLSX_MAIN_NS}}}sheet")
            if item.attrib.get("name") == sheet_name
        ),
        None,
    )
    if sheet is None:
        raise ValueError("기안지 시트를 찾을 수 없습니다.")
    relationship_id = sheet.attrib.get(f"{{{_OFFICE_REL_NS}}}id", "")
    relationships = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    relationship = next(
        (
            item
            for item in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
            if item.attrib.get("Id") == relationship_id
        ),
        None,
    )
    if relationship is None:
        raise ValueError("기안지 시트 관계를 찾을 수 없습니다.")
    target = relationship.attrib.get("Target", "").replace("\\", "/").lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def _inline_string_cell(xml: str, cell_ref: str, value: str, *, default_style: str) -> str:
    """기존 셀 속성을 보존하거나 누락 셀을 만들고 값을 literal inline string으로 쓴다."""

    escaped_value = escape(value).replace("\n", "&#10;").replace("\t", "&#9;")
    cell_pattern = re.compile(
        rf'<c(?P<attrs>[^>]*\br="{re.escape(cell_ref)}"[^>]*?)(?:\s*/>|>.*?</c>)',
        re.DOTALL,
    )
    match = cell_pattern.search(xml)
    if match is not None:
        attrs = re.sub(r'\s+t="[^"]*"', "", match.group("attrs")).rstrip()
        replacement = f'<c{attrs} t="inlineStr"><is><t>{escaped_value}</t></is></c>'
        updated, replacements = cell_pattern.subn(replacement, xml, count=1)
        if replacements != 1:
            raise ValueError(f"기안 입력 셀 {cell_ref}을 갱신하지 못했습니다.")
        return updated

    row_number = re.search(r"\d+$", cell_ref)
    if row_number is None:
        raise ValueError(f"기안 입력 셀 {cell_ref}이 올바르지 않습니다.")
    row_pattern = re.compile(
        rf'(?P<open><row\b[^>]*\br="{row_number.group()}"[^>]*>)(?P<cells>.*?)(?P<close></row>)',
        re.DOTALL,
    )
    row_match = row_pattern.search(xml)
    if row_match is None:
        sheet_data_pattern = re.compile(r"(?P<open><sheetData>)(?P<rows>.*?)(?P<close></sheetData>)", re.DOTALL)
        sheet_data_match = sheet_data_pattern.search(xml)
        if sheet_data_match is None:
            raise ValueError("기안지의 행 데이터를 찾을 수 없습니다.")
        row_value = int(row_number.group())
        rows = sheet_data_match.group("rows")
        insert_at = len(rows)
        for existing_row in re.finditer(r'<row\b[^>]*\br="(?P<number>\d+)"[^>]*>.*?</row>', rows, re.DOTALL):
            if int(existing_row.group("number")) > row_value:
                insert_at = existing_row.start()
                break
        new_row = (
            f'<row r="{row_value}"><c r="{cell_ref}" s="{default_style}" t="inlineStr">'
            f"<is><t>{escaped_value}</t></is></c></row>"
        )
        updated_rows = f"{rows[:insert_at]}{new_row}{rows[insert_at:]}"
        replacement = (
            f"{sheet_data_match.group('open')}{updated_rows}{sheet_data_match.group('close')}"
        )
        return f"{xml[:sheet_data_match.start()]}{replacement}{xml[sheet_data_match.end():]}"
    new_cell = (
        f'<c r="{cell_ref}" s="{default_style}" t="inlineStr">'
        f"<is><t>{escaped_value}</t></is></c>"
    )
    replacement = f"{row_match.group('open')}{new_cell}{row_match.group('cells')}{row_match.group('close')}"
    return f"{xml[:row_match.start()]}{replacement}{xml[row_match.end():]}"


@dataclass(frozen=True)
class _DeferredProposalTable:
    """본문 폭이나 행 한도를 넘어 세부내용 시트로 보낼 표."""

    heading: str
    table: ProposalTableBlock


def _xlsx_tag(name: str) -> str:
    return f"{{{_XLSX_MAIN_NS}}}{name}"


def _serialize_xml_preserving_root_namespaces(root: ElementTree.Element, original: bytes) -> bytes:
    """ElementTree가 버리는 mc:Ignorable용 namespace 별칭을 원본 root에 맞춰 복원한다."""

    original_text = original.decode("utf-8")
    serialized = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    serialized_text = serialized.decode("utf-8")
    original_root = _XML_ROOT_PATTERN.search(original_text)
    serialized_root = _XML_ROOT_PATTERN.search(serialized_text)
    if original_root is None or serialized_root is None:
        raise ValueError("XLSX XML root를 직렬화하지 못했습니다.")
    original_namespaces = {
        prefix or "": uri for prefix, _, uri in _XML_NAMESPACE_PATTERN.findall(original_root.group())
    }
    serialized_namespaces = {
        prefix or "": uri for prefix, _, uri in _XML_NAMESPACE_PATTERN.findall(serialized_root.group())
    }
    declarations: list[str] = []
    for prefix, uri in original_namespaces.items():
        existing = serialized_namespaces.get(prefix)
        if existing is not None:
            if existing != uri:
                raise ValueError("XLSX XML namespace 별칭이 서로 다른 URI를 가리킵니다.")
            continue
        attribute = "xmlns" if not prefix else f"xmlns:{prefix}"
        declarations.append(f' {attribute}="{escape(uri, {chr(34): "&quot;"})}"')
    if not declarations:
        return serialized
    insert_at = serialized_root.end() - 1
    serialized_text = f"{serialized_text[:insert_at]}{''.join(declarations)}{serialized_text[insert_at:]}"
    return serialized_text.encode("utf-8")


def _normalize_main_sheet_view(root: ElementTree.Element) -> None:
    """메인 시트의 표시 모드만 일반 보기로 바꾸고 나머지 view 속성은 보존한다."""

    for sheet_view in root.findall(f"{_xlsx_tag('sheetViews')}/{_xlsx_tag('sheetView')}"):
        sheet_view.set("view", "normal")


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = f"{chr(65 + remainder)}{result}"
    return result


def _column_number(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref)
    if letters is None:
        raise ValueError(f"올바르지 않은 XLSX 셀 주소입니다: {cell_ref}")
    result = 0
    for letter in letters.group():
        result = result * 26 + ord(letter) - 64
    return result


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value.expandtabs(4))


def _wrapped_line_count(value: str, display_width: int) -> int:
    physical_lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return sum(max(1, math.ceil(_display_width(line) / max(1, display_width))) for line in physical_lines)


def _row_height(values: list[str], display_widths: list[int], *, minimum: float = 18.0) -> float:
    line_count = max(
        (_wrapped_line_count(value, width) for value, width in zip(values, display_widths, strict=True)),
        default=1,
    )
    return min(_MAX_ROW_HEIGHT, max(minimum, line_count * _ROW_HEIGHT_PER_LINE + 3.0))


def _split_long_text(value: str, *, max_display_width: int = _WRAP_DISPLAY_WIDTH * 20) -> list[str]:
    """Excel 최대 행 높이에 닿기 전 문단을 공백 경계의 여러 행으로 나눈다."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if _display_width(normalized) <= max_display_width:
        return [normalized]
    chunks: list[str] = []
    remainder = normalized
    while remainder:
        width = 0
        cut = 0
        last_space = 0
        for index, char in enumerate(remainder):
            width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            if char.isspace():
                last_space = index + 1
            if width > max_display_width:
                cut = last_space if last_space >= max(1, index // 2) else index
                break
        if cut == 0:
            chunks.append(remainder.strip())
            break
        chunks.append(remainder[:cut].strip())
        remainder = remainder[cut:].strip()
    return [chunk for chunk in chunks if chunk]


def _split_editable_text_rows(value: str, *, max_display_width: int = _WRAP_DISPLAY_WIDTH) -> list[str]:
    """병합·자동 높이 없이 읽을 수 있도록 한 줄 너비 안에서 물리 행으로 나눈다."""

    physical_lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[str] = []
    for line in physical_lines:
        normalized = line.strip()
        if not normalized:
            continue
        rows.extend(_split_long_text(normalized, max_display_width=max_display_width))
    return rows


def _find_or_create_row(sheet_data: ElementTree.Element, row_number: int) -> ElementTree.Element:
    for row in sheet_data.findall(_xlsx_tag("row")):
        existing = int(row.attrib["r"])
        if existing == row_number:
            return row
        if existing > row_number:
            row = ElementTree.Element(_xlsx_tag("row"), {"r": str(row_number)})
            sheet_data.insert(list(sheet_data).index(next(item for item in sheet_data if int(item.attrib["r"]) == existing)), row)
            return row
    return ElementTree.SubElement(sheet_data, _xlsx_tag("row"), {"r": str(row_number)})


def _set_row_height(row: ElementTree.Element, height: float) -> None:
    row.attrib["ht"] = f"{height:.2f}".rstrip("0").rstrip(".")
    row.attrib["customHeight"] = "1"


def _set_inline_cell(
    sheet_data: ElementTree.Element,
    cell_ref: str,
    value: str | None,
    *,
    style: int,
) -> None:
    row_number_match = re.search(r"\d+$", cell_ref)
    if row_number_match is None:
        raise ValueError(f"올바르지 않은 XLSX 셀 주소입니다: {cell_ref}")
    row = _find_or_create_row(sheet_data, int(row_number_match.group()))
    for existing in list(row.findall(_xlsx_tag("c"))):
        if existing.attrib.get("r") == cell_ref:
            row.remove(existing)
    cell = ElementTree.Element(_xlsx_tag("c"), {"r": cell_ref, "s": str(style)})
    if value is not None:
        cell.attrib["t"] = "inlineStr"
        inline = ElementTree.SubElement(cell, _xlsx_tag("is"))
        text = ElementTree.SubElement(inline, _xlsx_tag("t"))
        if value[:1].isspace() or value[-1:].isspace() or "\n" in value:
            text.attrib["{http://www.w3.org/XML/1998/namespace}space"] = "preserve"
        text.text = value
    insert_at = len(row)
    target_column = _column_number(cell_ref)
    for index, existing in enumerate(row.findall(_xlsx_tag("c"))):
        if _column_number(existing.attrib["r"]) > target_column:
            insert_at = list(row).index(existing)
            break
    row.insert(insert_at, cell)


def _merge_cells_element(root: ElementTree.Element) -> ElementTree.Element:
    existing = root.find(_xlsx_tag("mergeCells"))
    if existing is not None:
        return existing
    sheet_data = root.find(_xlsx_tag("sheetData"))
    if sheet_data is None:
        raise ValueError("기안지의 행 데이터를 찾을 수 없습니다.")
    merge_cells = ElementTree.Element(_xlsx_tag("mergeCells"), {"count": "0"})
    root.insert(list(root).index(sheet_data) + 1, merge_cells)
    return merge_cells


def _add_merge(root: ElementTree.Element, reference: str) -> None:
    merge_cells = _merge_cells_element(root)
    if any(item.attrib.get("ref") == reference for item in merge_cells.findall(_xlsx_tag("mergeCell"))):
        return
    ElementTree.SubElement(merge_cells, _xlsx_tag("mergeCell"), {"ref": reference})
    merge_cells.attrib["count"] = str(len(merge_cells.findall(_xlsx_tag("mergeCell"))))


def _range_overlaps_body(reference: str, *, end_row: int = _BODY_END_ROW) -> bool:
    cells = reference.split(":")
    if len(cells) != 2:
        cells *= 2
    row_numbers = [int(re.search(r"\d+$", cell).group()) for cell in cells]  # type: ignore[union-attr]
    columns = [_column_number(cell) for cell in cells]
    return not (
        max(row_numbers) < _BODY_START_ROW
        or min(row_numbers) > end_row
        or max(columns) < _BODY_FIRST_COLUMN
        or min(columns) > _BODY_LAST_COLUMN
    )


def _clear_body_region(root: ElementTree.Element, *, end_row: int = _BODY_END_ROW) -> ElementTree.Element:
    sheet_data = root.find(_xlsx_tag("sheetData"))
    if sheet_data is None:
        raise ValueError("기안지의 행 데이터를 찾을 수 없습니다.")
    for row in sheet_data.findall(_xlsx_tag("row")):
        row_number = int(row.attrib["r"])
        if not _BODY_START_ROW <= row_number <= end_row:
            continue
        for cell in list(row.findall(_xlsx_tag("c"))):
            if _BODY_FIRST_COLUMN <= _column_number(cell.attrib["r"]) <= _BODY_LAST_COLUMN:
                row.remove(cell)
        row.attrib.pop("ht", None)
        row.attrib.pop("customHeight", None)
    merge_cells = root.find(_xlsx_tag("mergeCells"))
    if merge_cells is not None:
        for item in list(merge_cells.findall(_xlsx_tag("mergeCell"))):
            if _range_overlaps_body(item.attrib["ref"], end_row=end_row):
                merge_cells.remove(item)
        merge_cells.attrib["count"] = str(len(merge_cells.findall(_xlsx_tag("mergeCell"))))
    return sheet_data


def _expand_worksheet_dimension(root: ElementTree.Element, sheet_data: ElementTree.Element) -> None:
    """생성 본문이 원본 used range 아래로 이어질 때 dimension의 마지막 행만 확장한다."""

    dimension = root.find(_xlsx_tag("dimension"))
    if dimension is None:
        return
    reference = dimension.attrib.get("ref", "A1")
    end_reference = reference.split(":")[-1]
    end_column_match = re.match(r"[A-Z]+", end_reference)
    end_row_match = re.search(r"\d+$", end_reference)
    if end_column_match is None or end_row_match is None:
        return
    populated_rows = [
        int(row.attrib["r"])
        for row in sheet_data.findall(_xlsx_tag("row"))
        if row.find(_xlsx_tag("c")) is not None
    ]
    if not populated_rows:
        return
    last_row = max(int(end_row_match.group()), max(populated_rows))
    dimension.attrib["ref"] = f"A1:{end_column_match.group()}{last_row}"


def _append_renderer_styles(styles: bytes) -> tuple[bytes, dict[str, int]]:
    root = ElementTree.fromstring(styles)
    cell_xfs = root.find(_xlsx_tag("cellXfs"))
    if cell_xfs is None:
        raise ValueError("기안지의 셀 스타일을 찾을 수 없습니다.")
    definitions = {
        "section": ("6", "0", "0", "left", "center", False),
        "paragraph": ("4", "0", "0", "left", "center", False),
        "table_header": ("6", "24", "10", "center", "center", True),
        "table_body": ("4", "0", "10", "left", "top", True),
    }
    indexes: dict[str, int] = {}
    for name, (font_id, fill_id, border_id, horizontal, vertical, wrap_text) in definitions.items():
        indexes[name] = len(cell_xfs)
        attributes = {
            "numFmtId": "0",
            "fontId": font_id,
            "fillId": fill_id,
            "borderId": border_id,
            "xfId": "0",
            "applyFont": "1",
            "applyAlignment": "1",
        }
        if fill_id != "0":
            attributes["applyFill"] = "1"
        if border_id != "0":
            attributes["applyBorder"] = "1"
        xf = ElementTree.SubElement(cell_xfs, _xlsx_tag("xf"), attributes)
        ElementTree.SubElement(
            xf,
            _xlsx_tag("alignment"),
            {"horizontal": horizontal, "vertical": vertical, "wrapText": "1" if wrap_text else "0"},
        )
    cell_xfs.attrib["count"] = str(len(cell_xfs))
    return _serialize_xml_preserving_root_namespaces(root, styles), indexes


def _column_spans(width: int) -> list[tuple[int, int]]:
    base, remainder = divmod(_BODY_LAST_COLUMN - _BODY_FIRST_COLUMN + 1, width)
    spans: list[tuple[int, int]] = []
    start = _BODY_FIRST_COLUMN
    for index in range(width):
        size = base + (1 if index < remainder else 0)
        spans.append((start, start + size - 1))
        start += size
    return spans


def _render_editable_text_row(
    sheet_data: ElementTree.Element,
    row_number: int,
    value: str,
    *,
    style: int,
) -> None:
    """본문 한 줄을 A열에만 기록해 주변 셀과 기본 행 높이를 편집 가능하게 보존한다."""

    _set_inline_cell(sheet_data, f"A{row_number}", value, style=style)


def _render_inline_table(
    root: ElementTree.Element,
    sheet_data: ElementTree.Element,
    start_row: int,
    table: ProposalTableBlock,
    styles: dict[str, int],
) -> int:
    spans = _column_spans(len(table.headers))
    for offset, values in enumerate([table.headers, *table.rows]):
        row_number = start_row + offset
        style = styles["table_header" if offset == 0 else "table_body"]
        display_widths: list[int] = []
        for value, (start, end) in zip(values, spans, strict=True):
            for column in range(start, end + 1):
                _set_inline_cell(
                    sheet_data,
                    f"{_column_name(column)}{row_number}",
                    value if column == start else None,
                    style=style,
                )
            if start != end:
                _add_merge(root, f"{_column_name(start)}{row_number}:{_column_name(end)}{row_number}")
            display_widths.append(max(4, round(_WRAP_DISPLAY_WIDTH * (end - start + 1) / 26)))
        _set_row_height(
            _find_or_create_row(sheet_data, row_number),
            _row_height(list(values), display_widths, minimum=22.0 if offset == 0 else 18.0),
        )
    return start_row + len(table.rows) + 1


def _render_main_document(
    root: ElementTree.Element,
    document: ProposalDocumentV2,
    styles: dict[str, int],
) -> list[_DeferredProposalTable]:
    sheet_data = _clear_body_region(root, end_row=_STRUCTURED_BODY_END_ROW)
    row_number = _BODY_START_ROW
    deferred: list[_DeferredProposalTable] = []
    omitted = False

    def add_editable_line(value: str, style: int) -> bool:
        nonlocal row_number, omitted
        if row_number > _STRUCTURED_BODY_END_ROW:
            omitted = True
            return False
        _render_editable_text_row(
            sheet_data,
            row_number,
            value,
            style=style,
        )
        row_number += 1
        return True

    for section_index, section in enumerate(document.sections, start=1):
        add_editable_line(f"{section_index}. {section.heading}", styles["section"])
        for block in section.blocks:
            if isinstance(block, ProposalParagraphBlock):
                for chunk in _split_editable_text_rows(block.text):
                    add_editable_line(chunk, styles["paragraph"])
            elif isinstance(block, ProposalListBlock):
                for item_index, item in enumerate(block.items, start=1):
                    marker = f"{item_index})" if block.ordered else "-"
                    chunks = _split_editable_text_rows(item, max_display_width=_WRAP_DISPLAY_WIDTH - 4)
                    for chunk_index, chunk in enumerate(chunks):
                        prefix = f"{marker} " if chunk_index == 0 else "  "
                        add_editable_line(f"{prefix}{chunk}", styles["paragraph"])
            else:
                required_rows = len(block.rows) + 1
                remaining_rows = _STRUCTURED_BODY_END_ROW - row_number + 1
                if len(block.headers) <= _INLINE_TABLE_MAX_COLUMNS and required_rows <= remaining_rows:
                    row_number = _render_inline_table(root, sheet_data, row_number, block, styles)
                else:
                    deferred.append(_DeferredProposalTable(heading=section.heading, table=block))
                    add_editable_line(
                        f"※ {section.heading} 표는 '{_APPENDIX_SHEET_NAME}' 시트를 참조하세요.",
                        styles["paragraph"],
                    )
        for missing in section.missing_information:
            add_editable_line(f"[확인 필요] {missing}", styles["paragraph"])
    for missing in document.missing_information:
        add_editable_line(f"[확인 필요] {missing}", styles["paragraph"])

    if omitted and _STRUCTURED_BODY_END_ROW >= _BODY_START_ROW:
        final_row = _find_or_create_row(sheet_data, _STRUCTURED_BODY_END_ROW)
        for cell in list(final_row.findall(_xlsx_tag("c"))):
            if _BODY_FIRST_COLUMN <= _column_number(cell.attrib["r"]) <= _BODY_LAST_COLUMN:
                final_row.remove(cell)
        merge_cells = root.find(_xlsx_tag("mergeCells"))
        if merge_cells is not None:
            for item in list(merge_cells.findall(_xlsx_tag("mergeCell"))):
                reference = item.attrib["ref"]
                cells = reference.split(":")
                row_numbers = [int(re.search(r"\d+$", cell).group()) for cell in cells]  # type: ignore[union-attr]
                if min(row_numbers) <= _STRUCTURED_BODY_END_ROW <= max(row_numbers) and _range_overlaps_body(
                    reference,
                    end_row=_STRUCTURED_BODY_END_ROW,
                ):
                    merge_cells.remove(item)
            merge_cells.attrib["count"] = str(len(merge_cells.findall(_xlsx_tag("mergeCell"))))
        _render_editable_text_row(
            sheet_data,
            _STRUCTURED_BODY_END_ROW,
            _PROJECTION_OMISSION_NOTICE,
            style=styles["paragraph"],
        )
    _expand_worksheet_dimension(root, sheet_data)
    return deferred


def _appendix_sheet_xml(tables: list[_DeferredProposalTable], styles: dict[str, int]) -> bytes:
    root = ElementTree.Element(_xlsx_tag("worksheet"))
    dimension = ElementTree.SubElement(root, _xlsx_tag("dimension"), {"ref": "A1"})
    sheet_views = ElementTree.SubElement(root, _xlsx_tag("sheetViews"))
    ElementTree.SubElement(sheet_views, _xlsx_tag("sheetView"), {"workbookViewId": "0"})
    ElementTree.SubElement(root, _xlsx_tag("sheetFormatPr"), {"defaultRowHeight": "15"})
    max_columns = max(len(item.table.headers) for item in tables)
    columns = ElementTree.SubElement(root, _xlsx_tag("cols"))
    ElementTree.SubElement(
        columns,
        _xlsx_tag("col"),
        {"min": "1", "max": str(max_columns), "width": "20", "customWidth": "1"},
    )
    sheet_data = ElementTree.SubElement(root, _xlsx_tag("sheetData"))
    row_number = 1
    for table_index, item in enumerate(tables, start=1):
        last_column = _column_name(len(item.table.headers))
        _set_inline_cell(
            sheet_data,
            f"A{row_number}",
            f"표 {table_index}. {item.heading}",
            style=styles["section"],
        )
        if last_column != "A":
            _add_merge(root, f"A{row_number}:{last_column}{row_number}")
        _set_row_height(_find_or_create_row(sheet_data, row_number), 22.0)
        row_number += 1
        for offset, values in enumerate([item.table.headers, *item.table.rows]):
            style = styles["table_header" if offset == 0 else "table_body"]
            for column, value in enumerate(values, start=1):
                _set_inline_cell(sheet_data, f"{_column_name(column)}{row_number}", value, style=style)
            _set_row_height(
                _find_or_create_row(sheet_data, row_number),
                _row_height(list(values), [20] * len(values), minimum=22.0 if offset == 0 else 18.0),
            )
            row_number += 1
        row_number += 1
    dimension.attrib["ref"] = f"A1:{_column_name(max_columns)}{max(1, row_number - 1)}"
    ElementTree.SubElement(
        root,
        _xlsx_tag("pageMargins"),
        {"left": "0.3", "right": "0.3", "top": "0.5", "bottom": "0.5", "header": "0.2", "footer": "0.2"},
    )
    ElementTree.SubElement(
        root,
        _xlsx_tag("pageSetup"),
        {"orientation": "landscape", "fitToWidth": "1", "fitToHeight": "0"},
    )
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _install_appendix_sheet(files: dict[str, bytes], content: bytes) -> None:
    workbook_original = files["xl/workbook.xml"]
    relationships_original = files["xl/_rels/workbook.xml.rels"]
    workbook = ElementTree.fromstring(workbook_original)
    relationships = ElementTree.fromstring(relationships_original)
    sheets = workbook.find(_xlsx_tag("sheets"))
    if sheets is None:
        raise ValueError("기안지의 시트 목록을 찾을 수 없습니다.")
    existing = next((item for item in sheets if item.attrib.get("name") == _APPENDIX_SHEET_NAME), None)
    relationship_by_id = {item.attrib.get("Id"): item for item in relationships}
    if existing is not None:
        relationship_id = existing.attrib[f"{{{_OFFICE_REL_NS}}}id"]
        target = relationship_by_id[relationship_id].attrib["Target"].replace("\\", "/").lstrip("/")
        worksheet_path = target if target.startswith("xl/") else f"xl/{target}"
    else:
        previous_sheet_count = len(sheets)
        sheet_numbers = [
            int(match.group(1))
            for name in files
            if (match := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", name)) is not None
        ]
        next_sheet_number = max(sheet_numbers, default=0) + 1
        worksheet_path = f"xl/worksheets/sheet{next_sheet_number}.xml"
        used_ids = {
            int(match.group(1))
            for item in relationships
            if (match := re.fullmatch(r"rId(\d+)", item.attrib.get("Id", ""))) is not None
        }
        relationship_id = f"rId{max(used_ids, default=0) + 1}"
        ElementTree.SubElement(
            relationships,
            f"{{{_PACKAGE_REL_NS}}}Relationship",
            {
                "Id": relationship_id,
                "Type": f"{_OFFICE_REL_NS}/worksheet",
                "Target": f"worksheets/sheet{next_sheet_number}.xml",
            },
        )
        sheet_ids = [int(item.attrib.get("sheetId", "0")) for item in sheets]
        ElementTree.SubElement(
            sheets,
            _xlsx_tag("sheet"),
            {
                "name": _APPENDIX_SHEET_NAME,
                "sheetId": str(max(sheet_ids, default=0) + 1),
                f"{{{_OFFICE_REL_NS}}}id": relationship_id,
            },
        )
        content_types_original = files["[Content_Types].xml"]
        content_types = ElementTree.fromstring(content_types_original)
        part_name = f"/{worksheet_path}"
        if not any(item.attrib.get("PartName") == part_name for item in content_types):
            ElementTree.SubElement(
                content_types,
                f"{{{_CONTENT_TYPES_NS}}}Override",
                {
                    "PartName": part_name,
                    "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                },
            )
        files["[Content_Types].xml"] = _serialize_xml_preserving_root_namespaces(
            content_types,
            content_types_original,
        )
        app_properties_path = "docProps/app.xml"
        if app_properties_path in files:
            app_properties_original = files[app_properties_path]
            app_properties = ElementTree.fromstring(app_properties_original)
            heading_vector = app_properties.find(
                f"{{{_EXTENDED_PROPERTIES_NS}}}HeadingPairs/{{{_DOC_PROPS_VT_NS}}}vector"
            )
            if heading_vector is not None:
                variants = heading_vector.findall(f"{{{_DOC_PROPS_VT_NS}}}variant")
                for index, variant in enumerate(variants[:-1]):
                    label = variant.find(f"{{{_DOC_PROPS_VT_NS}}}lpstr")
                    if label is not None and label.text == "워크시트":
                        count = variants[index + 1].find(f"{{{_DOC_PROPS_VT_NS}}}i4")
                        if count is not None:
                            count.text = str(previous_sheet_count + 1)
                        break
            titles_vector = app_properties.find(
                f"{{{_EXTENDED_PROPERTIES_NS}}}TitlesOfParts/{{{_DOC_PROPS_VT_NS}}}vector"
            )
            if titles_vector is not None:
                title = ElementTree.Element(f"{{{_DOC_PROPS_VT_NS}}}lpstr")
                title.text = _APPENDIX_SHEET_NAME
                titles_vector.insert(previous_sheet_count, title)
                titles_vector.attrib["size"] = str(len(titles_vector))
            files[app_properties_path] = _serialize_xml_preserving_root_namespaces(
                app_properties,
                app_properties_original,
            )
    files[worksheet_path] = content
    files["xl/workbook.xml"] = _serialize_xml_preserving_root_namespaces(workbook, workbook_original)
    files["xl/_rels/workbook.xml.rels"] = _serialize_xml_preserving_root_namespaces(
        relationships,
        relationships_original,
    )


def render_proposal_workbook(template: bytes, document: ProposalDocumentV2) -> bytes:
    """V2 block을 편집 가능한 기본 행과 실제 matrix 표로 렌더링한 XLSX를 만든다."""

    if not template.startswith(b"PK\x03\x04"):
        raise ValueError("기안 초안 서식이 올바르지 않습니다.")
    source_buffer = io.BytesIO(template)
    output_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer, "r") as source:
        worksheet_path = _worksheet_path(source, _TITLE_SHEET_NAME)
        files = {item.filename: source.read(item.filename) for item in source.infolist()}
        xml = files[worksheet_path].decode("utf-8")
        xml = _inline_string_cell(xml, _TITLE_CELL, document.title, default_style="3")
        xml = _inline_string_cell(xml, _APPROVAL_REQUEST_CELL, document.approval_request, default_style="23")
        styles_content, styles = _append_renderer_styles(files["xl/styles.xml"])
        files["xl/styles.xml"] = styles_content
        root = ElementTree.fromstring(xml)
        _normalize_main_sheet_view(root)
        deferred = _render_main_document(root, document, styles)
        files[worksheet_path] = _serialize_xml_preserving_root_namespaces(root, xml.encode("utf-8"))
        if deferred:
            _install_appendix_sheet(files, _appendix_sheet_xml(deferred, styles))

        with zipfile.ZipFile(output_buffer, "w") as target:
            original_names = set()
            for item in source.infolist():
                original_names.add(item.filename)
                target.writestr(item, files[item.filename])
            for name, content in files.items():
                if name not in original_names:
                    target.writestr(name, content)
    generated = output_buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(generated), "r") as workbook:
        if workbook.testzip() is not None:
            raise ValueError("생성된 기안 초안 파일이 손상되었습니다.")
    return generated


def insert_proposal_fields(template: bytes, fields: ProposalDraftFields) -> bytes:
    """기존 XLSX ZIP 구조를 보존하며 제목·재가 요청·본문을 지정 위치에 literal string으로 쓴다."""

    if not template.startswith(b"PK\x03\x04"):
        raise ValueError("기안 초안 서식이 올바르지 않습니다.")
    body_lines = fields.body.splitlines()
    body_capacity = _BODY_END_ROW - _BODY_START_ROW + 1
    if len(body_lines) > body_capacity:
        raise ProposalDraftError(
            "proposal_body_too_long",
            f"기안 본문은 줄바꿈 기준 최대 {body_capacity}줄까지 작성할 수 있습니다.",
            422,
        )
    source_buffer = io.BytesIO(template)
    output_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer, "r") as source:
        worksheet_path = _worksheet_path(source, _TITLE_SHEET_NAME)
        with zipfile.ZipFile(output_buffer, "w") as target:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == worksheet_path:
                    xml = content.decode("utf-8")
                    xml = _inline_string_cell(xml, _TITLE_CELL, fields.title, default_style="3")
                    xml = _inline_string_cell(
                        xml,
                        _APPROVAL_REQUEST_CELL,
                        fields.approval_request,
                        default_style="23",
                    )
                    for index, line in enumerate(body_lines):
                        xml = _inline_string_cell(
                            xml,
                            f"A{_BODY_START_ROW + index}",
                            line,
                            default_style=_BODY_DEFAULT_STYLE,
                        )
                    content = xml.encode("utf-8")
                target.writestr(item, content)
    generated = output_buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(generated), "r") as workbook:
        if workbook.testzip() is not None:
            raise ValueError("생성된 기안 초안 파일이 손상되었습니다.")
    return generated


def insert_proposal_title(template: bytes, title: str) -> bytes:
    """구버전 호출자를 위해 제목만 지정하고 나머지 필드는 안전한 합성 문구로 채운다."""

    return insert_proposal_fields(
        template,
        ProposalDraftFields(title=title, approval_request="검토 후 재가하여 주시기 바랍니다.", body="초안"),
    )


class ProposalDraftService:
    """LLM 구조화 생성, XLSX 삽입, SMB 신규 저장, 다운로드 등록을 연결한다."""

    def __init__(
        self,
        settings: Settings | None = None,
        upload_manager: UploadManager | None = None,
        *,
        template_path: Path = _TEMPLATE_PATH,
        clock: Callable[[], datetime] | None = None,
        draft_generator: ProposalDraftGenerator | None = None,
        title_generator: ProposalDraftGenerator | None = None,
        registry: ProposalDraftRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._upload_manager = upload_manager
        self._template_path = template_path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._draft_generator = draft_generator or title_generator or (LocalProposalDraftGenerator(settings) if settings else None)
        self._registry = registry or ProposalDraftRegistry()

    def close(self) -> None:
        """기안 제목 생성기의 HTTP 연결을 닫는다."""

        if self._draft_generator is not None:
            self._draft_generator.close()

    def _read_template(self) -> bytes:
        try:
            content = self._template_path.read_bytes()
        except OSError as exc:
            raise ProposalDraftError("proposal_template_unavailable", "기안 초안 서식을 불러올 수 없습니다.", 503) from exc
        if not content.startswith(b"PK\x03\x04"):
            raise ProposalDraftError("proposal_template_invalid", "기안 초안 서식이 올바르지 않습니다.", 503)
        return content

    def get_template_download(self) -> ProposalDraftDownload:
        """호환성을 위해 아직 내용을 채우지 않은 원본 서식을 반환한다."""

        return ProposalDraftDownload(
            file_name=build_versioned_filename(_TEMPLATE_NAME, self._clock(), prefix=_PROPOSAL_PREFIX),
            content=self._read_template(),
        )

    def _verify_document(
        self,
        instruction: str,
        document: ProposalDocumentV2,
        evidence: list[DocumentCitation],
        *,
        proposal_type: ResolvedProposalType,
        user_answers: list[str] | None = None,
        allow_questions: bool = True,
        clarification_round: int = 0,
    ) -> tuple[ProposalDocumentV2, ProposalCompletionSummary, float]:
        """지원하는 생성기에서는 별도 근거 판정을 수행하고 구 테스트 double은 호환한다."""

        if self._draft_generator is None:
            raise ProposalDraftError("proposal_generation_not_configured", "기안 초안 생성 기능이 구성되지 않았습니다.", 503)
        assessor = getattr(self._draft_generator, "assess", None)
        if not callable(assessor):
            return document, _fallback_completion(document), 0.0
        started = time.perf_counter()
        verification = assessor(
            instruction,
            document,
            evidence,
            proposal_type=proposal_type,
            user_answers=user_answers,
            allow_questions=allow_questions,
        )
        try:
            verified_document, completion = apply_proposal_evidence_verification(
                document,
                verification,
                allow_questions=allow_questions,
                clarification_round=clarification_round,
            )
        except ValueError as exc:
            raise ProposalDraftError(
                "proposal_verification_response_invalid",
                "근거 검증 결과를 기안 본문에 안전하게 적용하지 못했습니다.",
                502,
            ) from exc
        return verified_document, completion, round((time.perf_counter() - started) * 1000, 1)

    def generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        selected_files: list[SelectedFileContext],
        proposal_type: ProposalTypeRequest = "auto",
        validation_ms: float = 0.0,
        retrieval_ms: float = 0.0,
    ) -> ProposalDraftGenerateResponse:
        """선택 문서 근거로 세 필드를 생성해 신규 XLSX로 저장하고 다운로드를 등록한다."""

        if self._draft_generator is None or self._upload_manager is None:
            raise ProposalDraftError("proposal_generation_not_configured", "기안 초안 생성 기능이 구성되지 않았습니다.", 503)
        started = time.perf_counter()
        type_resolution = resolve_proposal_type(proposal_type, instruction, evidence)
        filter_started = time.perf_counter()
        filtered = filter_proposal_evidence(
            evidence,
            proposal_type=type_resolution.proposal_type,
            instruction=instruction,
        )
        filter_ms = round((time.perf_counter() - filter_started) * 1000, 1)
        if not filtered.citations:
            raise ProposalDraftError(
                "proposal_relevant_evidence_unavailable",
                "선택한 첨부는 이 기안 유형의 본문 근거로 사용하기 어렵습니다. 행사 안내나 참가 계획 자료를 선택해 주세요.",
                422,
                evidence_filter=filtered.summary,
            )
        llm_started = time.perf_counter()
        draft_result = self._draft_generator.generate(
            instruction,
            filtered.citations,
            proposal_type=type_resolution.proposal_type,
        )
        llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
        generated_structured_document = draft_result.document is not None
        document = draft_result.document or _legacy_document(draft_result.fields)
        try:
            document = normalize_proposal_document(document, type_resolution.proposal_type)
            document, completion, verification_ms = self._verify_document(
                instruction,
                document,
                filtered.citations,
                proposal_type=type_resolution.proposal_type,
            )
            fields = (
                project_document_to_legacy_fields(document)
                if generated_structured_document
                or type_resolution.proposal_type == "event_attendance"
                or callable(getattr(self._draft_generator, "assess", None))
                else draft_result.fields
            )
        except ValueError as exc:
            raise ProposalDraftError(
                "proposal_llm_response_invalid",
                "로컬 LLM이 기안 유형에 맞는 본문 구조를 반환하지 않았습니다.",
                502,
            ) from exc

        if completion.status == "needs_clarification":
            draft_id = self._registry.register(
                ProposalDraftRecord(
                    download=None,
                    document=document,
                    instruction=instruction,
                    selected_files=tuple(selected_files),
                    proposal_type=type_resolution.proposal_type,
                    proposal_type_source=type_resolution.source,
                    completion=completion,
                )
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            _logger.info(
                "Proposal draft needs clarification: draft_id=%s questions=%d llm_ms=%.1f verification_ms=%.1f "
                "elapsed_ms=%.1f",
                draft_id,
                len(completion.questions),
                llm_ms,
                verification_ms,
                elapsed_ms,
            )
            return ProposalDraftGenerateResponse(
                draft_id=draft_id,
                title=fields.title,
                fields=fields,
                document=document,
                proposal_type=type_resolution.proposal_type,
                proposal_type_source=type_resolution.source,
                evidence_filter=filtered.summary,
                context_usage=draft_result.context_usage,
                completion=completion,
                saved_to_smb=False,
                model_used=draft_result.model,
                elapsed_ms=elapsed_ms,
                timings_ms={
                    "validation": validation_ms,
                    "retrieval": retrieval_ms,
                    "evidence_filter": filter_ms,
                    "llm": llm_ms,
                    "verification": verification_ms,
                    "workbook": 0.0,
                    "smb": 0.0,
                },
            )

        workbook_started = time.perf_counter()
        try:
            content = render_proposal_workbook(self._read_template(), document)
        except ProposalDraftError:
            raise
        except ValueError as exc:
            raise ProposalDraftError("proposal_workbook_generation_failed", "기안 초안 엑셀을 생성하지 못했습니다.", 503) from exc
        workbook_ms = round((time.perf_counter() - workbook_started) * 1000, 1)

        file_name = build_versioned_filename(
            f"{_title_document_name(fields.title)}.xlsx",
            self._clock(),
            prefix=_PROPOSAL_PREFIX,
        )
        smb_started = time.perf_counter()
        try:
            saved = self._upload_manager.save_proposal_draft(content, file_name)
        except UploadError as exc:
            raise ProposalDraftError(exc.code, exc.message, exc.status_code) from exc
        smb_ms = round((time.perf_counter() - smb_started) * 1000, 1)

        download = ProposalDraftDownload(file_name=saved.file_name, content=content)
        draft_id = self._registry.register(
            ProposalDraftRecord(
                download=download,
                document=document,
                instruction=instruction,
                selected_files=tuple(selected_files),
                proposal_type=type_resolution.proposal_type,
                proposal_type_source=type_resolution.source,
                completion=completion,
            )
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _logger.info(
            "Proposal draft completed: draft_id=%s llm_ms=%.1f workbook_ms=%.1f smb_ms=%.1f elapsed_ms=%.1f",
            draft_id,
            llm_ms,
            workbook_ms,
            smb_ms,
            elapsed_ms,
        )
        return ProposalDraftGenerateResponse(
            draft_id=draft_id,
            title=fields.title,
            fields=fields,
            document=document,
            proposal_type=type_resolution.proposal_type,
            proposal_type_source=type_resolution.source,
            evidence_filter=filtered.summary,
            context_usage=draft_result.context_usage,
            completion=completion,
            file_name=saved.file_name,
            download_url=f"/api/playground/drafts/proposal/{draft_id}",
            destination_label=saved.destination_label,
            model_used=draft_result.model,
            elapsed_ms=elapsed_ms,
            timings_ms={
                "validation": validation_ms,
                "retrieval": retrieval_ms,
                "evidence_filter": filter_ms,
                "llm": llm_ms,
                "verification": verification_ms,
                "workbook": workbook_ms,
                "smb": smb_ms,
            },
        )

    def get_clarification_record(
        self,
        draft_id: UUID,
        selected_files: list[SelectedFileContext],
    ) -> ProposalDraftRecord:
        """확인 대기 상태와 생성 당시 선택 문서 snapshot을 함께 검증한다."""

        record = self._registry.find(draft_id)
        if record is None:
            raise ProposalDraftError("proposal_draft_not_found", "확인할 기안 초안을 찾을 수 없습니다.", 404)
        if tuple(selected_files) != record.selected_files:
            raise ProposalDraftError(
                "proposal_clarification_scope_mismatch",
                "최초 기안의 선택 문서 범위가 달라졌습니다. 원래 확인 질문에서 다시 답해 주세요.",
                409,
            )
        if record.completion.status != "needs_clarification" or not record.completion.questions:
            raise ProposalDraftError(
                "proposal_clarification_not_required",
                "이 기안은 사용자 확인을 기다리는 상태가 아닙니다.",
                409,
            )
        return record

    def clarify(
        self,
        draft_id: UUID,
        answers: list[ProposalClarificationAnswer],
        selected_files: list[SelectedFileContext],
        evidence: list[DocumentCitation],
        *,
        validation_ms: float = 0.0,
        retrieval_ms: float = 0.0,
    ) -> ProposalDraftClarificationResponse:
        """최대 세 답변을 사용자 근거로 반영한 뒤에만 XLSX를 생성·신규 저장한다."""

        if self._draft_generator is None or self._upload_manager is None:
            raise ProposalDraftError("proposal_generation_not_configured", "기안 초안 생성 기능이 구성되지 않았습니다.", 503)
        started = time.perf_counter()
        base = self.get_clarification_record(draft_id, selected_files)
        expected = {question.question_id: question for question in base.completion.questions}
        received = {answer.question_id: answer for answer in answers}
        if set(received) != set(expected):
            raise ProposalDraftError(
                "proposal_clarification_answers_incomplete",
                "표시된 확인 질문에 모두 답해 주세요.",
                422,
            )
        answer_sources = [f"{expected[answer.question_id].prompt} 답변: {answer.answer}" for answer in answers]
        filter_started = time.perf_counter()
        filtered = filter_proposal_evidence(
            evidence,
            proposal_type=base.proposal_type,
            instruction=f"{base.instruction}\n" + "\n".join(answer_sources),
        )
        filter_ms = round((time.perf_counter() - filter_started) * 1000, 1)
        if not filtered.citations:
            raise ProposalDraftError(
                "proposal_relevant_evidence_unavailable",
                "선택한 첨부에서 기안 본문 근거를 다시 확인할 수 없습니다.",
                422,
                evidence_filter=filtered.summary,
            )

        llm_started = time.perf_counter()
        clarification_instruction = (
            f"원본 작성 요청:\n{base.instruction}\n\n사용자 확인 답변:\n" + "\n".join(answer_sources)
        )
        clarified_result = self._draft_generator.revise(
            clarification_instruction,
            base.document,
            filtered.citations,
            proposal_type=base.proposal_type,
        )
        llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
        clarified_document = clarified_result.document or _legacy_document(clarified_result.fields)
        try:
            clarified_document = normalize_proposal_document(clarified_document, base.proposal_type)
            clarified_document, completion, verification_ms = self._verify_document(
                base.instruction,
                clarified_document,
                filtered.citations,
                proposal_type=base.proposal_type,
                user_answers=answer_sources,
                allow_questions=False,
                clarification_round=1,
            )
            fields = project_document_to_legacy_fields(clarified_document)
        except ValueError as exc:
            raise ProposalDraftError(
                "proposal_llm_response_invalid",
                "확인 답변을 반영한 기안 본문을 완성하지 못했습니다.",
                502,
            ) from exc

        workbook_started = time.perf_counter()
        try:
            content = render_proposal_workbook(self._read_template(), clarified_document)
        except ProposalDraftError:
            raise
        except ValueError as exc:
            raise ProposalDraftError("proposal_workbook_generation_failed", "기안 초안 엑셀을 생성하지 못했습니다.", 503) from exc
        workbook_ms = round((time.perf_counter() - workbook_started) * 1000, 1)
        file_name = build_versioned_filename(
            f"{_title_document_name(fields.title)}.xlsx",
            self._clock(),
            prefix=_PROPOSAL_PREFIX,
        )
        smb_started = time.perf_counter()
        try:
            saved = self._upload_manager.save_proposal_draft(content, file_name)
        except UploadError as exc:
            raise ProposalDraftError(exc.code, exc.message, exc.status_code) from exc
        smb_ms = round((time.perf_counter() - smb_started) * 1000, 1)
        final_id = self._registry.register(
            ProposalDraftRecord(
                download=ProposalDraftDownload(file_name=saved.file_name, content=content),
                document=clarified_document,
                instruction=base.instruction,
                selected_files=base.selected_files,
                proposal_type=base.proposal_type,
                proposal_type_source=base.proposal_type_source,
                completion=completion,
                root_draft_id=draft_id,
            )
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _logger.info(
            "Proposal clarification completed: draft_id=%s answers=%d llm_ms=%.1f verification_ms=%.1f "
            "workbook_ms=%.1f smb_ms=%.1f elapsed_ms=%.1f",
            final_id,
            len(answers),
            llm_ms,
            verification_ms,
            workbook_ms,
            smb_ms,
            elapsed_ms,
        )
        return ProposalDraftClarificationResponse(
            draft_id=final_id,
            title=fields.title,
            fields=fields,
            document=clarified_document,
            proposal_type=base.proposal_type,
            proposal_type_source=base.proposal_type_source,
            evidence_filter=filtered.summary,
            context_usage=clarified_result.context_usage,
            completion=completion,
            file_name=saved.file_name,
            download_url=f"/api/playground/drafts/proposal/{final_id}",
            destination_label=saved.destination_label,
            model_used=clarified_result.model,
            elapsed_ms=elapsed_ms,
            timings_ms={
                "validation": validation_ms,
                "retrieval": retrieval_ms,
                "evidence_filter": filter_ms,
                "llm": llm_ms,
                "verification": verification_ms,
                "workbook": workbook_ms,
                "smb": smb_ms,
            },
            clarification_of_draft_id=draft_id,
            answered_question_count=len(answers),
        )

    def get_revision_record(
        self,
        draft_id: UUID,
        selected_files: list[SelectedFileContext],
    ) -> ProposalDraftRecord:
        """수정 대상과 클라이언트가 돌려준 원본 선택 snapshot을 함께 검증한다."""

        record = self._registry.find(draft_id)
        if record is None:
            raise ProposalDraftError("proposal_draft_not_found", "수정할 기안 초안을 찾을 수 없습니다.", 404)
        if tuple(selected_files) != record.selected_files:
            raise ProposalDraftError(
                "proposal_revision_scope_mismatch",
                "원본 기안의 선택 문서 범위가 달라졌습니다. 원본 초안에서 다시 수정해 주세요.",
                409,
            )
        if record.download is None:
            raise ProposalDraftError(
                "proposal_clarification_required",
                "확인 질문에 먼저 답한 뒤 초안을 수정해 주세요.",
                409,
            )
        return record

    def revise(
        self,
        draft_id: UUID,
        feedback: str,
        selected_files: list[SelectedFileContext],
        evidence: list[DocumentCitation],
        *,
        validation_ms: float = 0.0,
        retrieval_ms: float = 0.0,
    ) -> ProposalDraftRevisionResponse:
        """원본 기록은 유지하고 피드백을 반영한 새 minor 버전을 신규 저장한다."""

        if self._draft_generator is None or self._upload_manager is None:
            raise ProposalDraftError("proposal_generation_not_configured", "기안 초안 생성 기능이 구성되지 않았습니다.", 503)
        started = time.perf_counter()
        base = self.get_revision_record(draft_id, selected_files)
        filter_started = time.perf_counter()
        filtered = filter_proposal_evidence(
            evidence,
            proposal_type=base.proposal_type,
            instruction=f"{base.instruction}\n{feedback}",
        )
        filter_ms = round((time.perf_counter() - filter_started) * 1000, 1)
        if not filtered.citations:
            raise ProposalDraftError(
                "proposal_relevant_evidence_unavailable",
                "선택한 첨부는 이 기안 유형의 본문 근거로 사용하기 어렵습니다. 행사 안내나 참가 계획 자료를 선택해 주세요.",
                422,
                evidence_filter=filtered.summary,
            )

        llm_started = time.perf_counter()
        revision_instruction = f"원본 작성 요청:\n{base.instruction}\n수정 피드백:\n{feedback}"
        revised_result = self._draft_generator.revise(
            revision_instruction,
            base.document,
            filtered.citations,
            proposal_type=base.proposal_type,
        )
        llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
        revised_document = revised_result.document or _legacy_document(revised_result.fields)
        try:
            revised_document = normalize_proposal_document(revised_document, base.proposal_type)
            revised_document, completion, verification_ms = self._verify_document(
                f"{base.instruction}\n수정 피드백: {feedback}",
                revised_document,
                filtered.citations,
                proposal_type=base.proposal_type,
                allow_questions=False,
            )
            fields = project_document_to_legacy_fields(revised_document)
        except ValueError as exc:
            raise ProposalDraftError(
                "proposal_llm_response_invalid",
                "로컬 LLM이 기안 유형에 맞는 수정 문서 구조를 반환하지 않았습니다.",
                502,
            ) from exc

        workbook_started = time.perf_counter()
        try:
            content = render_proposal_workbook(self._read_template(), revised_document)
        except ProposalDraftError:
            raise
        except ValueError as exc:
            raise ProposalDraftError("proposal_workbook_generation_failed", "기안 수정본 엑셀을 생성하지 못했습니다.", 503) from exc
        workbook_ms = round((time.perf_counter() - workbook_started) * 1000, 1)

        try:
            root_draft_id, revision_minor = self._registry.next_revision_minor(draft_id)
        except KeyError as exc:
            raise ProposalDraftError("proposal_draft_not_found", "수정할 기안 초안을 찾을 수 없습니다.", 404) from exc
        if base.download is None:
            raise ProposalDraftError("proposal_clarification_required", "확인 질문에 먼저 답해 주세요.", 409)
        file_name = _revision_file_name(base.download.file_name, revision_minor)
        smb_started = time.perf_counter()
        try:
            saved = self._upload_manager.save_proposal_draft(content, file_name)
        except UploadError as exc:
            raise ProposalDraftError(exc.code, exc.message, exc.status_code) from exc
        smb_ms = round((time.perf_counter() - smb_started) * 1000, 1)

        download = ProposalDraftDownload(file_name=saved.file_name, content=content)
        revised_draft_id = self._registry.register(
            ProposalDraftRecord(
                download=download,
                document=revised_document,
                instruction=base.instruction,
                selected_files=base.selected_files,
                proposal_type=base.proposal_type,
                proposal_type_source=base.proposal_type_source,
                completion=completion,
                revision_minor=revision_minor,
                root_draft_id=root_draft_id,
            )
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        summary = _revision_summary(base.document, revised_document, revision_minor)
        _logger.info(
            "Proposal revision completed: draft_id=%s revision_minor=%d llm_ms=%.1f workbook_ms=%.1f "
            "smb_ms=%.1f elapsed_ms=%.1f",
            revised_draft_id,
            revision_minor,
            llm_ms,
            workbook_ms,
            smb_ms,
            elapsed_ms,
        )
        return ProposalDraftRevisionResponse(
            draft_id=revised_draft_id,
            title=fields.title,
            fields=fields,
            document=revised_document,
            proposal_type=base.proposal_type,
            proposal_type_source=base.proposal_type_source,
            evidence_filter=filtered.summary,
            context_usage=revised_result.context_usage,
            completion=completion,
            file_name=saved.file_name,
            download_url=f"/api/playground/drafts/proposal/{revised_draft_id}",
            destination_label=saved.destination_label,
            model_used=revised_result.model,
            elapsed_ms=elapsed_ms,
            timings_ms={
                "validation": validation_ms,
                "retrieval": retrieval_ms,
                "evidence_filter": filter_ms,
                "llm": llm_ms,
                "verification": verification_ms,
                "workbook": workbook_ms,
                "smb": smb_ms,
            },
            revision_of_draft_id=draft_id,
            revision_number=f"1.{revision_minor}",
            revision_summary=summary,
        )

    def get_download(self, draft_id: UUID) -> ProposalDraftDownload:
        """현재 대화 세션 중 생성한 파일만 UUID로 반환한다."""

        record = self._registry.find(draft_id)
        if record is None:
            raise ProposalDraftError("proposal_draft_not_found", "다운로드할 기안 초안을 찾을 수 없습니다.", 404)
        if record.download is None:
            raise ProposalDraftError(
                "proposal_clarification_required",
                "확인 질문에 답한 뒤 완성된 기안을 다운로드할 수 있습니다.",
                409,
            )
        return record.download
