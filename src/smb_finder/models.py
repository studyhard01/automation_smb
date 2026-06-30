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


# ── 내용 검색 (파일 본문) ─────────────────────────────────────


class ContentSearchRequest(BaseModel):
    """파일 내용 검색 요청 — 본문에 들어있는 키워드/자연어로 찾는다."""

    query: str = Field(description="찾을 내용 (예: 'BRCA1 변이 보고서', '음성 결과')")
    limit: int | None = Field(default=None, description="반환할 최대 파일 수 (미지정 시 서버 기본값)")


class RefreshContentRequest(BaseModel):
    """내용 인덱스 빌드 요청 — 인덱싱할 폴더 경로와 (선택) 접속 대상.

    보안: 자격증명(아이디/비밀번호)은 절대 요청으로 받지 않는다 — 항상 서버 .env에서만 온다.
    host/share_name만 선택적으로 덮어써 다른 공유폴더를 가리킬 수 있다(비우면 .env 기본값).
    """

    path: str = Field(
        default="",
        description="DB화할 폴더(공유 루트 기준 상대 경로, 예 '검사결과/2026/OO검사'). 비우면 공유 전체.",
    )
    host: str = Field(default="", description="SMB 호스트(IP). 비우면 .env의 SMB_HOST 사용.")
    share_name: str = Field(default="", description="공유폴더 이름. 비우면 .env의 SMB_SHARE_NAME 사용.")


class ContentHit(BaseModel):
    """내용이 매칭된 파일 1건."""

    path: str = Field(description="공유 루트 기준 파일 경로")
    name: str = Field(description="파일 이름")
    ext: str = Field(description="확장자 (점 포함, 예 '.xlsx')")
    size: int = Field(description="파일 크기(바이트)")
    mtime: float = Field(description="수정 시각 (epoch)")
    score: float = Field(description="관련도 점수 (높을수록 관련, bm25 기반)")
    snippet: str = Field(description="매칭 주변 본문 미리보기")


class ContentSearchResponse(BaseModel):
    """파일 내용 검색 응답."""

    query: str = Field(description="원본 질의")
    terms: list[str] = Field(default_factory=list, description="검색에 사용된 정규화 토큰")
    hits: list[ContentHit] = Field(default_factory=list, description="관련도순 파일 목록")
    elapsed_ms: float = Field(description="처리 소요 시간(ms) — 지연 모니터링용")
    over_budget: bool = Field(default=False, description="시간 예산 초과 여부")
    indexed_files: int = Field(default=0, description="현재 내용 인덱스에 적재된 파일 수")
