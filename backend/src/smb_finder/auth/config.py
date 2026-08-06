"""기존 조회 adapter와 분리된 인증 PostgreSQL 설정."""

from __future__ import annotations

import math
import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from smb_finder.config import _REPOSITORY_ENV_FILE


class AuthSettings(BaseSettings):
    """인증 저장소와 session cookie 설정.

    운영은 별도 쓰기 계정을 사용한다. 합성 데이터 로컬 환경에서 기존 계정에 인증 schema 쓰기 권한을
    명시적으로 부여한 경우에만 opt-in 설정으로 자격증명을 재사용할 수 있다.
    """

    model_config = SettingsConfigDict(env_file=_REPOSITORY_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    auth_db_enabled: bool = True
    auth_db_host: str = ""
    auth_db_port: int | None = Field(default=None, ge=1, le=65535)
    auth_db_name: str = ""
    auth_db_user: str = ""
    auth_db_password: str = ""
    auth_db_use_llmops_credentials: bool = False
    auth_db_schema: str = "auth"
    auth_db_connect_timeout_ms: int = Field(default=1000, ge=100, le=10_000)
    auth_db_query_timeout_ms: int = Field(default=2000, ge=100, le=10_000)

    # 주소/DB는 같은 server를 재사용할 수 있고, 자격증명 재사용은 별도 opt-in 없이는 금지한다.
    llmops_db_host: str = ""
    postgres_host: str = ""
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_user: str = ""
    postgres_password: str = ""
    llmops_postgres_db: str = ""
    dev_server: str = ""

    auth_session_cookie_name: str = "automation_smb_session"
    auth_session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    auth_cookie_secure: bool = False

    auth_initial_admin_username: str = "admin"
    auth_initial_admin_password: str = ""
    auth_initial_admin_email: str = ""
    auth_initial_admin_display_name: str = "최고 관리자"

    @property
    def effective_host(self) -> str:
        """인증 전용 host를 우선하고 기존 server 주소만 fallback한다."""

        if self.auth_db_host.strip():
            return self.auth_db_host.strip()
        if self.llmops_db_host.strip():
            return self.llmops_db_host.strip()
        if os.name == "nt" and self.dev_server.strip():
            return self.dev_server.strip()
        return (self.postgres_host or self.dev_server).strip()

    @property
    def effective_port(self) -> int:
        """인증 전용 port가 있으면 우선한다."""

        return self.auth_db_port or self.postgres_port

    @property
    def effective_database(self) -> str:
        """인증 전용 DB를 우선하고 같은 server DB를 선택적으로 허용한다."""

        return (self.auth_db_name or self.llmops_postgres_db).strip()

    @property
    def effective_user(self) -> str:
        """인증 전용 계정 또는 명시적으로 허용한 로컬 계정을 선택한다."""

        if self.auth_db_user.strip():
            return self.auth_db_user.strip()
        if self.auth_db_use_llmops_credentials:
            return self.postgres_user.strip()
        return ""

    @property
    def effective_password(self) -> str:
        """인증 전용 비밀번호 또는 명시적으로 허용한 로컬 비밀번호를 선택한다."""

        if self.auth_db_password:
            return self.auth_db_password
        if self.auth_db_use_llmops_credentials:
            return self.postgres_password
        return ""

    @property
    def configured(self) -> bool:
        """민감 값을 반환하지 않고 쓰기 가능한 별도 계정 구성 여부만 확인한다."""

        return bool(
            self.auth_db_enabled
            and self.effective_host
            and self.effective_database
            and self.effective_user
            and self.effective_password
        )

    def connection_kwargs(self) -> dict[str, object]:
        """PostgreSQL 연결 인자를 반환한다."""

        return {
            "host": self.effective_host,
            "port": self.effective_port,
            "dbname": self.effective_database,
            "user": self.effective_user,
            "password": self.effective_password,
            "connect_timeout": max(1, math.ceil(self.auth_db_connect_timeout_ms / 1000)),
            "application_name": "automation_smb_auth",
            "options": f"-c statement_timeout={self.auth_db_query_timeout_ms}",
        }
