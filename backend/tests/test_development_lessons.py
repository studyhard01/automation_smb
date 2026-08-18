"""개발 교훈 문서와 검토 범위 판정 테스트."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def lessons_module() -> ModuleType:
    """scripts 패키지화 없이 교훈 검사 모듈을 로드한다."""

    path = REPO_ROOT / "scripts" / "check_development_lessons.py"
    spec = importlib.util.spec_from_file_location("development_lessons_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repository_lessons_document_is_valid(lessons_module: ModuleType) -> None:
    text = (REPO_ROOT / "docs" / "DEVELOPMENT_LESSONS.md").read_text(encoding="utf-8")
    result = lessons_module.validate_lessons_text(text)
    assert result.passed, result.errors
    assert 1 <= result.entries <= lessons_module.MAX_ACTIVE_LESSONS


def test_lessons_require_exact_fields(lessons_module: ModuleType) -> None:
    text = """# 개발 교훈

## 현재 교훈

### 2026-07-20 — 필드 누락
- 상황: 테스트
- 교훈: 테스트

## 새 항목 템플릿
"""
    result = lessons_module.validate_lessons_text(text)
    assert result.passed is False
    assert any("다음 적용" in error for error in result.errors)


def test_lessons_reject_invalid_calendar_date(lessons_module: ModuleType) -> None:
    text = """# 개발 교훈

## 현재 교훈

### 2026-99-40 — 잘못된 날짜
- 상황: 테스트
- 교훈: 테스트
- 다음 적용: 테스트

## 새 항목 템플릿
"""
    result = lessons_module.validate_lessons_text(text)
    assert result.passed is False
    assert any("날짜" in error for error in result.errors)


def test_meaningful_change_scope(lessons_module: ModuleType) -> None:
    assert lessons_module.is_meaningful_change("backend/src/smb_finder/api.py")
    assert lessons_module.is_meaningful_change(".codex/skills/push/SKILL.md")
    assert lessons_module.is_meaningful_change("README.md")
    assert not lessons_module.is_meaningful_change("docs/DEVELOPMENT_LESSONS.md")
    assert not lessons_module.is_meaningful_change("notes/local.txt")


def test_push_review_requires_update_or_explicit_no_change(lessons_module: ModuleType) -> None:
    files = ["backend/src/smb_finder/api.py"]
    assert lessons_module._print_review_status(files, reviewed_no_change=False, strict=True) == 2
    assert lessons_module._print_review_status(files, reviewed_no_change=True, strict=True) == 0
    assert (
        lessons_module._print_review_status(
            [*files, lessons_module.LESSONS_RELATIVE_PATH],
            reviewed_no_change=False,
            strict=True,
        )
        == 0
    )
