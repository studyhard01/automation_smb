"""선택 문서 범위 검색과 공통 로컬 모델 Gateway를 연결한다."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from smb_finder.bot_core import JsonModelGateway, ModelGatewayError
from smb_finder.config import Settings
from smb_finder.llmops_search import LlmopsSearchError
from smb_finder.models import ArtifactLink, RetrievalMetadata

from .document_models import (
    ChatRequest,
    ChatResponse,
    RagGroundingMetadata,
    TokenUsage,
    ToolCallTrace,
)

_logger = logging.getLogger(__name__)


class _AnswerPayload(BaseModel):
    """문서 답변 모델이 반환할 수 있는 유일한 JSON shape."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    answer: str = Field(min_length=1, max_length=700)


_MODEL_ERROR_MESSAGES = {
    "model_not_configured": "로컬 LLM 주소 또는 모델이 구성되지 않았습니다.",
    "model_url_not_internal": "문서 내용은 온프레미스 LLM으로만 전송할 수 있습니다.",
    "model_timeout": "근거 답변 생성 시간이 초과됐습니다.",
    "model_unavailable": "근거 답변 생성용 로컬 LLM을 사용할 수 없습니다.",
    "invalid_model_response": "로컬 LLM 응답 형식을 확인할 수 없습니다.",
    "output_schema_invalid": "로컬 LLM 응답이 답변 계약을 충족하지 않았습니다.",
    "model_budget_exhausted": "근거 답변 생성 전에 요청 시간 예산이 소진됐습니다.",
}


class DocumentChatService:
    """검증된 서버 citation이 있을 때만 공통 Gateway로 답변을 생성한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        gateway: JsonModelGateway | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._clock = clock

    def run(
        self,
        request: ChatRequest,
        retriever: Any | None = None,
        *,
        request_id: str = "",
        scoped_retriever: Any | None = None,
        upload_manager: Any | None = None,
        model_gateway: JsonModelGateway | None = None,
        deadline: float | None = None,
    ) -> ChatResponse:
        """하나의 절대 deadline 안에서 검색하고 서버 설정 모델로 답변한다."""

        if scoped_retriever is not None:
            retriever = scoped_retriever
        started = self._clock()
        effective_deadline = (
            deadline if deadline is not None else (started + self._settings.playground_agent_budget_ms / 1000)
        )
        request_id = request_id or str(uuid.uuid4())
        session_id = request.session_id or f"pg-{uuid.uuid4()}"
        model = self._settings.llmops_chat_model.strip()
        gateway = model_gateway or self._gateway

        if not request.selected_files:
            return self._error(
                request_id,
                session_id,
                model,
                started,
                "selected_file_required",
                "왼쪽 파일 찾기에서 문서를 먼저 선택해 주세요.",
                deadline=effective_deadline,
            )
        database_files = [item for item in request.selected_files if item.source == "llmops"]
        uploaded_files = [item for item in request.selected_files if item.source == "upload"]
        if database_files and retriever is None:
            return self._error(
                request_id,
                session_id,
                model,
                started,
                "llmops_retrieval_not_configured",
                "선택 문서 검색기가 구성되지 않았습니다.",
                deadline=effective_deadline,
            )
        if uploaded_files and upload_manager is None:
            return self._error(
                request_id,
                session_id,
                model,
                started,
                "upload_context_unavailable",
                "첨부 파일 대화 기능이 구성되지 않았습니다.",
                deadline=effective_deadline,
            )

        results: list[tuple[str, Any]] = []
        if database_files:
            self._ensure_retrieval_budget(effective_deadline, started)
            selections = [(str(item.doc_id), str(item.revision_id)) for item in database_files]
            results.append(("database", retriever.retrieve(request.message, selections, deadline=effective_deadline)))
        if uploaded_files:
            self._ensure_retrieval_budget(effective_deadline, started)
            upload_selections = [(item.doc_id, item.revision_id) for item in uploaded_files]
            results.append(
                (
                    "upload",
                    upload_manager.retrieve(request.message, upload_selections, deadline=effective_deadline),
                )
            )

        citations, retrieval = self._merge_retrieval(results)
        database_scopes = [result.metadata.scope for source, result in results if source == "database"]
        artifacts = [
            ArtifactLink(
                doc_id=scope.doc_id,
                revision_id=scope.revision_id,
                preview_url=f"/api/playground/files/{scope.doc_id}/revisions/{scope.revision_id}/artifacts/preview",
                canonical_url=f"/api/playground/files/{scope.doc_id}/revisions/{scope.revision_id}/artifacts/canonical",
                graph_url=f"/api/playground/files/{scope.doc_id}/graph",
            )
            for scopes in database_scopes
            for scope in scopes
        ]
        trace = ToolCallTrace(elapsed_ms=retrieval.elapsed_ms, result_count=retrieval.result_count)
        grounding = RagGroundingMetadata(
            decision=retrieval.decision,
            candidate_count=retrieval.candidate_count,
            rejected_count=max(0, retrieval.candidate_count - retrieval.result_count),
        )

        if not citations:
            over_budget = self._clock() >= effective_deadline
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                model_used=model,
                assistant_message="선택한 문서에서 답변할 만큼 충분한 근거를 찾지 못했습니다.",
                tool_calls=[trace],
                elapsed_ms=self._elapsed(started),
                warnings=["rag_insufficient_evidence"] + (["playground_agent_over_budget"] if over_budget else []),
                over_budget=over_budget,
                rag_grounding=grounding,
                citations=[],
                retrieval=retrieval,
                artifacts=artifacts,
            )

        if gateway is None:
            return self._error_with_evidence(
                request_id,
                session_id,
                model,
                started,
                "model_not_configured",
                _MODEL_ERROR_MESSAGES["model_not_configured"],
                trace,
                grounding,
                citations,
                retrieval,
                artifacts,
                deadline=effective_deadline,
            )
        if self._clock() >= effective_deadline:
            return self._error_with_evidence(
                request_id,
                session_id,
                model,
                started,
                "model_budget_exhausted",
                _MODEL_ERROR_MESSAGES["model_budget_exhausted"],
                trace,
                grounding,
                citations,
                retrieval,
                artifacts,
                deadline=effective_deadline,
            )

        per_citation_chars = max(
            200,
            self._settings.rag_synthesis_evidence_chars // max(1, len(citations)),
        )
        evidence = [
            {
                "index": citation.index,
                "title": citation.title,
                "section_path": citation.section_path,
                "location": citation.location,
                "excerpt": citation.excerpt[:per_citation_chars],
            }
            for citation in citations
        ]
        recent_history = [
            item.model_dump() for item in request.history[-self._settings.playground_agent_context_messages :]
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "제공된 선택 문서 근거만 사용해 한국어로 답하세요. "
                    "주요 주장 뒤에 [근거 index]를 붙이고 근거가 부족하면 그 사실을 명시하세요. "
                    "답변은 핵심만 여섯 문장 이내로 작성하세요."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"question": request.message, "history": recent_history, "evidence": evidence},
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            result = gateway.invoke_json(
                purpose="document_answer",
                model=model,
                messages=messages,
                response_model=_AnswerPayload,
                deadline=effective_deadline,
                max_timeout_ms=self._settings.llm_timeout_ms,
                max_output_tokens=self._settings.rag_synthesis_max_tokens,
            )
        except ModelGatewayError as exc:
            _logger.warning("Document answer model call failed: request_id=%s error_code=%s", request_id, exc.code)
            return self._error_with_evidence(
                request_id,
                session_id,
                model,
                started,
                exc.code,
                _MODEL_ERROR_MESSAGES[exc.code],
                trace,
                grounding,
                citations,
                retrieval,
                artifacts,
                deadline=effective_deadline,
            )

        elapsed_ms = self._elapsed(started)
        over_budget = self._clock() > effective_deadline
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            model_used=result.model,
            assistant_message=result.payload.answer,
            tool_calls=[trace],
            elapsed_ms=elapsed_ms,
            warnings=["playground_agent_over_budget"] if over_budget else [],
            over_budget=over_budget,
            rag_grounding=grounding,
            citations=citations,
            retrieval=retrieval,
            artifacts=artifacts,
            token_usage=TokenUsage(
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
            ),
        )

    def _merge_retrieval(self, results: list[tuple[str, Any]]) -> tuple[list[Any], RetrievalMetadata]:
        """DB와 즉시 첨부 근거를 서로 독점하지 않도록 교차 병합한다."""

        citations: list[Any] = []
        max_length = max((len(result.citations) for _, result in results), default=0)
        for offset in range(max_length):
            for _, result in results:
                if offset < len(result.citations):
                    citations.append(result.citations[offset])
                if len(citations) >= self._settings.llmops_retrieval_top_k:
                    break
            if len(citations) >= self._settings.llmops_retrieval_top_k:
                break
        citations = [citation.model_copy(update={"index": index}) for index, citation in enumerate(citations, start=1)]

        scopes = []
        seen_scopes: set[tuple[Any, Any]] = set()
        timings: dict[str, float] = {}
        degraded: list[str] = []
        candidate_count = 0
        elapsed_ms = 0.0
        for source, result in results:
            metadata = result.metadata
            candidate_count += metadata.candidate_count
            elapsed_ms += metadata.elapsed_ms
            degraded.extend(metadata.degraded_dependencies)
            timings.update({f"{source}_{key}": value for key, value in metadata.timings_ms.items()})
            for scope in metadata.scope:
                key = (scope.doc_id, scope.revision_id)
                if key not in seen_scopes:
                    seen_scopes.add(key)
                    scopes.append(scope)

        elapsed_ms = round(elapsed_ms, 1)
        retrieval = RetrievalMetadata(
            trace_id=uuid.uuid4(),
            scope=scopes,
            result_count=len(citations),
            candidate_count=candidate_count,
            grounded=bool(citations),
            decision="answerable" if citations else "insufficient_evidence",
            degraded_dependencies=list(dict.fromkeys(degraded)),
            timings_ms=timings,
            elapsed_ms=elapsed_ms,
            over_budget=elapsed_ms > self._settings.playground_agent_budget_ms,
        )
        return citations, retrieval

    def _ensure_retrieval_budget(self, deadline: float, started: float) -> None:
        if self._clock() >= deadline:
            raise LlmopsSearchError(
                "llmops_retrieval_budget_exhausted",
                "선택 문서 검색 시간 예산이 소진됐습니다.",
                self._elapsed(started),
            )

    def _elapsed(self, started: float) -> float:
        return round(max(0.0, (self._clock() - started) * 1000), 1)

    def _error(
        self,
        request_id: str,
        session_id: str,
        model: str,
        started: float,
        code: str,
        message: str,
        *,
        deadline: float,
    ) -> ChatResponse:
        over_budget = self._clock() >= deadline
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            model_used=model,
            assistant_message=message,
            elapsed_ms=self._elapsed(started),
            warnings=["playground_agent_over_budget"] if over_budget else [],
            error_code=code,
            over_budget=over_budget,
        )

    def _error_with_evidence(
        self,
        request_id: str,
        session_id: str,
        model: str,
        started: float,
        code: str,
        message: str,
        trace: ToolCallTrace,
        grounding: RagGroundingMetadata,
        citations: list[Any],
        retrieval: RetrievalMetadata,
        artifacts: list[ArtifactLink],
        *,
        deadline: float,
    ) -> ChatResponse:
        over_budget = self._clock() >= deadline
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            model_used=model,
            assistant_message=message,
            tool_calls=[trace],
            elapsed_ms=self._elapsed(started),
            warnings=["playground_agent_over_budget"] if over_budget else [],
            error_code=code,
            over_budget=over_budget,
            rag_grounding=grounding,
            citations=citations,
            retrieval=retrieval,
            artifacts=artifacts,
        )
