"""Playground 설정·SMB 파일 첨부의 계약과 쓰기 경계를 검증한다."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.playground.upload_api import create_upload_router
from smb_finder.playground.upload_models import FileUploadResponse
from smb_finder.playground.upload_service import (
    RuntimeUploadSettingsStore,
    SmbUploadWriter,
    UploadError,
    UploadManager,
    normalize_relative_directory,
    sanitize_filename,
)


def _settings(tmp_path: Path, **overrides) -> Settings:  # noqa: ANN003
    values = {
        "_env_file": None,
        "smb_upload_enabled": True,
        "smb_host": "synthetic-server",
        "smb_share_name": "synthetic-share",
        "smb_username": "synthetic-user",
        "smb_password": "synthetic-password",
        "smb_upload_runtime_settings_path": str(tmp_path / "settings.json"),
        "smb_upload_default_relative_directory": "team/inbox",
        "smb_upload_allowed_extensions": ".pdf,.txt",
        "smb_upload_max_size_bytes": 32,
        "smb_upload_timeout_ms": 1000,
        "ollama_base_url": "http://local-llm.invalid",
        "llmops_chat_model": "synthetic-model",
    }
    values.update(overrides)
    return Settings(**values)


class StubWriter:
    allowed_extensions = (".pdf", ".txt")

    def __init__(self) -> None:
        self.closed = False
        self.received = b""

    def close(self) -> None:
        self.closed = True

    def upload(self, source, original_name: str, relative_directory: str) -> FileUploadResponse:  # noqa: ANN001
        self.received = source.read()
        assert relative_directory == "team/inbox"
        return FileUploadResponse(
            file_name=original_name,
            size_bytes=len(self.received),
            uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
            destination_label="관리 공유폴더",
            indexed=False,
        )


def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    files: dict | None = None,
) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body, files=files)

    return asyncio.run(send())


def test_settings_api_returns_only_public_contract_and_persists_relative_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = RuntimeUploadSettingsStore(Path(settings.smb_upload_runtime_settings_path), "team/inbox")
    manager = UploadManager(settings, store=store, writer=StubWriter())
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(app, "GET", "/api/playground/settings")
    assert response.status_code == 200
    assert response.json() == {
        "upload": {
            "enabled": True,
            "configured": True,
            "relative_directory": "team/inbox",
            "destination_label": "관리 공유폴더",
            "max_size_bytes": 32,
            "allowed_extensions": [".pdf", ".txt"],
        },
        "local_llm_configured": True,
    }
    serialized = response.text
    assert "synthetic-server" not in serialized
    assert "synthetic-user" not in serialized
    assert "synthetic-password" not in serialized

    patched = _request(
        app,
        "PATCH",
        "/api/playground/settings/upload",
        json_body={"relative_directory": "team/review"},
    )
    assert patched.status_code == 200
    assert patched.json()["upload"]["relative_directory"] == "team/review"
    assert json.loads(Path(settings.smb_upload_runtime_settings_path).read_text(encoding="utf-8")) == {
        "upload": {"relative_directory": "team/review"}
    }


def test_legacy_nas_fallback_is_usable_only_with_safe_relative_directory(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        smb_host="",
        smb_share_name="",
        smb_username="",
        smb_password="",
        smb_upload_default_relative_directory="",
        nas_url="smb://synthetic-server",
        nas_source_name="legacy-source-name",
        nas_fold_path="synthetic-share/team",
        nas_user="synthetic-user",
        nas_pw="synthetic-password",
    )
    manager = UploadManager(settings, writer=StubWriter())

    response = manager.get_settings()

    assert settings.effective_smb_upload_share_name == "synthetic-share"
    assert response.upload.configured is True
    assert response.upload.relative_directory == "team"

    unsafe = _settings(
        tmp_path,
        smb_upload_default_relative_directory="",
        nas_fold_path=r"\\synthetic-server\synthetic-share\team",
    )
    assert UploadManager(unsafe, writer=StubWriter()).get_settings().upload.relative_directory == ""


def test_legacy_runtime_value_migrates_from_old_fold_path_interpretation(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        smb_host="",
        smb_share_name="",
        smb_username="",
        smb_password="",
        smb_upload_default_relative_directory="",
        nas_url="smb://synthetic-server",
        nas_source_name="legacy-source-name",
        nas_fold_path="synthetic-share/team",
        nas_user="synthetic-user",
        nas_pw="synthetic-password",
    )
    runtime_path = Path(settings.smb_upload_runtime_settings_path)
    runtime_path.write_text(
        json.dumps({"upload": {"relative_directory": "synthetic-share/team"}}),
        encoding="utf-8",
    )

    response = UploadManager(settings, writer=StubWriter()).get_settings()

    assert response.upload.relative_directory == "team"


@pytest.mark.parametrize(
    "value",
    [r"\\synthetic-server\synthetic-share", r"C:\synthetic", "../synthetic", "team/../synthetic", "/synthetic"],
)
def test_settings_api_rejects_non_relative_or_traversal_paths(tmp_path: Path, value: str) -> None:
    settings = _settings(tmp_path)
    manager = UploadManager(settings, writer=StubWriter())
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(
        app,
        "PATCH",
        "/api/playground/settings/upload",
        json_body={"relative_directory": value},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_relative_directory"


def test_upload_api_uses_multipart_file_field_and_returns_no_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    writer = StubWriter()
    manager = UploadManager(settings, writer=writer)
    app = FastAPI()
    app.include_router(create_upload_router(settings, manager))

    response = _request(
        app,
        "POST",
        "/api/playground/files/upload",
        files={"file": ("synthetic.txt", b"synthetic body", "text/plain")},
    )

    assert response.status_code == 201
    assert response.json()["file_name"] == "synthetic.txt"
    assert response.json()["size_bytes"] == 14
    assert response.json()["indexed"] is False
    assert "path" not in response.text.lower()
    assert writer.received == b"synthetic body"


def test_directory_and_filename_validation_rejects_path_semantics() -> None:
    assert normalize_relative_directory(r"team\inbox") == "team/inbox"
    assert sanitize_filename("synthetic report.pdf") == "synthetic report.pdf"

    for name in (r"..\synthetic.pdf", "folder/synthetic.pdf", "CON.txt", "synthetic.pdf. ", "bad?.pdf"):
        with pytest.raises(UploadError) as raised:
            sanitize_filename(name)
        assert raised.value.code == "invalid_file_name"


class _RemoteBuffer(io.BytesIO):
    def __init__(self, fake: "FakeSmb", path: str) -> None:
        super().__init__()
        self._fake = fake
        self._path = path

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self._fake.files[self._path] = self.getvalue()
        self.close()


class _FakePath:
    def __init__(self, fake: "FakeSmb") -> None:
        self._fake = fake

    def isdir(self, _path: str) -> bool:
        return True

    def exists(self, path: str) -> bool:
        return path in self._fake.files


class FakeSmb:
    def __init__(self) -> None:
        self.path = _FakePath(self)
        self.files: dict[str, bytes] = {}
        self.removed: list[str] = []
        self.rename_race = False

    def register_session(self, _host: str, **_kwargs) -> None:
        return None

    def reset_connection_cache(self) -> None:
        return None

    def open_file(self, path: str, *, mode: str) -> _RemoteBuffer:
        assert mode == "xb"
        assert path not in self.files
        return _RemoteBuffer(self, path)

    def remove(self, path: str) -> None:
        self.removed.append(path)
        self.files.pop(path, None)

    def rename(self, source: str, destination: str) -> None:
        if self.rename_race:
            self.files[destination] = b"other writer"
        if destination in self.files:
            raise FileExistsError
        self.files[destination] = self.files.pop(source)


def test_smb_writer_enforces_actual_size_and_cleans_only_its_partial(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(_settings(tmp_path, smb_upload_max_size_bytes=4), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"12345"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "file_too_large"
    assert len(fake.removed) == 1
    assert ".upload-" in fake.removed[0]
    assert fake.files == {}


def test_smb_writer_explicit_gate_blocks_before_any_smb_write(tmp_path: Path) -> None:
    fake = FakeSmb()
    writer = SmbUploadWriter(_settings(tmp_path, smb_upload_enabled=False), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"safe"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "upload_disabled"
    assert fake.files == {}
    assert fake.removed == []


def test_smb_writer_never_overwrites_when_destination_appears_during_rename(tmp_path: Path) -> None:
    fake = FakeSmb()
    fake.rename_race = True
    writer = SmbUploadWriter(_settings(tmp_path), smb_module=fake)

    with pytest.raises(UploadError) as raised:
        writer.upload(io.BytesIO(b"safe"), "synthetic.txt", "team/inbox")

    assert raised.value.code == "smb_upload_failed"
    assert b"other writer" in fake.files.values()
    assert all(not path.endswith("synthetic.txt") or body == b"other writer" for path, body in fake.files.items())
