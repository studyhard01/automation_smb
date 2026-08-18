"""Codex 도구가 환경 파일을 수정하려는 호출을 실행 전에 차단한다."""

from __future__ import annotations

import json
import re
import sys
from typing import Any


ENV_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])\.env(?:\.[A-Za-z0-9_.-]+)?(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
PATCH_TARGET_PATTERN = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File:\s*(?P<path>.+?)\s*$",
    re.MULTILINE,
)
PATCH_MOVE_PATTERN = re.compile(r"^\*\*\* Move to:\s*(?P<path>.+?)\s*$", re.MULTILINE)
MUTATING_COMMAND_PATTERN = re.compile(
    r"(?ix)"
    r"(?:\b(?:Set-Content|Add-Content|Out-File|Clear-Content|Remove-Item|Move-Item|"
    r"Rename-Item|Copy-Item|New-Item)\b|"
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|mv|cp|touch|truncate|tee|install)\b|"
    r"(?:^|[&|]\s*)(?:del|erase|ren|move|copy)\b|"
    r"\bsed\s+-[^\r\n;|&]*i|\bperl\s+-[^\r\n;|&]*p?i|"
    r"\bgit\s+(?:checkout|restore|clean)\b|"
    r"\b(?:WriteAllText|WriteAllLines|WriteText|write_text|unlink|rename|replace)\b|"
    r"\bopen\s*\([^\r\n]*[, ]\s*['\"][wax+][^'\"]*['\"]|"
    r"(?:^|[^<])>>?)"
)


def _targets_env_file(command: str) -> bool:
    """명령 또는 patch 대상 경로에 .env 계열 파일이 있는지 확인한다."""

    return bool(ENV_PATH_PATTERN.search(command))


def _patch_modifies_env(command: str) -> bool:
    """apply_patch가 .env 계열 파일 자체를 대상으로 하는지 확인한다."""

    targets = [match.group("path") for match in PATCH_TARGET_PATTERN.finditer(command)]
    targets.extend(match.group("path") for match in PATCH_MOVE_PATTERN.finditer(command))
    return any(_targets_env_file(target.strip("'\"")) for target in targets)


def should_block(payload: dict[str, Any]) -> bool:
    """hook 입력이 환경 파일 쓰기인지 판정한다."""

    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    if not command or not _targets_env_file(command):
        return False
    if tool_name == "apply_patch":
        return _patch_modifies_env(command)
    if tool_name == "Bash":
        return bool(MUTATING_COMMAND_PATTERN.search(command))
    return False


def main() -> int:
    """stdin JSON을 검사하고 차단 결정을 stdout JSON으로 반환한다."""

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"환경 파일 보호 hook 입력을 읽지 못했습니다: {exc}", file=sys.stderr)
        return 2

    if should_block(payload):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "환경 파일(.env, .env.*) 수정은 사용자 명시 승인 전까지 차단됩니다. "
                            "승인 후 사용자가 /hooks에서 이 보호 hook을 잠시 비활성화해야 합니다."
                        ),
                    }
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
