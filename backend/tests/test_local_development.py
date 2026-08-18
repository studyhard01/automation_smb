"""개발자 친화적인 로컬 Backend·Frontend 실행 계약의 회귀 테스트."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_local_dependencies_are_excluded_from_git() -> None:
    """로컬 의존성 디렉터리는 Git 추적에서 제외한다."""

    gitignore = _read(".gitignore")

    assert "backend/.venv/" in gitignore
    assert "frontend/node_modules/" in gitignore


def test_readme_runs_backend_with_direct_uvicorn_command() -> None:
    """Backend 개발 서버는 가상환경 활성화 후 uvicorn으로 직접 실행한다."""

    readme = _read("README.md")
    entrypoint = _read("backend/main.py")

    assert "Activate.ps1" in readme
    assert "uvicorn main:app --reload" in readme
    assert "from smb_finder.api import app" in entrypoint


def test_readme_runs_frontend_with_npm_and_vite_targets_backend() -> None:
    """Frontend는 npm으로 실행하며 Vite는 로컬 Backend와 사용 가능한 포트를 선택한다."""

    readme = _read("README.md")
    vite_config = _read("frontend/vite.config.ts")

    assert "cd .\\frontend" in readme
    assert "npm run dev" in readme
    assert 'process.env.VITE_BACKEND_URL || "http://127.0.0.1:8010"' in vite_config
    assert "strictPort: false" in vite_config
    assert "strictPort: false" in vite_config
