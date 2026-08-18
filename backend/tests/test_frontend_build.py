"""Vue Frontend와 FastAPI 정적 배포 경계의 회귀 테스트."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = REPO_ROOT / "frontend"
WEB_ROOT = REPO_ROOT / "backend" / "src" / "smb_finder" / "web"


def test_frontend_package_uses_vue_vite_and_typescript() -> None:
    """Frontend 원본은 별도 Vue/Vite/TypeScript package로 관리한다."""

    manifest = json.loads((FRONTEND_ROOT / "package.json").read_text(encoding="utf-8"))

    assert "vue" in manifest["dependencies"]
    assert "vite" in manifest["devDependencies"]
    assert "typescript" in manifest["devDependencies"]
    assert manifest["scripts"]["build"] == "vue-tsc -b && vite build"


def test_fastapi_web_directory_contains_vue_production_bundle() -> None:
    """FastAPI 정적 디렉터리에는 Vite production bundle만 둔다."""

    index = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    javascript_bundles = list((WEB_ROOT / "assets").glob("index-*.js"))
    css_bundles = list((WEB_ROOT / "assets").glob("index-*.css"))

    assert 'id="app"' in index
    assert 'type="module"' in index
    assert "/playground/assets/index-" in index
    assert len(javascript_bundles) == 1
    assert len(css_bundles) == 1
    assert not (WEB_ROOT / "assets" / "playground.js").exists()


def test_frontend_selected_file_contract_is_explicit() -> None:
    """왼쪽 파일 선택은 중앙 채팅 요청의 selected_files 계약으로 이어진다."""

    app_source = (FRONTEND_ROOT / "src" / "App.vue").read_text(encoding="utf-8")
    sidebar_source = (FRONTEND_ROOT / "src" / "components" / "FileSidebar.vue").read_text(encoding="utf-8")

    assert "selected_files: selectedFiles.value.map(selectedFilePayload)" in app_source
    assert 'role="option"' in sidebar_source
    assert "선택했습니다" not in app_source
    assert "선택을 해제했습니다" not in app_source
    assert 'strong>{{ uploadPending ? "업로드 중" : "파일 첨부" }}</strong>' in sidebar_source
    assert 'class="settings-button"' in sidebar_source
