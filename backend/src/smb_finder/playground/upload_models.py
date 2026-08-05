"""Playground 설정과 SMB 파일 첨부 API의 공개 모델."""

from __future__ import annotations

from datetime import datetime

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


class PlaygroundSettingsResponse(BaseModel):
    """설정 화면에서 안전하게 표시할 수 있는 전체 설정."""

    model_config = ConfigDict(extra="forbid")

    upload: UploadSettingsView
    local_llm_configured: bool


class UploadSettingsPatch(BaseModel):
    """사용자가 변경할 수 있는 비밀이 아닌 업로드 경로 설정."""

    model_config = ConfigDict(extra="forbid")

    relative_directory: str = Field(min_length=1, max_length=240)


class FileUploadResponse(BaseModel):
    """SMB 업로드 결과. 실제 경로는 노출하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    file_name: str
    size_bytes: int = Field(ge=1)
    uploaded_at: datetime
    destination_label: str
    indexed: bool = False
