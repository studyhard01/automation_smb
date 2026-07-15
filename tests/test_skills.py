"""Playground SKILL.md 저장·slash command·agent prompt 적용 테스트."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from smb_finder.config import Settings
from smb_finder.playground.agent import PlaygroundAgent
from smb_finder.playground.api import create_playground_router
from smb_finder.playground.models import ChatRequest
from smb_finder.playground.skills import SkillStore, SkillStoreError
from smb_finder.playground.tools import PlaygroundRuntime, build_tool_registry


def _document(skill_id: str, body: str = "Follow this synthetic workflow.") -> str:
    return (
        "---\n"
        f"name: {skill_id}\n"
        "description: Use for a synthetic custom workflow.\n"
        "---\n\n"
        f"# Custom Skill\n\n{body}\n"
    )


def test_builtin_skills_use_real_skill_md_shape(tmp_path):
    skills = {skill.id: skill for skill in SkillStore(tmp_path).list()}

    assert {"tools", "skills", "rag-grounded-answer", "smb-navigation", "report-workflow", "latency-first"} <= set(
        skills
    )
    assert skills["tools"].document.startswith("---\nname: tools\n")
    assert skills["tools"].source == "builtin"
    assert skills["tools"].editable is False


def test_user_skill_crud_persists_as_skill_md(tmp_path):
    store = SkillStore(tmp_path)

    created = store.create("custom-flow", _document("custom-flow"))
    assert created.editable is True
    assert (tmp_path / "custom-flow" / "SKILL.md").read_text(encoding="utf-8") == created.document

    updated = store.update("custom-flow", _document("custom-flow", "Use the updated workflow."))
    assert "updated workflow" in updated.instructions

    store.delete("custom-flow")
    assert store.get("custom-flow") is None


def test_skill_frontmatter_name_must_match_directory_id(tmp_path):
    with pytest.raises(SkillStoreError) as exc_info:
        SkillStore(tmp_path).create("custom-flow", _document("different-name"))

    assert exc_info.value.code == "skill_name_mismatch"


def test_slash_commands_do_not_require_an_llm_connection(tmp_path):
    settings = Settings(_env_file=None, llm_model="", openai_api_key="", playground_skills_dir=str(tmp_path))
    runtime = PlaygroundRuntime(settings=settings)
    agent = PlaygroundAgent(settings, skill_store=SkillStore(tmp_path))
    registry = build_tool_registry(runtime)

    tools_response = agent.run(ChatRequest(message="/tools", provider="openai"), registry)
    skills_response = agent.run(ChatRequest(message="/skills", provider="openai"), registry)

    assert tools_response.error_code == ""
    assert "find_folder" in tools_response.assistant_message
    assert tools_response.active_skill_ids == ["tools"]
    assert skills_response.error_code == ""
    assert "rag-grounded-answer" in skills_response.assistant_message
    assert skills_response.active_skill_ids == ["skills"]


def test_selected_skill_document_is_injected_into_system_prompt(monkeypatch, tmp_path):
    store = SkillStore(tmp_path)
    store.create("custom-flow", _document("custom-flow", "Always answer with the synthetic workflow marker."))
    settings = Settings(
        _env_file=None,
        llm_model="local-model",
        llm_base_url="http://127.0.0.1:18080/v1",
        playground_skills_dir=str(tmp_path),
    )
    agent = PlaygroundAgent(settings, skill_store=store)
    captured: dict[str, object] = {}

    def fake_chat(messages, **kwargs):  # noqa: ANN001, ARG001
        captured["messages"] = messages
        return {"action": "final_answer", "answer": "synthetic ok", "rationale": "skill applied"}

    monkeypatch.setattr(agent, "_chat_json", fake_chat)

    response = agent.run(
        ChatRequest(message="run synthetic flow", selected_skill_ids=["custom-flow"]),
        registry={},
    )

    system_message = captured["messages"][0]["content"]  # type: ignore[index]
    assert "name: custom-flow" in system_message
    assert "synthetic workflow marker" in system_message
    assert response.active_skill_ids == ["custom-flow"]


def test_skills_api_crud_uses_configured_local_directory(tmp_path):
    settings = Settings(
        _env_file=None,
        llm_model="local-model",
        llm_base_url="http://127.0.0.1:18080/v1",
        playground_skills_dir=str(tmp_path),
    )
    app = FastAPI()
    app.include_router(create_playground_router(lambda: PlaygroundRuntime(settings=settings)))

    async def exercise_api() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get("/api/playground/skills")
            created = await client.post(
                "/api/playground/skills",
                json={"skill_id": "custom-flow", "document": _document("custom-flow")},
            )
            updated = await client.put(
                "/api/playground/skills/custom-flow",
                json={"document": _document("custom-flow", "Updated instructions.")},
            )
            deleted = await client.delete("/api/playground/skills/custom-flow")
            return listed, created, updated, deleted

    listed, created, updated, deleted = asyncio.run(exercise_api())

    assert listed.status_code == 200
    assert any(skill["id"] == "tools" for skill in listed.json())
    assert created.status_code == 201
    assert created.json()["source"] == "user"
    assert "Updated instructions" in updated.json()["instructions"]
    assert deleted.status_code == 204
