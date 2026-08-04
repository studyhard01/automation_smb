"""짧은 개발 교훈 문서의 형식과 commit/push 검토 계약을 검사한다."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]
LESSONS_RELATIVE_PATH = "docs/DEVELOPMENT_LESSONS.md"
LESSONS_PATH = REPO_ROOT / LESSONS_RELATIVE_PATH
MAX_ACTIVE_LESSONS = 12
MAX_ENTRY_CHARACTERS = 700
CURRENT_SECTION = "## 현재 교훈"
TEMPLATE_SECTION = "## 새 항목 템플릿"
LESSON_HEADER = re.compile(r"^### (?P<date>\d{4}-\d{2}-\d{2}) — (?P<title>.+)$", re.MULTILINE)
REQUIRED_FIELDS = ("- 상황:", "- 교훈:", "- 다음 적용:")
MEANINGFUL_PREFIXES = (
    "backend/src/",
    "backend/tests/",
    "scripts/",
    "config/",
    "integrations/",
    ".github/",
    ".githooks/",
    ".codex/skills/",
)
MEANINGFUL_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "OS.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
}


@dataclass(frozen=True)
class LessonsValidation:
    """교훈 문서 검사 결과."""

    entries: int
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """오류가 없으면 통과한다."""

        return not self.errors


def _active_section(text: str) -> str | None:
    start = text.find(CURRENT_SECTION)
    end = text.find(TEMPLATE_SECTION)
    if start < 0 or end < 0 or end <= start:
        return None
    return text[start + len(CURRENT_SECTION) : end]


def validate_lessons_text(text: str) -> LessonsValidation:
    """현재 교훈의 개수, 필드, 길이, 최신순을 검사한다."""

    errors: list[str] = []
    section = _active_section(text)
    if section is None:
        return LessonsValidation(0, ("`현재 교훈`과 `새 항목 템플릿` 구획이 필요합니다.",))

    matches = list(LESSON_HEADER.finditer(section))
    if not matches:
        return LessonsValidation(0, ("현재 교훈이 한 개 이상 필요합니다.",))
    if len(matches) > MAX_ACTIVE_LESSONS:
        errors.append(f"현재 교훈은 최대 {MAX_ACTIVE_LESSONS}개만 유지합니다.")

    dates: list[str] = []
    for index, match in enumerate(matches):
        entry_end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        entry = section[match.start() : entry_end].strip()
        title = match.group("title").strip()
        recorded_on = match.group("date")
        dates.append(recorded_on)
        try:
            date.fromisoformat(recorded_on)
        except ValueError:
            errors.append(f"`{title}` 항목의 날짜가 유효하지 않습니다.")
        if not title:
            errors.append(f"{index + 1}번 교훈 제목이 비어 있습니다.")
        if len(entry) > MAX_ENTRY_CHARACTERS:
            errors.append(f"`{title}` 항목이 {MAX_ENTRY_CHARACTERS}자를 넘습니다.")
        for field in REQUIRED_FIELDS:
            if sum(line.startswith(field) for line in entry.splitlines()) != 1:
                errors.append(f"`{title}` 항목에 `{field}` 필드가 정확히 한 개 필요합니다.")
    if dates != sorted(dates, reverse=True):
        errors.append("현재 교훈은 날짜 최신순으로 정렬해야 합니다.")
    return LessonsValidation(len(matches), tuple(errors))


def is_meaningful_change(path: str) -> bool:
    """교훈 검토가 필요한 코드·설정·핵심 문서 변경인지 판정한다."""

    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    return normalized in MEANINGFUL_FILES or normalized.startswith(MEANINGFUL_PREFIXES)


def _git_changed_files(arguments: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", *arguments],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError("Git 변경 범위를 읽지 못했습니다.")
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def _staged_lessons_text(changed_files: list[str]) -> str:
    if LESSONS_RELATIVE_PATH not in changed_files:
        return LESSONS_PATH.read_text(encoding="utf-8")
    result = subprocess.run(
        ["git", "show", f":{LESSONS_RELATIVE_PATH}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError("staged 개발 교훈 문서를 읽지 못했습니다.")
    return result.stdout


def _print_review_status(changed_files: list[str], *, reviewed_no_change: bool, strict: bool) -> int:
    meaningful = [path for path in changed_files if path != LESSONS_RELATIVE_PATH and is_meaningful_change(path)]
    lessons_updated = LESSONS_RELATIVE_PATH in changed_files
    if not meaningful or lessons_updated:
        return 0
    if reviewed_no_change:
        print("개발 교훈 검토: 재사용 가능한 새 교훈 없음으로 명시했습니다.")
        return 0
    message = (
        "개발 교훈 검토 필요: 코드·설정 변경이 있지만 docs/DEVELOPMENT_LESSONS.md 변경이 없습니다. "
        "재사용 가능한 문제가 있었다면 문서를 갱신하세요."
    )
    if strict:
        print(message, file=sys.stderr)
        print("새 교훈이 없다면 push 검사에 --reviewed-no-change를 명시하세요.", file=sys.stderr)
        return 2
    print(f"알림: {message}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="개발 교훈 문서와 commit/push 검토 계약 검사")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="staged 변경을 검사하고 교훈 갱신 필요를 알림")
    mode.add_argument("--diff-range", help="push할 Git diff 범위를 검사하며 교훈 검토를 강제")
    parser.add_argument(
        "--reviewed-no-change",
        action="store_true",
        help="diff-range에 재사용 가능한 새 교훈이 없음을 명시",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.reviewed_no_change and not args.diff_range:
        print("--reviewed-no-change는 --diff-range와 함께 사용해야 합니다.", file=sys.stderr)
        return 3
    try:
        if args.staged:
            changed_files = _git_changed_files(["--cached"])
            text = _staged_lessons_text(changed_files)
        else:
            changed_files = _git_changed_files([args.diff_range]) if args.diff_range else []
            text = LESSONS_PATH.read_text(encoding="utf-8")
        result = validate_lessons_text(text)
    except (OSError, RuntimeError) as exc:
        print(f"개발 교훈 검사 오류: {exc}", file=sys.stderr)
        return 3

    if not result.passed:
        print("개발 교훈 문서 형식 오류:", file=sys.stderr)
        for error in result.errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"개발 교훈 문서: {result.entries}개 항목, 형식 통과")
    return _print_review_status(
        changed_files,
        reviewed_no_change=args.reviewed_no_change,
        strict=bool(args.diff_range),
    )


if __name__ == "__main__":
    raise SystemExit(main())
