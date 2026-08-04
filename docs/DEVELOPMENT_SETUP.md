# 개발 환경 설정

## 전제

- Windows 11 개발 머신
- Python 3.11 + uv
- Node.js + npm
- 합성 데이터가 적재된 PostgreSQL·MinIO·Neo4j read-only 계정
- 온프레미스 Ollama와 Embedding 모델

## 설치

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb
uv sync --python 3.11 --native-tls --extra dev
Copy-Item .env.example .env
npm.cmd --prefix .\frontend install
```

사내 SSL 검사 때문에 uv가 실패하면 `--native-tls`를 유지한다. 실제 접속값은 `.env`에만 입력하고 출력·문서·커밋에
복사하지 않는다.

## 실행

```powershell
npm.cmd --prefix .\frontend run build
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 -Action Start -Port 8011
```

- 화면: `http://127.0.0.1:8011/playground`
- OpenAPI: `http://127.0.0.1:8011/docs`
- 상태: `http://127.0.0.1:8011/api/playground/stores/status`

Vite HMR이 필요하면 별도 터미널에서 다음을 실행한다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_frontend.ps1 -Action Dev
```

## 검증

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/src/smb_finder/api.py backend/src/smb_finder/llmops_*.py backend/src/smb_finder/playground/document_*.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py backend/tests/test_llmops_stores.py backend/tests/test_llmops_api_contracts.py
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
uv run --no-sync python scripts/evaluate_quality.py
```

PowerShell은 `*`를 Python 프로그램에 전달하지 않을 수 있으므로 실제 Ruff 실행은 품질 스크립트가 사용하는 명시 경로가
가장 안전하다.

레거시 영구 삭제 승인 전에는 `backend/tests/` 전체 수집보다 위 현재 수직 흐름 테스트 또는
`scripts/evaluate_quality.py`를 기준으로 사용한다.
