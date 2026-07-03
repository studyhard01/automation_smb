"""입출력 데이터 모델 (Pydantic).

이 모델들이 L4 도구의 API 계약이자, L2/L3(자체 디스패처·Dify)에 등록되는 시그니처다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _normalize_relative_path(value: str) -> str:
    """공유 루트 기준 상대 경로만 허용하도록 정규화한다."""
    path = (value or "").strip().replace("\\", "/").strip("/")
    if not path:
        return ""
    if ":" in path or path.startswith("//"):
        raise ValueError("path는 공유 루트 기준 상대 경로만 허용합니다")
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("path에는 현재/상위 경로 참조를 사용할 수 없습니다")
    return "/".join(parts)


def _normalize_smb_name(value: str, field_name: str) -> str:
    """host/share override에 UNC 조각이나 경로 구분자가 섞이지 않게 제한한다."""
    text = (value or "").strip()
    if not text:
        return ""
    if any(ch in text for ch in "\\/\r\n\t"):
        raise ValueError(f"{field_name}에는 경로 구분자나 제어 문자를 사용할 수 없습니다")
    if field_name == "share_name" and text in {".", ".."}:
        raise ValueError("share_name에는 현재/상위 경로 참조를 사용할 수 없습니다")
    return text


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
    result_count: int = Field(default=0, description="반환된 폴더 결과 수")
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

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalize_relative_path(value)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return _normalize_smb_name(value, "host")

    @field_validator("share_name")
    @classmethod
    def validate_share_name(cls, value: str) -> str:
        return _normalize_smb_name(value, "share_name")


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
    result_count: int = Field(default=0, description="반환된 파일 결과 수")
    elapsed_ms: float = Field(description="처리 소요 시간(ms) — 지연 모니터링용")
    over_budget: bool = Field(default=False, description="시간 예산 초과 여부")
    indexed_files: int = Field(default=0, description="현재 내용 인덱스에 적재된 파일 수")


class RefreshIndexResponse(BaseModel):
    """폴더 인덱스 갱신 응답."""

    folders: int = Field(description="갱신 후 인덱스에 적재된 폴더 수")
    indexed_folders: int = Field(description="갱신 후 인덱스에 적재된 폴더 수")
    elapsed_ms: float = Field(description="처리 소요 시간(ms)")
    over_budget: bool = Field(default=False, description="시간 예산 초과 여부")
    cache_path: str = Field(description="폴더 인덱스 캐시 파일 경로 표시값. 절대경로는 파일명만 남겨 축약")


class RefreshContentResponse(BaseModel):
    """내용 인덱스 갱신 응답."""

    path: str = Field(default="", description="색인 대상 상대 경로. 비어 있으면 공유 전체")
    target: str = Field(description="대상 공유 표시. 내부 host/share 값은 노출하지 않고 configured/override 여부만 표시")
    host: str = Field(description="configured 또는 override")
    share: str = Field(description="configured 또는 override")
    host_override_used: bool = Field(default=False, description="요청 host override 사용 여부")
    share_override_used: bool = Field(default=False, description="요청 share_name override 사용 여부")
    indexed: int = Field(description="새로 적재한 파일 수")
    unsupported: int = Field(description="지원하지 않는 형식으로 건너뛴 파일 수")
    empty: int = Field(description="본문이 비어 건너뛴 파일 수")
    errors: int = Field(description="처리 오류로 건너뛴 파일 수")
    skipped_unchanged: int = Field(default=0, description="size/mtime이 같아 본문 읽기를 건너뛴 파일 수")
    removed_stale: int = Field(default=0, description="이번 범위에서 더 이상 발견되지 않아 삭제한 기존 인덱스 수")
    elapsed_sec: float = Field(description="처리 소요 시간(초)")
    elapsed_ms: float = Field(description="처리 소요 시간(ms)")
    over_budget: bool = Field(default=False, description="시간 예산 초과 여부")
    indexed_files: int = Field(default=0, description="갱신 후 내용 인덱스에 적재된 전체 파일 수")
    db_path: str = Field(description="내용 인덱스 SQLite DB 경로 표시값. 절대경로는 파일명만 남겨 축약")


class ContentIndexJobCreateResponse(BaseModel):
    """내용 인덱싱 job 생성 응답."""

    job_id: str = Field(description="job 식별자")
    status: str = Field(description="queued 또는 running")
    status_url: str = Field(description="job 상태 조회 경로")


class ContentIndexJobStatusResponse(BaseModel):
    """내용 인덱싱 job 상태 응답."""

    job_id: str = Field(description="job 식별자")
    status: str = Field(description="queued | running | succeeded | failed")
    path: str = Field(default="", description="색인 대상 상대 경로. 비어 있으면 공유 전체")
    target: str = Field(default="configured", description="configured 또는 override")
    host_override_used: bool = Field(default=False, description="요청 host override 사용 여부")
    share_override_used: bool = Field(default=False, description="요청 share_name override 사용 여부")
    created_at: float = Field(description="job 생성 시각(epoch)")
    started_at: float | None = Field(default=None, description="job 시작 시각(epoch)")
    finished_at: float | None = Field(default=None, description="job 종료 시각(epoch)")
    elapsed_ms: float = Field(default=0.0, description="job 실행 소요 시간(ms)")
    indexed: int = Field(default=0, description="새로 적재한 파일 수")
    unsupported: int = Field(default=0, description="지원하지 않는 형식으로 건너뛴 파일 수")
    empty: int = Field(default=0, description="본문이 비어 건너뛴 파일 수")
    errors: int = Field(default=0, description="처리 오류로 건너뛴 파일 수")
    skipped_unchanged: int = Field(default=0, description="size/mtime이 같아 본문 읽기를 건너뛴 파일 수")
    removed_stale: int = Field(default=0, description="이번 범위에서 더 이상 발견되지 않아 삭제한 기존 인덱스 수")
    over_budget: bool = Field(default=False, description="시간 예산 초과 여부")
    error_code: str = Field(default="", description="실패 코드")
    message: str = Field(default="", description="안전한 상태 메시지")


class HealthResponse(BaseModel):
    """서비스 상태 응답."""

    status: str = Field(description="ok 또는 degraded")
    ready: bool = Field(description="핵심 검색 상태가 준비되었는지 여부")
    indexed_folders: int = Field(description="로드된 폴더 인덱스 수")
    indexed_files: int = Field(description="로드된 내용 인덱스 파일 수")
    folder_index_loaded: bool = Field(description="폴더 인덱스 객체 로드 여부")
    content_index_loaded: bool = Field(description="내용 인덱스 객체 로드 여부")
    cache_path: str = Field(description="폴더 인덱스 캐시 파일 경로 표시값. 절대경로는 파일명만 남겨 축약")
    db_path: str = Field(description="내용 인덱스 SQLite DB 경로 표시값. 절대경로는 파일명만 남겨 축약")


class ApiErrorResponse(BaseModel):
    """공통 오류 응답 형태."""

    code: str = Field(description="기계가 읽는 오류 코드")
    message: str = Field(description="사용자/운영자가 볼 수 있는 안전한 오류 메시지")
    retryable: bool = Field(default=False, description="같은 요청을 재시도할 가치가 있는지")
    elapsed_ms: float = Field(default=0.0, description="실패까지 걸린 시간(ms). 알 수 없으면 0")
