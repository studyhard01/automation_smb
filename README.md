# automation_smb

`automation_smb`는 팀 공유 문서를 자연어로 찾고, 사용자가 선택한 문서만 근거로 대화·요약·기안 초안을 만드는
온프레미스 문서 업무 서비스입니다.

현재 저장소는 **합성 데이터 기능 검증 단계**입니다. 실제 의료데이터 사용 승인을 의미하지 않으며, 외부 LLM 없이
사내 PostgreSQL·MinIO·Neo4j·Ollama와 제한된 SMB 경로만 사용하도록 설계했습니다.

## 가장 먼저 이해할 한 문장

> 자연어로 파일 검색 → 후보 문서 선택 → 선택한 활성 Revision 안에서만 근거 검색 → Citation이 있는 답변·요약 또는 기안 XLSX 생성

## 사용자가 할 수 있는 일

| 기능 | 사용자 경험 | Backend 처리 |
|---|---|---|
| 문서 검색 | 왼쪽 패널에 찾고 싶은 파일을 자연어로 입력 | Ollama 검색어 확장 후 PostgreSQL·MinIO·Neo4j 후보를 병렬 통합 |
| 선택 문서 대화 | 검색 결과에서 최대 5개 문서를 선택하고 질문 | 선택한 `(doc_id, revision_id)`만 Hybrid/RRF 검색하고 근거 기반 답변 생성 |
| 문서 요약 | 오른쪽 `문서 요약` 실행 | 전용 서버 기능이 아니라 선택 문서 대화에 표준 요약 프롬프트를 전달 |
| 기안 초안 작성 | 참고 문서와 목적을 입력하고 XLSX 다운로드 | 근거 선별·구조화 생성·주장 검증·XLSX 렌더 후 SMB에 새 파일로 저장 |
| 파일 첨부 | 로컬 파일을 대화 참고 문서로 추가 | 허용된 SMB 하위 경로에 비덮어쓰기 저장 후 runtime registry로 참조 |
| 문서 보기·버전 확인 | Preview/Canonical과 Revision 관계 확인 | MinIO와 Neo4j를 read-only로 조회 |
| 사용자 관리 | 가입·로그인·서비스 권한 관리 | 문서 저장소와 분리된 PostgreSQL `auth` schema 사용 |

## 핵심 동작 흐름

```text
Vue UI
  ├─ 파일 검색 ──> FastAPI ──> Ollama 검색어 확장
  │                              ├─ PostgreSQL 문서/Chunk
  │                              ├─ MinIO artifact registry
  │                              └─ Neo4j Revision graph
  │
  ├─ 대화·요약 ─> 활성 Revision 재검증 ─> 선택 범위 Hybrid/RRF ─> Ollama 답변 + Citation
  │
  └─ 기안 작성 ─> 선택 범위 근거 검색 ─> 근거 선별/패킹 ─> 생성/검증 ─> XLSX ─> SMB create-only
```

문서 검색과 문서 내용 검색은 다릅니다.

- **파일 검색**은 어떤 문서를 선택할지 찾는 단계입니다.
- **선택 문서 검색**은 선택한 Revision 내부에서 답변 근거 Chunk를 찾는 단계입니다.
- 화면이 보낸 UUID를 그대로 신뢰하지 않고, 대화와 기안 직전에 활성 Revision을 다시 검증합니다.

## 저장소 구조

```text
automation_smb/
├─ frontend/                         # Vue 3 + TypeScript 원본과 UI 테스트
│  └─ src/
│     ├─ App.vue                     # 검색·대화·요약·기안 화면 오케스트레이션
│     ├─ api/                        # FastAPI 호출 client
│     └─ components/                 # 검색/대화/설정/인증 화면
├─ backend/
│  ├─ main.py                        # 로컬 uvicorn 진입점
│  ├─ src/smb_finder/
│  │  ├─ api.py                      # FastAPI app, lifespan, router 조립
│  │  ├─ config.py                   # 환경변수 기반 runtime 설정
│  │  ├─ model_gateway.py            # 온프레미스 JSON LLM 호출 공통 계약
│  │  ├─ models.py                   # 문서 검색·근거·저장소 상태 계약
│  │  ├─ intent.py                   # 빠른 검색어 정규화
│  │  ├─ extract.py                  # 업로드 문서 텍스트 추출
│  │  ├─ llmops_search.py            # PostgreSQL 파일 후보 검색
│  │  ├─ llmops_multistore_search.py # PostgreSQL·MinIO·Neo4j 후보 통합
│  │  ├─ llmops_retrieval.py         # 선택 Revision Hybrid/RRF 검색
│  │  ├─ llmops_artifacts.py         # MinIO Preview/Canonical 조회
│  │  ├─ llmops_graph.py             # Neo4j 버전 관계 조회
│  │  ├─ playground/
│  │  │  ├─ document_api.py          # 검색·보기·상태·대화 API
│  │  │  ├─ document_chat.py         # 근거 답변 서비스
│  │  │  ├─ document_models.py       # 대화 API 계약
│  │  │  ├─ upload_api.py            # 설정·첨부·기안 API
│  │  │  ├─ upload_service.py        # SMB create-only 저장과 업로드 근거
│  │  │  ├─ proposal_draft.py        # 기안 생성·검증·XLSX 렌더·registry
│  │  │  ├─ proposal_context.py      # 기안 근거 예산·패킹
│  │  │  └─ proposal_evidence.py     # 유형별 참고 문서 선별
│  │  ├─ auth/                       # 로그인·세션·사용자·서비스 권한
│  │  ├─ evaluation/                 # 기안 전용 합성 데이터 평가
│  │  └─ web/                        # Vite production 산출물; 직접 수정 금지
│  └─ tests/                         # 현재 기능만 검증하는 Backend 테스트
├─ docs/                             # 현재 설계·운영·품질 문서
├─ scripts/                          # 실행·품질 검사·기안 평가 CLI
├─ Dockerfile                        # Vue build + non-root FastAPI image
├─ compose.yaml                      # 로컬/LAN container 실행
├─ pyproject.toml                    # Python 의존성과 Ruff/Pytest 설정
└─ uv.lock                           # 재현 가능한 Python lock
```

핵심 답변 구현은 `backend/src/smb_finder/playground/document_chat.py`, 기안 구현은
`backend/src/smb_finder/playground/proposal_draft.py`에 있습니다.

## 기술 구성과 소유권

| 영역 | 기술 | 이 저장소의 책임 |
|---|---|---|
| Frontend | Vue 3, TypeScript, Vite | 검색·선택·대화·기안·인증 UI |
| API | FastAPI, Pydantic | 입력 검증, deadline, 오류 계약, 응답 조립 |
| 문서 metadata/Chunk | PostgreSQL, pgvector, pg_trgm | read-only 후보 및 Hybrid/RRF 검색 |
| 문서 artifact | MinIO | read-only Preview/Canonical 조회 |
| 버전 관계 | Neo4j | read-only Document/Revision 관계 조회 |
| LLM/Embedding | 온프레미스 Ollama | 검색어 확장, Embedding, 답변, 기안 생성·검증 |
| 파일 저장 | SMB | 승인된 하위 경로에 신규 파일만 생성 |
| 인증 | 별도 PostgreSQL `auth` schema | 사용자·세션·서비스 권한 저장 |

문서 수집·변환·Chunk·Embedding 적재는 upstream 데이터 파이프라인이 소유합니다. 이 서비스는 문서 저장소를
생성하거나 수정하지 않는 consumer입니다.

## 로컬 개발 시작

### 1. 준비물

- Python 3.11
- `uv`
- Node.js 22와 npm
- 기능별로 필요한 PostgreSQL·MinIO·Neo4j·Ollama·SMB 접속 설정

환경값은 루트 `.env`에만 둡니다. 기존 `.env`가 있으면 덮어쓰지 말고, 처음 준비할 때만 `.env.example`을 참고합니다.
자격증명과 내부 주소는 코드·문서·로그에 기록하지 않습니다.

### 2. 의존성 설치

PowerShell에서 저장소 루트를 기준으로 실행합니다.

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
try {
  uv sync --python 3.11 --native-tls --frozen --extra dev
} finally {
  Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
}
npm.cmd --prefix .\frontend ci
```

사내 SSL 검사로 Python 패키지 설치가 실패할 때만 해당 설치 명령에
`--allow-insecure-host pypi.org --allow-insecure-host files.pythonhosted.org`를 추가합니다.

### 3. 개발 서버 실행

첫 번째 터미널:

```powershell
cd .\backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8010
```

두 번째 터미널:

```powershell
cd .\frontend
npm run dev
```

- Frontend: `http://127.0.0.1:5173/playground/`
- Backend OpenAPI: `http://127.0.0.1:8010/docs`
- Backend health: `http://127.0.0.1:8010/health`

Vite는 `/api`와 `/health`를 기본적으로 `127.0.0.1:8010`으로 전달합니다. Backend 포트를 바꾸면 Frontend 시작 전에
`VITE_BACKEND_URL`을 설정합니다.

## Docker 실행

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\docker\prepare_env.ps1
docker compose build
docker compose up -d
```

- 기본 화면: `http://127.0.0.1:8011/playground`
- 기본 health: `http://127.0.0.1:8011/health`
- 다른 host port가 필요하면 compose 실행 전 `AUTOMATION_SMB_PORT`를 현재 셸에 지정합니다.

세부 사항은 [Docker 배포 문서](docs/DOCKER_DEPLOYMENT.md)를 따릅니다.

## 테스트와 품질 확인

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
try {
  uv run --no-sync ruff check backend/src backend/tests scripts
  uv run --no-sync pytest -m "not integration" backend/tests
  uv run --no-sync python scripts/evaluate_quality.py
} finally {
  Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
}
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
```

실제 저장소 smoke는 합성 데이터가 준비된 read-only 환경에서 수행합니다. 실제 SMB 쓰기 smoke는 사용자가 대상과
실행을 별도로 승인한 경우에만 수행합니다.

## 주요 API

| 목적 | Endpoint |
|---|---|
| 자연어 파일 검색 | `POST /api/playground/files/search` |
| 선택 문서 대화·요약 | `POST /api/playground/chat` |
| Preview/Canonical | `GET /api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}` |
| 버전 관계 | `GET /api/playground/files/{doc_id}/graph` |
| 저장소 상태 | `GET /api/playground/stores/status` |
| 공개 설정 | `GET /api/playground/settings` |
| 업로드/기안 상대 경로 | `PATCH /api/playground/settings/upload`, `PATCH /api/playground/settings/proposal-draft` |
| 파일 첨부 | `POST /api/playground/files/upload` |
| 기안 생성 | `POST /api/playground/drafts/proposal` |
| 필수정보 답변 후 완성 | `POST /api/playground/drafts/proposal/{draft_id}/clarifications` |
| 기안 수정본 생성 | `POST /api/playground/drafts/proposal/{draft_id}/revisions` |
| 생성 XLSX 다운로드 | `GET /api/playground/drafts/proposal/{draft_id}` |
| 인증·사용자 관리 | `/api/auth/*`, `/api/users/*` |
| 서비스 상태 | `GET /health` |

## 반드시 지켜야 하는 경계

- 합성 데이터 단계가 끝나기 전 실제 환자·검체 데이터를 입력하지 않습니다.
- 문서 저장소 PostgreSQL·MinIO·Neo4j는 read-only입니다.
- SMB는 기존 항목 수정·이동·삭제·덮어쓰기를 하지 않고 `xb` 방식 신규 생성만 허용합니다.
- 문서 내용·경로·목록을 외부 LLM이나 외부 저장소로 보내지 않습니다.
- `.env`와 `.env.*`는 승인 없이 수정하지 않습니다.
- 실제 SMB 쓰기, 외부 배포, Git push는 각각 별도 승인이 필요합니다.
- 모든 DB·Embedding·LLM·SMB 단계에 timeout/deadline을 유지하고 지연을 기록합니다.

## 현재 범위 밖

- 로컬 SMB 전체 순회와 자체 SQLite 인덱싱
- 외부 LLM provider
- 문서 저장소 schema/object/graph 쓰기
- 운영 SSO, 문서별 ACL 강제, 실제 의료데이터 운영

## 인수인계 시 읽을 문서

[문서 안내](docs/README.md)에 읽는 순서와 문서별 책임을 정리했다. 처음에는 다음 네 문서만 순서대로 읽는다.

1. [현재 구현 상태](docs/IMPLEMENTATION_STATUS.md)
2. [개발 환경](docs/DEVELOPMENT_SETUP.md)
3. [운영 아키텍처](docs/PRODUCTION_ARCHITECTURE.md)
4. [제품 목표와 다음 단계](docs/PRODUCT_DEVELOPMENT_PLAN.md)

새 담당자는 먼저 `/health`와 `/api/playground/stores/status`로 연결 상태를 확인한 뒤, 합성 데이터로
검색 → 선택 → 대화 → 요약 → 기안 생성 순서의 smoke를 수행하면 됩니다.
