"""API 응답 계약 검증 — 라이브 SMB 없이 엔드포인트 함수 단위로 확인한다."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException

from smb_finder import api
from smb_finder.config import Settings
from smb_finder.content_index import ContentIndex
from smb_finder.index import FolderIndex
from smb_finder.models import RefreshContentRequest
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.api import create_playground_router
from smb_finder.playground.models import ChatResponse
from smb_finder.playground.tools import PlaygroundRuntime
from smb_finder.smb_client import FolderEntry


def test_openapi_operation_ids_are_stable():
    schema = api.app.openapi()
    operations = {
        path: methods
        for path, methods in schema["paths"].items()
        if path in {"/find", "/search-content", "/refresh", "/refresh-content", "/health"}
        or path.startswith("/api/playground")
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
    assert operations["/api/playground/tools"]["get"]["operationId"] == "list_playground_tools"
    assert (
        operations["/api/playground/karyotype-summary"]["post"]["operationId"]
        == "summarize_cytogenetics_karyotype"
    )
    assert "403" not in operations["/api/playground/karyotype-summary"]["post"]["responses"]
    assert operations["/api/playground/chat"]["post"]["operationId"] == "run_playground_chat"
    assert operations["/api/playground/tool-draft"]["post"]["operationId"] == "draft_playground_tool"
    assert operations["/api/playground/llm-status"]["post"]["operationId"] == "check_playground_llm"
    assert "ApiErrorResponse" in schema["components"]["schemas"]


def _playground_api_app() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(
        create_playground_router(
            lambda: PlaygroundRuntime(
                settings=Settings(
                    _env_file=None,
                    llm_model="local-model",
                    llm_base_url="http://127.0.0.1:8080/v1",
                ),
                rag_searcher=object(),
            )
        )
    )
    return test_app


async def _get_playground_tools() -> httpx.Response:
    transport = httpx.ASGITransport(app=_playground_api_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/playground/tools")


def test_playground_tools_api_serializes_execution_type():
    response = asyncio.run(_get_playground_tools())

    assert response.status_code == 200
    tools = {tool["id"]: tool for tool in response.json()}
    assert tools["cytogenetics_karyotype_summary"]["execution_type"] == "llm"
    assert {
        tool_id for tool_id, tool in tools.items() if tool["execution_type"] == "code"
    } == {
        "find_folder",
        "search_content",
        "search_rag_chunks",
        "cytogenetics_report",
        "ngs_report",
        "refresh_content",
    }
    assert tools["find_folder"]["enabled"] is True
    assert tools["search_rag_chunks"]["enabled"] is True
    assert tools["cytogenetics_karyotype_summary"]["enabled"] is True
    assert tools["refresh_content"]["enabled"] is False
    assert "provider_availability" not in tools["find_folder"]
    assert "external_provider_allowed" not in tools["find_folder"]
    assert tools["find_folder"]["category"] == "smb"
    assert tools["search_rag_chunks"]["category"] == "database"
    assert tools["cytogenetics_report"]["category"] == "report"


def test_playground_chat_api_generates_request_id(monkeypatch):
    def fake_run(self, request, registry, openai_api_key="", request_id=""):  # noqa: ANN001, ARG001
        return ChatResponse(
            request_id=request_id,
            session_id="synthetic-session",
            provider_used=request.provider,
            model_used="local-model",
            assistant_message="synthetic ok",
        )

    monkeypatch.setattr(PlaygroundAgent, "run", fake_run)

    async def post_chat():
        transport = httpx.ASGITransport(app=_playground_api_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/playground/chat",
                json={
                    "message": "synthetic request",
                    "provider": "local",
                    "selected_tool_ids": ["find_folder"],
                },
            )

    response = asyncio.run(post_chat())

    assert response.status_code == 200
    assert str(UUID(response.json()["request_id"])) == response.json()["request_id"]


async def _post_karyotype_summary(payload: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_playground_api_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/playground/karyotype-summary", json=payload)


def test_karyotype_summary_api_uses_requested_llm_and_returns_its_summary(monkeypatch):
    captured: dict[str, object] = {}

    def fake_chat(self, messages, **kwargs):  # noqa: ANN001, ARG001
        captured["messages"] = messages
        captured.update(kwargs)
        return {"karyotype_summary": "핵형분석요약결과: API에서 선택한 LLM의 결과입니다."}

    monkeypatch.setattr(PlaygroundAgent, "_chat_json", fake_chat)
    raw_iscn = "46,XX,t(9;22)(q34;q11.2)[20]"
    response = asyncio.run(
        _post_karyotype_summary(
            {
                "iscn": raw_iscn,
                "provider": "local",
                "local_base_url": "http://127.0.0.1:18080/v1",
                "model": "qwen3:30b-a3b",
            }
        )
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary_kind"] == "cytogenetics_karyotype_summary"
    assert payload["karyotype_summary"] == "핵형분석요약결과: API에서 선택한 LLM의 결과입니다."
    assert "clone_count" not in payload
    assert "iscn" not in payload
    assert captured["model"] == "qwen3:30b-a3b"
    assert captured["base_url"] == "http://127.0.0.1:18080/v1"
    assert raw_iscn in str(captured["messages"])


def test_karyotype_summary_api_sends_unparsed_notation_to_llm(monkeypatch):
    captured: list[object] = []

    def fake_chat(self, messages, **kwargs):  # noqa: ANN001, ARG001
        captured.extend(messages)
        return {"karyotype_summary": "핵형분석요약결과: LLM이 입력을 검토했습니다."}

    monkeypatch.setattr(PlaygroundAgent, "_chat_json", fake_chat)
    raw_iscn = "46,XX,t(9;22)(q34;q11.2[20]"
    response = asyncio.run(_post_karyotype_summary({"iscn": raw_iscn}))

    assert response.status_code == 200
    assert raw_iscn in str(captured)


@pytest.mark.parametrize(
    ("iscn", "status_code", "error_code"),
    [
        ("", 400, "empty_iscn"),
        ("X" * 501, 422, "iscn_too_long"),
    ],
)
def test_karyotype_summary_api_rejects_input_without_echoing_raw_iscn(iscn, status_code, error_code):
    response = asyncio.run(_post_karyotype_summary({"iscn": iscn}))

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == error_code
    if iscn:
        assert iscn not in response.text


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
