"""로컬 LLM 제목을 기안 XLSX에 넣고 새 파일로 보관하는 서비스."""

from __future__ import annotations

import io
import ipaddress
import json
import logging
import re
import threading
import time
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
_BODY_DEFAULT_STYLE = "7"
_LEGACY_BODY_MAX_CHARS = 6000
_LEGACY_LINE_MAX_CHARS = 1000
_PROJECTION_OMISSION_NOTICE = "※ 구조화 초안의 일부 세부 행은 엑셀 표시 범위로 인해 생략되었습니다."
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_INVALID_TITLE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_NUMBER_OR_DATE_PATTERN = re.compile(r"\d")


class ProposalDraftError(RuntimeError):
    """내부 경로와 원본 예외를 숨기고 공개할 수 있는 기안 초안 오류."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        context_usage: ProposalContextUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.context_usage = context_usage


class ProposalDraftGenerateRequest(BaseModel):
    """선택 문서와 사용자의 기안 설명을 결합하기 위한 요청."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=2000)
    selected_files: list[SelectedFileContext] = Field(min_length=1, max_length=5)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        """공백뿐인 LLM 지시는 API 경계에서 거부한다."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("작성할 기안에 대한 설명을 입력해 주세요.")
        return normalized


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
    title: str = Field(min_length=1, max_length=80)
    approval_request: str = Field(min_length=1, max_length=1000)
    sections: list[ProposalSectionV2] = Field(min_length=1, max_length=20)
    missing_information: list[str] = Field(max_length=50)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return _clean_title(value)


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
    context_usage: ProposalContextUsage
    file_name: str
    download_url: str
    destination_label: str
    saved_to_smb: bool = True
    model_used: str
    elapsed_ms: float = Field(ge=0)
    timings_ms: dict[str, float] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProposalDraftResult:
    """LLM 호출에서 확정한 세 필드와 공개 가능한 메타데이터."""

    fields: ProposalDraftFields
    model: str
    document: ProposalDocumentV2 | None = None
    context_usage: ProposalContextUsage = dataclass_field(default_factory=ProposalContextUsage)


@dataclass(frozen=True)
class ProposalDraftDownload:
    """브라우저 다운로드에 필요한 파일명과 XLSX 바이트."""

    file_name: str
    content: bytes


class ProposalDraftGenerator(Protocol):
    """테스트에서 LLM을 대체할 수 있는 구조화 기안 생성 계약."""

    def generate(self, instruction: str, evidence: list[DocumentCitation]) -> ProposalDraftResult:
        """사용자 설명과 선택 문서 근거를 받아 세 필드를 반환한다."""

    def close(self) -> None:
        """재사용 중인 연결을 닫는다."""


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


class LocalProposalDraftGenerator:
    """온프레미스 Ollama에서 strict V2 기안 문서를 생성한다."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(timeout=max(1.0, settings.proposal_llm_timeout_ms / 1000))

    def close(self) -> None:
        """재사용하던 HTTP 연결을 닫는다."""

        self._client.close()

    def generate(self, instruction: str, evidence: list[DocumentCitation]) -> ProposalDraftResult:
        """외부 주소는 거부하고 선택 문서 근거로 strict V2 기안을 반환한다."""

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
            "모든 필드를 반드시 포함하세요."
        )
        context = pack_proposal_context(
            instruction,
            evidence,
            max_chars=self._settings.proposal_context_max_chars,
            per_document_chars=self._settings.proposal_context_per_document_chars,
            max_citations=self._settings.proposal_context_max_citations,
        )
        if not context.text:
            raise ProposalDraftError(
                "proposal_evidence_unavailable",
                "선택한 문서에서 기안 작성에 사용할 근거를 찾지 못했습니다.",
                422,
            )

        for attempt in range(2):
            payload = self._build_payload(model, system_prompt, instruction, context)
            try:
                response = self._client.post(f"{root_url}/api/chat", json=payload)
                response.raise_for_status()
                decoded, prompt_eval_count = _decode_llm_payload(response.json())
                if decoded.get("schema_version") == "proposal-document-v2":
                    document = ProposalDocumentV2.model_validate(decoded)
                    _validate_document_citations(document, context.citation_ids)
                    fields = project_document_to_legacy_fields(document)
                else:
                    # 배포 전환 중인 기존 로컬 모델 응답도 API 호환을 위해 단일 섹션으로 승격한다.
                    fields = ProposalDraftFields.model_validate(decoded)
                    document = _legacy_document(fields, context.citation_ids[0])
                usage = context.usage.model_copy(
                    update={
                        "estimated_input_tokens": estimate_proposal_tokens(
                            len(system_prompt) + len(instruction) + len(context.text)
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
                    context_usage=usage,
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
    ) -> dict:
        """Ollama JSON 요청을 컨텍스트 예산과 출력 예산으로 제한한다."""

        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"기안 설명:\n{instruction.strip()}\n\n선택 문서 근거:\n{context.text}",
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
    """현재 프로세스에서 생성한 다운로드만 제한된 개수로 보관한다."""

    def __init__(self, *, max_records: int = 25) -> None:
        self._max_records = max_records
        self._records: OrderedDict[UUID, ProposalDraftDownload] = OrderedDict()
        self._lock = threading.RLock()

    def register(self, download: ProposalDraftDownload) -> UUID:
        """추측하기 어려운 ID를 발급하고 오래된 메모리 항목을 제거한다."""

        draft_id = uuid.uuid4()
        with self._lock:
            self._records[draft_id] = download
            while len(self._records) > self._max_records:
                self._records.popitem(last=False)
        return draft_id

    def find(self, draft_id: UUID) -> ProposalDraftDownload | None:
        """현재 프로세스에 남아 있는 생성 파일을 반환한다."""

        with self._lock:
            return self._records.get(draft_id)


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

    def generate(
        self,
        instruction: str,
        evidence: list[DocumentCitation],
        *,
        validation_ms: float = 0.0,
        retrieval_ms: float = 0.0,
    ) -> ProposalDraftGenerateResponse:
        """선택 문서 근거로 세 필드를 생성해 신규 XLSX로 저장하고 다운로드를 등록한다."""

        if self._draft_generator is None or self._upload_manager is None:
            raise ProposalDraftError("proposal_generation_not_configured", "기안 초안 생성 기능이 구성되지 않았습니다.", 503)
        started = time.perf_counter()
        llm_started = time.perf_counter()
        draft_result = self._draft_generator.generate(instruction, evidence)
        llm_ms = round((time.perf_counter() - llm_started) * 1000, 1)
        document = draft_result.document or _legacy_document(draft_result.fields)

        workbook_started = time.perf_counter()
        try:
            content = insert_proposal_fields(self._read_template(), draft_result.fields)
        except ProposalDraftError:
            raise
        except ValueError as exc:
            raise ProposalDraftError("proposal_workbook_generation_failed", "기안 초안 엑셀을 생성하지 못했습니다.", 503) from exc
        workbook_ms = round((time.perf_counter() - workbook_started) * 1000, 1)

        file_name = build_versioned_filename(
            f"{_title_document_name(draft_result.fields.title)}.xlsx",
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
        draft_id = self._registry.register(download)
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
            title=draft_result.fields.title,
            fields=draft_result.fields,
            document=document,
            context_usage=draft_result.context_usage,
            file_name=saved.file_name,
            download_url=f"/api/playground/drafts/proposal/{draft_id}",
            destination_label=saved.destination_label,
            model_used=draft_result.model,
            elapsed_ms=elapsed_ms,
            timings_ms={
                "validation": validation_ms,
                "retrieval": retrieval_ms,
                "llm": llm_ms,
                "workbook": workbook_ms,
                "smb": smb_ms,
            },
        )

    def get_download(self, draft_id: UUID) -> ProposalDraftDownload:
        """현재 대화 세션 중 생성한 파일만 UUID로 반환한다."""

        download = self._registry.find(draft_id)
        if download is None:
            raise ProposalDraftError("proposal_draft_not_found", "다운로드할 기안 초안을 찾을 수 없습니다.", 404)
        return download
