"""Langflow 워크플로우 생성용 데이터 모델."""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

DEFAULT_SERVICE_URL = "http://localhost:8010"
WorkflowTemplateId = Literal["folder_search"]
LlmProvider = Literal["auto", "rule", "local", "openai"]


def _allowed_hosts_from_env() -> set[str]:
    raw = os.getenv("LANGFLOW_COMPOSER_ALLOWED_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def is_internal_http_url(value: str) -> bool:
    """외부 전송을 막기 위해 localhost, 사설 IP, 명시 allowlist만 허용한다."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False

    host = parsed.hostname.strip().lower()
    allowed_hosts = _allowed_hosts_from_env()
    if host in {"localhost", "host.docker.internal"} or host in allowed_hosts:
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 사내 단일 라벨 DNS(smb-finder 등)는 허용하고, public FQDN은 기본 차단한다.
        return "." not in host
    return ip.is_private or ip.is_loopback or ip.is_link_local


class WorkflowTemplate(BaseModel):
    """지원하는 워크플로우 템플릿 정의."""

    template_id: WorkflowTemplateId
    display_name: str
    description: str
    component_names: list[str]


class WorkflowSpec(BaseModel):
    """사용자 요청을 워크플로우 템플릿 파라미터로 정규화한 결과."""

    template_id: WorkflowTemplateId = "folder_search"
    service_url: str = DEFAULT_SERVICE_URL
    limit: int = Field(default=5, ge=1, le=50)
    timeout_ms: int = Field(default=1500, ge=100, le=10000)
    planner: str = "rule"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    supported: bool = True
    unsupported_reason: str = ""
    warnings: list[str] = Field(default_factory=list)

    @field_validator("service_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        """URL 안에 자격증명이 들어가지 않게 하고 HTTP(S)만 허용한다."""
        if not is_internal_http_url(value):
            raise ValueError("service_url must be localhost, a private network URL, or an allowed internal host")
        return value.rstrip("/")


class WorkflowGenerationResult(BaseModel):
    """생성된 Langflow JSON과 생성 메타데이터."""

    template_id: WorkflowTemplateId
    provider_used: str
    flow: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


FOLDER_SEARCH_TEMPLATE = WorkflowTemplate(
    template_id="folder_search",
    display_name="SMB 공유폴더 찾기",
    description="사용자가 입력한 자연어 질의를 기존 SMBFolderFinder 컴포넌트의 /find 호출로 연결한다.",
    component_names=["ChatInput", "SMBFolderFinder", "ChatOutput"],
)

SUPPORTED_TEMPLATES: dict[str, WorkflowTemplate] = {
    FOLDER_SEARCH_TEMPLATE.template_id: FOLDER_SEARCH_TEMPLATE,
}
