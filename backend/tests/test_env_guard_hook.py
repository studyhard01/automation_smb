"""환경 파일 보호 Codex hook 회귀 테스트."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".codex" / "hooks" / "protect_env_files.py"


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("protect_env_files", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _payload(tool_name: str, command: str) -> dict[str, object]:
    return {"tool_name": tool_name, "tool_input": {"command": command}}


def test_blocks_apply_patch_update_to_env_local() -> None:
    hook = _load_hook()
    command = "*** Begin Patch\n*** Update File: C:/workspace/.env.local\n*** End Patch"

    assert hook.should_block(_payload("apply_patch", command)) is True


def test_blocks_shell_write_to_env() -> None:
    hook = _load_hook()

    assert hook.should_block(_payload("Bash", "Set-Content -LiteralPath .env -Value 'x=1'")) is True


def test_blocks_python_write_to_env() -> None:
    hook = _load_hook()

    assert hook.should_block(_payload("Bash", 'python -c "open(\'.env.local\', \'w\').write(\'x=1\')"')) is True


def test_allows_reading_env() -> None:
    hook = _load_hook()

    assert hook.should_block(_payload("Bash", "Get-Content -LiteralPath .env.local")) is False


def test_allows_read_only_env_mount() -> None:
    hook = _load_hook()
    command = "docker run --rm --mount type=bind,source=.env.local,target=/run/.env.local,readonly image check"

    assert hook.should_block(_payload("Bash", command)) is False


def test_allows_patch_to_non_env_file() -> None:
    hook = _load_hook()
    command = "*** Begin Patch\n*** Update File: README.md\n*** End Patch"

    assert hook.should_block(_payload("apply_patch", command)) is False
