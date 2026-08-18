"""내용 검색 오케스트레이터 — 질의를 토큰화해 FTS5 인덱스에서 검색한다.

폴더 찾기의 Finder에 대응하는, 파일 본문 검색용 진입 로직.
소요 시간을 측정해 응답에 싣고, 시간 예산 초과를 표시한다(지연 모니터링).
"""

from __future__ import annotations

import logging
import time

from . import intent
from .config import Settings
from .content_index import ContentIndex
from .models import ContentSearchRequest, ContentSearchResponse

_logger = logging.getLogger(__name__)


class ContentSearcher:
    """파일 내용 검색 오케스트레이터."""

    def __init__(self, index: ContentIndex, settings: Settings) -> None:
        self._index = index
        self._settings = settings

    def search(self, request: ContentSearchRequest) -> ContentSearchResponse:
        """자연어/키워드 질의를 처리해 본문이 매칭된 파일을 반환한다."""
        started = time.perf_counter()
        limit = request.limit or self._settings.content_default_limit

        # 규칙 기반 정규화로 군더더기('파일', '찾아줘' 등)를 떼고 토큰만 남긴다(LLM 불필요).
        normalized = intent.normalize_rule(request.query)
        terms = normalized.split()
        hits = self._index.search(terms, limit)

        elapsed_ms = (time.perf_counter() - started) * 1000
        over_budget = elapsed_ms > self._settings.content_search_budget_ms
        if over_budget:
            _logger.warning(
                "내용 검색 시간 예산 초과: %.1fms > %dms (query_len=%d, hits=%d)",
                elapsed_ms, self._settings.content_search_budget_ms, len(request.query), len(hits),
            )

        return ContentSearchResponse(
            query=request.query,
            terms=terms,
            hits=hits,
            result_count=len(hits),
            elapsed_ms=round(elapsed_ms, 1),
            over_budget=over_budget,
            indexed_files=self._index.count(),
        )
