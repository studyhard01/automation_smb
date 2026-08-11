"""기안 V2 계약, 컨텍스트 예산, 제한 오류 재시도를 합성 데이터로 검증한다."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from smb_finder.config import Settings
from smb_finder.models import DocumentCitation, RetrievalScores
from smb_finder.playground.proposal_context import pack_proposal_context
from smb_finder.playground.proposal_draft import (
    LocalProposalDraftGenerator,
    ProposalDocumentV2,
    ProposalDraftError,
    insert_proposal_fields,
    project_document_to_legacy_fields,
)


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

    payload = _document_payload()
    payload["sections"][1]["blocks"][0]["rows"] = [["열 수 불일치"]]
    with pytest.raises(ValidationError):
        ProposalDocumentV2.model_validate(payload)


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

    template_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "smb_finder"
        / "playground"
        / "templates"
        / "proposal_draft.xlsx"
    )
    generated = insert_proposal_fields(template_path.read_bytes(), fields)
    assert generated.startswith(b"PK\x03\x04")


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


class _SequenceClient:
    def __init__(self, *, always_limit: bool = False) -> None:
        self.always_limit = always_limit
        self.calls: list[dict] = []

    def post(self, url: str, *, json: dict) -> httpx.Response:
        self.calls.append(json)
        request = httpx.Request("POST", url)
        if self.always_limit or len(self.calls) == 1:
            return httpx.Response(
                400,
                json={"error": "prompt is too long for the context window"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "message": {"content": json_module.dumps(_document_payload(), ensure_ascii=False)},
                "prompt_eval_count": 321,
            },
            request=request,
        )

    def close(self) -> None:
        return None


json_module = json


def test_context_limit_retries_once_with_half_budget_and_records_usage() -> None:
    client = _SequenceClient()
    generator = LocalProposalDraftGenerator(_settings(), client=client)
    evidence = [_citation(1, "합성 근거 " * 120, score=0.9)]

    result = generator.generate("합성 구매 기안을 작성해 줘", evidence)

    assert len(client.calls) == 2
    first_context = client.calls[0]["messages"][1]["content"]
    retry_context = client.calls[1]["messages"][1]["content"]
    assert len(retry_context) < len(first_context)
    assert result.context_usage.retry_count == 1
    assert result.context_usage.context_budget_chars == 1000
    assert result.context_usage.first_attempt_context_chars is not None
    assert result.context_usage.prompt_eval_count == 321
    assert result.document is not None
    assert result.fields.title == result.document.title


def test_context_limit_retry_is_bounded_to_two_calls() -> None:
    client = _SequenceClient(always_limit=True)
    generator = LocalProposalDraftGenerator(_settings(), client=client)

    with pytest.raises(ProposalDraftError) as raised:
        generator.generate("합성 기안을 작성해 줘", [_citation(1, "합성 근거 " * 120)])

    assert raised.value.code == "proposal_context_limit_exceeded"
    assert raised.value.status_code == 422
    assert raised.value.context_usage is not None
    assert raised.value.context_usage.retry_count == 1
    assert raised.value.context_usage.first_attempt_context_chars is not None
    assert len(client.calls) == 2
