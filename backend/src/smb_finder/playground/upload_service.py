"""제한된 설정 저장과 SMB 파일 업로드 서비스."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO

import smbclient

from smb_finder.config import Settings

from .upload_models import FileUploadResponse, PlaygroundSettingsResponse, UploadSettingsView

_logger = logging.getLogger(__name__)
_DESTINATION_LABEL = "관리 공유폴더"
_CHUNK_SIZE = 64 * 1024
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


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


class RuntimeUploadSettingsStore:
    """비밀이 아닌 상대 폴더 설정 하나만 runtime JSON에 저장한다."""

    def __init__(
        self,
        path: Path,
        default_relative_directory: str = "",
        *,
        legacy_relative_directory: str = "",
    ) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._default = self._safe_default(default_relative_directory)
        self._legacy = self._safe_default(legacy_relative_directory)

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
        """검증한 상대 폴더만 원자적으로 저장한다."""

        normalized = normalize_relative_directory(value)
        payload = {"upload": {"relative_directory": normalized}}
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f".{self._path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self._path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return normalized


class SmbUploadWriter:
    """제한된 동시성과 시간 예산으로 SMB 대상 폴더에 한 파일을 기록한다."""

    def __init__(self, settings: Settings, *, smb_module: Any = smbclient) -> None:
        self._settings = settings
        self._smb = smb_module
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
        """파일을 임시 이름으로 기록한 뒤 동일 폴더에서 최종 이름으로 이동한다."""

        if not self._settings.smb_upload_enabled:
            raise UploadError("upload_disabled", "파일 첨부 기능이 비활성화되어 있습니다.", 403)
        if not self._settings.smb_upload_credentials_configured:
            raise UploadError("upload_not_configured", "공유폴더 연결 설정이 필요합니다.", 503)

        normalized_directory = normalize_relative_directory(relative_directory)
        file_name = sanitize_filename(original_name)
        extension = Path(file_name).suffix.lower()
        if not extension or extension not in self.allowed_extensions:
            raise UploadError("file_type_not_allowed", "허용되지 않은 파일 형식입니다.", 415)
        if not self._slots.acquire(blocking=False):
            raise UploadError("upload_busy", "다른 파일을 업로드하고 있습니다. 잠시 후 다시 시도해 주세요.", 429)

        partial_path = ""
        partial_created = False
        try:
            self._connect()
            deadline = time.monotonic() + self._settings.smb_upload_timeout_ms / 1000
            root = rf"\\{self._settings.effective_smb_upload_host}\{self._settings.effective_smb_upload_share_name}"
            destination = str(PureWindowsPath(root, *normalized_directory.split("/")))
            if not self._smb.path.isdir(destination):
                raise UploadError("upload_directory_unavailable", "설정한 업로드 폴더를 사용할 수 없습니다.", 503)

            final_path = str(PureWindowsPath(destination, file_name))
            if self._smb.path.exists(final_path):
                raise UploadError("file_already_exists", "같은 이름의 파일이 이미 있습니다.", 409)

            partial_path = str(PureWindowsPath(destination, f".upload-{uuid.uuid4().hex}.part"))
            size_bytes = 0
            with self._smb.open_file(partial_path, mode="xb") as target:
                partial_created = True
                while True:
                    if time.monotonic() > deadline:
                        raise UploadError("upload_timeout", "파일 업로드 시간이 초과되었습니다.", 504)
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self._settings.smb_upload_max_size_bytes:
                        raise UploadError("file_too_large", "파일 크기가 허용 한도를 초과했습니다.", 413)
                    target.write(chunk)

            if size_bytes == 0:
                raise UploadError("empty_file", "빈 파일은 첨부할 수 없습니다.", 400)
            if time.monotonic() > deadline:
                raise UploadError("upload_timeout", "파일 업로드 시간이 초과되었습니다.", 504)
            if self._smb.path.exists(final_path):
                raise UploadError("file_already_exists", "같은 이름의 파일이 이미 있습니다.", 409)
            # smbclient.rename은 replace_if_exists=False로 동작한다. 직전 exists 검사 이후 경합이 나도 덮어쓰지 않는다.
            self._smb.rename(partial_path, final_path)
            partial_created = False
            return FileUploadResponse(
                file_name=file_name,
                size_bytes=size_bytes,
                uploaded_at=datetime.now(UTC),
                destination_label=_DESTINATION_LABEL,
                indexed=False,
            )
        except UploadError:
            raise
        except Exception as exc:  # noqa: BLE001 - 내부 SMB 경로와 예외 메시지를 숨긴다.
            _logger.warning("SMB 파일 업로드 실패: error_type=%s", type(exc).__name__)
            raise UploadError("smb_upload_failed", "공유폴더에 파일을 저장하지 못했습니다.", 503) from exc
        finally:
            if partial_created and partial_path:
                try:
                    self._smb.remove(partial_path)
                except Exception:  # noqa: BLE001 - 생성한 임시 파일 정리 실패만 기록한다.
                    _logger.warning("SMB 업로드 임시 파일 정리 실패")
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
    ) -> None:
        self.settings = settings
        default_directory = settings.effective_smb_upload_default_relative_directory
        self.store = store or RuntimeUploadSettingsStore(
            Path(settings.smb_upload_runtime_settings_path),
            default_relative_directory=default_directory,
            legacy_relative_directory=settings.nas_fold_path,
        )
        self.writer = writer or SmbUploadWriter(settings)

    def close(self) -> None:
        """SMB writer를 종료한다."""

        self.writer.close()

    def get_settings(self) -> PlaygroundSettingsResponse:
        """비밀·주소 없이 설정 화면 계약을 생성한다."""

        relative_directory = self.store.get_relative_directory()
        return PlaygroundSettingsResponse(
            upload=UploadSettingsView(
                enabled=self.settings.smb_upload_enabled,
                configured=bool(self.settings.smb_upload_credentials_configured and relative_directory),
                relative_directory=relative_directory,
                destination_label=_DESTINATION_LABEL,
                max_size_bytes=self.settings.smb_upload_max_size_bytes,
                allowed_extensions=list(self.writer.allowed_extensions),
            ),
            local_llm_configured=self.settings.local_llm_configured,
        )

    def update_relative_directory(self, value: str) -> PlaygroundSettingsResponse:
        """상대 폴더를 저장하고 갱신된 전체 공개 설정을 반환한다."""

        self.store.set_relative_directory(value)
        return self.get_settings()

    def upload(self, source: BinaryIO, original_name: str) -> FileUploadResponse:
        """현재 상대 폴더에 파일을 업로드한다."""

        relative_directory = self.store.get_relative_directory()
        if not relative_directory:
            raise UploadError("upload_directory_not_configured", "업로드 폴더를 먼저 설정해 주세요.", 503)
        return self.writer.upload(source, original_name, relative_directory)
