"""입출력 데이터 모델 (Pydantic).

이 모델들이 L4 도구의 API 계약이자, L2/L3(자체 디스패처·Dify)에 등록되는 시그니처다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FindRequest(BaseModel):
    """폴더 찾기 요청 — 사용자의 자연어 명령 그대로 받는다."""

    query: str = Field(description="자연어 명령 또는 키워드 (예: 'OO검사 결과 폴더 찾아줘')")
    limit: int | None = Field(default=None, description="반환할 최대 폴더 수 (미지정 시 서버 기본값)")


class FolderHit(BaseModel):
    """검색된 폴더 1건."""

    path: str = Field(description="공유 루트 기준 폴더 경로")
    name: str = Field(description="폴더 이름 (말단)")
    score: float = Field(description="질의와의 관련도 점수 (높을수록 관련)")
    depth: int = Field(description="공유 루트로부터의 깊이")


class FindResponse(BaseModel):
    """폴더 찾기 응답."""

    query: str = Field(description="원본 질의")
    normalized_query: str = Field(description="검색에 실제 사용된 정규화 키워드")
    hits: list[FolderHit] = Field(default_factory=list, description="관련도순 폴더 목록")
    elapsed_ms: float = Field(description="처리 소요 시간(ms) — 지연 모니터링용")
    source: str = Field(description="결과 출처: 'index'(인메모리) | 'live'(실시간 fallback)")
    over_budget: bool = Field(default=False, description="시간 예산 초과 여부 (부분 결과일 수 있음)")
