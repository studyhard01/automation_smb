"""저장소 품질 rubric과 evaluator의 핵심 계약 테스트."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def quality_module() -> ModuleType:
    """scripts 패키지화 없이 evaluator 모듈을 로드한다."""

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


def test_rubric_is_versioned_100_points_with_exact_category_weights(rubric: dict) -> None:
    assert rubric["version"] == "1.0.0"
    assert rubric["target_score"] == 85
    assert [category["weight"] for category in rubric["categories"]] == [25, 20, 20, 15, 10, 10]
    assert sum(category["weight"] for category in rubric["categories"]) == 100
    assert all(
        sum(criterion["points"] for criterion in category["criteria"]) == category["weight"]
        for category in rubric["categories"]
    )


def test_scoring_and_hard_gate_exit_policy(quality_module: ModuleType, rubric: dict) -> None:
    results = []
    for category in rubric["categories"]:
        for criterion in category["criteria"]:
            outcome = quality_module.CheckOutcome(True, "ok")
            results.append(quality_module._result_from_outcome(category, criterion, outcome))
    report = quality_module.summarize_results(rubric, results)
    assert report["score"] == 100
    assert quality_module.determine_exit_code(report, strict_score=False) == 0

    failed = list(results)
    first_hard = next(index for index, result in enumerate(failed) if result.hard_gate)
    failed[first_hard] = quality_module.CriterionResult(
        **{**failed[first_hard].__dict__, "status": "failed", "awarded": 0.0}
    )
    hard_failed_report = quality_module.summarize_results(rubric, failed)
    assert quality_module.determine_exit_code(hard_failed_report, strict_score=False) == 1

    advisory_only = list(results)
    for index, result in enumerate(advisory_only):
        if not result.hard_gate:
            advisory_only[index] = quality_module.CriterionResult(**{**result.__dict__, "status": "advisory", "awarded": 0.0})
    low_report = quality_module.summarize_results(rubric, advisory_only)
    assert low_report["hard_gates_passed"] is True
    assert quality_module.determine_exit_code(low_report, strict_score=False) == 0
    assert quality_module.determine_exit_code(low_report, strict_score=True) == 2


def test_synthetic_fixture_contract_and_hygiene(quality_module: ModuleType, rubric: dict) -> None:
    fixture_result = quality_module.check_synthetic_fixtures(REPO_ROOT, rubric["policy"])
    sensitive_result = quality_module.check_sensitive_values(REPO_ROOT, rubric["policy"])
    assert fixture_result.passed, fixture_result.details
    assert sensitive_result.passed, sensitive_result.details


def test_markdown_links_and_canonical_docs_are_aligned(quality_module: ModuleType, rubric: dict) -> None:
    links = quality_module.check_markdown_links(REPO_ROOT)
    alignment = quality_module.check_docs_alignment(REPO_ROOT, rubric["policy"])
    assert links.passed, links.details
    assert alignment.passed, alignment.details


def test_command_orchestration_uses_workspace_basetemp_without_recursive_execution(
    quality_module: ModuleType,
    rubric: dict,
) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_runner(command: list[str], cwd: Path, env: dict[str, str]):
        calls.append((command, cwd, env))
        return quality_module.CommandResult(returncode=0, stdout="ok")

    results = quality_module.evaluate_project(
        REPO_ROOT,
        rubric,
        command_runner=fake_runner,
    )
    calls_by_tool = {command[2]: (command, cwd, env) for command, cwd, env in calls}
    assert set(calls_by_tool) == {"ruff", "pytest"}
    pytest_command = calls_by_tool["pytest"][0]
    basetemp_arg = next(item for item in pytest_command if item.startswith("--basetemp="))
    assert str(REPO_ROOT / ".tmp" / "quality") in basetemp_arg
    assert pytest_command[3:6] == ["tests", "-m", "not integration"]
    assert all(result.status != "failed" for result in results)


def test_static_only_does_not_claim_skipped_hard_gates_passed(quality_module: ModuleType, rubric: dict) -> None:
    results = quality_module.evaluate_project(REPO_ROOT, rubric, static_only=True)
    report = quality_module.summarize_results(rubric, results)
    assert sorted(report["skipped_hard_gates"]) == ["pytest_non_integration", "ruff"]
    assert report["hard_gates_passed"] is False
    assert quality_module.determine_exit_code(report, strict_score=False) == 1


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
    report = quality_module.summarize_results(rubric, [result])
    output = tmp_path / "quality.json"
    quality_module._write_json_report(output, report)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["rubric_version"] == "1.0.0"
    assert "hard_gates_passed" in loaded
