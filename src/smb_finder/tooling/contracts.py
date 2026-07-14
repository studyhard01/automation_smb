"""검색 도구가 REST·Playground·MCP에서 공유하는 안전한 입출력 계약."""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, Field, PrivateAttr, field_validator

_IPV4_CANDIDATE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


def _contains_ip_literal(value: str) -> bool:
    for candidate in _IPV4_CANDIDATE.findall(value):
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return True
    stripped = value.strip("[]() ")
    try:
        return ipaddress.ip_address(stripped).version == 6
    except ValueError:
        return False


def _safe_name(value: str) -> str:
    """결과명 자체에 내부 IP나 제어문자가 들어오지 않게 한다."""

    text = (value or "").strip()
    if not text or any(char in text for char in ":\r\n\t") or _contains_ip_literal(text):
        raise ValueError("안전한 결과명을 반환할 수 없습니다")
    return text


def _root_relative_path(value: str) -> str:
    """내부 호스트나 절대경로가 도구 응답으로 노출되지 않게 제한한다."""

    raw = (value or "").strip()
    if (
        not raw
        or raw.startswith(("/", "\\"))
        or ":" in raw
        or any(char in raw for char in "\r\n\t")
        or _contains_ip_literal(raw)
    ):
        raise ValueError("공유 루트 기준 상대 경로만 허용합니다")
    normalized = raw.replace("\\", "/")
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise ValueError("상대 경로에 현재/상위 경로 참조를 사용할 수 없습니다")
    return normalized


class FindFolderInput(BaseModel):
    """공유 폴더 검색 도구 입력."""

    query: str = Field(min_length=1, max_length=500, description="찾을 폴더의 이름 또는 자연어 단서")
    limit: int | None = Field(default=None, ge=1, le=20, description="반환할 최대 결과 수")

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query는 비어 있을 수 없습니다")
        return value


class SearchContentInput(BaseModel):
    """로컬 내용 인덱스 검색 도구 입력."""

    query: str = Field(min_length=1, max_length=500, description="찾을 파일 본문의 키워드 또는 자연어 단서")
    limit: int | None = Field(default=None, ge=1, le=20, description="반환할 최대 결과 수")

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query는 비어 있을 수 없습니다")
        return value


class FolderToolHit(BaseModel):
    """외부 전송 가능한 폴더 검색 결과 1건."""

    name: str
    path: str
    score: float = 0.0
    depth: int = 0

    _validate_name = field_validator("name")(_safe_name)
    _validate_path = field_validator("path")(_root_relative_path)


class FindFolderOutput(BaseModel):
    """원문 query와 SMB 연결 정보를 제외한 폴더 검색 결과."""

    hits: list[FolderToolHit] = Field(default_factory=list)
    result_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    over_budget: bool = False


class ContentToolHit(BaseModel):
    """본문 snippet과 파일 메타데이터를 제외한 내용 검색 결과 1건."""

    name: str
    path: str
    ext: str = Field(pattern=r"^\.[A-Za-z0-9]{1,16}$")
    score: float = 0.0

    _validate_name = field_validator("name")(_safe_name)
    _validate_path = field_validator("path")(_root_relative_path)


class SearchContentOutput(BaseModel):
    """원문 query와 본문 snippet을 제외한 내용 검색 결과."""

    hits: list[ContentToolHit] = Field(default_factory=list)
    result_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0)
    over_budget: bool = False
    _indexed_files: int = PrivateAttr(default=0)

    @property
    def indexed_files(self) -> int:
        """Playground 메시지 구분용 내부 상태이며 MCP 직렬화·schema에는 포함하지 않는다."""
        return self._indexed_files

    def set_indexed_files(self, value: int) -> None:
        """공통 실행기가 로컬 formatter에만 필요한 색인 건수를 주입한다."""
        self._indexed_files = max(0, int(value))
