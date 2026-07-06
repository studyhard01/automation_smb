"""API 응답 계약 검증 — 라이브 SMB 없이 엔드포인트 함수 단위로 확인한다."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from smb_finder import api
from smb_finder.config import Settings
from smb_finder.content_index import ContentIndex
from smb_finder.index import FolderIndex
from smb_finder.models import RefreshContentRequest
from smb_finder.smb_client import FolderEntry


def test_openapi_operation_ids_are_stable():
    schema = api.app.openapi()
    operations = {
        path: methods
        for path, methods in schema["paths"].items()
        if path in {"/find", "/search-content", "/refresh", "/refresh-content", "/health"}
        or path.startswith("/admin/content-index-jobs")
    }

    assert operations["/find"]["post"]["operationId"] == "find_share_folder"
    assert operations["/search-content"]["post"]["operationId"] == "search_file_content"
    assert operations["/refresh"]["post"]["operationId"] == "refresh_index"
    assert operations["/refresh-content"]["post"]["operationId"] == "index_folder_content"
    assert operations["/health"]["get"]["operationId"] == "health_check"
    assert operations["/admin/content-index-jobs"]["post"]["operationId"] == "create_content_index_job"
    assert operations["/admin/content-index-jobs"]["get"]["operationId"] == "list_content_index_jobs"
    assert operations["/admin/content-index-jobs/{job_id}"]["get"]["operationId"] == "get_content_index_job"
    assert "ApiErrorResponse" in schema["components"]["schemas"]


def test_refresh_content_request_normalizes_relative_path():
    req = RefreshContentRequest(path=r"검사결과\2026/OO검사", host="fileserver", share_name="labshare")

    assert req.path == "검사결과/2026/OO검사"
    assert req.host == "fileserver"
    assert req.share_name == "labshare"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "../secret"),
        ("path", r"C:\secret"),
        ("host", r"server\share"),
        ("share_name", "../share"),
    ],
)
def test_refresh_content_request_rejects_unsafe_override(field, value):
    payload = {"path": "", "host": "", "share_name": ""}
    payload[field] = value

    with pytest.raises(ValueError):
        RefreshContentRequest(**payload)


def test_health_response_contract(tmp_path):
    old_settings = api._settings
    old_state = dict(api._state)
    content_index = ContentIndex(str(tmp_path / "content.fts.db"))
    content_index.add(path="A/a.txt", name="a.txt", content="BRCA1", ext=".txt", size=1, mtime=1.0)
    content_index.commit()
    try:
        api._settings = Settings(
            smb_index_cache_path=str(tmp_path / "folder.index.json"),
            content_index_db_path=str(tmp_path / "content.fts.db"),
        )
        api._state.clear()
        api._state["index"] = FolderIndex([FolderEntry(path="A", name="A", depth=1)])
        api._state["content_index"] = content_index

        resp = asyncio.run(api.health())

        assert resp.status == "ok"
        assert resp.ready is True
        assert resp.indexed_folders == 1
        assert resp.indexed_files == 1
        assert resp.cache_path == "folder.index.json"
        assert resp.db_path == "content.fts.db"
        assert str(tmp_path) not in resp.model_dump_json()
    finally:
        content_index.close()
        api._settings = old_settings
        api._state.clear()
        api._state.update(old_state)


def test_refresh_index_response_contract(monkeypatch, tmp_path):
    old_settings = api._settings
    old_state = dict(api._state)
    try:
        api._settings = Settings(
            smb_index_cache_path=str(tmp_path / "folder.index.json"),
            smb_index_build_budget_sec=60,
            admin_api_token="secret",
        )
        api._state.clear()
        api._state["index"] = FolderIndex([])

        def fake_build_index(settings):
            return FolderIndex([FolderEntry(path="A", name="A", depth=1)])

        monkeypatch.setattr(api, "build_index", fake_build_index)

        resp = asyncio.run(api.refresh_index(x_admin_token="secret"))

        assert resp.folders == 1
        assert resp.indexed_folders == 1
        assert resp.elapsed_ms >= 0
        assert resp.over_budget is False
        assert resp.cache_path == "folder.index.json"
    finally:
        api._settings = old_settings
        api._state.clear()
        api._state.update(old_state)


def test_refresh_index_requires_admin_token():
    old_settings = api._settings
    try:
        api._settings = Settings(admin_api_token="secret")

        with pytest.raises(HTTPException) as exc:
            asyncio.run(api.refresh_index())

        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "admin_forbidden"
    finally:
        api._settings = old_settings


def test_refresh_content_response_redacts_target(monkeypatch, tmp_path):
    old_settings = api._settings
    old_state = dict(api._state)
    content_index = ContentIndex(str(tmp_path / "content.fts.db"))
    try:
        api._settings = Settings(content_index_db_path=str(tmp_path / "content.fts.db"))
        api._state.clear()
        api._state["content_index"] = content_index

        def fake_build_content_index(settings, subpath="", host="", share_name=""):
            return {
                "path": subpath,
                "target": "override",
                "host": "override",
                "share": "override",
                "host_override_used": bool(host),
                "share_override_used": bool(share_name),
                "indexed": 0,
                "unsupported": 0,
                "empty": 0,
                "errors": 0,
                "elapsed_sec": 0.0,
                "over_budget": False,
            }

        monkeypatch.setattr(api, "build_content_index", fake_build_content_index)

        api._settings = Settings(
            content_index_db_path=str(tmp_path / "content.fts.db"),
            admin_api_token="secret",
            smb_host="example.invalid",
            smb_share_name="sensitive-share",
        )

        resp = asyncio.run(
            api.refresh_content_index(
                RefreshContentRequest(path="A", host="example.invalid", share_name="sensitive-share"),
                x_admin_token="secret",
            )
        )

        assert resp.path == "A"
        assert resp.host == "override"
        assert resp.share == "override"
        assert resp.host_override_used is True
        assert resp.share_override_used is True
        assert "example.invalid" not in resp.model_dump_json()
        assert "sensitive-share" not in resp.model_dump_json()
        assert resp.db_path == "content.fts.db"
    finally:
        content_index.close()
        api._settings = old_settings
        api._state.clear()
        api._state.update(old_state)


def test_refresh_content_requires_admin_token():
    old_settings = api._settings
    try:
        api._settings = Settings(admin_api_token="secret")

        with pytest.raises(HTTPException) as exc:
            asyncio.run(api.refresh_content_index(RefreshContentRequest(path="A")))

        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "admin_forbidden"
    finally:
        api._settings = old_settings


def test_refresh_content_rejects_disallowed_smb_override(monkeypatch, tmp_path):
    old_settings = api._settings
    old_state = dict(api._state)
    content_index = ContentIndex(str(tmp_path / "content.fts.db"))
    called = False
    try:
        api._settings = Settings(
            content_index_db_path=str(tmp_path / "content.fts.db"),
            admin_api_token="secret",
            smb_host="configured-host",
            smb_share_name="configured-share",
        )
        api._state.clear()
        api._state["content_index"] = content_index

        def fake_build_content_index(settings, subpath="", host="", share_name=""):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(api, "build_content_index", fake_build_content_index)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                api.refresh_content_index(
                    RefreshContentRequest(path="A", host="attacker.example", share_name="configured-share"),
                    x_admin_token="secret",
                )
            )

        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "smb_host_not_allowed"
        assert called is False
    finally:
        content_index.close()
        api._settings = old_settings
        api._state.clear()
        api._state.update(old_state)


def test_refresh_content_reports_busy(monkeypatch, tmp_path):
    old_settings = api._settings
    old_state = dict(api._state)
    content_index = ContentIndex(str(tmp_path / "content.fts.db"))
    try:
        api._settings = Settings(
            content_index_db_path=str(tmp_path / "content.fts.db"),
            admin_api_token="secret",
            smb_host="configured-host",
            smb_share_name="configured-share",
        )
        api._state.clear()
        api._state["content_index"] = content_index

        def fake_build_content_index(settings, subpath="", host="", share_name=""):
            raise RuntimeError("content index build already running")

        monkeypatch.setattr(api, "build_content_index", fake_build_content_index)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(api.refresh_content_index(RefreshContentRequest(path="A"), x_admin_token="secret"))

        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "content_index_busy"
    finally:
        content_index.close()
        api._settings = old_settings
        api._state.clear()
        api._state.update(old_state)


def test_admin_content_job_requires_configured_token():
    old_settings = api._settings
    try:
        api._settings = Settings(admin_api_token="")

        with pytest.raises(HTTPException) as exc:
            api._require_admin_token("anything")

        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "admin_api_disabled"
    finally:
        api._settings = old_settings


def test_admin_content_job_rejects_wrong_token():
    old_settings = api._settings
    try:
        api._settings = Settings(admin_api_token="secret")

        with pytest.raises(HTTPException) as exc:
            api._require_admin_token("wrong")

        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "admin_forbidden"
    finally:
        api._settings = old_settings


def test_admin_content_job_create_and_get(monkeypatch):
    old_settings = api._settings
    old_jobs = api._content_jobs
    try:
        api._settings = Settings(
            admin_api_token="secret",
            content_index_job_retention=10,
            smb_host="example.invalid",
            smb_share_name="labshare",
        )
        api._content_jobs = api.ContentIndexJobStore(retention=10)

        def fake_add_task(func, *args, **kwargs):
            return None

        background_tasks = BackgroundTasks()
        monkeypatch.setattr(background_tasks, "add_task", fake_add_task)

        created = asyncio.run(
            api.create_content_index_job(
                RefreshContentRequest(path="A/2026", host="example.invalid", share_name="labshare"),
                background_tasks,
                x_admin_token="secret",
            )
        )
        status = asyncio.run(api.get_content_index_job(created.job_id, x_admin_token="secret"))
        jobs = asyncio.run(api.list_content_index_jobs(x_admin_token="secret"))

        assert created.status == "queued"
        assert created.status_url.endswith(created.job_id)
        assert status.job_id == created.job_id
        assert status.path == "A/2026"
        assert status.host_override_used is True
        assert "example.invalid" not in status.model_dump_json()
        assert "labshare" not in status.model_dump_json()
        assert [job.job_id for job in jobs] == [created.job_id]
    finally:
        api._settings = old_settings
        api._content_jobs = old_jobs
