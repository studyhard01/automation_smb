"""오케스트레이터 — 질의를 받아 정규화 → 인덱스 검색 → 응답을 만든다.

이 계층이 L3①(자체 얇은 디스패처)의 폴더찾기 경로다.
소요 시간을 측정해 응답에 싣고, 시간 예산 초과를 표시한다(지연 모니터링).
"""

from __future__ import annotations

import logging
import time

from . import intent
from .config import Settings
from .index import FolderIndex
from .models import FindRequest, FindResponse

_logger = logging.getLogger(__name__)


class Finder:
    """폴더 찾기 오케스트레이터."""

    def __init__(self, index: FolderIndex, settings: Settings) -> None:
        self._index = index
        self._settings = settings

    def find(self, request: FindRequest) -> FindResponse:
        """자연어 질의를 처리해 관련 폴더를 반환한다."""
        started = time.perf_counter()
        limit = request.limit or self._settings.find_default_limit

        normalized = intent.normalize(request.query, self._settings)
        hits = self._index.search(normalized, limit)

        elapsed_ms = (time.perf_counter() - started) * 1000
        over_budget = elapsed_ms > self._settings.find_budget_ms
        if over_budget:
            _logger.warning(
                "시간 예산 초과: %.1fms > %dms (query=%r)",
                elapsed_ms, self._settings.find_budget_ms, request.query,
            )

        return FindResponse(
            query=request.query,
            normalized_query=normalized,
            hits=hits,
            elapsed_ms=round(elapsed_ms, 1),
            source="index",
            over_budget=over_budget,
        )
