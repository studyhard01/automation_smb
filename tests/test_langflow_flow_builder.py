"""Langflow 워크플로우 자동 생성기 테스트."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from integrations.langflow.flow_builder import LangflowFlowInstaller, WorkflowPlanner, render_langflow_flow
from integrations.langflow.scripts.generate_flow import generate_workflow_file


def test_planner_selects_folder_search_for_korean_request():
    spec = WorkflowPlanner().plan("검사실 일반 사용자가 공유폴더를 찾는 workflow를 만들어줘", llm_provider="rule")

    assert spec.template_id == "folder_search"
    assert spec.planner == "rule"
    assert spec.confidence >= 0.7


def test_planner_selects_folder_search_for_english_request():
    spec = WorkflowPlanner().plan("Create a workflow that lets staff find SMB folders", llm_provider="rule")

    assert spec.template_id == "folder_search"
    assert spec.service_url == "http://localhost:8010"


def test_renderer_reuses_smb_folder_finder_without_secrets():
    spec = WorkflowPlanner().plan(
        r"환자 홍길동 \\example.invalid\share SMB_PASSWORD= 폴더 찾기",
        llm_provider="rule",
        service_url="http://host.docker.internal:8010",
    )
    result = render_langflow_flow(spec, generated_at=datetime(2026, 7, 3, tzinfo=timezone.utc))
    dumped = json.dumps(result.flow, ensure_ascii=False)

    assert "SMBFolderFinder" in dumped
    assert "ChatInput" in dumped
    assert "ChatOutput" in dumped
    assert "http://host.docker.internal:8010" in dumped
    assert "OPENAI_API_KEY" not in dumped
    assert "ADMIN_API_TOKEN" not in dumped
    assert "SMB_PASSWORD" not in dumped
    assert "example.invalid" not in dumped
    assert "홍길동" not in dumped


def test_service_url_rejects_public_hosts():
    with pytest.raises(ValueError):
        WorkflowPlanner().plan(
            "공유폴더 찾기 workflow",
            llm_provider="rule",
            service_url="https://attacker.example",
        )


def test_auto_planner_does_not_call_external_local_llm(monkeypatch):
    called = False

    def fake_post(self, *args, **kwargs):  # noqa: ANN001, ARG001
        nonlocal called
        called = True
        raise AssertionError("external endpoint must not be called")

    monkeypatch.setattr(httpx.Client, "post", fake_post)

    spec = WorkflowPlanner().plan(
        "새 workflow를 구성해줘",
        llm_provider="auto",
        local_base_url="https://llm.example/v1",
        local_model="model",
    )

    assert called is False
    assert "local_llm_url_not_internal" in spec.warnings


def test_renderer_rejects_unsupported_template_request():
    spec = WorkflowPlanner().plan("폴더 내용 DB화 workflow를 만들어줘", llm_provider="rule")

    assert spec.supported is False
    with pytest.raises(ValueError):
        render_langflow_flow(spec)


def test_installer_creates_flow_via_langflow_api():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert request.method == "POST"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["name"] == "SMB 공유폴더 찾기"
        assert payload["data"]["nodes"]
        assert "metadata" not in payload
        return httpx.Response(
            201,
            json={"id": "11111111-1111-1111-1111-111111111111", "name": payload["name"]},
        )

    spec = WorkflowPlanner().plan("공유폴더 찾기 workflow", llm_provider="rule")
    flow = render_langflow_flow(spec).flow
    installer = LangflowFlowInstaller(transport=httpx.MockTransport(handler))

    result = installer.install(flow)

    assert result.action == "created"
    assert result.flow_url.endswith("/flow/11111111-1111-1111-1111-111111111111")
    assert [request.method for request in requests] == ["GET", "POST"]


def test_installer_updates_existing_flow():
    flow_id = "22222222-2222-2222-2222-222222222222"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": flow_id, "name": "SMB 공유폴더 찾기"}])
        assert request.method == "PATCH"
        return httpx.Response(200, json={"id": flow_id, "name": "SMB 공유폴더 찾기"})

    spec = WorkflowPlanner().plan("공유폴더 찾기 workflow", llm_provider="rule")
    flow = render_langflow_flow(spec).flow
    installer = LangflowFlowInstaller(transport=httpx.MockTransport(handler))

    result = installer.install(flow)

    assert result.action == "updated"
    assert [request.method for request in requests] == ["GET", "PATCH"]


def test_installer_rejects_public_langflow_url():
    with pytest.raises(ValueError):
        LangflowFlowInstaller(langflow_url="https://langflow.example")


def test_installer_sends_folder_id_in_payload():
    folder_id = "33333333-3333-3333-3333-333333333333"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["folder_id"] == folder_id
        assert not request.url.params
        return httpx.Response(201, json={"id": "44444444-4444-4444-4444-444444444444", "name": payload["name"]})

    spec = WorkflowPlanner().plan("공유폴더 찾기 workflow", llm_provider="rule")
    flow = render_langflow_flow(spec).flow
    installer = LangflowFlowInstaller(transport=httpx.MockTransport(handler))

    result = installer.install(flow, folder_id=folder_id)

    assert result.action == "created"


def test_cli_writes_flow_json(tmp_path):
    out = tmp_path / "folder_search.json"
    meta = generate_workflow_file(
        instruction="공유폴더 찾기 workflow",
        out=out,
        llm_provider="rule",
    )

    flow = json.loads(out.read_text(encoding="utf-8"))
    assert out.exists()
    assert meta["template_id"] == "folder_search"
    assert flow["metadata"]["template_id"] == "folder_search"
    assert flow["metadata"]["reused_component"] == "SMBFolderFinder"
