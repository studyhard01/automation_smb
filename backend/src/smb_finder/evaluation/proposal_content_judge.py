"""온프레미스 LLM으로 기안 내용 45점 루브릭을 판정한다."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from smb_finder.playground.proposal_draft import ProposalDocumentV2, _is_internal_http_url

from .proposal_models import ProposalContentJudgeDiagnostics, ProposalContentJudgeScores


_RUBRIC_PROMPT = """당신은 조직 내부 결재용 한국어 기안의 품질 심사자입니다.
정답 기안의 문구를 흉내 냈는지가 아니라 사용자 요청, 참고 근거, 현재 기안만으로 결재 가능한 초안인지 평가하세요.
근거에 없는 구체적 사실·수치·효과는 감점하고, 확인 불가능한 값을 솔직하게 누락 또는 확인 필요로 둔 것은 허위 작성보다
높게 평가하세요. 문장이 근거와 글자 그대로 같을 필요는 없지만 의미가 뒷받침되어야 합니다.

다음 일곱 항목을 독립적으로 평가하세요.
1. grounded_accuracy (0~12): 모든 주요 주장·수치·고유명사가 근거 또는 사용자 요청과 일치하고 서로 모순되지 않는가.
   12=중요 주장 전부 근거 있음, 6=경미한 과장·불명확성, 0=핵심 사실의 환각·중대한 모순.
2. decision_completeness (0~9): 결재자가 승인 여부를 판단하는 데 필요한 대상·범위·금액·일정·요청사항이 근거 범위에서
   충분한가. 유형상 필요하지 않은 항목은 요구하지 마세요. 9=필수 정보 충족, 4.5=일부 핵심 누락, 0=결정 불가능.
3. purpose_and_necessity (0~7): 목적과 배경·필요성이 근거에 맞게 명확하며 요청 행동과 연결되는가.
4. actionability_and_feasibility (0~6): 수행 내용, 담당·대상, 일정·예산 등 확인 가능한 실행 정보가 구체적이고 현실적인가.
5. logical_structure (0~5): 제목, 승인 요청, 본문 섹션의 순서와 논리 흐름이 일관되고 중복·충돌이 없는가.
6. business_writing (0~4): 결재 문서에 적합한 객관적이고 정중한 한국어이며 승인 요청이 명확한가.
7. conciseness_and_readability (0~2): 불필요한 반복 없이 빠르게 읽히고 표·목록·문단 선택이 적절한가.

추가로 중요한 근거 없는 주장 수, 필수 의사결정 정보 누락 수, 내부 모순 수와 전체 판정 신뢰도(0~1)를 세세요.
점수는 허용 범위의 숫자로 반환하세요. 반환값은 마크다운 없는 JSON 객체 하나이며 아래 키 외에는 금지합니다.
schema_version은 proposal-content-judge-v1이어야 합니다.
scores에는 grounded_accuracy, decision_completeness, purpose_and_necessity, actionability_and_feasibility,
logical_structure, business_writing, conciseness_and_readability만 넣으세요.
최상위에는 unsupported_material_claim_count, missing_critical_item_count, contradiction_count, confidence를 넣으세요.
자유서술 평가·원문 인용·개인정보·파일명은 반환하지 마세요."""


class _JudgeResponse(BaseModel):
    """Ollama 응답에서 허용하는 최소 숫자 판정 계약."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    scores: ProposalContentJudgeScores
    unsupported_material_claim_count: int = Field(ge=0)
    missing_critical_item_count: int = Field(ge=0)
    contradiction_count: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)


class ProposalContentJudge(Protocol):
    """live runner가 사용하는 내용 judge 최소 계약."""

    @property
    def model(self) -> str: ...

    @property
    def config_fingerprint(self) -> str: ...

    def judge(
        self,
        instruction: str,
        document: ProposalDocumentV2,
        evidence: Sequence[tuple[str, str]],
        *,
        proposal_type: str,
    ) -> ProposalContentJudgeDiagnostics: ...

    def close(self) -> None: ...


class LocalProposalContentJudge:
    """고정 루브릭과 temperature 0으로 Ollama judge를 실행한다."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        num_ctx: int = 32_768,
        max_tokens: int = 1_024,
        timeout_ms: int = 180_000,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._model = model.strip()
        self._num_ctx = num_ctx
        self._max_tokens = max_tokens
        self._timeout_ms = timeout_ms
        self._client = client or httpx.Client(timeout=max(1.0, timeout_ms / 1000))

    @property
    def model(self) -> str:
        return self._model

    @property
    def config_fingerprint(self) -> str:
        import hashlib

        payload = json.dumps(
            {
                "rubric": "proposal-content-rubric-v1",
                "model": self._model,
                "num_ctx": self._num_ctx,
                "max_tokens": self._max_tokens,
                "temperature": 0,
                "think": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def close(self) -> None:
        self._client.close()

    def _failure(self, code: str, started: float) -> ProposalContentJudgeDiagnostics:
        return ProposalContentJudgeDiagnostics(
            status="failed",
            judge_model=self._model or None,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            failure_code=code,
        )

    def _evidence_text(
        self,
        instruction: str,
        document_json: str,
        evidence: Sequence[tuple[str, str]],
    ) -> str | None:
        fixed_chars = len(_RUBRIC_PROMPT) + len(instruction) + len(document_json) + 1_000
        available_chars = max(0, (self._num_ctx - self._max_tokens - 1_024) * 3 - fixed_chars)
        if available_chars < 256 or not evidence:
            return None
        per_source = max(256, min(12_000, available_chars // len(evidence)))
        parts: list[str] = []
        used = 0
        for citation_id, text in evidence:
            compact = " ".join(text.split())[:per_source]
            entry = f"[{citation_id}] {compact}"
            if used + len(entry) > available_chars:
                remaining = available_chars - used
                if remaining >= 256:
                    parts.append(entry[:remaining])
                break
            parts.append(entry)
            used += len(entry)
        return "\n".join(parts) if parts else None

    def judge(
        self,
        instruction: str,
        document: ProposalDocumentV2,
        evidence: Sequence[tuple[str, str]],
        *,
        proposal_type: str,
    ) -> ProposalContentJudgeDiagnostics:
        """judge 오류를 생성 실패로 전파하지 않고 숫자 진단 실패로 격리한다."""

        started = time.perf_counter()
        if not self._base_url or not self._model:
            return self._failure("not_configured", started)
        if not _is_internal_http_url(self._base_url):
            return self._failure("invalid_endpoint", started)
        document_json = document.model_dump_json(exclude_none=True)
        evidence_text = self._evidence_text(instruction, document_json, evidence)
        if evidence_text is None:
            return self._failure("context_limit", started)
        user_prompt = (
            f"기안 유형: {proposal_type}\n사용자 요청:\n{instruction.strip()}\n\n"
            f"평가할 기안:\n{document_json}\n\n참고 근거:\n{evidence_text}"
        )
        repair = ""
        for attempt in range(2):
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _RUBRIC_PROMPT + repair},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "think": False,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "num_ctx": self._num_ctx,
                    "num_predict": self._max_tokens,
                },
            }
            try:
                response = self._client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
            except httpx.HTTPError:
                return self._failure("http_error", started)
            try:
                raw_content = response.json()["message"]["content"]
                decoded = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                verdict = _JudgeResponse.model_validate(decoded)
                if verdict.schema_version != "proposal-content-judge-v1":
                    raise ValueError("judge schema version mismatch")
                return ProposalContentJudgeDiagnostics(
                    status="completed",
                    judge_model=self._model,
                    scores=verdict.scores,
                    unsupported_material_claim_count=verdict.unsupported_material_claim_count,
                    missing_critical_item_count=verdict.missing_critical_item_count,
                    contradiction_count=verdict.contradiction_count,
                    confidence=verdict.confidence,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError):
                if attempt == 0:
                    repair = " 이전 응답이 JSON 숫자 계약을 위반했습니다. 허용된 키와 범위만 사용해 다시 반환하세요."
                    continue
                return self._failure("invalid_response", started)
        return self._failure("invalid_response", started)
