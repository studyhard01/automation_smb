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
cd C:\Users\AI_team\Desktop\project\automation_smb
uv sync --python 3.11 --native-tls --extra dev
Copy-Item .env.example .env
npm.cmd --prefix .\frontend install
```

사내 SSL 검사 때문에 uv가 실패하면 `--native-tls`를 유지한다. 실제 접속값은 `.env`에만 입력하고 출력·문서·커밋에
복사하지 않는다.

파일 검색은 기본적으로 `LLMOPS_FILE_SEARCH_LLM_ENABLED=true`이며 `OLLAMA_BASE_URL`의 온프레미스 모델로 질의를
확장한다. 검색 전용 제한은 `LLMOPS_FILE_SEARCH_LLM_TIMEOUT_MS=6000`, 전체 hard budget은
`LLMOPS_FILE_SEARCH_BUDGET_MS=8000`이다. 세 저장소 중 MinIO·Neo4j가 실패하면 부분 결과와 경고를 반환하고,
PostgreSQL 기준 원장이 실패하면 검색을 503으로 종료한다.

파일 첨부는 `SMB_UPLOAD_ENABLED=true`로 명시적으로 켠 경우에만 동작한다. canonical `SMB_HOST`,
`SMB_SHARE_NAME`, `SMB_USERNAME`, `SMB_PASSWORD`를 우선하며, 공유폴더 안의 상대 업로드 경로만 설정 화면에서 바꾼다.
실제 host·share·계정·비밀번호와 전체 경로는 Browser API에 반환하지 않는다. 첨부 성공은 `indexed=false`이며 자동
검색 반영은 별도 ingestion 범위다.

## 실행

```powershell
npm.cmd --prefix .\frontend run build
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 -Action Start -Port 8011
```

- 화면: `http://127.0.0.1:8011/playground`
- OpenAPI: `http://127.0.0.1:8011/docs`
- 상태: `http://127.0.0.1:8011/api/playground/stores/status`
- 공개 설정: `http://127.0.0.1:8011/api/playground/settings`

Vite HMR이 필요하면 별도 터미널에서 다음을 실행한다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_frontend.ps1 -Action Dev
```

## 검증

```powershell
.\.venv\Scripts\python.exe -m ruff check backend/src/smb_finder/api.py backend/src/smb_finder/llmops_*.py backend/src/smb_finder/playground/document_*.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py backend/tests/test_llmops_stores.py backend/tests/test_llmops_api_contracts.py backend/tests/test_upload_api.py
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
uv run --no-sync python scripts/evaluate_quality.py
```

PowerShell은 `*`를 Python 프로그램에 전달하지 않을 수 있으므로 실제 Ruff 실행은 품질 스크립트가 사용하는 명시 경로가
가장 안전하다.

레거시 영구 삭제 승인 전에는 `backend/tests/` 전체 수집보다 위 현재 수직 흐름 테스트 또는
`scripts/evaluate_quality.py`를 기준으로 사용한다.
