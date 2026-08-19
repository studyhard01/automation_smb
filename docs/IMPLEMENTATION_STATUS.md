# 구현 현황

기준일: 2026-08-19

## 현재 상태

| 영역 | 상태 | 구현 내용 |
|---|---|---|
| 자연어 파일 검색 | 완료 | Ollama 검색어 확장과 PostgreSQL·MinIO·Neo4j 병렬 후보 통합 |
| 선택 문서 검증 | 완료 | 대화·기안 직전 활성 Revision과 업로드 registry를 재검증하고 stale이면 409 |
| 선택 문서 대화 | 완료 | 선택 범위 Hybrid/RRF, Citation, 온프레미스 JSON Gateway, deadline·지연 반환 |
| 문서 요약 | 완료 | 문서 대화 API에 표준 요약 프롬프트를 전달하는 Frontend 프리셋 |
| 문서 보기·버전 | 완료 | MinIO Preview/Canonical과 Neo4j Version Graph read-only 조회 |
| 파일 첨부 | 완료 | 허용된 SMB 하위 경로에 `xb` 신규 저장 후 즉시 선택 가능한 업로드 근거 생성 |
| 기안 초안 | 완료 | 근거 선별·패킹, strict V2 생성, 품질 편집, 주장 검증, 질문, XLSX, SMB 신규 저장 |
| 기안 수정 | 완료 | 원본 snapshot과 유형을 유지하고 별도 minor 수정본 생성 |
| 기안 평가 | 완료 | 합성 dataset preflight, gold 비노출 live 실행, 온프레미스 Judge, XLSX·지연 평가 |
| 로그인·회원가입·사용자 관리 | 완료 | 별도 PostgreSQL `auth` schema와 관리자·최고 관리자·서비스 권한 |
| MCP metadata | 완료/기본 비활성 | loopback·token 보호 `/mcp`, UUID metadata tool 1개, bounded executor |
| Playground 권한 강제 | 미완료 | 로그인·서비스 권한은 있으나 모든 Playground 업무 API에 principal 강제 전 |
| 문서별 ACL·감사 | 미완료 | 운영 전 문서 권한과 감사 주체 설계 필요 |
| Docker 패키징 | 완료 | Vue build와 non-root FastAPI runtime, healthcheck, LAN host port 지원 |

## 활성 Backend 구조

- `api.py`: FastAPI app·lifespan·router·정적 Vue 조립
- `model_gateway.py`: 검색·대화·기안이 공유하는 온프레미스 JSON LLM 계약
- `llmops_search.py`: PostgreSQL 파일 후보 검색과 활성 Revision 검증
- `llmops_multistore_search.py`: 검색어 확장과 세 저장소 후보 통합
- `llmops_retrieval.py`: 선택 Revision Hybrid/RRF Chunk 검색
- `llmops_artifacts.py`: PostgreSQL 검증 후 MinIO artifact 조회
- `llmops_graph.py`: Neo4j 문서·Revision 관계 조회
- `playground/document_*.py`: 문서 API·근거 대화·계약
- `playground/upload_*.py`: runtime 설정·SMB 첨부·업로드 근거
- `playground/proposal_*.py`: 기안 근거·생성·검증·XLSX·수정·다운로드
- `auth/`: 인증·세션·사용자·서비스 권한
- `mcp_server.py`, `tooling/`: 선택적 read-only metadata MCP
- `evaluation/proposal_*.py`: 기안 전용 합성 평가

## 현재 공개 API

- `POST /api/playground/files/search`
- `POST /api/playground/chat`
- `GET /api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}`
- `GET /api/playground/files/{doc_id}/graph`
- `GET /api/playground/stores/status`
- `GET /api/playground/settings`
- `PATCH /api/playground/settings/upload`
- `PATCH /api/playground/settings/proposal-draft`
- `POST /api/playground/files/upload`
- `POST /api/playground/drafts/proposal`
- `POST /api/playground/drafts/proposal/{draft_id}/clarifications`
- `POST /api/playground/drafts/proposal/{draft_id}/revisions`
- `GET /api/playground/drafts/proposal/{draft_id}`
- `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/seelis-login`
- `GET /api/auth/me`, `POST /api/auth/logout`
- `GET /api/users`, `PATCH /api/users/{user_id}`
- `GET /health`, `GET /playground`, `GET /login`, `GET /register`, `GET /user`
- `/mcp` — 기본 비활성, OpenAPI 비노출

## 제거 대상 판정 결과

2026-08-19에 다음 비활성 구현과 연결 테스트·스크립트·계획 문서를 제거했다.

- 직접 SMB/SQLite 인덱스·Finder·Content Search
- 이전 범용 Playground Agent·Tool·Skill CRUD
- 이전 RAG·Telemetry·MLflow 문서 챗봇 평가
- QC 감사·핵형·NGS 보고서 데모
- 별도 LangGraph Studio integration과 dormant Bot Core graph scaffold
- 프런트에서 사용하지 않던 빈 기안 템플릿 API와 구 XLSX 삽입 호환 함수
- 직접 실행 명령을 중복하던 Backend·Frontend PowerShell wrapper

현재 runtime에 필요한 `extract.py`, MCP/tooling, 기안 평가 모듈은 유지했다. 환경 파일과 실제 데이터·캐시는 정리
대상에서 제외했다.

## 알려진 후속 과제

1. Playground API에 로그인 principal과 서비스 권한을 실제 강제한다.
2. 실제 합성 저장소 기준 검색 정확도와 cold/warm 지연 baseline을 갱신한다.
3. `proposal_draft.py`의 생성·검증·OOXML 렌더 책임을 기능 변경 없이 단계적으로 분리한다.
4. 운영 전 문서별 ACL·감사 로그·SSO 경계를 설계한다.

## 최근 검증

2026-08-19 구조 정리본에서 다음을 확인했다.

- Ruff 통과, Backend 비통합 테스트 `271 passed`
- Frontend typecheck 통과, Vitest `12 files / 71 tests passed`, production build 통과
- 프로젝트 품질 평가 `85.00/100`, 목표 달성, hard gate 통과, 목표 미달 항목 없음
- 합성 실측 baseline 미등록으로 evidence freshness는 `skipped`이며 운영 전 갱신 필요
- Docker 이미지 build 및 컨테이너 health 통과, `/playground`와 OpenAPI 200
- 합성 read-only 파일 검색 200, 3건, `150.7ms` (단일 smoke 값이며 p50/p95 baseline은 아님)
