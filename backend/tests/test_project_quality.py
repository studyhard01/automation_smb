"""저장소 품질 rubric과 evaluator의 현재 계약 테스트."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def quality_module() -> ModuleType:
    path = REPO_ROOT / "scripts" / "evaluate_quality.py"
    spec = importlib.util.spec_from_file_location("project_quality_evaluator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rubric(quality_module: ModuleType) -> dict:
    return quality_module.load_rubric(REPO_ROOT / "config" / "project_quality_rubric.json")


def test_rubric_is_versioned_and_totals_100(rubric: dict) -> None:
    assert rubric["version"] == "2.0.0"
    assert rubric["target_score"] == 85
    assert sum(category["weight"] for category in rubric["categories"]) == 100
    assert all(
        sum(criterion["points"] for criterion in category["criteria"]) == category["weight"]
        for category in rubric["categories"]
    )


def test_scoring_and_hard_gate_exit_policy(quality_module: ModuleType, rubric: dict) -> None:
    results = []
    for category in rubric["categories"]:
        for criterion in category["criteria"]:
            results.append(
                quality_module._result_from_outcome(category, criterion, quality_module.CheckOutcome(True, "ok"))
            )
    report = quality_module.summarize_results(rubric, results)
    assert report["score"] == 100
    assert quality_module.determine_exit_code(report, strict_score=False) == 0

    failed = list(results)
    index = next(i for i, result in enumerate(failed) if result.hard_gate)
    failed[index] = quality_module.CriterionResult(**{**failed[index].__dict__, "status": "failed", "awarded": 0.0})
    assert quality_module.determine_exit_code(quality_module.summarize_results(rubric, failed), strict_score=False) == 1


def test_markdown_links_and_canonical_docs_are_aligned(quality_module: ModuleType, rubric: dict) -> None:
    links = quality_module.check_markdown_links(REPO_ROOT)
    alignment = quality_module.check_docs_alignment(REPO_ROOT, rubric["policy"])
    assert links.passed, links.details
    assert alignment.passed, alignment.details


def test_command_orchestration_targets_active_backend(quality_module: ModuleType, rubric: dict) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_runner(command: list[str], cwd: Path, env: dict[str, str]):
        calls.append((command, cwd, env))
        return quality_module.CommandResult(returncode=0, stdout="ok")

    results = quality_module.evaluate_project(REPO_ROOT, rubric, command_runner=fake_runner)
    calls_by_tool = {command[2]: command for command, _cwd, _env in calls if len(command) > 2}
    assert set(calls_by_tool) == {"ruff", "pytest"}
    assert "backend/src/smb_finder/api.py" in calls_by_tool["ruff"]
    assert "backend/src/smb_finder/playground/proposal_evidence.py" in calls_by_tool["ruff"]
    assert "backend/src/smb_finder/evaluation/proposal_live_runner.py" in calls_by_tool["ruff"]
    assert "backend/tests/test_proposal_live_runner.py" in calls_by_tool["ruff"]
    assert "backend/tests/test_llmops_search.py" in calls_by_tool["pytest"]
    assert "backend/tests/test_proposal_live_runner.py" in calls_by_tool["pytest"]
    pytest_call = next((command, env) for command, _cwd, env in calls if len(command) > 2 and command[2] == "pytest")
    basetemp_argument = next(item for item in pytest_call[0] if item.startswith("--basetemp="))
    basetemp = Path(basetemp_argument.split("=", maxsplit=1)[1]).resolve()
    assert not basetemp.is_relative_to(REPO_ROOT.resolve())
    assert Path(pytest_call[1]["TEMP"]).resolve() == basetemp.parent
    assert Path(pytest_call[1]["TMP"]).resolve() == basetemp.parent
    assert Path(pytest_call[1]["TMPDIR"]).resolve() == basetemp.parent
    assert not basetemp.parent.exists()
    assert all(result.status != "failed" for result in results)


def test_static_only_does_not_claim_skipped_hard_gates_passed(quality_module: ModuleType, rubric: dict) -> None:
    results = quality_module.evaluate_project(REPO_ROOT, rubric, static_only=True)
    report = quality_module.summarize_results(rubric, results)
    assert sorted(report["skipped_hard_gates"]) == ["pytest_non_integration", "ruff"]
    assert report["hard_gates_passed"] is False


def test_json_report_contract_is_machine_readable(quality_module: ModuleType, rubric: dict, tmp_path: Path) -> None:
    result = quality_module.CriterionResult(
        id="sample",
        title="샘플",
        category_id=rubric["categories"][0]["id"],
        category_title=rubric["categories"][0]["title"],
        points=1,
        awarded=1,
        hard_gate=True,
        status="passed",
        summary="ok",
        details=(),
    )
    output = tmp_path / "quality.json"
    quality_module._write_json_report(output, quality_module.summarize_results(rubric, [result]))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["rubric_version"] == "2.0.0"
    assert "hard_gates_passed" in loaded


def test_active_runtime_has_no_external_llm_or_legacy_search_imports() -> None:
    api_source = (REPO_ROOT / "backend" / "src" / "smb_finder" / "api.py").read_text(encoding="utf-8")
    chat_source = (REPO_ROOT / "backend" / "src" / "smb_finder" / "playground" / "document_chat.py").read_text(
        encoding="utf-8"
    )
    assert "mcp_server" not in api_source
    assert "content_index" not in api_source
    assert "openai.com" not in chat_source
    assert "selected_files" in chat_source
