"""제한된 설정 저장과 SMB 파일 업로드 서비스."""

from __future__ import annotations

import errno
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO
from uuid import UUID

import smbclient

from smb_finder.config import Settings
from smb_finder.extract import SUPPORTED_EXTENSIONS, extract_text
from smb_finder.llmops_retrieval import ScopedRetrievalResult
from smb_finder.models import DocumentCitation, RetrievalMetadata, RetrievalScope, RetrievalScores

from .upload_models import (
    FileUploadResponse,
    PlaygroundSettingsResponse,
    ProposalDraftSettingsView,
    UploadedFileSelection,
    UploadSettingsView,
)

_logger = logging.getLogger(__name__)
_DESTINATION_LABEL = "관리 공유폴더"
_PROPOSAL_DESTINATION_LABEL = "기안 문서 폴더"
_CHUNK_SIZE = 64 * 1024
_UPLOAD_PREFIX = "[업로드] "
_INITIAL_UPLOAD_VERSION = "v1.0"
_KST = timezone(timedelta(hours=9))
_VERSIONED_UPLOAD_SUFFIX = re.compile(r"_\d{8}_v\d+(?:\.\d+)+$", re.IGNORECASE)
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _is_file_exists_error(exc: BaseException) -> bool:
    """smbclient의 일반 SMBOSError까지 동명 파일 충돌로 분류한다."""

    return isinstance(exc, FileExistsError) or (
        isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EEXIST
    )


@dataclass(frozen=True)
class UploadedFileRecord:
    """클라이언트 경로를 신뢰하지 않고 업로드 원본을 다시 찾기 위한 런타임 레코드."""

    doc_id: UUID
    revision_id: UUID
    file_name: str
    relative_directory: str
    size_bytes: int
    uploaded_at: datetime


class UploadError(RuntimeError):
    """클라이언트에 안전하게 반환할 수 있는 업로드 오류."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def normalize_relative_directory(value: str) -> str:
    """공유 루트 기준 상대 폴더만 허용하고 `/` 구분자로 정규화한다."""

    candidate = value.strip()
    if not candidate:
        raise ValueError("업로드 폴더를 입력해 주세요.")
    if candidate.startswith(("\\", "/")) or re.match(r"^[A-Za-z]:", candidate):
        raise ValueError("공유 루트 기준 상대 폴더만 입력할 수 있습니다.")
    if any(ord(char) < 32 for char in candidate):
        raise ValueError("업로드 폴더에 제어 문자를 사용할 수 없습니다.")

    normalized = candidate.replace("\\", "/")
    segments = normalized.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise ValueError("업로드 폴더에 빈 경로나 상위 경로를 사용할 수 없습니다.")
    if any(any(char in segment for char in '<>:"|?*') for segment in segments):
        raise ValueError("업로드 폴더에 사용할 수 없는 문자가 있습니다.")
    if any(segment != segment.strip() or segment.endswith(".") for segment in segments):
        raise ValueError("업로드 폴더의 앞뒤 공백 또는 마지막 점을 제거해 주세요.")
    if any(segment.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES for segment in segments):
        raise ValueError("업로드 폴더에 예약된 이름을 사용할 수 없습니다.")
    return "/".join(segments)


def sanitize_filename(value: str) -> str:
    """브라우저가 보낸 정상 basename은 보존하고 경로성·위험 파일명은 거부한다."""

    if not value or value != value.strip() or value.endswith("."):
        raise UploadError("invalid_file_name", "올바른 파일명이 필요합니다.", 400)
    if _INVALID_FILENAME_CHARS.search(value) or value in {".", ".."} or len(value) > 180:
        raise UploadError("invalid_file_name", "올바른 파일명이 필요합니다.", 400)

    suffix = Path(value).suffix
    stem = value[: -len(suffix)] if suffix else value
    if stem.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise UploadError("invalid_file_name", "올바른 파일명이 필요합니다.", 400)
    return value


def build_versioned_filename(original_name: str, created_at: datetime, *, prefix: str) -> str:
    """원본 확장자를 보존해 `접두사 문서명_YYYYMMDD_v1.0` 저장명을 만든다."""

    safe_name = sanitize_filename(original_name)
    suffix = Path(safe_name).suffix
    document_name = safe_name[: -len(suffix)] if suffix else safe_name
    if document_name.startswith(prefix):
        document_name = document_name[len(prefix) :]
    document_name = _VERSIONED_UPLOAD_SUFFIX.sub("", document_name).strip()
    if not document_name:
        raise UploadError("invalid_file_name", "저장할 문서 이름이 필요합니다.", 400)

    normalized_time = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    created_date = normalized_time.astimezone(_KST).strftime("%Y%m%d")
    return sanitize_filename(f"{prefix}{document_name}_{created_date}_{_INITIAL_UPLOAD_VERSION}{suffix}")


def build_upload_filename(original_name: str, uploaded_at: datetime) -> str:
    """원본 확장자를 보존해 `[업로드] 문서명_YYYYMMDD_v1.0` 규칙의 저장명을 만든다."""

    return build_versioned_filename(original_name, uploaded_at, prefix=_UPLOAD_PREFIX)


class RuntimeUploadSettingsStore:
    """비밀이 아닌 Playground 상대 폴더 설정을 runtime JSON에 병합 저장한다."""

    def __init__(
        self,
        path: Path,
        default_relative_directory: str = "",
        *,
        legacy_relative_directory: str = "",
        proposal_draft_default_relative_directory: str = "",
    ) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._default = self._safe_default(default_relative_directory)
        self._legacy = self._safe_default(legacy_relative_directory)
        self._proposal_draft_default = self._safe_default(proposal_draft_default_relative_directory)

    @staticmethod
    def _safe_default(value: str) -> str:
        try:
            return normalize_relative_directory(value)
        except ValueError:
            return ""

    def get_relative_directory(self) -> str:
        """저장된 값을 읽고 손상되었으면 안전한 기본값으로 복구한다."""

        with self._lock:
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                value = payload.get("upload", {}).get("relative_directory", "")
                normalized = normalize_relative_directory(value)
                if self._legacy and normalized == self._legacy and self._default != self._legacy:
                    return self._default
                return normalized
            except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                return self._default

    def set_relative_directory(self, value: str) -> str:
        """검증한 업로드 상대 폴더를 다른 runtime 설정을 보존해 저장한다."""

        return self._set_section_relative_directory("upload", value)

    def get_proposal_draft_relative_directory(self) -> str:
        """기안 초안 저장 상대 폴더를 읽고 손상되었으면 빈 기본값으로 복구한다."""

        with self._lock:
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                value = payload.get("proposal_draft", {}).get("relative_directory", "")
                return normalize_relative_directory(value)
            except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
                return self._proposal_draft_default

    def set_proposal_draft_relative_directory(self, value: str) -> str:
        """검증한 기안 초안 상대 폴더를 다른 runtime 설정을 보존해 저장한다."""

        return self._set_section_relative_directory("proposal_draft", value)

    def _set_section_relative_directory(self, section_name: str, value: str) -> str:
        normalized = normalize_relative_directory(value)
        with self._lock:
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
                payload = {}
            section = payload.get(section_name)
            if not isinstance(section, dict):
                section = {}
            section["relative_directory"] = normalized
            payload[section_name] = section
            self._write_payload(payload)
        return normalized

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._path)
        finally:
            if temporary.exists():
                temporary.unlink()


class UploadRegistry:
    """업로드 UUID와 SMB 상대 위치를 비커밋 runtime JSON에 원자적으로 보관한다."""

    def __init__(self, path: Path, *, max_records: int = 500) -> None:
        self._path = path
        self._max_records = max_records
        self._lock = threading.RLock()

    def register(
        self,
        file_name: str,
        relative_directory: str,
        size_bytes: int,
        uploaded_at: datetime,
    ) -> UploadedFileRecord:
        """새 업로드에 외부 경로와 무관한 UUID를 발급하고 기록한다."""

        record = UploadedFileRecord(
            doc_id=uuid.uuid4(),
            revision_id=uuid.uuid4(),
            file_name=sanitize_filename(file_name),
            relative_directory=normalize_relative_directory(relative_directory),
            size_bytes=size_bytes,
            uploaded_at=uploaded_at,
        )
        with self._lock:
            records = [*self._load(), record][-self._max_records :]
            payload = {
                "version": 1,
                "uploads": [
                    {
                        "doc_id": str(item.doc_id),
                        "revision_id": str(item.revision_id),
                        "file_name": item.file_name,
                        "relative_directory": item.relative_directory,
                        "size_bytes": item.size_bytes,
                        "uploaded_at": item.uploaded_at.isoformat(),
                    }
                    for item in records
                ],
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self._path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return record

    def find(self, doc_id: UUID, revision_id: UUID) -> UploadedFileRecord | None:
        """사용자 입력 경로 대신 서버가 발급한 두 UUID가 모두 일치하는 레코드만 반환한다."""

        with self._lock:
            return next(
                (
                    record
                    for record in self._load()
                    if record.doc_id == doc_id and record.revision_id == revision_id
                ),
                None,
            )

    def _load(self) -> list[UploadedFileRecord]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            rows = payload.get("uploads", [])
        except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError, TypeError):
            return []

        records: list[UploadedFileRecord] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                records.append(
                    UploadedFileRecord(
                        doc_id=UUID(str(row["doc_id"])),
                        revision_id=UUID(str(row["revision_id"])),
                        file_name=sanitize_filename(str(row["file_name"])),
                        relative_directory=normalize_relative_directory(str(row["relative_directory"])),
                        size_bytes=int(row["size_bytes"]),
                        uploaded_at=datetime.fromisoformat(str(row["uploaded_at"])),
                    )
                )
            except (KeyError, TypeError, ValueError, UploadError):
                continue
        return records


class SmbUploadWriter:
    """제한된 동시성과 시간 예산으로 SMB 대상 폴더에 한 파일을 기록한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        smb_module: Any = smbclient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._smb = smb_module
        self._clock = clock or (lambda: datetime.now(UTC))
        self._slots = threading.BoundedSemaphore(settings.smb_upload_max_concurrency)
        self._session_lock = threading.Lock()
        self._connected = False

    def close(self) -> None:
        """등록한 SMB 세션 캐시를 정리한다."""

        if self._connected:
            self._smb.reset_connection_cache()
            self._connected = False

    def _connect(self) -> None:
        if self._connected:
            return
        with self._session_lock:
            if self._connected:
                return
            try:
                timeout_seconds = max(1, self._settings.smb_upload_timeout_ms // 1000)
                self._smb.register_session(
                    self._settings.effective_smb_upload_host,
                    username=self._settings.effective_smb_upload_username,
                    password=self._settings.effective_smb_upload_password,
                    connection_timeout=timeout_seconds,
                )
                self._connected = True
            except Exception as exc:  # noqa: BLE001 - SMB 구현별 예외를 외부에 노출하지 않는다.
                _logger.warning("SMB 업로드 연결 실패: error_type=%s", type(exc).__name__)
                raise UploadError("smb_upload_unavailable", "공유폴더에 연결할 수 없습니다.", 503) from exc

    def upload(self, source: BinaryIO, original_name: str, relative_directory: str) -> FileUploadResponse:
        """검증을 마친 파일을 최종 이름으로 한 번만 exclusive 생성한다."""

        if not self._settings.smb_upload_enabled:
            raise UploadError("upload_disabled", "파일 첨부 기능이 비활성화되어 있습니다.", 403)
        if not self._settings.smb_upload_credentials_configured:
            raise UploadError("upload_not_configured", "공유폴더 연결 설정이 필요합니다.", 503)

        normalized_directory = normalize_relative_directory(relative_directory)
        original_file_name = sanitize_filename(original_name)
        extension = Path(original_file_name).suffix.lower()
        if not extension or extension not in self.allowed_extensions:
            raise UploadError("file_type_not_allowed", "허용되지 않은 파일 형식입니다.", 415)
        uploaded_at = self._clock()
        file_name = build_upload_filename(original_file_name, uploaded_at)
        if not self._slots.acquire(blocking=False):
            raise UploadError("upload_busy", "다른 파일을 업로드하고 있습니다. 잠시 후 다시 시도해 주세요.", 429)

        try:
            deadline = time.monotonic() + self._settings.smb_upload_timeout_ms / 1000
            content = self._read_upload_content(source, deadline)
            self._connect()
            root = rf"\\{self._settings.effective_smb_upload_host}\{self._settings.effective_smb_upload_share_name}"
            destination = str(PureWindowsPath(root, *normalized_directory.split("/")))
            if not self._smb.path.isdir(destination):
                raise UploadError("upload_directory_unavailable", "설정한 업로드 폴더를 사용할 수 없습니다.", 503)

            final_path = str(PureWindowsPath(destination, file_name))
            try:
                with self._smb.open_file(final_path, mode="xb") as target:
                    for offset in range(0, len(content), _CHUNK_SIZE):
                        if time.monotonic() > deadline:
                            raise UploadError("upload_timeout", "파일 업로드 시간이 초과되었습니다.", 504)
                        target.write(content[offset : offset + _CHUNK_SIZE])
            except Exception as exc:  # noqa: BLE001 - smbclient는 충돌도 SMBOSError로 반환할 수 있다.
                if _is_file_exists_error(exc):
                    raise UploadError("file_already_exists", "같은 이름의 파일이 이미 있습니다.", 409) from exc
                raise
            if time.monotonic() > deadline:
                raise UploadError("upload_timeout", "파일 업로드 시간이 초과되었습니다.", 504)
            return FileUploadResponse(
                file_name=file_name,
                size_bytes=len(content),
                uploaded_at=uploaded_at,
                destination_label=_DESTINATION_LABEL,
                indexed=False,
            )
        except UploadError:
            raise
        except Exception as exc:  # noqa: BLE001 - 내부 SMB 경로와 예외 메시지를 숨긴다.
            _logger.warning("SMB 파일 업로드 실패: error_type=%s", type(exc).__name__)
            raise UploadError("smb_upload_failed", "공유폴더에 파일을 저장하지 못했습니다.", 503) from exc
        finally:
            self._slots.release()

    def create_xlsx(self, content: bytes, file_name: str, relative_directory: str) -> FileUploadResponse:
        """완성된 XLSX를 기존 파일 변경 없이 최종 이름으로 한 번만 생성한다."""

        if not self._settings.smb_upload_enabled:
            raise UploadError("upload_disabled", "기안 초안 저장 기능이 비활성화되어 있습니다.", 403)
        if not self._settings.smb_upload_credentials_configured:
            raise UploadError("upload_not_configured", "공유폴더 연결 설정이 필요합니다.", 503)

        normalized_directory = normalize_relative_directory(relative_directory)
        safe_name = sanitize_filename(file_name)
        if Path(safe_name).suffix.lower() != ".xlsx":
            raise UploadError("file_type_not_allowed", "기안 초안은 XLSX 형식으로만 저장할 수 있습니다.", 415)
        if not content:
            raise UploadError("empty_file", "빈 기안 초안은 저장할 수 없습니다.", 400)
        if len(content) > self._settings.smb_upload_max_size_bytes:
            raise UploadError("file_too_large", "기안 초안 크기가 허용 한도를 초과했습니다.", 413)
        if not self._slots.acquire(blocking=False):
            raise UploadError("upload_busy", "다른 파일을 저장하고 있습니다. 잠시 후 다시 시도해 주세요.", 429)

        uploaded_at = self._clock()
        try:
            deadline = time.monotonic() + self._settings.smb_upload_timeout_ms / 1000
            self._connect()
            root = rf"\\{self._settings.effective_smb_upload_host}\{self._settings.effective_smb_upload_share_name}"
            destination = str(PureWindowsPath(root, *normalized_directory.split("/")))
            if not self._smb.path.isdir(destination):
                raise UploadError("upload_directory_unavailable", "설정한 기안 저장 폴더를 사용할 수 없습니다.", 503)

            final_path = str(PureWindowsPath(destination, safe_name))
            try:
                with self._smb.open_file(final_path, mode="xb") as target:
                    for offset in range(0, len(content), _CHUNK_SIZE):
                        if time.monotonic() > deadline:
                            raise UploadError("upload_timeout", "기안 초안 저장 시간이 초과되었습니다.", 504)
                        target.write(content[offset : offset + _CHUNK_SIZE])
            except Exception as exc:  # noqa: BLE001 - smbclient는 충돌도 SMBOSError로 반환할 수 있다.
                if _is_file_exists_error(exc):
                    raise UploadError("file_already_exists", "같은 이름의 기안 파일이 이미 있습니다.", 409) from exc
                raise
            if time.monotonic() > deadline:
                raise UploadError("upload_timeout", "기안 초안 저장 시간이 초과되었습니다.", 504)
            return FileUploadResponse(
                file_name=safe_name,
                size_bytes=len(content),
                uploaded_at=uploaded_at,
                destination_label=_PROPOSAL_DESTINATION_LABEL,
                indexed=False,
            )
        except UploadError:
            raise
        except Exception as exc:  # noqa: BLE001 - 내부 SMB 경로와 예외 메시지를 숨긴다.
            _logger.warning("SMB 기안 초안 저장 실패: error_type=%s", type(exc).__name__)
            raise UploadError("smb_upload_failed", "공유폴더에 기안 초안을 저장하지 못했습니다.", 503) from exc
        finally:
            self._slots.release()

    def _read_upload_content(self, source: BinaryIO, deadline: float) -> bytes:
        """SMB 파일을 만들기 전에 크기·빈 파일·시간 예산을 로컬에서 검증한다."""

        content = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise UploadError("upload_timeout", "파일 업로드 시간이 초과되었습니다.", 504)
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > self._settings.smb_upload_max_size_bytes:
                raise UploadError("file_too_large", "파일 크기가 허용 한도를 초과했습니다.", 413)
        if not content:
            raise UploadError("empty_file", "빈 파일은 첨부할 수 없습니다.", 400)
        return bytes(content)

    def read(self, file_name: str, relative_directory: str, *, expected_size: int) -> bytes:
        """등록된 업로드 원본을 제한된 크기와 시간 안에서 다시 읽는다."""

        normalized_directory = normalize_relative_directory(relative_directory)
        safe_name = sanitize_filename(file_name)
        if expected_size < 1 or expected_size > self._settings.smb_upload_max_size_bytes:
            raise UploadError("uploaded_file_too_large", "첨부 파일 크기를 안전하게 확인할 수 없습니다.", 413)
        if not self._slots.acquire(blocking=False):
            raise UploadError("upload_busy", "다른 파일 작업을 처리하고 있습니다. 잠시 후 다시 시도해 주세요.", 429)

        try:
            self._connect()
            deadline = time.monotonic() + self._settings.smb_upload_timeout_ms / 1000
            root = rf"\\{self._settings.effective_smb_upload_host}\{self._settings.effective_smb_upload_share_name}"
            source_path = str(PureWindowsPath(root, *normalized_directory.split("/"), safe_name))
            if not self._smb.path.exists(source_path):
                raise UploadError("uploaded_file_not_found", "첨부한 파일을 공유폴더에서 찾을 수 없습니다.", 404)

            data = bytearray()
            with self._smb.open_file(source_path, mode="rb") as source:
                while True:
                    if time.monotonic() > deadline:
                        raise UploadError("uploaded_file_read_timeout", "첨부 파일을 읽는 시간이 초과됐습니다.", 504)
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > self._settings.smb_upload_max_size_bytes:
                        raise UploadError("uploaded_file_too_large", "첨부 파일 크기가 허용 범위를 넘었습니다.", 413)
            if not data:
                raise UploadError("uploaded_file_empty", "첨부 파일 내용이 비어 있습니다.", 422)
            return bytes(data)
        except UploadError:
            raise
        except Exception as exc:  # noqa: BLE001 - 내부 SMB 예외를 사용자에게 노출하지 않는다.
            _logger.warning("SMB 첨부 파일 읽기 실패: error_type=%s", type(exc).__name__)
            raise UploadError("uploaded_file_read_failed", "첨부 파일을 공유폴더에서 읽지 못했습니다.", 503) from exc
        finally:
            self._slots.release()

    @property
    def allowed_extensions(self) -> tuple[str, ...]:
        """정규화한 확장자 allowlist를 반환한다."""

        normalized: set[str] = set()
        for raw in self._settings.smb_upload_allowed_extensions.split(","):
            extension = raw.strip().lower()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = f".{extension}"
            if re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
                normalized.add(extension)
        return tuple(sorted(normalized))


class UploadManager:
    """공개 설정 조회·변경과 SMB writer를 결합한다."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: RuntimeUploadSettingsStore | None = None,
        writer: SmbUploadWriter | None = None,
        registry: UploadRegistry | None = None,
    ) -> None:
        self.settings = settings
        default_directory = settings.effective_smb_upload_default_relative_directory
        self.store = store or RuntimeUploadSettingsStore(
            Path(settings.smb_upload_runtime_settings_path),
            default_relative_directory=default_directory,
            legacy_relative_directory=settings.nas_fold_path,
            proposal_draft_default_relative_directory=settings.proposal_draft_default_relative_directory,
        )
        self.writer = writer or SmbUploadWriter(settings)
        self.registry = registry or UploadRegistry(Path(settings.smb_upload_registry_path))

    def close(self) -> None:
        """SMB writer를 종료한다."""

        self.writer.close()

    def get_settings(self) -> PlaygroundSettingsResponse:
        """비밀·주소 없이 설정 화면 계약을 생성한다."""

        relative_directory = self.store.get_relative_directory()
        proposal_draft_relative_directory = self.store.get_proposal_draft_relative_directory()
        return PlaygroundSettingsResponse(
            upload=UploadSettingsView(
                enabled=self.settings.smb_upload_enabled,
                configured=bool(self.settings.smb_upload_credentials_configured and relative_directory),
                relative_directory=relative_directory,
                destination_label=_DESTINATION_LABEL,
                max_size_bytes=self.settings.smb_upload_max_size_bytes,
                allowed_extensions=list(self.writer.allowed_extensions),
            ),
            proposal_draft=ProposalDraftSettingsView(
                relative_directory=proposal_draft_relative_directory,
                destination_label=_PROPOSAL_DESTINATION_LABEL,
            ),
            local_llm_configured=self.settings.local_llm_configured,
        )

    def update_relative_directory(self, value: str) -> PlaygroundSettingsResponse:
        """상대 폴더를 저장하고 갱신된 전체 공개 설정을 반환한다."""

        self.store.set_relative_directory(value)
        return self.get_settings()

    def update_proposal_draft_relative_directory(self, value: str) -> PlaygroundSettingsResponse:
        """기안 초안 상대 폴더를 저장하고 갱신된 전체 공개 설정을 반환한다."""

        self.store.set_proposal_draft_relative_directory(value)
        return self.get_settings()

    def upload(self, source: BinaryIO, original_name: str) -> FileUploadResponse:
        """현재 상대 폴더에 파일을 업로드한다."""

        relative_directory = self.store.get_relative_directory()
        if not relative_directory:
            raise UploadError("upload_directory_not_configured", "업로드 폴더를 먼저 설정해 주세요.", 503)
        response = self.writer.upload(source, original_name, relative_directory)
        record = self.registry.register(
            response.file_name,
            relative_directory,
            response.size_bytes,
            response.uploaded_at,
        )
        conversation_ready = Path(record.file_name).suffix.lower() in SUPPORTED_EXTENSIONS
        return response.model_copy(
            update={
                "conversation_ready": conversation_ready,
                "selected_file": UploadedFileSelection(
                    doc_id=record.doc_id,
                    revision_id=record.revision_id,
                    file_name=record.file_name,
                    title=Path(record.file_name).stem,
                    extension=Path(record.file_name).suffix.lower(),
                    size_bytes=record.size_bytes,
                ),
            }
        )

    def save_proposal_draft(self, content: bytes, file_name: str) -> FileUploadResponse:
        """설정된 기안 폴더에 확정된 이름으로 신규 XLSX를 한 번만 추가한다."""

        relative_directory = self.store.get_proposal_draft_relative_directory()
        if not relative_directory:
            raise UploadError("proposal_directory_not_configured", "기안 초안 저장 폴더를 먼저 설정해 주세요.", 503)

        safe_name = sanitize_filename(file_name)
        return self.writer.create_xlsx(content, safe_name, relative_directory)

    def validate_selections(self, selections: list[tuple[UUID, UUID]]) -> bool:
        """업로드 문서 UUID가 모두 서버 레지스트리에 등록돼 있는지 확인한다."""

        return all(self.registry.find(doc_id, revision_id) is not None for doc_id, revision_id in selections)

    def retrieve(self, query: str, selections: list[tuple[UUID, UUID]]) -> ScopedRetrievalResult:
        """업로드한 SMB 원본을 즉시 추출해 선택 문서 대화용 근거를 만든다."""

        started = time.perf_counter()
        read_ms = 0.0
        extract_ms = 0.0
        candidates: list[tuple[UploadedFileRecord, int, str, float]] = []
        scopes: list[RetrievalScope] = []
        degraded: list[str] = []
        query_tokens = self._query_tokens(query)

        for doc_id, revision_id in selections:
            record = self.registry.find(doc_id, revision_id)
            if record is None:
                raise UploadError("uploaded_file_not_found", "첨부 파일 참조가 만료됐습니다. 다시 첨부해 주세요.", 404)
            scopes.append(RetrievalScope(doc_id=record.doc_id, revision_id=record.revision_id))

            read_started = time.perf_counter()
            data = self.writer.read(
                record.file_name,
                record.relative_directory,
                expected_size=record.size_bytes,
            )
            read_ms += (time.perf_counter() - read_started) * 1000

            extract_started = time.perf_counter()
            extracted = extract_text(
                record.file_name,
                data,
                max_chars=max(self.settings.llmops_chunk_max_chars, 200_000),
            )
            extract_ms += (time.perf_counter() - extract_started) * 1000
            if extracted.status != "ok":
                degraded.append(f"uploaded_file_{extracted.status}")
                continue
            for chunk_index, text in enumerate(self._chunks(extracted.text, self.settings.llmops_chunk_max_chars)):
                folded = text.casefold()
                matches = sum(folded.count(token) for token in query_tokens)
                lexical = matches / max(1, len(query_tokens))
                candidates.append((record, chunk_index, text, lexical))

        ranked = sorted(candidates, key=lambda item: (-item[3], item[1]))
        if ranked and not any(item[3] > 0 for item in ranked):
            ranked = sorted(ranked, key=lambda item: item[1])
        selected = ranked[: self.settings.llmops_retrieval_top_k]
        citations = [
            DocumentCitation(
                index=index,
                doc_id=record.doc_id,
                revision_id=record.revision_id,
                chunk_id=uuid.uuid5(record.revision_id, f"upload-chunk-{chunk_index}"),
                title=Path(record.file_name).stem,
                section_path=["첨부 문서"],
                location={"source": "upload", "chunk": chunk_index + 1},
                excerpt=text,
                scores=RetrievalScores(
                    lexical=round(lexical, 6),
                    rrf=round(1 / (60 + index), 6),
                ),
            )
            for index, (record, chunk_index, text, lexical) in enumerate(selected, start=1)
        ]
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        metadata = RetrievalMetadata(
            trace_id=uuid.uuid4(),
            scope=scopes,
            result_count=len(citations),
            candidate_count=len(candidates),
            grounded=bool(citations),
            decision="answerable" if citations else "insufficient_evidence",
            degraded_dependencies=list(dict.fromkeys(degraded)),
            timings_ms={
                "smb_read": round(read_ms, 1),
                "extract": round(extract_ms, 1),
            },
            elapsed_ms=elapsed_ms,
            over_budget=elapsed_ms > self.settings.playground_agent_budget_ms,
        )
        return ScopedRetrievalResult(citations=citations, metadata=metadata)

    @staticmethod
    def _query_tokens(query: str) -> tuple[str, ...]:
        """즉시 근거 정렬에 쓸 중복 없는 한국어·영문 토큰을 만든다."""

        return tuple(dict.fromkeys(token for token in re.findall(r"[0-9A-Za-z가-힣_]+", query.casefold()) if len(token) > 1))

    @staticmethod
    def _chunks(text: str, max_chars: int) -> list[str]:
        """본문을 LLM 근거 상한에 맞는 고정 크기 조각으로 나눈다."""

        normalized = re.sub(r"\n{3,}", "\n\n", text).strip()
        return [normalized[offset : offset + max_chars] for offset in range(0, len(normalized), max_chars)]
