# 개발 환경 설정

## 전제

- Windows 11 개발 머신
- Python 3.11 + uv
- Node.js + npm
- 합성 데이터가 적재된 PostgreSQL·MinIO·Neo4j read-only 계정
- 온프레미스 Ollama와 Embedding 모델
- 파일 첨부를 검증할 때 합성 테스트용 SMB share와 쓰기 전용 하위 폴더

## 설치

```powershell
cd C:\VSCodeWorkSpace\automation_smb
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv sync --python 3.11 --native-tls --frozen --extra dev
Remove-Item Env:UV_PROJECT_ENVIRONMENT
npm.cmd --prefix .\frontend ci
```

Python 3.11 의존성은 `backend/.venv`에, npm 의존성은 `frontend/node_modules`에 설치한다. Frontend는 Node.js/npm 자체
격리를 사용하므로 Python 가상환경을 만들지 않는다. 두 디렉터리는 Git에서 제외된다. 사내 SSL 검사 때문에 uv가
실패하지 않도록 `--native-tls`를 사용한다. Backend package는 `backend/src`와 연결되는 editable 형태로 설치되어
소스 수정이 reload에 바로 반영된다. 실제 접속값은 `.env`에만 입력하고 출력·문서·커밋에 복사하지 않는다.

`--native-tls`로도 `invalid peer certificate: UnknownIssuer`가 발생하는 사내 SSL 검사 환경에서는 공개 Python index와
wheel host에만 설치 명령 한 번의 예외를 적용한다.

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv sync --python 3.11 --native-tls --frozen --extra dev `
  --allow-insecure-host pypi.org `
  --allow-insecure-host files.pythonhosted.org
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

파일 검색은 기본적으로 `LLMOPS_FILE_SEARCH_LLM_ENABLED=true`이며 `OLLAMA_BASE_URL`의 온프레미스 모델로 질의를
확장한다. 검색 전용 제한은 `LLMOPS_FILE_SEARCH_LLM_TIMEOUT_MS=6000`, 전체 hard budget은
`LLMOPS_FILE_SEARCH_BUDGET_MS=8000`이다. 세 저장소 중 MinIO·Neo4j가 실패하면 부분 결과와 경고를 반환하고,
PostgreSQL 기준 원장이 실패하면 검색을 503으로 종료한다.

파일 첨부는 `SMB_UPLOAD_ENABLED=true`로 명시적으로 켠 경우에만 동작한다. canonical `SMB_HOST`,
`SMB_SHARE_NAME`, `SMB_USERNAME`, `SMB_PASSWORD`를 우선하며, 공유폴더 안의 상대 업로드 경로만 설정 화면에서 바꾼다.
실제 host·share·계정·비밀번호와 전체 경로는 Browser API에 반환하지 않는다. 첨부 성공은 `indexed=false`이며 자동
검색 반영은 별도 ingestion 범위다.

## 실행

Backend와 Frontend를 Docker 포트와 겹치지 않게 별도 터미널에서 직접 실행한다.

```powershell
# Terminal 1: 저장소 루트에서 시작
cd .\backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8010

# Terminal 2: 저장소 루트
cd .\frontend
npm run dev
```

- 화면: `http://127.0.0.1:5173/playground/`
- OpenAPI: `http://127.0.0.1:8010/docs`
- 상태: `http://127.0.0.1:8010/api/playground/stores/status`
- 공개 설정: `http://127.0.0.1:8010/api/playground/settings`

`5173`을 다른 프로세스가 사용 중이면 Vite가 `5174`처럼 다음 사용 가능한 포트로 자동 전환한다. 이 경우
`npm run dev` 터미널에 표시된 `Local` 주소를 사용한다.

기존처럼 Vite production bundle과 FastAPI를 한 프로세스로 확인하려면 먼저 Frontend를 build한 뒤 Backend를 `8011`에서
실행한다. Docker 컨테이너가 `8011`을 사용 중이면 먼저 중지하거나 다른 포트를 선택한다.

```powershell
npm.cmd --prefix .\frontend run build
cd .\backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8011
```

## 검증

```powershell
.\backend\.venv\Scripts\python.exe -m ruff check backend/src/smb_finder/api.py backend/src/smb_finder/llmops_*.py backend/src/smb_finder/playground/document_*.py
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py backend/tests/test_llmops_stores.py backend/tests/test_llmops_api_contracts.py backend/tests/test_upload_api.py backend/tests/test_local_development.py
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
.\backend\.venv\Scripts\python.exe scripts\evaluate_quality.py
```

PowerShell은 `*`를 Python 프로그램에 전달하지 않을 수 있으므로 실제 Ruff 실행은 품질 스크립트가 사용하는 명시 경로가
가장 안전하다.

레거시 영구 삭제 승인 전에는 `backend/tests/` 전체 수집보다 위 현재 수직 흐름 테스트 또는
`scripts/evaluate_quality.py`를 기준으로 사용한다.
