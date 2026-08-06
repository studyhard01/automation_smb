# automation_smb

LLMOps 데이터셋을 읽기 전용으로 조회하고, 명시적으로 허용된 SMB 하위 폴더에만 파일을 첨부하는 공유 문서
검색·대화 서비스입니다. 현재 단계는 실제 의료 환경과 무관한
합성 데이터 기능 검증이며, 제품의 성공 기준은 다음 한 줄입니다.

> 왼쪽 패널에서 자연어로 파일 검색(멀티스토어) → 후보 선택 → 선택한 Revision 범위만 검색 → 중앙 채팅에서 근거와 함께 답변

## 현재 제품 경계

- Frontend: Vue 3 + TypeScript + Vite
- Backend: FastAPI + Pydantic, `backend/src/` 패키지
- 검색: 온프레미스 Ollama 질의 확장 + PostgreSQL·MinIO·Neo4j read-only 통합 조회
- 문서 근거: 선택한 `(doc_id, revision_id)` 범위를 강제한 Hybrid/RRF Chunk 검색
- 답변: 온프레미스 Ollama만 사용, Citation과 검색 지연 반환
- 문서 보기: MinIO Preview/Canonical read-only 조회
- 버전 관계: Neo4j Document/Revision 관계 read-only 조회
- 파일 첨부: 설정된 SMB share 내부 상대 경로에만 저장, 크기·확장자 제한과 기존 파일 비덮어쓰기 적용
- 환경 설정: 왼쪽 하단 설정에서 업로드 상대 경로와 연결 상태만 관리하며 주소·계정·비밀번호는 노출하지 않음

현재 제품 경계에서 제외한 항목은 Langflow, MCP, LangGraph Studio, 로컬 SQLite/SMB 직접 인덱싱, 범용 Tool/Skill
편집기, QC·유전검사 데모, 외부 LLM provider입니다. DB가 연결되지 않았을 때 규칙 기반 가짜 결과나 fixture로
대체하지 않고 명시적인 오류를 반환합니다.

## 구조

```text
automation_smb/
├─ frontend/                         # Vue 원본과 UI 테스트
├─ backend/
│  ├─ src/smb_finder/
│  │  ├─ api.py                      # FastAPI 진입점
│  │  ├─ llmops_search.py            # 자연어 파일 후보 검색
│  │  ├─ llmops_multistore_search.py # LLM·PostgreSQL·MinIO·Neo4j 검색 통합
│  │  ├─ llmops_retrieval.py         # 선택 Revision Hybrid/RRF 검색
│  │  ├─ llmops_artifacts.py         # MinIO 문서 보기
│  │  ├─ llmops_graph.py             # Neo4j 버전 관계
│  │  ├─ auth/                        # 별도 PostgreSQL 로그인·사용자·서비스 권한
│  │  ├─ playground/document_*.py    # 선택 문서 채팅 API·서비스·계약
│  │  └─ playground/upload_*.py      # 제한된 SMB 첨부·비밀 없는 runtime 설정
│  └─ tests/                         # Backend 단위·계약 테스트
├─ docs/                             # 목표·현황·설계·운영 문서
├─ scripts/                          # 실행·품질 검사 스크립트
├─ Dockerfile                        # Vue build + FastAPI non-root image
├─ compose.yaml                      # runtime env·LAN port·healthcheck
├─ docker.env.example                # container endpoint override 예시
├─ pyproject.toml
└─ uv.lock
```

핵심 답변 구현은 `backend/src/smb_finder/playground/document_chat.py`에 있다.

`backend/src/smb_finder/web/`은 Vite production 산출물입니다. 직접 수정하지 않고 `npm run build`로 갱신합니다.

## API

| 목적 | Endpoint |
|---|---|
| 멀티스토어 자연어 파일 검색 | `POST /api/playground/files/search` |
| 선택 문서 근거 대화 | `POST /api/playground/chat` |
| Preview/Canonical | `GET /api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}` |
| 버전 관계 | `GET /api/playground/files/{doc_id}/graph` |
| 저장소 연결 상태 | `GET /api/playground/stores/status` |
| 공개 설정 조회 | `GET /api/playground/settings` |
| 업로드 상대 경로 변경 | `PATCH /api/playground/settings/upload` |
| 공유폴더 파일 첨부 | `POST /api/playground/files/upload` |
| 회원가입 / 로그인 / 현재 사용자 / 로그아웃 | `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` |
| 관리자 사용자 목록·권한 변경 | `GET /api/users`, `PATCH /api/users/{user_id}` |
| 서비스 상태 | `GET /health` |
| Vue 화면 | `GET /playground`, `GET /login`, `GET /register`, `GET /user` |

파일 선택은 화면 상태만 믿지 않습니다. 채팅 요청 직전에 Backend가 선택한 UUID pair가 현재 활성 Revision인지 다시
검증하며, 변경됐으면 `409 selected_file_stale`을 반환합니다.

## 설치와 실행

### Backend·Frontend 로컬 분리 실행

Docker 방식은 그대로 유지한다. 개발 중에는 Backend와 Frontend를 두 터미널에서 직접 실행한다.
Backend는 `backend/.venv`의 Python 3.11 환경을 사용하고, Frontend는 프로젝트별 `frontend/node_modules`를 사용한다.
Node.js/npm은 자체적으로 프로젝트 의존성을 격리하므로 Frontend용 Python 가상환경은 만들지 않는다.
Backend package는 `backend/src`와 연결되는 editable 형태로 가상환경에 설치되므로 소스 수정이 reload에 바로 반영된다.

최초 한 번 `.env`를 준비하고 의존성을 설치한다. `UV_PROJECT_ENVIRONMENT`는 설치 위치만
`backend/.venv`로 지정하며, 설치가 끝나면 현재 터미널에서 제거한다. 기존 `.env`가 있으면 복사하지 않는다.

```powershell
cd C:\VSCodeWorkSpace\automation_smb
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv sync --python 3.11 --native-tls --frozen --extra dev
Remove-Item Env:UV_PROJECT_ENVIRONMENT
npm.cmd --prefix .\frontend ci
```

사내 SSL 검사 때문에 `invalid peer certificate: UnknownIssuer`가 발생한 경우에만 공개 Python index와 wheel host를
설치 명령 한 번에 한정해 허용한 후 다시 실행한다.

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv sync --python 3.11 --native-tls --frozen --extra dev `
  --allow-insecure-host pypi.org `
  --allow-insecure-host files.pythonhosted.org
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

첫 번째 터미널에서 Backend 가상환경을 활성화하고, Docker 기본 포트 `8011`과 충돌하지 않는 `8010`에서
reload 개발 서버를 직접 실행한다.

```powershell
cd C:\VSCodeWorkSpace\automation_smb
cd .\backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8010
```

두 번째 터미널에서 Vite 개발 서버를 직접 시작한다. `/api`와 `/health` 요청은 기본적으로 Backend `8010`으로 전달된다.

```powershell
cd C:\VSCodeWorkSpace\automation_smb
cd .\frontend
npm run dev
```

- Frontend: `http://127.0.0.1:5173/playground/`
- Backend OpenAPI: `http://127.0.0.1:8010/docs`
- Backend health: `http://127.0.0.1:8010/health`
- Backend·Frontend 중지: 각 실행 터미널에서 `Ctrl+C`
- Backend 가상환경 종료: Backend 터미널에서 `deactivate`

`backend/.venv`와 `frontend/node_modules`는 Git에서 제외된다. Backend 포트를 변경하려면 Frontend 실행 전에
`$env:VITE_BACKEND_URL = "http://127.0.0.1:<변경한 포트>"`를 설정한다. Frontend `5173`이 이미 사용 중이면 Vite는
`5174`처럼 다음 사용 가능한 포트로 자동 시작하므로, 터미널에 표시된 `Local` 주소로 접속한다.

인증 저장소는 기존 LLMOps read-only 계정과 분리한다. `.env`의 `AUTH_DB_USER`·`AUTH_DB_PASSWORD`에 `AUTH_DB_SCHEMA`을
생성하고 쓸 수 있는 별도 계정을 넣으면 Backend 시작 시 `users`, `services`, `user_service_permissions`, `sessions`를
idempotent하게 만든다. 최초 관리자 생성 시에만 `AUTH_INITIAL_ADMIN_PASSWORD`를 넣고, 생성 확인 뒤 즉시 값을 지운다.
가입 사용자는 기본적으로 `user`, 서비스 접근 없음이며 관리자가 `/user` 화면에서 권한을 명시적으로 부여한다.
합성 데이터 전용 로컬 DB의 기존 계정에 인증 schema 쓰기 권한을 따로 확인한 경우에만
`AUTH_DB_USE_LLMOPS_CREDENTIALS=true`로 명시적 재사용할 수 있으며, 운영에서는 별도 최소 권한 계정을 사용한다.

### 기존 단일 서비스 로컬 실행

```powershell
cd C:\VSCodeWorkSpace\automation_smb
npm.cmd --prefix .\frontend run build
cd .\backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8011
```

화면은 `http://127.0.0.1:8011/playground`, OpenAPI는 `http://127.0.0.1:8011/docs`에서 확인합니다. `.env`에는
PostgreSQL·MinIO·Neo4j의 read-only 계정, 온프레미스 Ollama 주소와 필요할 때 SMB 접속 정보를 넣습니다. SMB 첨부는
`SMB_UPLOAD_ENABLED=true`로 명시적으로 켜야 하며, 실제 값·내부 주소·파일 목록은 코드·문서·로그에 기록하지 않습니다.

### Docker와 LAN 실행

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\docker\prepare_env.ps1
docker compose build
docker compose up -d
```

로컬 주소는 `http://127.0.0.1:8011/playground`, 같은 LAN의 다른 PC에서는
`http://<Docker-host-LAN-IPv4>:8011/playground`를 사용합니다. 포트 충돌, 사내 SSL 검사, Windows 방화벽과
컨테이너 endpoint 설정은 [Docker 배포 문서](docs/DOCKER_DEPLOYMENT.md)를 따릅니다.

## 테스트

```powershell
.\backend\.venv\Scripts\python.exe scripts\evaluate_quality.py
.\backend\.venv\Scripts\python.exe -m pytest backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py backend/tests/test_llmops_stores.py backend/tests/test_llmops_api_contracts.py backend/tests/test_upload_api.py backend/tests/test_local_development.py
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
```

영구 삭제 승인 전까지 `backend/tests/`에는 실행 경로에서 분리된 레거시 테스트가 물리적으로 남아 있으므로 전체 디렉터리
수집 대신 위 현재 수직 흐름 명령이나 [품질 평가 script](scripts/evaluate_quality.py)를 사용합니다.

실제 연결 smoke는 합성 데이터가 들어 있는 read-only DB 저장소에서 수행합니다. SMB 쓰기 smoke는 승인된 합성 파일과
설정된 업로드 하위 폴더에서만 별도로 수행합니다.

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8011/api/playground/files/search `
  -ContentType 'application/json' -Body '{"query":"합성 WBS 찾아줘","limit":5}'

Invoke-RestMethod -Uri http://127.0.0.1:8011/api/playground/stores/status
```

## 문서

- [현재 개발 목표와 계획](docs/PRODUCT_DEVELOPMENT_PLAN.md)
- [구현 현황](docs/IMPLEMENTATION_STATUS.md)
- [정리 인벤토리](docs/CLEANUP_INVENTORY.md)
- [DB 연동 계약](docs/DATASET_DB_INTEGRATION_PLAN.md)
- [Frontend 구조](docs/FRONTEND_ARCHITECTURE.md)
- [개발 환경](docs/DEVELOPMENT_SETUP.md)
- [Docker 배포와 LAN 접속](docs/DOCKER_DEPLOYMENT.md)
- [파일 첨부와 설정](docs/FILE_UPLOAD_AND_SETTINGS.md)
- [운영 아키텍처](docs/PRODUCTION_ARCHITECTURE.md)
- [품질 기준](docs/PROJECT_QUALITY_RUBRIC.md)
- [개발 교훈](docs/DEVELOPMENT_LESSONS.md)
