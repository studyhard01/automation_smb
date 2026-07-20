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


class CaseProvenance(BaseModel):
    """평가 case가 corpus 근거와 검토 단계를 거쳤는지 나타내는 출처 계약."""

    review_status: Literal["candidate", "validated"]
    source_kind: Literal["corpus", "behavioral"]
    generation_method: Literal["manual", "corpus-derived"]
    source_chunk_hashes: dict[str, str] = Field(default_factory=dict)
    reviewed_on: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def validate_source_hashes(self) -> CaseProvenance:
        """근거 hash 형식과 behavioral case의 비본문 계약을 검증한다."""

        for chunk_key, content_hash in self.source_chunk_hashes.items():
            normalized_hash = content_hash.strip().casefold()
            if not chunk_key.strip() or len(normalized_hash) != 64 or any(ch not in "0123456789abcdef" for ch in normalized_hash):
                raise ValueError("source_chunk_hashes는 chunk key와 64자리 SHA-256 hash여야 합니다.")
        if self.source_kind == "behavioral" and self.source_chunk_hashes:
            raise ValueError("behavioral case에는 corpus chunk hash를 넣지 않습니다.")
        return self


class GoldenCase(BaseModel):
    """JSONL 한 줄에 대응하는 합성 평가 case."""

    case_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    inputs: GoldenInputs
    expectations: GoldenExpectations
    tags: dict[str, str] = Field(default_factory=dict)
    provenance: CaseProvenance

    @model_validator(mode="after")
    def validate_validated_evidence(self) -> GoldenCase:
        """검증 완료된 corpus case는 모든 기대 chunk의 내용 hash를 가져야 한다."""

        if self.provenance.review_status != "validated" or self.provenance.source_kind != "corpus":
            return self
        expected = set(self.expectations.expected_chunk_keys)
        recorded = set(self.provenance.source_chunk_hashes)
        missing = sorted(expected - recorded)
        if missing:
            raise ValueError(f"validated corpus case에 source chunk hash가 없습니다: {', '.join(missing)}")
        return self


class GoldenDataset(BaseModel):
    """검증 완료된 golden case 묶음."""

    name: str
    version: str
    fingerprint: str
    tier: Literal["golden", "candidate"] = "golden"
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
    candidate_count: int = 0
    rejected_count: int = 0
    similarity_cutoff: float = 0.0
    top_similarity: float | None = None
    no_answer: bool = False
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


class EndToEndCaseEvaluationResult(BaseModel):
    """한 case의 실제 agent/tool/답변 전체 흐름 평가 결과."""

    case_id: str
    status: Literal["ok", "error"]
    question: str
    answer: str = ""
    expected_tool_calls: list[str] = Field(default_factory=list)
    actual_tool_calls: list[str] = Field(default_factory=list)
    retrieved: list[RetrievedChunk] = Field(default_factory=list)
    hit_at_k: float | None = None
    recall_at_k: float | None = None
    reciprocal_rank: float | None = None
    tool_exact_match: float = 0.0
    no_answer_correct: float | None = None
    grounding_decision: str = ""
    similarity_cutoff: float = 0.0
    top_similarity: float | None = None
    fact_coverage: float | None = None
    citation_match: float | None = None
    agent_steps: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: float = 0.0
    over_budget: bool = False
    error_code: str = ""
    error_message: str = ""


class EndToEndAggregateMetrics(BaseModel):
    """MLflow에 기록할 실제 문서 챗봇 전체 흐름 집계값."""

    total_cases: int
    successful_cases: int
    error_cases: int
    success_rate: float = 0.0
    error_rate: float = 0.0
    hit_at_k: float = 0.0
    recall_at_k: float = 0.0
    mrr: float = 0.0
    tool_exact_match: float = 0.0
    no_answer_accuracy: float | None = None
    fact_coverage: float | None = None
    citation_match: float | None = None
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    over_budget_rate: float = 0.0
    avg_llm_calls: float = 0.0
    avg_total_tokens: float = 0.0
    total_tokens: int = 0

    def as_mlflow_metrics(self) -> dict[str, float]:
        """None을 제외하고 MLflow metric 숫자로 변환한다."""

        values = self.model_dump()
        return {key: float(value) for key, value in values.items() if value is not None}


class JudgeScorerResult(BaseModel):
    """LLM judge 하나의 실행 또는 건너뜀 상태."""

    name: str
    status: Literal["completed", "failed", "skipped"]
    error: str = ""


class JudgeEvaluationSummary(BaseModel):
    """비결정적·외부 호출인 Phase 3 judge의 fail-open 실행 요약."""

    enabled: bool = False
    status: Literal["disabled", "completed", "partial", "failed", "skipped"] = "disabled"
    model: str = ""
    scorers: list[JudgeScorerResult] = Field(default_factory=list)


class EndToEndEvaluationReport(BaseModel):
    """agent/tool/검색/생성을 모두 포함한 Phase 2 evaluation run 결과."""

    mode: Literal["end-to-end"] = "end-to-end"
    dataset_name: str
    dataset_version: str
    dataset_fingerprint: str
    corpus: CorpusSnapshot
    provider: str
    model: str
    skill_id: str
    skill_fingerprint: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    cases: list[EndToEndCaseEvaluationResult]
    aggregate: EndToEndAggregateMetrics
    judge: JudgeEvaluationSummary = Field(default_factory=JudgeEvaluationSummary)
    trace_errors: list[str] = Field(default_factory=list)


EvaluationReportLike = EvaluationReport | EndToEndEvaluationReport
