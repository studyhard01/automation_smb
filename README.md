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
- 기안 초안: 선택 문서 근거와 사용자 설명으로 로컬 LLM의 근거 인용·section·문단·목록·표를 포함한 strict V2 문서를 만들고, 기존 3필드/기안 템플릿과 호환 투영해 SMB 신규 저장한 뒤 대화창 다운로드 제공
- 기안 평가: 외부 dataset을 읽기 전용으로 받아 근거·LLM 내용·XLSX·도구 흐름을 100점으로 채점하고, runtime/model 컨텍스트 초과를 원문 없이 집계
- 파일 첨부: `[업로드] 문서명_YYYYMMDD_v1.0.확장자` 저장 규칙으로 SMB share 내부 상대 경로에 비덮어쓰기 저장하고, 업로드 직후 대화 참고 파일에 자동 추가해 원본을 즉시 근거로 사용
- 환경 설정: 왼쪽 하단 설정에서 업로드·기안 상대 경로와 연결 상태만 관리하며 주소·계정·비밀번호는 노출하지 않음

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
│  │  ├─ playground/document_*.py    # 선택 문서 채팅 API·서비스·계약
│  │  ├─ playground/upload_*.py      # 제한된 SMB 첨부·비밀 없는 runtime 설정
│  │  ├─ playground/proposal_*.py    # 선택 문서 기반 구조화 기안·XLSX 생성·다운로드 API
│  │  └─ evaluation/proposal_*.py    # 기안 dataset preflight·gold 비노출 live 생성·100점 평가·비식별 통계
│  └─ tests/                         # Backend 단위·계약 테스트
├─ docs/                             # 목표·현황·설계·운영 문서
├─ scripts/                          # 실행·품질 검사·기안 평가 CLI
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
| 기안 상대 경로 변경 | `PATCH /api/playground/settings/proposal-draft` |
| LLM 기안 XLSX 생성·SMB 저장 | `POST /api/playground/drafts/proposal` |
| 기안 필수정보 답변·완성 | `POST /api/playground/drafts/proposal/{draft_id}/clarifications` |
| 기안 피드백 수정본 XLSX 생성·SMB 저장 | `POST /api/playground/drafts/proposal/{draft_id}/revisions` |
| 생성 기안 XLSX 다운로드 | `GET /api/playground/drafts/proposal/{draft_id}` |
| 빈 기안 템플릿 다운로드 | `GET /api/playground/drafts/proposal` |
| 공유폴더 파일 첨부 | `POST /api/playground/files/upload` |
| 서비스 상태 | `GET /health` |
| Vue 화면 | `GET /playground` |

파일 선택은 화면 상태만 믿지 않습니다. 채팅 요청 직전에 Backend가 선택한 UUID pair가 현재 활성 Revision인지 다시
검증하며, 변경됐으면 `409 selected_file_stale`을 반환합니다. 업로드 파일도 서버 runtime registry의 UUID pair를 다시
검증하며, 클라이언트가 보낸 파일명이나 경로를 원본 조회 경로로 신뢰하지 않습니다.

## 설치와 실행

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb
uv sync --python 3.11 --native-tls --extra dev
Copy-Item .env.example .env
npm.cmd --prefix .\frontend install
npm.cmd --prefix .\frontend run build

powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 -Action Start -Port 8011
```

화면은 `http://127.0.0.1:8011/playground`, OpenAPI는 `http://127.0.0.1:8011/docs`에서 확인합니다. `.env`에는
PostgreSQL·MinIO·Neo4j의 read-only 계정, 온프레미스 Ollama 주소와 필요할 때 SMB 접속 정보를 넣습니다. SMB 첨부와
기안 자동 저장은 `SMB_UPLOAD_ENABLED=true`로 명시적으로 켜야 하며, 실제 값·내부 주소·파일 목록은 코드·문서·로그에
기록하지 않습니다.

### Docker와 LAN 실행

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\docker\prepare_env.ps1
docker compose build
docker compose up -d
```

로컬 주소는 `http://127.0.0.1:8011/playground`, 같은 LAN의 다른 PC에서는
`http://<Docker-host-LAN-IPv4>:8011/playground`를 사용합니다. 포트 충돌, 사내 SSL 검사, Windows 방화벽과
컨테이너 endpoint 설정은 [Docker 배포 문서](docs/DOCKER_DEPLOYMENT.md)를 따릅니다. `8013`은 기본값이 아니라
필요할 때 현재 셸에서 `$env:AUTOMATION_SMB_PORT = "8013"`으로 지정하는 host port override입니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_quality.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py backend/tests/test_llmops_stores.py backend/tests/test_llmops_api_contracts.py backend/tests/test_proposal_draft.py backend/tests/test_proposal_evaluation.py backend/tests/test_proposal_live_runner.py backend/tests/test_upload_api.py
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
- [기안 초안 작성](docs/PROPOSAL_DRAFT.md)
- [기안 생성 도구 평가](docs/PROPOSAL_EVALUATION.md)
- [운영 아키텍처](docs/PRODUCTION_ARCHITECTURE.md)
- [품질 기준](docs/PROJECT_QUALITY_RUBRIC.md)
- [개발 교훈](docs/DEVELOPMENT_LESSONS.md)
