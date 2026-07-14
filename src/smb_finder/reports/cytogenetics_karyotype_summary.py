"""선택한 LLM으로 ISCN 핵형분석요약을 생성하는 Playground tool."""

from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Any

from smb_finder.playground.models import ToolExecutionResult

from .models import CytogeneticsKaryotypeSummaryResponse

MAX_ISCN_LENGTH = 500
DEFAULT_LLM_BUDGET_MS = 30_000
InvokeJson = Callable[[list[dict[str, str]], int, str], dict[str, Any]]


def _error_result(code: str, message: str, *, input_length: int, started: float) -> ToolExecutionResult:
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return ToolExecutionResult(
        status="error",
        result_text=message,
        error_code=code,
        arguments_summary=f"iscn_len={input_length} elapsed_ms={elapsed_ms}",
    )


def _summary_messages(iscn: str) -> list[dict[str, str]]:
    """핵형 해석을 전적으로 LLM에 맡기는 JSON 응답 프롬프트를 만든다."""

    return [
        {
            "role": "system",
            "content": (
                "You are a cytogenetics laboratory assistant. Interpret the supplied synthetic-test ISCN karyotype "
                "notation and write a concise Korean factual karyotype analysis summary. Return only one JSON object "
                "with exactly this schema: {\"karyotype_summary\":\"핵형분석요약결과: ...\"}. Summarize chromosome "
                "count, sex-chromosome complement, clones, analyzed-cell counts, and abnormalities represented by the "
                "notation. Reproduce the complete ISCN notation exactly once and never translate, expand, or alter ISCN "
                "tokens such as der, t, del, dup, inv, p, or q. Do not infer a disease, diagnosis, prognosis, treatment, "
                "patient identity, or unstated fact. "
                "When the notation is ambiguous or incomplete, say so in the summary instead of inventing details."
            ),
        },
        {
            "role": "user",
            "content": (
                "Complete the summary task now. Do not echo the input as an `iscn` field. "
                "The only top-level JSON key must be `karyotype_summary`.\n"
                f"Input payload: {json.dumps({'iscn': iscn}, ensure_ascii=False)}\n"
                'Required output shape: {"karyotype_summary":"핵형분석요약결과: ..."}'
            ),
        },
    ]


def run_cytogenetics_karyotype_summary(
    args: dict[str, Any],
    *,
    invoke_json: InvokeJson | None = None,
    budget_ms: int = DEFAULT_LLM_BUDGET_MS,
) -> ToolExecutionResult:
    """선택한 provider/model의 LLM으로 ISCN 핵형분석요약결과를 생성한다."""

    started = time.perf_counter()
    iscn = str(args.get("iscn") or "").strip()
    if not iscn:
        return _error_result("empty_iscn", "ISCN 핵형 입력이 비어 있습니다.", input_length=0, started=started)
    if len(iscn) > MAX_ISCN_LENGTH:
        return _error_result(
            "iscn_too_long",
            f"ISCN 핵형 입력은 {MAX_ISCN_LENGTH}자 이하여야 합니다.",
            input_length=len(iscn),
            started=started,
        )
    if invoke_json is None:
        return _error_result(
            "llm_context_required",
            "핵형분석요약은 선택한 LLM 실행 정보가 필요합니다.",
            input_length=len(iscn),
            started=started,
        )

    try:
        response = invoke_json(_summary_messages(iscn), 500, "cytogenetics_karyotype_summary")
    except Exception:  # noqa: BLE001 - 원문이나 provider 응답을 API 오류에 노출하지 않는다.
        return _error_result(
            "karyotype_summary_llm_failed",
            "핵형분석요약 LLM 호출에 실패했습니다.",
            input_length=len(iscn),
            started=started,
        )

    summary = str(response.get("karyotype_summary") or "").strip()
    if not summary:
        return _error_result(
            "karyotype_summary_invalid_response",
            "핵형분석요약 LLM 응답에 결과가 없습니다.",
            input_length=len(iscn),
            started=started,
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    payload = CytogeneticsKaryotypeSummaryResponse(
        karyotype_summary=summary,
        elapsed_ms=elapsed_ms,
        over_budget=elapsed_ms > max(1, budget_ms),
    )
    return ToolExecutionResult(
        result_text=summary,
        observation_text=summary,
        result_payload=payload.model_dump(),
        arguments_summary=f"iscn_len={len(iscn)} llm_elapsed_ms={elapsed_ms}",
    )
