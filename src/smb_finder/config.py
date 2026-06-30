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

    # ── L2 의도 해석 LLM (OpenAI 호환) ──
    llm_intent_enabled: bool = Field(default=False, description="모호한 질의를 LLM으로 정규화할지")
    llm_base_url: str = Field(default="http://localhost:8080", description="OpenAI 호환 LLM base_url")
    llm_model: str = Field(default="", description="LLM 모델명 (llama.cpp는 빈 값 가능)")
    llm_api_key: str = Field(default="", description="LLM API 키 (llama.cpp는 빈 값 가능)")
    llm_timeout_ms: int = Field(default=800, description="LLM 호출 timeout(ms)")

    @property
    def smb_root(self) -> str:
        r"""공유 루트 UNC 경로 (\\host\share)."""
        return rf"\\{self.smb_host}\{self.smb_share_name}"

    @property
    def exclude_set(self) -> set[str]:
        """인덱싱 제외 폴더명 집합 (소문자 정규화)."""
        return {x.strip().lower() for x in self.smb_index_exclude.split(",") if x.strip()}


def load_settings() -> Settings:
    """설정 인스턴스를 생성한다."""
    return Settings()
