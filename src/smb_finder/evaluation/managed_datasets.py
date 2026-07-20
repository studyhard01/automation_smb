"""로컬 JSONL 평가셋을 MLflow 3.x Evaluation Dataset으로 등록하는 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import CorpusSnapshot, GoldenDataset


@dataclass(frozen=True)
class ManagedDatasetRegistration:
    """MLflow Evaluation Dataset upsert 결과."""

    dataset_id: str
    name: str
    tier: str
    version: str
    fingerprint: str
    record_count: int


def build_managed_dataset_records(dataset: GoldenDataset) -> list[dict[str, Any]]:
    """내부 평가 계약을 MLflow reserved expectation key가 포함된 record로 변환한다."""

    records: list[dict[str, Any]] = []
    for case in dataset.cases:
        expectations = case.expectations
        records.append(
            {
                "inputs": {
                    "question": case.inputs.question,
                    "top_k": case.inputs.top_k,
                },
                "expectations": {
                    "expected_facts": expectations.expected_facts,
                    "expected_response": expectations.reference_answer,
                    "expected_document_keys": expectations.expected_document_keys,
                    "expected_chunk_keys": expectations.expected_chunk_keys,
                    "expected_tool_calls": [call.name for call in expectations.expected_tool_calls],
                    "should_answer": expectations.should_answer,
                },
                "tags": {
                    "case_id": case.case_id,
                    "synthetic": case.tags.get("synthetic", "true"),
                    "category": case.tags.get("category", ""),
                    "difficulty": case.tags.get("difficulty", ""),
                    "review_status": case.provenance.review_status,
                    "source_kind": case.provenance.source_kind,
                },
            }
        )
    return records


def register_managed_dataset(
    mlflow: Any,
    *,
    tracking_uri: str,
    experiment_name: str,
    dataset_name: str,
    dataset: GoldenDataset,
    corpus: CorpusSnapshot,
) -> ManagedDatasetRegistration:
    """동일 입력은 갱신되는 MLflow input-hash upsert로 dataset을 생성하거나 업데이트한다."""

    mlflow.set_tracking_uri(tracking_uri)
    experiment = mlflow.set_experiment(experiment_name)
    datasets_api = mlflow.genai.datasets
    escaped_name = dataset_name.replace("'", "''")
    matches = list(
        datasets_api.search_datasets(
            experiment_ids=[str(experiment.experiment_id)],
            filter_string=f"name = '{escaped_name}'",
            max_results=2,
        )
    )
    if len(matches) > 1:
        raise RuntimeError(f"같은 이름의 MLflow dataset이 여러 개입니다: {dataset_name}")

    tags = {
        "synthetic": "true",
        "tier": dataset.tier,
        "version": dataset.version,
        "fingerprint": dataset.fingerprint,
        "corpus_fingerprint": corpus.fingerprint,
        "corpus_document_count": str(corpus.document_count),
        "corpus_chunk_count": str(corpus.chunk_count),
    }
    if matches:
        managed = matches[0]
        datasets_api.set_dataset_tags(dataset_id=managed.dataset_id, tags=tags)
    else:
        managed = datasets_api.create_dataset(
            name=dataset_name,
            experiment_id=[str(experiment.experiment_id)],
            tags=tags,
        )

    managed.merge_records(build_managed_dataset_records(dataset))
    record_count = len(managed.to_df())
    return ManagedDatasetRegistration(
        dataset_id=str(managed.dataset_id),
        name=dataset_name,
        tier=dataset.tier,
        version=dataset.version,
        fingerprint=dataset.fingerprint,
        record_count=record_count,
    )
