"""L2 의도 해석 — 자연어 명령 → 검색 키워드.

fast-path(규칙 기반)가 기본. 모호한 질의만 LLM으로 보낸다(설정 시).
지연이 생명이므로 LLM 경로는 timeout을 강제하고, 실패 시 규칙 기반 결과로 폴백한다.
"""

from __future__ import annotations

import logging
import re

import httpx

from .config import Settings

_logger = logging.getLogger(__name__)

# 폴더 찾기 명령에서 흔한 군더더기 — 제거 후 핵심어만 남긴다.
_FILLER = {
    "폴더", "디렉토리", "디렉터리", "파일", "찾아줘", "찾아", "찾기", "검색",
    "좀", "해줘", "알려줘", "보여줘", "줘", "어디", "어디야", "있어", "있나",
    "그", "저", "이", "의", "결과", "관련", "관련된",
}
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def normalize_rule(query: str) -> str:
    """규칙 기반 정규화: 군더더기 토큰을 제거하고 핵심어만 남긴다 (LLM 불필요)."""
    tokens = _TOKEN.findall(query)
    kept = [t for t in tokens if t not in _FILLER]
    return " ".join(kept) if kept else query.strip()


def normalize(query: str, settings: Settings) -> str:
    """질의를 검색 키워드로 정규화한다.

    1) 규칙 기반으로 먼저 줄인다.
    2) LLM이 켜져 있고 규칙 결과가 모호하면(여러 토큰이 남으면) LLM으로 보정.
       LLM 실패/timeout 시 규칙 결과를 그대로 쓴다.
    """
    rule = normalize_rule(query)
    if not settings.llm_intent_enabled:
        return rule
    if len(rule.split()) <= 1:
        # 충분히 명확 — LLM 태울 필요 없음(지연 절약)
        return rule
    refined = _refine_with_llm(query, settings)
    return refined or rule


def _refine_with_llm(query: str, settings: Settings) -> str | None:
    """OpenAI 호환 엔드포인트로 키워드 1개를 추출한다. 실패 시 None."""
    prompt = (
        "다음 명령에서 검색할 폴더의 핵심 키워드만 공백으로 구분해 한 줄로 출력해라. "
        f"설명 금지.\n명령: {query}"
    )
    payload = {
        "model": settings.llm_model or "local",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32,
        "temperature": 0.0,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    url = settings.llm_base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    url += "/chat/completions"

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=settings.llm_timeout_ms / 1000)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        return content.splitlines()[0].strip() if content else None
    except Exception as e:  # noqa: BLE001 — 지연 사수: LLM 실패는 폴백으로 흡수
        _logger.warning("LLM 의도 해석 실패 (규칙 폴백): %s", e)
        return None
