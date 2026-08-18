"""LangGraph 외피 설정 로더 — .env.studio에서 smb-finder 주소·LLM·예산을 읽는다.

smb_finder 서비스의 config.py와 **같은 env 키 이름**(LLM_BASE_URL/LLM_MODEL/LLM_API_KEY)을
재사용한다. 비밀정보(자격증명·내부 IP·API 키)는 코드에 하드코딩하지 않고 .env에서만 주입한다.
(CLAUDE.md 보안 규칙)

보안: 이 외피는 **사내 localhost의 smb-finder + 온프레미스 LLM만** 호출하도록 기본값이 잡혀 있다.
외부 호스트로 바꾸지 말 것. LangSmith 트레이싱(클라우드 업로드)은 .env.studio에서 꺼 둔다.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _is_internal_http_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip().lower()
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host
    return ip.is_private or ip.is_loopback or ip.is_link_local


class AgentSettings(BaseSettings):
    """LangGraph 에이전트 외피 설정 (환경변수 기반)."""

    model_config = SettingsConfigDict(env_file=".env.studio", env_file_encoding="utf-8", extra="ignore")

    # ── 사내 smb-finder 서비스 (진짜 검색 로직은 전부 여기) ──
    smb_finder_url: str = Field(
        default="http://localhost:8010",
        description="사내 smb-finder base URL. 보안상 localhost 등 사내망만 사용.",
    )
    tool_timeout_ms: int = Field(
        default=1500, description="smb-finder 호출 timeout(ms). 초과 시 빈 결과로 즉시 반환(지연 사수)."
    )
    find_limit: int = Field(default=5, description="폴더 찾기 기본 반환 수")
    content_limit: int = Field(default=10, description="내용 검색 기본 반환 수")
    admin_api_token: str = Field(
        default="",
        description="smb-finder /admin API token. Empty keeps the legacy /refresh-content fallback.",
    )
    content_index_poll_interval_ms: int = Field(default=1000, description="Content index job status poll interval.")
    content_index_poll_timeout_ms: int = Field(default=5000, description="Maximum time spent polling a content job.")

    # ── 온프레미스 LLM (OpenAI 호환) — smb_finder와 동일 키 재사용 ──
    llm_base_url: str = Field(
        default="http://localhost:8080/v1",
        description="OpenAI 호환 LLM base_url(온프레미스). llama.cpp 등. 외부 API 금지.",
    )
    llm_model: str = Field(default="local-model", description="LLM 모델명")
    llm_api_key: str = Field(default="local-no-key", description="LLM API 키 (llama.cpp 등은 임의값 가능)")
    llm_timeout_ms: int = Field(default=20_000, description="에이전트 LLM 호출 timeout(ms)")
    llm_temperature: float = Field(default=0.0, description="에이전트 LLM temperature")

    @field_validator("smb_finder_url", "llm_base_url")
    @classmethod
    def validate_internal_url(cls, value: str) -> str:
        if not _is_internal_http_url(value):
            raise ValueError("LangGraph URLs must point to localhost or an internal network host")
        return value.rstrip("/")


def load_settings() -> AgentSettings:
    """설정 인스턴스를 생성한다."""
    return AgentSettings()
