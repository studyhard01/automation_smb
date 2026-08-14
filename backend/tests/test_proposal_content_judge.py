from __future__ import annotations

import json as json_module

import httpx

from smb_finder.evaluation.proposal_content_judge import LocalProposalContentJudge
from smb_finder.playground.proposal_draft import ProposalDocumentV2


def _document() -> ProposalDocumentV2:
    return ProposalDocumentV2.model_validate(
        {
            "schema_version": "proposal-document-v2",
            "title": "합성 자동화 구축",
            "approval_request": "검토 후 승인하여 주시기 바랍니다.",
            "sections": [
                {
                    "heading": "목적",
                    "semantic_role": "purpose",
                    "citations": ["E001"],
                    "blocks": [{"type": "paragraph", "text": "합성 자동화 시스템을 구축합니다."}],
                    "missing_information": [],
                }
            ],
            "missing_information": [],
        }
    )


def _verdict() -> dict[str, object]:
    return {
        "schema_version": "proposal-content-judge-v1",
        "scores": {
            "grounded_accuracy": 11,
            "decision_completeness": 8,
            "purpose_and_necessity": 6,
            "actionability_and_feasibility": 5,
            "logical_structure": 4,
            "business_writing": 3.5,
            "conciseness_and_readability": 2,
        },
        "unsupported_material_claim_count": 0,
        "missing_critical_item_count": 1,
        "contradiction_count": 0,
        "confidence": 0.9,
    }


class _SequenceClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        self.calls.append(json)
        content = self.responses.pop(0)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"message": {"content": content if isinstance(content, str) else json_module.dumps(content)}},
            request=request,
        )

    def close(self) -> None:
        return None


def test_local_content_judge_returns_seven_dimension_score() -> None:
    client = _SequenceClient([_verdict()])
    judge = LocalProposalContentJudge("http://127.0.0.1:11434", "synthetic-judge", client=client)

    result = judge.judge(
        "합성 자동화 기안을 작성하세요.",
        _document(),
        [("E001", "합성 자동화 시스템을 구축한다.")],
        proposal_type="general",
    )

    assert result.status == "completed"
    assert result.total_score == 39.5
    assert result.missing_critical_item_count == 1
    assert len(client.calls) == 1
    assert client.calls[0]["think"] is False
    assert client.calls[0]["options"]["temperature"] == 0  # type: ignore[index]


def test_local_content_judge_retries_invalid_json_once() -> None:
    client = _SequenceClient(["not-json", _verdict()])
    judge = LocalProposalContentJudge("http://127.0.0.1:11434", "synthetic-judge", client=client)

    result = judge.judge(
        "합성 자동화 기안을 작성하세요.",
        _document(),
        [("E001", "합성 자동화 시스템을 구축한다.")],
        proposal_type="general",
    )

    assert result.status == "completed"
    assert len(client.calls) == 2
    assert "JSON 숫자 계약을 위반" in client.calls[1]["messages"][0]["content"]  # type: ignore[index]


def test_local_content_judge_rejects_external_endpoint_without_call() -> None:
    client = _SequenceClient([_verdict()])
    judge = LocalProposalContentJudge("https://example.com", "synthetic-judge", client=client)

    result = judge.judge(
        "합성 자동화 기안을 작성하세요.",
        _document(),
        [("E001", "합성 자동화 시스템을 구축한다.")],
        proposal_type="general",
    )

    assert result.status == "failed"
    assert result.failure_code == "invalid_endpoint"
    assert client.calls == []
