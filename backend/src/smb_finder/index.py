"""폴더 인덱스 — 지연 최소화의 핵심.

폴더 트리를 한 번 순회해 메모리에 올려두고, 사용자 질의는 **인메모리로** 검색한다.
디스크 캐시(JSON)로 영속화해 재시작 시에도 즉시 검색 가능하게 한다.
실시간 SMB 순회는 인덱스가 비었을 때의 fallback으로만 쓴다.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .models import FolderHit
from .smb_client import FolderEntry

_logger = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """검색 정규화: 소문자 + 공백 제거. (한글은 그대로, 대소문자/공백만 무시)"""
    return _WS.sub("", text.lower())


class FolderIndex:
    """폴더 엔트리들의 인메모리 인덱스 + 빠른 검색."""

    def __init__(self, entries: list[FolderEntry] | None = None) -> None:
        self._entries: list[FolderEntry] = entries or []
        # 정규화된 폴더명을 미리 계산해 검색 시 반복 비용 제거
        self._norm_names: list[str] = [_norm(e.name) for e in self._entries]

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def is_empty(self) -> bool:
        return not self._entries

    @property
    def entries(self) -> list[FolderEntry]:
        """인덱싱된 폴더 엔트리 목록 (읽기용)."""
        return self._entries

    def replace(self, entries: list[FolderEntry]) -> None:
        """인덱스 내용을 통째로 교체한다 (재인덱싱)."""
        self._entries = entries
        self._norm_names = [_norm(e.name) for e in entries]

    def search(self, query: str, limit: int) -> list[FolderHit]:
        """질의와 관련된 폴더를 점수순으로 반환한다 (인메모리, < 수십 ms 목표).

        점수 규칙(단순·빠름):
          - 폴더명 == 질의           : 100
          - 폴더명이 질의로 시작      : 80
          - 폴더명이 질의를 포함      : 60
          - 질의가 폴더명을 포함      : 40
          - 그 외                    : 0 (제외)
        동점이면 얕은 폴더(depth 작은 것) 우선.
        """
        q = _norm(query)
        if not q:
            return []

        scored: list[tuple[float, FolderEntry]] = []
        for entry, name in zip(self._entries, self._norm_names):
            score = self._score(q, name)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda t: (-t[0], t[1].depth, len(t[1].name)))
        return [
            FolderHit(path=e.path, name=e.name, score=s, depth=e.depth)
            for s, e in scored[:limit]
        ]

    @staticmethod
    def _score(q: str, name: str) -> float:
        """정규화된 질의 q와 폴더명 name의 관련도."""
        if name == q:
            return 100.0
        if name.startswith(q):
            return 80.0
        if q in name:
            return 60.0
        if name in q:
            return 40.0
        return 0.0

    # ── 영속화 ──────────────────────────────────────────────
    def save(self, cache_path: str) -> None:
        """인덱스를 JSON으로 저장한다."""
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"path": e.path, "name": e.name, "depth": e.depth} for e in self._entries]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        _logger.info("인덱스 저장: %s (%d건)", cache_path, len(payload))

    @classmethod
    def load(cls, cache_path: str) -> "FolderIndex":
        """JSON 캐시에서 인덱스를 로드한다. 없으면 빈 인덱스."""
        path = Path(cache_path)
        if not path.exists():
            return cls([])
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [FolderEntry(path=d["path"], name=d["name"], depth=d["depth"]) for d in data]
        _logger.info("인덱스 로드: %s (%d건)", cache_path, len(entries))
        return cls(entries)
