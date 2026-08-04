"""Playground PDF/Markdown 첨부파일의 로컬 임시 저장소."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from smb_finder.extract import extract_text

from .models import AttachmentMetadata, ExtractedDocument

_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ALLOWED_EXTENSIONS = {".md": "text/markdown", ".markdown": "text/markdown", ".pdf": "application/pdf"}


class AttachmentStoreError(ValueError):
    """첨부파일 검증·저장·조회 오류."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AttachmentStore:
    """사용자 파일명을 저장 경로에 쓰지 않는 Git 제외 로컬 저장소."""

    def __init__(self, root: str | Path, *, max_bytes: int, max_text_chars: int):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_text_chars = max_text_chars

    def save(self, filename: str, data: bytes, media_type: str = "") -> AttachmentMetadata:
        """허용된 PDF/Markdown 한 건을 검증하고 임의 ID 경로에 저장한다."""

        safe_name = self._safe_filename(filename)
        extension = Path(safe_name).suffix.lower()
        if extension not in _ALLOWED_EXTENSIONS:
            raise AttachmentStoreError("attachment_type_not_allowed", "PDF 또는 Markdown 파일만 첨부할 수 있습니다.")
        if not data:
            raise AttachmentStoreError("attachment_empty", "빈 파일은 첨부할 수 없습니다.")
        if len(data) > self.max_bytes:
            raise AttachmentStoreError(
                "attachment_too_large",
                f"첨부파일은 {self.max_bytes // (1024 * 1024)}MB를 넘을 수 없습니다.",
            )
        if extension == ".pdf" and not data.startswith(b"%PDF-"):
            raise AttachmentStoreError("attachment_invalid_pdf", "PDF 시그니처를 확인할 수 없습니다.")

        attachment_id = uuid.uuid4().hex
        resolved_media_type = _ALLOWED_EXTENSIONS[extension]
        if media_type and media_type not in {resolved_media_type, "application/octet-stream"}:
            resolved_media_type = media_type
        metadata = AttachmentMetadata(
            id=attachment_id,
            filename=safe_name,
            media_type=resolved_media_type,
            extension=extension,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            created_at=datetime.now(UTC).isoformat(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        payload_path = self._payload_path(metadata)
        temporary_payload = payload_path.with_suffix(f"{extension}.tmp")
        temporary_payload.write_bytes(data)
        temporary_payload.replace(payload_path)
        metadata_path = self._metadata_path(attachment_id)
        temporary_metadata = metadata_path.with_suffix(".json.tmp")
        temporary_metadata.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
        temporary_metadata.replace(metadata_path)
        return metadata

    def get(self, attachment_id: str) -> AttachmentMetadata:
        """첨부 ID로 공개 메타데이터를 읽는다."""

        self._validate_id(attachment_id)
        try:
            metadata = AttachmentMetadata.model_validate_json(
                self._metadata_path(attachment_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise AttachmentStoreError("attachment_not_found", "첨부파일을 찾을 수 없습니다.") from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AttachmentStoreError("attachment_metadata_invalid", "첨부파일 메타데이터를 읽지 못했습니다.") from exc
        if metadata.id != attachment_id or metadata.extension not in _ALLOWED_EXTENSIONS:
            raise AttachmentStoreError("attachment_metadata_invalid", "첨부파일 메타데이터를 읽지 못했습니다.")
        return metadata

    def extract(self, attachment_id: str) -> ExtractedDocument:
        """첨부파일 바이트를 기존 로컬 추출기로 처리한다."""

        started = time.perf_counter()
        metadata = self.get(attachment_id)
        try:
            data = self._payload_path(metadata).read_bytes()
        except OSError as exc:
            raise AttachmentStoreError("attachment_read_failed", "첨부파일을 읽지 못했습니다.") from exc
        extracted = extract_text(metadata.filename, data, self.max_text_chars)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return ExtractedDocument(
            attachment=metadata,
            text=extracted.text,
            status=extracted.status,  # type: ignore[arg-type]
            detail=extracted.detail,
            text_chars=len(extracted.text),
            truncated=len(extracted.text) >= self.max_text_chars,
            elapsed_ms=elapsed_ms,
        )

    def delete(self, attachment_id: str) -> None:
        """지정한 로컬 첨부파일과 메타데이터를 삭제한다."""

        metadata = self.get(attachment_id)
        self._payload_path(metadata).unlink(missing_ok=True)
        self._metadata_path(attachment_id).unlink(missing_ok=True)

    def _payload_path(self, metadata: AttachmentMetadata) -> Path:
        return self.root / f"{metadata.id}{metadata.extension}"

    def _metadata_path(self, attachment_id: str) -> Path:
        return self.root / f"{attachment_id}.json"

    @staticmethod
    def _safe_filename(filename: str) -> str:
        normalized = str(filename or "").replace("\\", "/").split("/")[-1].strip()
        normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized)[:180]
        if not normalized or normalized in {".", ".."}:
            raise AttachmentStoreError("attachment_filename_invalid", "유효한 파일명이 필요합니다.")
        return normalized

    @staticmethod
    def _validate_id(attachment_id: str) -> None:
        if not _ATTACHMENT_ID_RE.fullmatch(str(attachment_id or "")):
            raise AttachmentStoreError("attachment_not_found", "첨부파일을 찾을 수 없습니다.")
