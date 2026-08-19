"""기안 V2 계약, 컨텍스트 예산, 제한 오류 재시도를 합성 데이터로 검증한다."""

from __future__ import annotations

import io
import re
import zipfile
from hashlib import sha256
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree

import pytest
from pydantic import ValidationError

from smb_finder.model_gateway import ModelGatewayError, ModelGatewayResult, ModelTokenUsage
from smb_finder.config import Settings
from smb_finder.models import DocumentCitation, RetrievalScores
from smb_finder.playground.proposal_context import pack_proposal_context
from smb_finder.playground.proposal_draft import (
    LocalProposalDraftGenerator,
    ProposalDocumentV2,
    ProposalDraftError,
    ProposalEvidenceVerification,
    ProposalClaimVerdict,
    ProposalParagraphBlock,
    _enforce_required_fact_safety_net,
    _explicit_expected_effect_safety_net,
    _merge_quality_refinement,
    _purchase_background_safety_net,
    apply_proposal_evidence_verification,
    normalize_proposal_document,
    project_document_to_legacy_fields,
    render_proposal_workbook,
    resolve_proposal_type,
)
from smb_finder.playground.proposal_evidence import filter_proposal_evidence


def _citation(
    index: int,
    excerpt: str,
    *,
    document: int = 1,
    score: float = 0.1,
) -> DocumentCitation:
    return DocumentCitation(
        index=index,
        doc_id=UUID(f"00000000-0000-0000-0000-{document:012d}"),
        revision_id=UUID(f"10000000-0000-0000-0000-{document:012d}"),
        chunk_id=UUID(f"20000000-0000-0000-0000-{index:012d}"),
        title=f"합성 참고 문서 {document}",
        section_path=["합성 계획"],
        excerpt=excerpt,
        scores=RetrievalScores(rrf=score),
    )


def _settings(**overrides) -> Settings:  # noqa: ANN003
    values = {
        "_env_file": None,
        "ollama_base_url": "http://127.0.0.1:11434",
        "llmops_chat_model": "synthetic-model",
        "proposal_context_max_chars": 2000,
        "proposal_context_per_document_chars": 1000,
        "proposal_context_max_citations": 10,
    }
    values.update(overrides)
    return Settings(**values)


def _assert_ignorable_namespace_prefixes_are_declared(content: bytes) -> None:
    text = content.decode("utf-8")
    root = re.search(r"<(?![?!])[^>]+>", text)
    assert root is not None
    ignorable = re.search(r"\b(?:[A-Za-z_][\w.-]*:)?Ignorable=['\"]([^'\"]+)['\"]", root.group())
    assert ignorable is not None
    for prefix in ignorable.group(1).split():
        assert re.search(rf"\sxmlns:{re.escape(prefix)}=['\"][^'\"]+['\"]", root.group()) is not None


def _document_payload() -> dict:
    return {
        "schema_version": "proposal-document-v2",
        "title": "합성 장비 구매 계획",
        "approval_request": "관련 내용을 검토 후 재가하여 주시기 바랍니다.",
        "sections": [
            {
                "heading": "구매 목적",
                "semantic_role": "purpose",
                "citations": ["E001"],
                "blocks": [{"type": "paragraph", "text": "합성 자동화 범위를 개선합니다."}],
                "missing_information": [],
            },
            {
                "heading": "예산",
                "semantic_role": "budget",
                "citations": ["E001"],
                "blocks": [
                    {
                        "type": "table",
                        "headers": ["구분", "금액"],
                        "rows": [["합성 품목", "10,000원"]],
                    }
                ],
                "missing_information": [],
            },
        ],
        "missing_information": ["최종 집행일 확인"],
    }


def test_proposal_document_v2_is_strict_and_rejects_invalid_table_shape() -> None:
    payload = _document_payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ProposalDocumentV2.model_validate(payload)


def test_verification_claim_pattern_uses_ollama_compatible_ascii_digits() -> None:
    schema = ProposalEvidenceVerification.model_json_schema()
    pattern = schema["$defs"]["ProposalClaimVerdict"]["properties"]["claim_id"]["pattern"]

    assert pattern == r"^C[0-9]{4}$"
    with pytest.raises(ValidationError):
        ProposalClaimVerdict(claim_id="C12A4", status="supported", action="keep", citations=["E001"])


def test_required_money_safety_net_asks_once_when_event_fee_is_missing() -> None:
    document = ProposalDocumentV2.model_validate(_document_payload())
    claims = [
        ProposalClaimVerdict(claim_id="C0001", status="supported", action="keep", citations=["E001"]),
        ProposalClaimVerdict(claim_id="C0002", status="supported", action="keep", citations=["E001"]),
    ]
    verification = ProposalEvidenceVerification(
        schema_version="proposal-evidence-verification-v1",
        claims=claims,
        questions=[],
    )
    from smb_finder.playground.proposal_draft import _proposal_claims

    result = _enforce_required_fact_safety_net(
        verification,
        _proposal_claims(document),
        instruction="합성 행사 참가 비용 승인을 포함한 기안을 작성해 주세요.",
        evidence=[_citation(1, "합성 행사의 참가 목적과 주요 내용")],
        user_answers=[],
        proposal_type="event_attendance",
        allow_questions=True,
    )

    assert [question.field_key for question in result.questions] == ["participation_fee"]


def test_required_money_safety_net_accepts_user_supplied_event_fee() -> None:
    document = ProposalDocumentV2.model_validate(_document_payload())
    from smb_finder.playground.proposal_draft import _proposal_claims

    verification = ProposalEvidenceVerification(
        schema_version="proposal-evidence-verification-v1",
        claims=[
            ProposalClaimVerdict(claim_id=claim.claim_id, status="supported", action="keep", citations=["U002"])
            for claim in _proposal_claims(document)
        ],
        questions=[],
    )
    result = _enforce_required_fact_safety_net(
        verification,
        _proposal_claims(document),
        instruction="합성 행사 참가 비용 승인을 포함한 기안을 작성해 주세요.",
        evidence=[_citation(1, "합성 행사의 참가 목적과 주요 내용")],
        user_answers=["participation_fee: 참가비는 총 200,000원입니다."],
        proposal_type="event_attendance",
        allow_questions=True,
    )

    assert result.questions == []

    payload = _document_payload()
    payload["sections"][1]["blocks"][0]["rows"] = [["열 수 불일치"]]
    with pytest.raises(ValidationError):
        ProposalDocumentV2.model_validate(payload)


def test_evidence_verification_replaces_section_citations_with_verified_claim_sources() -> None:
    document = ProposalDocumentV2.model_validate(_document_payload())
    document = document.model_copy(
        update={
            "sections": [
                document.sections[0].model_copy(update={"citations": ["E002"]}),
                document.sections[1].model_copy(update={"citations": ["E001"]}),
            ]
        }
    )
    verification = ProposalEvidenceVerification(
        schema_version="proposal-evidence-verification-v1",
        claims=[
            ProposalClaimVerdict(claim_id="C0001", status="supported", action="keep", citations=["E001"]),
            ProposalClaimVerdict(claim_id="C0002", status="supported", action="keep", citations=["E002"]),
        ],
        questions=[],
    )

    verified, completion = apply_proposal_evidence_verification(
        document,
        verification,
        allow_questions=True,
        clarification_round=0,
    )

    assert completion.status == "completed"
    assert [section.citations for section in verified.sections] == [["E001"], ["E002"]]


def test_proposal_type_resolution_is_explicit_conservative_and_deterministic() -> None:
    purchase = resolve_proposal_type("auto", "합성 장비 구매 기안을 작성해 줘", [_citation(1, "합성 근거")])
    event = resolve_proposal_type(
        "auto",
        "합성 박람회 참석 기안을 작성해 줘",
        [_citation(1, "합성 행사 안내")],
    )
    conflict = resolve_proposal_type(
        "auto",
        "합성 박람회 참석용 장비 구매 기안을 작성해 줘",
        [_citation(1, "합성 근거")],
    )
    explicit = resolve_proposal_type("event_attendance", "합성 장비 구매", [_citation(1, "합성 근거")])
    receipt_does_not_override_instruction = resolve_proposal_type(
        "auto",
        "합성 박람회 참석 기안을 작성해 줘",
        [_citation(1, "합성 근거").model_copy(update={"title": "합성 장비 구매 결제 영수증"})],
    )

    assert (purchase.proposal_type, purchase.source) == ("purchase", "rule")
    assert (event.proposal_type, event.source) == ("event_attendance", "rule")
    assert (conflict.proposal_type, conflict.source) == ("general", "fallback")
    assert (explicit.proposal_type, explicit.source) == ("event_attendance", "user")
    assert (receipt_does_not_override_instruction.proposal_type, receipt_does_not_override_instruction.source) == (
        "event_attendance",
        "rule",
    )


def test_event_evidence_filter_excludes_post_event_proofs_by_document_and_reports_only_aggregates() -> None:
    evidence = [
        _citation(1, "행사 발표 주제와 전시 분야를 안내합니다.", document=1).model_copy(
            update={"title": "합성 행사 프로그램"}
        ),
        _citation(2, "결제 영수증 승인 금액입니다.", document=2).model_copy(update={"title": "합성 결제 영수증"}),
        _citation(3, "같은 영수증의 카드 전표입니다.", document=2).model_copy(update={"title": "합성 결제 영수증"}),
        _citation(4, "참석 확인증 발급 내역입니다.", document=3).model_copy(update={"title": "합성 참석 확인증"}),
    ]

    filtered = filter_proposal_evidence(
        evidence,
        proposal_type="event_attendance",
        instruction="합성 박람회 참가 기안을 작성해 줘",
    )

    assert filtered.citations == [evidence[0]]
    assert filtered.summary.model_dump() == {
        "schema_version": "proposal-evidence-filter-v1",
        "input_document_count": 3,
        "included_document_count": 1,
        "excluded_document_count": 2,
        "input_citation_count": 4,
        "included_citation_count": 1,
        "excluded_citation_count": 3,
        "excluded_reason_counts": {"attendance_confirmation": 1, "receipt": 1},
        "fallback_used": False,
    }


def test_event_evidence_filter_allows_a_proof_only_when_feedback_explicitly_requests_it() -> None:
    receipt = _citation(1, "결제 영수증 승인 금액입니다.").model_copy(update={"title": "합성 결제 영수증"})

    filtered = filter_proposal_evidence(
        [receipt],
        proposal_type="event_attendance",
        instruction="결제 영수증의 금액을 참가 내용에 반영해 줘",
    )

    assert filtered.citations == [receipt]
    assert filtered.summary.excluded_document_count == 0


def test_event_evidence_filter_keeps_registration_confirmation_and_ignores_a_single_excerpt_word() -> None:
    registration = _citation(1, "참가 신청 접수가 완료되었습니다.", document=1).model_copy(
        update={"title": "합성 참가신청 확인"}
    )
    guide = _citation(2, "현장에서 영수증을 발급할 수 있습니다.", document=2).model_copy(
        update={"title": "합성 행사 안내"}
    )

    filtered = filter_proposal_evidence(
        [registration, guide],
        proposal_type="event_attendance",
        instruction="합성 박람회 참가 기안을 작성해 줘",
    )

    assert filtered.citations == [registration, guide]
    assert filtered.summary.excluded_document_count == 0


def test_event_document_is_normalized_to_compact_order_and_drops_full_schedule_section() -> None:
    payload = _document_payload()
    payload["sections"] = [
        {
            "heading": "행사 개요",
            "semantic_role": "details",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "일시·장소·참석자 합성 정보"}],
            "missing_information": [],
        },
        {
            "heading": "전체 일정",
            "semantic_role": "schedule",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "시간대별 전체 프로그램"}],
            "missing_information": [],
        },
        {
            "heading": "참가 목적",
            "semantic_role": "purpose",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "기술 동향 확인"}],
            "missing_information": [],
        },
        {
            "heading": "주요 발표 주제",
            "semantic_role": "details",
            "citations": ["E001"],
            "blocks": [{"type": "list", "ordered": False, "items": ["합성 주제"]}],
            "missing_information": [],
        },
    ]
    document = ProposalDocumentV2.model_validate(payload)

    normalized = normalize_proposal_document(document, "event_attendance")

    assert [section.heading for section in normalized.sections] == ["참가 목적", "참가 내용", "행사 주요 내용"]
    rendered = project_document_to_legacy_fields(normalized).body
    assert "행사 개요" not in rendered
    assert "전체 일정" not in rendered
    assert "시간대별 전체 프로그램" not in rendered
    assert "일시·장소·참석자 합성 정보" in rendered


def test_v2_workbook_keeps_text_rows_editable_and_renders_five_column_table_without_pipe_text() -> None:
    payload = _document_payload()
    payload["proposal_type"] = "purchase"
    payload["sections"] = [
        {
            "heading": "세부내용",
            "semantic_role": "details",
            "citations": ["E001"],
            "blocks": [
                {"type": "paragraph", "text": "합성 장문입니다. " * 40},
                {
                    "type": "table",
                    "headers": ["순번", "품명", "수량", "단가", "금액"],
                    "rows": [["1", "=합성 장비", "2", "10,000", "20,000"]],
                },
            ],
            "missing_information": [],
        }
    ]
    payload["missing_information"] = []
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )

    generated = render_proposal_workbook(template_path.read_bytes(), ProposalDocumentV2.model_validate(payload))

    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(generated)) as workbook:
        assert workbook.testzip() is None
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml")
        styles_xml = workbook.read("xl/styles.xml")
        sheet = ElementTree.fromstring(sheet_xml)
        styles = ElementTree.fromstring(styles_xml)
    _assert_ignorable_namespace_prefixes_are_declared(sheet_xml)
    _assert_ignorable_namespace_prefixes_are_declared(styles_xml)
    merges = {item.attrib["ref"] for item in sheet.findall("main:mergeCells/main:mergeCell", namespace)}
    assert not any(
        reference.startswith(("A15:", "A16:", "A17:", "A18:", "A19:", "A20:", "A21:", "A22:", "A23:"))
        for reference in merges
    )
    body_cell = sheet.find(".//main:c[@r='A16']", namespace)
    assert body_cell is not None
    body_style = styles.findall("main:cellXfs/main:xf", namespace)[int(body_cell.attrib["s"])]
    assert body_style.find("main:alignment", namespace).attrib["wrapText"] == "0"  # type: ignore[union-attr]
    body_row = sheet.find(".//main:row[@r='16']", namespace)
    assert body_row is not None
    assert "ht" not in body_row.attrib
    assert "customHeight" not in body_row.attrib
    assert [cell.attrib["r"] for cell in body_row.findall("main:c", namespace)] == ["A16"]
    table_header_cell = next(
        cell
        for cell in sheet.findall(".//main:c", namespace)
        if cell.findtext("main:is/main:t", namespaces=namespace) == "순번"
    )
    table_row = int(re.search(r"\d+$", table_header_cell.attrib["r"]).group())  # type: ignore[union-attr]
    assert {
        f"A{table_row}:F{table_row}",
        f"G{table_row}:K{table_row}",
        f"L{table_row}:P{table_row}",
        f"Q{table_row}:U{table_row}",
        f"V{table_row}:Z{table_row}",
    }.issubset(merges)
    header_style = styles.findall("main:cellXfs/main:xf", namespace)[int(table_header_cell.attrib["s"])]
    assert header_style.attrib["borderId"] != "0"
    assert header_style.find("main:alignment", namespace).attrib["wrapText"] == "1"  # type: ignore[union-attr]
    assert "customHeight" in sheet.find(f".//main:row[@r='{table_row}']", namespace).attrib  # type: ignore[union-attr]
    texts = [item.text or "" for item in sheet.findall(".//main:c/main:is/main:t", namespace)]
    assert not any(" | " in value for value in texts)
    formula_safe = next(
        (
            cell
            for cell in sheet.findall(".//main:c", namespace)
            if cell.findtext("main:is/main:t", namespaces=namespace) == "=합성 장비"
        ),
        None,
    )
    assert formula_safe is not None
    assert formula_safe.find("main:f", namespace) is None


def test_generated_main_sheet_uses_normal_view_and_preserves_other_view_settings() -> None:
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )
    template = template_path.read_bytes()
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(template)) as workbook:
        original_sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    original_view = original_sheet.find("main:sheetViews/main:sheetView", namespace)
    assert original_view is not None
    assert original_view.attrib.get("view") == "pageBreakPreview"

    generated = render_proposal_workbook(template, ProposalDocumentV2.model_validate(_document_payload()))

    with zipfile.ZipFile(io.BytesIO(generated)) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml")
    generated_sheet = ElementTree.fromstring(sheet_xml)
    generated_view = generated_sheet.find("main:sheetViews/main:sheetView", namespace)
    assert generated_view is not None
    expected_attributes = {**original_view.attrib, "view": "normal"}
    assert generated_view.attrib == expected_attributes
    assert b'view="pageBreakPreview"' not in sheet_xml
    assert b'view="normal"' in sheet_xml


def test_wide_table_is_preserved_as_matrix_in_appendix_sheet() -> None:
    payload = _document_payload()
    payload["sections"] = [
        {
            "heading": "넓은 합성 표",
            "semantic_role": "details",
            "citations": ["E001"],
            "blocks": [
                {
                    "type": "table",
                    "headers": [f"열 {index}" for index in range(1, 8)],
                    "rows": [[f"값 {index}" for index in range(1, 8)]],
                }
            ],
            "missing_information": [],
        }
    ]
    payload["missing_information"] = []
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "smb_finder" / "playground" / "templates" / "proposal_draft.xlsx"
    )

    generated = render_proposal_workbook(template_path.read_bytes(), ProposalDocumentV2.model_validate(payload))

    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(generated)) as workbook:
        workbook_content = workbook.read("xl/workbook.xml")
        workbook_xml = ElementTree.fromstring(workbook_content)
        appendix = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
        main = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    _assert_ignorable_namespace_prefixes_are_declared(workbook_content)
    assert any(item.attrib["name"] == "세부내용" for item in workbook_xml.findall("main:sheets/main:sheet", namespace))
    assert appendix.findtext(".//main:c[@r='A2']/main:is/main:t", namespaces=namespace) == "열 1"
    assert appendix.findtext(".//main:c[@r='G3']/main:is/main:t", namespaces=namespace) == "값 7"
    main_texts = [item.text or "" for item in main.findall(".//main:c/main:is/main:t", namespace)]
    assert any("세부내용" in value and "참조" in value for value in main_texts)
    assert not any(" | " in value for value in main_texts)


def test_legacy_projection_is_deterministic_and_preserves_table_header() -> None:
    document = ProposalDocumentV2.model_validate(_document_payload())

    first = project_document_to_legacy_fields(document)
    second = project_document_to_legacy_fields(document)

    assert first == second
    assert "구분 | 금액" in first.body
    assert "합성 품목 | 10,000원" in first.body
    assert "[확인 필요] 최종 집행일 확인" in first.body
    assert len(first.body.splitlines()) <= 33


def test_legacy_projection_normalizes_internal_block_line_breaks_to_single_rows() -> None:
    payload = _document_payload()
    payload["sections"][0]["blocks"] = [
        {"type": "paragraph", "text": "첫 문장\n둘째 문장\r셋째 문장"},
        {"type": "list", "ordered": False, "items": ["합성 항목\r\n세부 설명"]},
        {
            "type": "table",
            "headers": ["구분\n유형", "금액\r기준"],
            "rows": [["합성\n품목", "10,000원\r\n2026-08-31"]],
        },
    ]
    payload["sections"][0]["missing_information"] = ["최종\n담당 확인"]

    fields = project_document_to_legacy_fields(ProposalDocumentV2.model_validate(payload))

    assert "첫 문장 둘째 문장 셋째 문장" in fields.body
    assert "- 합성 항목 세부 설명" in fields.body
    assert "구분 유형 | 금액 기준" in fields.body
    assert "합성 품목 | 10,000원 2026-08-31" in fields.body
    assert "[확인 필요] 최종 담당 확인" in fields.body
    assert len(fields.body.splitlines()) <= 33
    assert len(fields.body) <= 6000


def test_large_multiline_table_projection_always_fits_workbook_and_keeps_omission_notice() -> None:
    payload = _document_payload()
    payload["sections"] = [
        {
            "heading": "대형 합성 표",
            "semantic_role": "details",
            "citations": ["E001"],
            "blocks": [
                {
                    "type": "table",
                    "headers": ["구분\n항목", "금액\r\n일정"],
                    "rows": [
                        [
                            f"합성 품목 {index}\n{'가' * 900}",
                            f"{index:,}원\r\n2026-08-{index % 28 + 1:02d}",
                        ]
                        for index in range(1, 101)
                    ],
                }
            ],
            "missing_information": [],
        }
    ]
    payload["missing_information"] = []
    document = ProposalDocumentV2.model_validate(payload)

    fields = project_document_to_legacy_fields(document)

    assert len(fields.body.splitlines()) <= 33
    assert len(fields.body) <= 6000
    assert fields.body.splitlines()[-1] == "※ 구조화 초안의 일부 세부 행은 엑셀 표시 범위로 인해 생략되었습니다."
    assert "구분 항목 | 금액 일정" in fields.body

def test_context_packer_is_deterministic_bounded_and_preserves_important_lines() -> None:
    filler = "일반 설명 " * 60
    important = "구분 | 금액 | 일정\n합성 품목 | 10,000원 | 2026-08-31"
    citations = [
        _citation(1, f"{filler}\n{important}", score=0.9),
        _citation(2, f"{filler}\n{important}", document=2, score=0.8),
        _citation(3, "다른 합성 근거", document=2, score=0.7),
    ]

    first = pack_proposal_context(
        "합성 품목 금액과 일정을 포함해 줘",
        citations,
        max_chars=240,
        per_document_chars=150,
        max_citations=3,
    )
    second = pack_proposal_context(
        "합성 품목 금액과 일정을 포함해 줘",
        list(reversed(citations)),
        max_chars=240,
        per_document_chars=150,
        max_citations=3,
    )

    assert first.text == second.text
    assert len(first.text) <= 240
    assert "구분 | 금액 | 일정" in first.text
    assert "10,000원" in first.text
    assert first.usage.deduplicated_citation_count == 1
    assert first.usage.truncated is True
    assert first.usage.model_dump().keys().isdisjoint({"excerpt", "path", "title"})
    assert first.citation_ids == second.citation_ids
    assert first.source_chunk_ids == second.source_chunk_ids
    assert len(first.citation_ids) == len(first.source_chunk_ids)
    assert first.context_sha256 == sha256(first.text.encode("utf-8")).hexdigest()


class _SequenceGateway:
    def __init__(self, sequence: list[dict | ModelGatewayError] | None = None) -> None:
        self.sequence = sequence or [_document_payload()]
        self.calls: list[dict] = []

    def invoke_json(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        value = self.sequence[min(len(self.calls) - 1, len(self.sequence) - 1)]
        if isinstance(value, ModelGatewayError):
            raise value
        return ModelGatewayResult(
            payload=kwargs["response_model"].model_validate(value),
            provider="ollama",
            model=kwargs["model"],
            usage=ModelTokenUsage(prompt_tokens=321, completion_tokens=10),
            elapsed_ms=5.0,
        )

    def close(self) -> None:
        return None


def test_gateway_generation_records_packed_context_usage_and_deadline() -> None:
    gateway = _SequenceGateway()
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway, clock=lambda: 100.0)
    evidence = [_citation(1, "합성 근거 " * 120, score=0.9)]

    result = generator.generate("합성 구매 기안을 작성해 줘", evidence, deadline=100.25)

    assert len(gateway.calls) == 1
    assert gateway.calls[0]["purpose"] == "proposal_generation"
    assert gateway.calls[0]["deadline"] == 100.25
    assert gateway.calls[0]["context_window_tokens"] == _settings().proposal_llm_num_ctx
    assert result.context_usage.retry_count == 0
    assert result.context_usage.prompt_eval_count == 321
    assert result.document is not None
    assert result.fields.title == result.document.title
    assert result.packed_citation_map == (("E001", str(evidence[0].chunk_id)),)
    context_only = gateway.calls[0]["messages"][1]["content"].split("\n\n선택 문서 근거:\n", 1)[1]
    assert result.packed_context_sha256 == sha256(context_only.encode("utf-8")).hexdigest()


def test_expired_deadline_does_not_invoke_proposal_gateway() -> None:
    gateway = _SequenceGateway()
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway, clock=lambda: 100.0)

    with pytest.raises(ProposalDraftError) as raised:
        generator.generate("합성 기안을 작성해 줘", [_citation(1, "합성 근거 " * 120)], deadline=100.0)

    assert raised.value.code == "proposal_llm_unavailable"
    assert gateway.calls == []


def test_invalid_structured_response_retries_once_with_repair_instruction() -> None:
    gateway = _SequenceGateway([ModelGatewayError("output_schema_invalid", retryable=False), _document_payload()])
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway)

    result = generator.generate("합성 구매 기안을 작성해 줘", [_citation(1, "합성 구매 근거")])

    assert result.document is not None
    assert len(gateway.calls) == 2
    assert "strict JSON 계약을 충족하지 못했습니다" in gateway.calls[1]["messages"][0]["content"]


def test_context_limit_retries_once_with_half_context_separately_from_structure_repair() -> None:
    gateway = _SequenceGateway(
        [
            ModelGatewayError("output_schema_invalid", retryable=False),
            ModelGatewayError("model_context_limit", retryable=True),
            _document_payload(),
        ]
    )
    settings = _settings(proposal_context_max_chars=2000, proposal_context_per_document_chars=2000)
    generator = LocalProposalDraftGenerator(settings, gateway=gateway)

    result = generator.generate("합성 구매 기안을 작성해 줘", [_citation(1, "합성 구매 근거 " * 500)])

    assert len(gateway.calls) == 3
    assert result.context_usage.retry_count == 1
    assert result.context_usage.first_attempt_context_chars is not None
    assert result.context_usage.first_attempt_context_chars > result.context_usage.context_chars
    assert "strict JSON 계약을 충족하지 못했습니다" in gateway.calls[2]["messages"][0]["content"]


def test_second_context_limit_returns_safe_error_with_half_context_usage() -> None:
    gateway = _SequenceGateway(
        [
            ModelGatewayError("model_context_limit", retryable=True),
            ModelGatewayError("model_context_limit", retryable=True),
        ]
    )
    generator = LocalProposalDraftGenerator(
        _settings(proposal_context_max_chars=2000, proposal_context_per_document_chars=2000),
        gateway=gateway,
    )

    with pytest.raises(ProposalDraftError) as raised:
        generator.generate("합성 구매 기안을 작성해 줘", [_citation(1, "합성 구매 근거 " * 500)])

    assert raised.value.code == "proposal_context_limit_exceeded"
    assert raised.value.context_usage is not None
    assert raised.value.context_usage.retry_count == 1
    assert len(gateway.calls) == 2


def test_evidence_assessment_uses_shared_gateway_and_same_deadline() -> None:
    gateway = _SequenceGateway(
        [
            {
                "schema_version": "proposal-evidence-verification-v1",
                "claims": [
                    {"claim_id": "C0001", "status": "supported", "action": "keep", "citations": ["E001"]},
                    {"claim_id": "C0002", "status": "supported", "action": "keep", "citations": ["E001"]},
                ],
                "questions": [],
            }
        ]
    )
    generator = LocalProposalDraftGenerator(
        _settings(),
        gateway_getter=lambda: gateway,
        clock=lambda: 100.0,
    )

    verification = generator.assess(
        "합성 구매 승인 요청",
        ProposalDocumentV2.model_validate(_document_payload()),
        [_citation(1, "합성 장비 구매 근거")],
        proposal_type="purchase",
        deadline=100.25,
    )

    assert len(verification.claims) == 2
    assert gateway.calls[0]["purpose"] == "proposal_validation"
    assert gateway.calls[0]["deadline"] == 100.25
    assert gateway.calls[0]["context_window_tokens"] == _settings().proposal_llm_num_ctx


def test_assessment_maps_context_limit_without_retry_and_preserves_usage() -> None:
    gateway = _SequenceGateway([ModelGatewayError("model_context_limit", retryable=True)])
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway)

    with pytest.raises(ProposalDraftError) as raised:
        generator.assess(
            "합성 구매 승인 요청",
            ProposalDocumentV2.model_validate(_document_payload()),
            [_citation(1, "합성 장비 구매 근거")],
            proposal_type="purchase",
        )

    assert raised.value.code == "proposal_context_limit_exceeded"
    assert raised.value.context_usage is not None
    assert len(gateway.calls) == 1


def test_expired_assessment_deadline_does_not_invoke_gateway() -> None:
    gateway = _SequenceGateway()
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway, clock=lambda: 100.0)

    with pytest.raises(ProposalDraftError) as raised:
        generator.assess(
            "합성 구매 승인 요청",
            ProposalDocumentV2.model_validate(_document_payload()),
            [_citation(1, "합성 장비 구매 근거")],
            proposal_type="purchase",
            deadline=100.0,
        )

    assert raised.value.code == "proposal_verification_unavailable"
    assert gateway.calls == []


def test_quality_refinement_uses_type_checklist_and_normalizes_approval_request() -> None:
    gateway = _SequenceGateway()
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway)
    document = ProposalDocumentV2.model_validate(_document_payload())

    result = generator.refine(
        "합성 구매 승인 요청",
        document,
        [_citation(1, "합성 분석 키트 A 3세트 총 360,000원")],
        proposal_type="purchase",
    )

    assert result.document is not None
    assert result.document.approval_request == "다음과 같이 구매를 진행하고자 하오니 검토 후 승인하여 주시기 바랍니다."
    assert "품목, 수량, 단가, 총액" in gateway.calls[0]["messages"][1]["content"]


def test_event_quality_refinement_adds_missing_grounded_decision_sentence() -> None:
    gateway = _SequenceGateway()
    generator = LocalProposalDraftGenerator(_settings(), gateway=gateway)
    document = ProposalDocumentV2.model_validate(_document_payload())
    evidence = [
        _citation(
            1,
            "합성 행사는 2026년 10월 15일 합성 컨벤션 홀에서 열린다. 참석자는 2명이며 참가비는 총 160,000원이다.",
        )
    ]

    result = generator.refine(
        "합성 행사 참석 승인 요청",
        document,
        evidence,
        proposal_type="event_attendance",
    )

    assert result.document is not None
    body = project_document_to_legacy_fields(result.document).body
    assert "2026년 10월 15일" in body
    assert "합성 컨벤션 홀" in body
    assert "참석자는 2명" in body
    assert "참가비는 총 160,000원" in body


def test_quality_refinement_preserves_existing_claims_and_rejects_new_background() -> None:
    original = ProposalDocumentV2.model_validate(_document_payload())
    refined_payload = _document_payload()
    refined_payload["sections"][0]["blocks"] = [
        {"type": "paragraph", "text": "근거에 없던 효율 저하를 새로 주장합니다."}
    ]
    refined_payload["sections"].insert(
        1,
        {
            "heading": "배경·필요성",
            "semantic_role": "background",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "근거에 없는 새 배경입니다."}],
            "missing_information": [],
        },
    )
    refined_payload["sections"].append(
        {
            "heading": "구매 요청",
            "semantic_role": "request",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "합성 품목 구매를 승인해 주시기 바랍니다."}],
            "missing_information": [],
        }
    )
    refined = ProposalDocumentV2.model_validate(refined_payload)

    merged = _merge_quality_refinement(original, refined)

    purpose = next(section for section in merged.sections if section.semantic_role == "purpose")
    assert purpose.blocks == original.sections[0].blocks
    assert all(section.semantic_role != "background" for section in merged.sections)
    assert any(section.semantic_role == "request" for section in merged.sections)


def test_explicit_expected_effect_uses_source_sentence_instead_of_model_expansion() -> None:
    document = ProposalDocumentV2.model_validate(_document_payload())
    document = document.model_copy(
        update={
            "sections": [
                *document.sections[:-1],
                document.sections[-1].model_copy(
                    update={
                        "heading": "기대효과",
                        "semantic_role": "expected_effect",
                        "blocks": [
                            ProposalParagraphBlock(
                                type="paragraph",
                                text="업무 효율이 크게 향상될 것으로 기대합니다.",
                            )
                        ],
                    }
                ),
            ]
        }
    )
    citation = _citation(1, "도입 후 기대효과는 시험 준비 시간 단축과 절차 표준화이다.")

    grounded = _explicit_expected_effect_safety_net(
        document,
        [citation],
        (("E001", str(citation.chunk_id)),),
    )

    expected = next(section for section in grounded.sections if section.semantic_role == "expected_effect")
    assert expected.blocks[0].text == "도입 후 기대효과는 시험 준비 시간 단축과 절차 표준화이다."


def test_non_numeric_derived_claim_is_omitted_when_not_present_in_source() -> None:
    document = ProposalDocumentV2.model_validate(_document_payload())
    from smb_finder.playground.proposal_draft import _proposal_claims

    claims = _proposal_claims(document)
    verification = ProposalEvidenceVerification(
        schema_version="proposal-evidence-verification-v1",
        claims=[
            ProposalClaimVerdict(
                claim_id=claim.claim_id,
                status="derived" if index == 0 else "supported",
                action="keep",
                citations=["E001"],
            )
            for index, claim in enumerate(claims)
        ],
        questions=[],
    )

    corrected = _enforce_required_fact_safety_net(
        verification,
        claims,
        instruction="합성 구매 승인 요청",
        evidence=[_citation(1, "합성 품목은 10,000원이다.")],
        user_answers=[],
        proposal_type="purchase",
        allow_questions=True,
    )

    assert corrected.claims[0].status == "unsupported"
    assert corrected.claims[0].action == "omit"


def test_purchase_background_is_kept_only_when_reference_explicitly_supports_it() -> None:
    payload = _document_payload()
    payload["sections"].append(
        {
            "heading": "배경·필요성",
            "semantic_role": "background",
            "citations": ["E001"],
            "blocks": [{"type": "paragraph", "text": "업무 효율이 저하되고 있습니다."}],
            "missing_information": [],
        }
    )
    document = ProposalDocumentV2.model_validate(payload)

    removed = _purchase_background_safety_net(document, [_citation(1, "합성 키트 도입 목적을 안내한다.")])
    preserved = _purchase_background_safety_net(document, [_citation(1, "현재 문제점과 도입 필요성을 안내한다.")])

    assert all(section.semantic_role != "background" for section in removed.sections)
    assert any(section.semantic_role == "background" for section in preserved.sections)


def test_revision_reserves_base_document_from_runtime_context_budget() -> None:
    gateway = _SequenceGateway()
    generator = LocalProposalDraftGenerator(
        _settings(
            proposal_llm_num_ctx=4096,
            proposal_llm_max_tokens=512,
            proposal_context_max_chars=30_000,
            proposal_context_per_document_chars=10_000,
        ),
        gateway=gateway,
    )
    payload = _document_payload()
    payload["sections"][0]["blocks"] = [
        {"type": "paragraph", "text": "합성 기존 본문 " * 280},
    ]
    document = ProposalDocumentV2.model_validate(payload)

    with pytest.raises(ProposalDraftError) as raised:
        generator.revise(
            "본문을 간결하게 수정해 줘",
            document,
            [_citation(1, "합성 근거 " * 120)],
            proposal_type="general",
        )

    assert raised.value.code == "proposal_context_limit_exceeded"
    assert raised.value.context_usage is not None
    assert raised.value.context_usage.context_budget_chars < 256
    assert gateway.calls == []
