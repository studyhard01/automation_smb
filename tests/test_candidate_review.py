"""합성 evaluation candidate 사람 검토 CLI 계약 테스트."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def review_module() -> ModuleType:
    path = REPO_ROOT / "scripts" / "review_evaluation_candidate.py"
    spec = importlib.util.spec_from_file_location("candidate_review_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _candidate(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "inputs": {"question": "합성 프로젝트의 다음 일정은?", "top_k": 5},
        "expectations": {
            "expected_document_keys": [],
            "expected_chunk_keys": [],
            "expected_tool_calls": [],
            "expected_facts": [],
            "reference_answer": "",
            "should_answer": False,
        },
        "tags": {"synthetic": "true", "category": "clarification", "difficulty": "easy"},
        "provenance": {
            "review_status": "candidate",
            "source_kind": "behavioral",
            "generation_method": "manual",
            "source_chunk_hashes": {},
        },
    }


def _write_candidates(path: Path) -> None:
    cases = [_candidate("synthetic-review-001"), _candidate("synthetic-review-002")]
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) for case in cases) + "\n",
        encoding="utf-8",
    )


def test_record_review_keeps_candidate_tier_and_records_decision(review_module: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "candidates.jsonl"
    _write_candidates(path)

    reviewed = review_module.record_review(
        path,
        case_id="synthetic-review-001",
        decision="approve",
        reviewed_on="2026-07-20",
    )

    assert reviewed["provenance"]["review_status"] == "candidate"
    assert reviewed["provenance"]["review_decision"] == "approve"
    loaded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert loaded[0]["provenance"]["reviewed_on"] == "2026-07-20"
    assert "review_decision" not in loaded[1]["provenance"]


def test_record_review_requires_notes_for_revision_and_explicit_replace(
    review_module: ModuleType,
    tmp_path: Path,
) -> None:
    path = tmp_path / "candidates.jsonl"
    _write_candidates(path)

    with pytest.raises(review_module.CandidateReviewError, match="notes"):
        review_module.record_review(
            path,
            case_id="synthetic-review-001",
            decision="revise",
            reviewed_on="2026-07-20",
        )

    review_module.record_review(
        path,
        case_id="synthetic-review-001",
        decision="reject",
        reviewed_on="2026-07-20",
        notes="질문이 근거보다 넓습니다.",
    )
    with pytest.raises(review_module.CandidateReviewError, match="이미 검토"):
        review_module.record_review(
            path,
            case_id="synthetic-review-001",
            decision="approve",
            reviewed_on="2026-07-20",
        )
