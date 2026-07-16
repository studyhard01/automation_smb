"""문서 챗봇 golden dataset과 평가 결과 계약."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class GoldenInputs(BaseModel):
    """한 평가 case에 전달할 합성 입력."""

    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)


class ExpectedToolCall(BaseModel):
    """평가에서 기대하는 tool 호출."""

    name: str = Field(min_length=1)


class GoldenExpectations(BaseModel):
    """검색·tool·답변의 기대값."""

    expected_document_keys: list[str] = Field(default_factory=list)
    expected_chunk_keys: list[str] = Field(default_factory=list)
    expected_tool_calls: list[ExpectedToolCall] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list)
    reference_answer: str = ""
    should_answer: bool = True

    @model_validator(mode="after")
    def validate_answerable_case(self) -> GoldenExpectations:
        """답변 가능 case에는 검색 정답이나 참고 답변이 최소 하나 있어야 한다."""

        expects_rag = any(call.name == "search_rag_chunks" for call in self.expected_tool_calls)
        has_reference = bool(self.expected_document_keys or self.expected_chunk_keys or self.reference_answer.strip())
        if self.should_answer and expects_rag and not has_reference:
            raise ValueError("RAG 답변 가능 case에는 문서/chunk key 또는 reference_answer가 필요합니다.")
        return self


class GoldenCase(BaseModel):
    """JSONL 한 줄에 대응하는 합성 평가 case."""

    case_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    inputs: GoldenInputs
    expectations: GoldenExpectations
    tags: dict[str, str] = Field(default_factory=dict)


class GoldenDataset(BaseModel):
    """검증 완료된 golden case 묶음."""

    name: str
    version: str
    fingerprint: str
    cases: list[GoldenCase]


class RetrievedChunk(BaseModel):
    """평가 artifact에 남길 경로 비노출 검색 결과."""

    document_key: str
    chunk_key: str
    rank: int = Field(ge=1)
    similarity: float


class CaseEvaluationResult(BaseModel):
    """한 case의 retrieval-only 평가 결과."""

    case_id: str
    status: Literal["ok", "error", "skipped"]
    question: str
    top_k: int
    expected_document_keys: list[str] = Field(default_factory=list)
    expected_chunk_keys: list[str] = Field(default_factory=list)
    expected_tool_calls: list[str] = Field(default_factory=list)
    actual_tool_calls: list[str] = Field(default_factory=list)
    retrieved: list[RetrievedChunk] = Field(default_factory=list)
    hit_at_k: float | None = None
    recall_at_k: float | None = None
    reciprocal_rank: float | None = None
    tool_exact_match: float = 0.0
    no_answer_correct: float | None = None
    embedding_ms: float = 0.0
    db_ms: float = 0.0
    elapsed_ms: float = 0.0
    over_budget: bool = False
    error_code: str = ""
    error_message: str = ""


class AggregateMetrics(BaseModel):
    """MLflow metric으로 기록할 retrieval-only 집계값."""

    total_cases: int
    evaluated_cases: int
    skipped_cases: int
    successful_cases: int
    error_cases: int
    hit_at_k: float = 0.0
    recall_at_k: float = 0.0
    mrr: float = 0.0
    tool_exact_match: float = 0.0
    no_answer_accuracy: float | None = None
    success_rate: float = 0.0
    error_rate: float = 0.0
    over_budget_rate: float = 0.0
    embedding_p50_ms: float = 0.0
    embedding_p95_ms: float = 0.0
    db_p50_ms: float = 0.0
    db_p95_ms: float = 0.0
    retrieval_p50_ms: float = 0.0
    retrieval_p95_ms: float = 0.0

    def as_mlflow_metrics(self) -> dict[str, float]:
        """None을 제외하고 MLflow가 받는 숫자 metric으로 변환한다."""

        values = self.model_dump()
        return {key: float(value) for key, value in values.items() if value is not None}


class CorpusSnapshot(BaseModel):
    """본문·경로 없이 계산한 로컬 RAG corpus 식별자."""

    fingerprint: str
    document_count: int = 0
    chunk_count: int = 0
    embedding_models: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    """한 retrieval-only evaluation run 전체 결과."""

    mode: Literal["retrieval"] = "retrieval"
    dataset_name: str
    dataset_version: str
    dataset_fingerprint: str
    corpus: CorpusSnapshot
    top_k_override: int | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cases: list[CaseEvaluationResult]
    aggregate: AggregateMetrics
