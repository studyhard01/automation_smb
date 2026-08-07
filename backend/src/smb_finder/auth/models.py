"""인증·사용자 관리 API의 공개 계약."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class RegisterRequest(BaseModel):
    """공개 회원가입 요청."""

    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=256)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        candidate = value.strip()
        if not USERNAME_PATTERN.fullmatch(candidate):
            raise ValueError("사용자 이름은 영문자 또는 숫자로 시작하고 영문자·숫자·._-만 사용할 수 있습니다.")
        return candidate

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        candidate = value.strip().casefold()
        if not EMAIL_PATTERN.fullmatch(candidate):
            raise ValueError("올바른 이메일 형식이 아닙니다.")
        return candidate


class LoginRequest(BaseModel):
    """사용자 이름과 비밀번호 로그인 요청."""

    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SeeLisLoginRequest(BaseModel):
    """SeeLIS 계정 로그인 요청. 비밀번호는 정규화하지 않는다."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(alias="userId", min_length=1, max_length=64)
    password: str = Field(alias="pswd", min_length=1, max_length=256)

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("SeeLIS 사용자 ID 형식이 올바르지 않습니다.")
        return value


class ServiceSummary(BaseModel):
    """관리 화면에 공개하는 서비스 항목."""

    key: str
    name: str
    description: str = ""
    is_active: bool = True


class UserResponse(BaseModel):
    """비밀번호·session 식별자를 제외한 사용자 정보."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: str | None = None
    display_name: str
    system_role: Literal["admin", "user"]
    is_superuser: bool
    is_active: bool
    all_services_access: bool
    service_keys: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


class UserEnvelope(BaseModel):
    """단일 사용자 응답."""

    user: UserResponse


class UsersEnvelope(BaseModel):
    """사용자 관리 목록과 할당 가능한 서비스."""

    users: list[UserResponse]
    services: list[ServiceSummary]


class UserUpdateRequest(BaseModel):
    """관리자가 변경할 수 있는 역할과 서비스 권한."""

    system_role: Literal["admin", "user"]
    is_superuser: bool
    is_active: bool
    all_services_access: bool
    service_keys: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("service_keys")
    @classmethod
    def normalize_service_keys(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip().casefold() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_superuser_role(self) -> UserUpdateRequest:
        if self.is_superuser and self.system_role != "admin":
            raise ValueError("최고 관리자는 admin 역할이어야 합니다.")
        return self


class AuthErrorBody(BaseModel):
    """Frontend가 안정적으로 분기할 수 있는 인증 오류."""

    code: str
    message: str
