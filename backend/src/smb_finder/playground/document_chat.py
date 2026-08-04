"""선택 문서 범위 Hybrid 검색과 로컬 LLM 답변을 연결한다."""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx

from smb_finder.config import Settings
from smb_finder.models import ArtifactLink

from .document_models import (
    ChatRequest,
    ChatResponse,
    RagGroundingMetadata,
    TokenUsage,
    ToolCallTrace,
)

_logger = logging.getLogger(__name__)


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


def _parse_answer(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0].strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("LLM 응답이 JSON 객체가 아닙니다.")
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        raise ValueError("LLM 응답에 answer가 없습니다.")
    return answer


class DocumentChatService:
    """DB 근거가 있을 때만 온프레미스 Ollama로 답변을 합성한다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.Client(timeout=max(0.1, settings.llm_timeout_ms / 1000))

    def close(self) -> None:
        """재사용하던 HTTP 연결을 닫는다."""

        self._client.close()

    def run(
        self,
        request: ChatRequest,
        retriever: Any | None = None,
        *,
        request_id: str = "",
        scoped_retriever: Any | None = None,
    ) -> ChatResponse:
        """선택 UUID scope를 검색하고 근거가 있을 때만 답변을 생성한다."""

        if scoped_retriever is not None:
            retriever = scoped_retriever
        started = time.perf_counter()
        request_id = request_id or str(uuid.uuid4())
        session_id = request.session_id or f"pg-{uuid.uuid4()}"
        model = request.model.strip() or self._settings.llmops_chat_model.strip()

        if not request.selected_files:
            return self._error(
                request_id,
                session_id,
                model,
                started,
                "selected_file_required",
                "왼쪽 파일 찾기에서 문서를 먼저 선택해 주세요.",
            )
        if retriever is None:
            return self._error(
                request_id,
                session_id,
                model,
                started,
                "llmops_retrieval_not_configured",
                "선택 문서 검색기가 구성되지 않았습니다.",
            )

        selections = [(str(item.doc_id), str(item.revision_id)) for item in request.selected_files]
        result = retriever.retrieve(request.message, selections)
        citations = result.citations
        retrieval = result.metadata
        artifacts = [
            ArtifactLink(
                doc_id=scope.doc_id,
                revision_id=scope.revision_id,
                preview_url=f"/api/playground/files/{scope.doc_id}/revisions/{scope.revision_id}/artifacts/preview",
                canonical_url=f"/api/playground/files/{scope.doc_id}/revisions/{scope.revision_id}/artifacts/canonical",
                graph_url=f"/api/playground/files/{scope.doc_id}/graph",
            )
            for scope in retrieval.scope
        ]
        trace = ToolCallTrace(elapsed_ms=retrieval.elapsed_ms, result_count=retrieval.result_count)
        grounding = RagGroundingMetadata(
            decision=retrieval.decision,
            candidate_count=retrieval.candidate_count,
            rejected_count=max(0, retrieval.candidate_count - retrieval.result_count),
        )

        if not citations:
            return ChatResponse(
                request_id=request_id,
                session_id=session_id,
                model_used=model,
                assistant_message="선택한 문서에서 답변할 만큼 충분한 근거를 찾지 못했습니다.",
                tool_calls=[trace],
                elapsed_ms=self._elapsed(started),
                warnings=["rag_insufficient_evidence"],
                rag_grounding=grounding,
                citations=[],
                retrieval=retrieval,
                artifacts=artifacts,
            )

        root_url = self._settings.ollama_base_url.strip().rstrip("/")
        if not root_url or not model:
            return self._error_with_evidence(
                request_id,
                session_id,
                model,
                started,
                "local_llm_not_configured",
                "로컬 LLM 주소 또는 모델이 구성되지 않았습니다.",
                trace,
                grounding,
                citations,
                retrieval,
                artifacts,
            )
        if not _is_internal_http_url(root_url):
            return self._error_with_evidence(
                request_id,
                session_id,
                model,
                started,
                "local_llm_url_not_internal",
                "문서 내용은 온프레미스 LLM으로만 전송할 수 있습니다.",
                trace,
                grounding,
                citations,
                retrieval,
                artifacts,
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
        recent_history = [item.model_dump() for item in request.history[-self._settings.playground_agent_context_messages :]]
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "제공된 선택 문서 근거만 사용해 한국어로 답하세요. "
                        "주요 주장 뒤에 [근거 index]를 붙이고, 근거가 부족하면 그 사실을 명시하세요. "
                        "답변은 핵심만 여섯 문장 이내로 작성하세요. "
                        "JSON {\"answer\":\"...\"}만 반환하세요."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": request.message, "history": recent_history, "evidence": evidence},
                        ensure_ascii=False,
                    ),
                },
            ],
            "stream": False,
            "think": False,
            "format": {
                "type": "object",
                "properties": {"answer": {"type": "string", "maxLength": 700}},
                "required": ["answer"],
                "additionalProperties": False,
            },
            "options": {"temperature": 0, "num_predict": self._settings.rag_synthesis_max_tokens},
        }
        try:
            response_json = self._chat_json(root_url, payload)
            if "answer" in response_json:
                answer = str(response_json["answer"]).strip()
            else:
                raw_content = response_json.get("message", {}).get("content")
                answer = _parse_answer(raw_content if isinstance(raw_content, str) else "")
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            _logger.warning(
                "선택 문서 답변 생성 실패: request_id=%s failure_type=%s",
                request_id,
                type(exc).__name__,
            )
            return self._error_with_evidence(
                request_id,
                session_id,
                model,
                started,
                "local_llm_failed",
                "근거 답변을 생성하는 로컬 LLM 호출에 실패했습니다.",
                trace,
                grounding,
                citations,
                retrieval,
                artifacts,
            )

        prompt_tokens = int(response_json.get("prompt_eval_count") or 0)
        completion_tokens = int(response_json.get("eval_count") or 0)
        elapsed_ms = self._elapsed(started)
        over_budget = elapsed_ms > self._settings.playground_agent_budget_ms
        warnings = ["playground_agent_over_budget"] if over_budget else []
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            model_used=model,
            assistant_message=answer,
            tool_calls=[trace],
            elapsed_ms=elapsed_ms,
            warnings=warnings,
            over_budget=over_budget,
            rag_grounding=grounding,
            citations=citations,
            retrieval=retrieval,
            artifacts=artifacts,
            token_usage=TokenUsage(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    def _chat_json(self, root_url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Ollama native API 응답을 JSON 객체로 반환한다."""

        response = self._client.post(f"{root_url}/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Ollama 응답이 JSON 객체가 아닙니다.")
        return data

    @staticmethod
    def _elapsed(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    def _error(
        self,
        request_id: str,
        session_id: str,
        model: str,
        started: float,
        code: str,
        message: str,
    ) -> ChatResponse:
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            model_used=model,
            assistant_message=message,
            elapsed_ms=self._elapsed(started),
            error_code=code,
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
        retrieval: Any,
        artifacts: list[ArtifactLink],
    ) -> ChatResponse:
        return ChatResponse(
            request_id=request_id,
            session_id=session_id,
            model_used=model,
            assistant_message=message,
            tool_calls=[trace],
            elapsed_ms=self._elapsed(started),
            error_code=code,
            rag_grounding=grounding,
            citations=citations,
            retrieval=retrieval,
            artifacts=artifacts,
        )
