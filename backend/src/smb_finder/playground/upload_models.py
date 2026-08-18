"""Playground 설정과 SMB 파일 첨부 API의 공개 모델."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UploadSettingsView(BaseModel):
    """비밀정보와 실제 SMB 주소를 제외한 업로드 설정."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    configured: bool
    relative_directory: str
    destination_label: str
    max_size_bytes: int = Field(ge=1)
    allowed_extensions: list[str]


class ProposalDraftSettingsView(BaseModel):
    """기안 초안의 향후 공유폴더 저장 위치에 대한 공개 설정."""

    model_config = ConfigDict(extra="forbid")

    relative_directory: str
    destination_label: str


class PlaygroundSettingsResponse(BaseModel):
    """설정 화면에서 안전하게 표시할 수 있는 전체 설정."""

    model_config = ConfigDict(extra="forbid")

    upload: UploadSettingsView
    proposal_draft: ProposalDraftSettingsView
    local_llm_configured: bool


class UploadSettingsPatch(BaseModel):
    """사용자가 변경할 수 있는 비밀이 아닌 업로드 경로 설정."""

    model_config = ConfigDict(extra="forbid")

    relative_directory: str = Field(min_length=1, max_length=240)


class ProposalDraftSettingsPatch(BaseModel):
    """사용자가 변경할 수 있는 기안 초안 저장 상대 경로 설정."""

    model_config = ConfigDict(extra="forbid")

    relative_directory: str = Field(min_length=1, max_length=240)


class UploadedFileSelection(BaseModel):
    """업로드 직후 대화 참고 파일에 넣을 수 있는 공개 식별자."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["upload"] = "upload"
    doc_id: UUID
    revision_id: UUID
    file_name: str
    title: str
    extension: str = ""
    size_bytes: int = Field(ge=1)
    score: float = 1.0
    match_source: Literal["content"] = "content"


class FileUploadResponse(BaseModel):
    """SMB 업로드 결과. 실제 경로는 노출하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    file_name: str
    size_bytes: int = Field(ge=1)
    uploaded_at: datetime
    destination_label: str
    indexed: bool = False
    conversation_ready: bool = False
    selected_file: UploadedFileSelection | None = None
