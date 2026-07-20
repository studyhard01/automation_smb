from __future__ import annotations

import json

import pytest

from smb_finder.evaluation.datasets import GoldenDatasetError, load_golden_dataset
from smb_finder.evaluation.document_chatbot import run_retrieval_evaluation
from smb_finder.evaluation.models import CorpusSnapshot, GoldenDataset
from smb_finder.rag_search import RagChunkHit, RagSearchError, RagSearchResponse


def _dataset() -> GoldenDataset:
    return GoldenDataset.model_validate(
        {
            "name": "synthetic.jsonl",
            "version": "v1",
            "fingerprint": "abc123",
            "cases": [
                {
                    "case_id": "retrieve-001",
                    "inputs": {"question": "합성 Alpha 질문", "top_k": 5},
                    "expectations": {
                        "expected_document_keys": ["alpha.md"],
                        "expected_chunk_keys": ["alpha.md#3"],
                        "expected_tool_calls": [{"name": "search_rag_chunks"}],
                        "reference_answer": "Alpha 답변",
                    },
                    "provenance": {
                        "review_status": "validated",
                        "source_kind": "corpus",
                        "generation_method": "manual",
                        "source_chunk_hashes": {"alpha.md#3": "a" * 64},
                    },
                },
                {
                    "case_id": "general-002",
                    "inputs": {"question": "안녕하세요", "top_k": 5},
                    "expectations": {
                        "expected_tool_calls": [],
                        "reference_answer": "안녕하세요",
                    },
                    "provenance": {
                        "review_status": "validated",
                        "source_kind": "behavioral",
                        "generation_method": "manual",
                    },
                },
            ],
        }
    )


def test_load_golden_dataset_validates_count_and_fingerprint(tmp_path):
    path = tmp_path / "golden.jsonl"
    case = _dataset().cases[0].model_dump(mode="json")
    path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")

    dataset = load_golden_dataset(path, version="test-v1")

    assert dataset.version == "test-v1"
    assert len(dataset.cases) == 1
    assert len(dataset.fingerprint) == 16


def test_load_golden_dataset_rejects_duplicate_case_ids(tmp_path):
    path = tmp_path / "duplicate.jsonl"
    case = _dataset().cases[0].model_dump(mode="json")
    path.write_text("\n".join(json.dumps(case, ensure_ascii=False) for _ in range(2)), encoding="utf-8")

    with pytest.raises(GoldenDatasetError, match="중복 case_id"):
        load_golden_dataset(path)


def test_retrieval_runner_scores_expected_cases_and_skips_general_request():
    class FakeSearcher:
        def search(self, query: str, limit: int) -> RagSearchResponse:
            assert query == "합성 Alpha 질문"
            assert limit == 3
            return RagSearchResponse(
                query=query,
                hits=[
                    RagChunkHit(
                        chunk_id=10,
                        document_id=1,
                        chunk_index=3,
                        content="artifact에는 기록하지 않을 합성 본문",
                        similarity=0.91,
                        file_name="alpha.md",
                        file_path="C:/internal/synthetic/alpha.md",
                    )
                ],
                result_count=1,
                embedding_model="synthetic-embedding",
                embedding_ms=4,
                db_ms=3,
                elapsed_ms=7,
            )

    report = run_retrieval_evaluation(
        _dataset(),
        FakeSearcher(),
        corpus=CorpusSnapshot(fingerprint="corpus1", document_count=1, chunk_count=1),
        top_k_override=3,
    )

    assert report.aggregate.total_cases == 2
    assert report.aggregate.evaluated_cases == 1
    assert report.aggregate.skipped_cases == 1
    assert report.aggregate.hit_at_k == 1
    assert report.aggregate.mrr == 1
    assert report.cases[0].retrieved[0].document_key == "alpha.md"
    assert "internal" not in report.model_dump_json()
    assert report.cases[1].status == "skipped"


def test_retrieval_runner_keeps_search_error_as_case_result():
    class ErrorSearcher:
        def search(self, query: str, limit: int):  # noqa: ANN201, ARG002
            raise RagSearchError("embedding_unavailable", "합성 임베딩 오류", 12.5)

    dataset = _dataset().model_copy(update={"cases": [_dataset().cases[0]]})
    report = run_retrieval_evaluation(
        dataset,
        ErrorSearcher(),
        corpus=CorpusSnapshot(fingerprint="corpus1"),
    )

    assert report.aggregate.error_cases == 1
    assert report.aggregate.error_rate == 1
    assert report.cases[0].error_code == "embedding_unavailable"
    assert report.cases[0].elapsed_ms == 12.5


def test_retrieval_runner_counts_cutoff_abstention_as_correct_no_answer():
    dataset = GoldenDataset.model_validate(
        {
            "name": "synthetic-no-answer.jsonl",
            "version": "v1",
            "fingerprint": "noanswer1",
            "cases": [
                {
                    "case_id": "synthetic-no-answer-001",
                    "inputs": {"question": "corpus에 없는 합성 질문", "top_k": 5},
                    "expectations": {
                        "expected_tool_calls": [{"name": "search_rag_chunks"}],
                        "should_answer": False,
                    },
                    "provenance": {
                        "review_status": "validated",
                        "source_kind": "behavioral",
                        "generation_method": "manual",
                    },
                }
            ],
        }
    )

    class CutoffSearcher:
        def search(self, query: str, limit: int) -> RagSearchResponse:  # noqa: ARG002
            return RagSearchResponse(
                query=query,
                hits=[],
                result_count=0,
                candidate_count=5,
                rejected_count=5,
                similarity_cutoff=0.4,
                top_similarity=0.284252,
                no_answer=True,
                embedding_model="synthetic-embedding",
            )

    report = run_retrieval_evaluation(
        dataset,
        CutoffSearcher(),
        corpus=CorpusSnapshot(fingerprint="corpus1"),
    )

    assert report.aggregate.no_answer_accuracy == 1
    assert report.cases[0].no_answer_correct == 1
    assert report.cases[0].retrieved == []
    assert report.cases[0].no_answer is True
    assert report.cases[0].similarity_cutoff == 0.4
    assert report.cases[0].top_similarity == 0.284252
    assert report.cases[0].rejected_count == 5
