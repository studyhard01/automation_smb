"""설정 로더 — .env 환경변수에서 SMB·인덱스·LLM 설정을 읽는다.

비밀정보(자격증명·내부 IP)는 코드에 하드코딩하지 않고 .env에서만 주입한다.
(CLAUDE.md 보안 규칙)
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경변수 기반 서비스 설정."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── SMB 접속 ──
    smb_host: str = Field(default="", description="SMB 서버 IP")
    smb_share_name: str = Field(default="", description="SMB 공유 폴더 이름")
    smb_username: str = Field(default="", description="SMB 사용자명")
    smb_password: str = Field(default="", description="SMB 비밀번호")

    # ── 인덱스 (지연 최소화 핵심) ──
    smb_index_cache_path: str = Field(default=".cache/folder.index.json", description="인덱스 캐시 파일 경로")
    smb_index_max_depth: int = Field(default=4, description="인덱싱 최대 깊이 (0=무제한). 깊을수록 빌드가 급격히 느려짐")
    smb_index_build_budget_sec: int = Field(default=60, description="인덱스 빌드 시간 상한(초). 초과 시 부분 인덱스로 저장")
    smb_index_exclude: str = Field(default="#recycle,@eaDir,.DS_Store", description="인덱싱 제외 폴더명(쉼표 구분, 대소문자 무시)")
    smb_index_refresh_sec: int = Field(default=900, description="백그라운드 재인덱싱 주기(초), 0=비활성")

    # ── 시간 예산 ──
    find_budget_ms: int = Field(default=1500, description="이 시간을 넘기면 부분 결과라도 반환")
    find_default_limit: int = Field(default=5, description="기본 반환 폴더 수")

    # ── 내용 검색 인덱스 (FTS5, 파일 본문) ──
    content_index_db_path: str = Field(default=".cache/content.fts.db", description="내용 인덱스 SQLite(FTS5) 경로 — 환자 본문 포함, 외부 공유 금지")
    content_index_max_depth: int = Field(default=0, description="내용 인덱싱 최대 깊이 (0=무제한)")
    content_index_build_budget_sec: int = Field(default=300, description="내용 인덱스 빌드 시간 상한(초). 추출이 느려 폴더보다 넉넉")
    content_max_file_mb: float = Field(default=20.0, description="이보다 큰 파일은 내용 인덱싱 건너뜀(추출 비용·지연 방지)")
    content_max_chars_per_file: int = Field(default=200_000, description="파일당 저장 본문 상한(글자). 인덱스 비대화 방지")
    content_extensions: str = Field(
        default=".txt,.csv,.tsv,.md,.log,.json,.xml,.htm,.html,.docx,.xlsx,.pptx,.pdf",
        description="내용 인덱싱 대상 확장자(쉼표 구분). pdf는 pypdf 설치 시에만 본문 추출",
    )
    content_search_budget_ms: int = Field(default=1500, description="내용 검색 시간 예산(ms)")
    content_default_limit: int = Field(default=10, description="기본 반환 파일 수")

    # ── 관리자 API (무거운 인덱싱 작업) ──
    admin_api_token: str = Field(
        default="",
        description="관리자 전용 API 토큰. 비어 있으면 /admin/* 엔드포인트는 비활성화",
    )
    content_index_job_retention: int = Field(default=50, description="메모리에 보관할 최근 내용 인덱싱 job 수")
    smb_allowed_hosts: str = Field(
        default="",
        description="관리자 인덱싱에서 host override를 허용할 SMB 호스트 목록(쉼표 구분). 비우면 SMB_HOST만 허용",
    )
    smb_allowed_shares: str = Field(
        default="",
        description="관리자 인덱싱에서 share_name override를 허용할 공유명 목록(쉼표 구분). 비우면 SMB_SHARE_NAME만 허용",
    )

    # ── L2 의도 해석 LLM (OpenAI 호환) ──
    llm_intent_enabled: bool = Field(default=False, description="모호한 질의를 LLM으로 정규화할지")
    llm_base_url: str = Field(default="http://localhost:8080", description="OpenAI 호환 LLM base_url")
    llm_model: str = Field(default="", description="LLM 모델명 (llama.cpp는 빈 값 가능)")
    llm_api_key: str = Field(default="", description="LLM API 키 (llama.cpp는 빈 값 가능)")
    llm_timeout_ms: int = Field(default=10000, description="LLM 호출 timeout(ms)")
    openai_base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI API base_url")
    openai_model: str = Field(default="gpt-4.1-mini", description="Playground OpenAI 기본 모델명")
    openai_api_key: str = Field(default="", description="OpenAI API 키. UI 요청 값이 우선")

    # ── LangSmith 관측성 (기본 OFF, OpenAI provider 호출만 추적) ──
    langsmith_tracing: bool = Field(default=False, description="LangSmith tracing 활성화 여부")
    langsmith_api_key: str = Field(default="", description="LangSmith API key. .env에만 저장")
    langsmith_project: str = Field(default="automation-smb-playground", description="LangSmith project 이름")
    langsmith_endpoint: str = Field(default="", description="self-hosted LangSmith endpoint. 비우면 LangSmith Cloud")

    # ── Playground 제한형 agent/debug ──
    playground_agent_max_steps: int = Field(default=3, description="Playground agent 최대 판단 단계 수")
    playground_agent_max_tool_calls: int = Field(default=2, description="Playground agent 요청당 최대 tool 호출 수")
    playground_agent_budget_ms: int = Field(default=10000, description="Playground agent 전체 시간 예산(ms)")
    playground_agent_context_messages: int = Field(default=6, description="Playground agent에 전달할 최근 대화 수")
    playground_agent_result_chars: int = Field(default=2000, description="LLM observation에 전달할 tool 결과 최대 글자 수")
    playground_debug_raw_llm: bool = Field(default=False, description="테스트용 raw LLM debug 반환 허용")
    playground_debug_preview_chars: int = Field(default=4000, description="raw LLM debug preview 최대 글자 수")

    @property
    def smb_root(self) -> str:
        r"""공유 루트 UNC 경로 (\\host\share)."""
        return rf"\\{self.smb_host}\{self.smb_share_name}"

    @property
    def exclude_set(self) -> set[str]:
        """인덱싱 제외 폴더명 집합 (소문자 정규화)."""
        return {x.strip().lower() for x in self.smb_index_exclude.split(",") if x.strip()}

    @property
    def content_ext_set(self) -> set[str]:
        """내용 인덱싱 대상 확장자 집합 (소문자, 점 포함)."""
        return {x.strip().lower() for x in self.content_extensions.split(",") if x.strip()}

    @property
    def smb_allowed_host_set(self) -> set[str]:
        """관리자 override 허용 SMB 호스트 집합."""
        configured = {self.smb_host.strip().lower()} if self.smb_host.strip() else set()
        extra = {x.strip().lower() for x in self.smb_allowed_hosts.split(",") if x.strip()}
        return configured | extra

    @property
    def smb_allowed_share_set(self) -> set[str]:
        """관리자 override 허용 SMB 공유명 집합."""
        configured = {self.smb_share_name.strip().lower()} if self.smb_share_name.strip() else set()
        extra = {x.strip().lower() for x in self.smb_allowed_shares.split(",") if x.strip()}
        return configured | extra

    @property
    def content_max_file_bytes(self) -> int:
        """내용 인덱싱 파일 크기 상한(바이트)."""
        return int(self.content_max_file_mb * 1024 * 1024)


def load_settings() -> Settings:
    """설정 인스턴스를 생성한다."""
    return Settings()
