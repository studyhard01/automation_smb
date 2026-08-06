# 구현 현황

기준일: 2026-08-06

## 한눈에 보기

| 영역 | 상태 | 현재 구현 |
|---|---|---|
| Frontend/Backend 분리 | 완료 | `frontend/`, `backend/src`, `backend/tests` |
| Figma Playground 정렬 | 완료 | 헤더 71px, 3열 288/816/336px, 중앙 대화·하단 Composer 구조 |
| 자연어 파일 검색 | 완료 | 로컬 LLM 확장 + PostgreSQL·MinIO·Neo4j 통합 검색 |
| 파일 후보 선택 | 완료 | 최대 5개, UUID pair를 채팅 요청에 전달 |
| 선택 문서 검증 | 완료 | 채팅 직전 활성 Revision 재검증, stale이면 409 |
| 선택 범위 검색 | 완료 | Hybrid/RRF, scope 위반 차단, Citation 반환 |
| 근거 답변 | 완료 | 온프레미스 Ollama native JSON, 근거 없으면 LLM 미호출 |
| 오른쪽 문서 기능 | 완료 | 문서 요약·보고서 초안만 제공, Q&A는 중앙 채팅으로 통합 |
| MinIO 보기 | 완료 | Preview/Canonical read-only, Object URI 미노출 |
| Neo4j 버전 관계 | 완료 | 각 검색 결과의 버전 확인 버튼에서 즉시 조회 |
| 저장소 상태 | 완료 | PostgreSQL·MinIO·Neo4j 연결/degraded 상태 표시 |
| 파일 첨부 | 완료 | 설정된 SMB share 하위 상대 경로에 크기·확장자 제한, 비덮어쓰기 저장 |
| 환경 설정 | 완료 | 왼쪽 하단 설정에서 업로드 상대 경로와 저장소·로컬 LLM 상태 관리, 비밀·내부 주소 미노출 |
| 로그인·회원가입·사용자 관리 | 완료 | Vue 화면, FastAPI API, PostgreSQL `auth` schema의 사용자·서비스·권한·세션 연동 |
| 서비스 권한 저장 | 완료 | 관리자·최고 관리자 분리, 전체 또는 서비스별 권한, 자기 잠금·마지막 최고 관리자 보호 |
| Playground 권한 강제 | 미구현 | 개발 단계 직접 접속 요구로 `/playground/`와 업무 API에는 아직 인증·서비스 권한을 강제하지 않음 |
| Docker 패키징 | 완료 | Vue/FastAPI 단일 non-root image, Compose runtime env·healthcheck·LAN port |
| LAN 접속 | 부분 완료 | host LAN 주소 HTTP 200 확인, 다른 물리 PC와 Windows 방화벽 규칙은 관리자 확인 필요 |
| 지연 목표 | 미달 | 검색은 목표권, LLM 포함 채팅은 추가 개선 필요 |
| 문서별 ACL·감사 | 미구현 | 운영 전 서비스 권한 강제, 문서별 접근 제어와 권한 변경 감사 설계·승인 필요 |
| 실제 보고서 Artifact | 미구현 | 현재는 선택 문서 기반 텍스트 초안만 생성 |

## 활성 Backend 경로

- `api.py`: app lifespan, Vue bundle, health
- `llmops_search.py`: 파일 후보 검색과 활성 Revision 검증
- `llmops_multistore_search.py`: 로컬 LLM 질의 확장, 세 저장소 병렬 조회와 후보 통합
- `llmops_retrieval.py`: 선택 범위 Hybrid/RRF Chunk 검색
- `llmops_artifacts.py`: PostgreSQL ACL/Revision 확인 후 MinIO 조회
- `llmops_graph.py`: Neo4j 버전 관계 조회
- `playground/document_api.py`: 현재 공개 API
- `playground/document_chat.py`: 근거 검색과 로컬 LLM 합성
- `playground/document_models.py`: 채팅 API 계약
- `playground/upload_api.py`: 공개 설정 GET/PATCH와 multipart 파일 첨부 API
- `playground/upload_service.py`: runtime 상대 경로 저장, SMB 제한 쓰기와 비덮어쓰기
- `playground/upload_models.py`: 비밀 없는 설정·첨부 응답 계약
- `auth/api.py`: 회원가입·로그인·세션·관리자 사용자 API
- `auth/service.py`: 비밀번호 검증과 관리자·최고 관리자 정책
- `auth/store.py`: 별도 PostgreSQL `auth` schema와 사용자·서비스 권한·세션 transaction

## 현재 API

- `POST /api/playground/files/search`
- `POST /api/playground/chat`
- `GET /api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}`
- `GET /api/playground/files/{doc_id}/graph`
- `GET /api/playground/stores/status`
- `GET /api/playground/settings`
- `PATCH /api/playground/settings/upload`
- `POST /api/playground/files/upload`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/auth/logout`
- `GET /api/users`
- `PATCH /api/users/{user_id}`
- `GET /health`
- `GET /playground`
- `GET /login`, `GET /register`, `GET /user`

## 제거 대상 판정

현재 목표에서 제외되어 새 실행 경로가 참조하지 않는 묶음은 다음과 같다.

- 직접 SMB/SQLite: `content_*`, `finder.py`, `index*.py`, `smb_client.py`, `jobs.py`
- 예전 RAG·평가: `rag_search.py`, `evaluation/`, MLflow 평가 script/data
- 범용 확장 실험: `mcp_server.py`, `tooling/`, `integrations/langgraph/`
- 별도 데모: `qc_audit/`, `reports/`, Playground Tool/Skill/attachment 코드
- 관련 테스트·문서·생성 잔여물

새 runtime과 UI에서는 이미 연결을 끊었다. 물리적 영구 삭제는 대량 삭제 승인 Gate로 남겨 두며, 승인 후 한 번에 지운 뒤
전체 검증한다. `.env`, `.cache`, 실제 저장소 데이터는 정리 대상이 아니다.

## 최근 검증

- Backend 활성 Ruff·pytest와 Docker·업로드·인증 집중 테스트: 통과
- Backend DB 수직 흐름 집중 테스트: 17개 통과
- Backend 첨부·Docker·API 집중 테스트: 21개 통과
- Frontend 테스트: 인증·설정·첨부·LAN HTTP UUID fallback 회귀 테스트를 포함해 34개 통과
- 인증 Backend 집중 테스트: 13개 통과
- 인증 Browser 흐름: 로그인·회원가입·역할·서비스 권한 저장·로그아웃 확인
- Frontend typecheck/build: 통과
- production bundle: `backend/src/smb_finder/web/` 갱신
- Figma 기준 브라우저 확인: 1440×960에서 헤더 71px, 본문 889px, 3열 288/816/336px 일치
- 현재 앱 창 확인: 1265×1272에서 3열 유지, body 가로 overflow 없음, browser warning/error 0건
- 반응형 확인: 1024×768에서 좌측 248px+중앙 761px, 우측 패널 다음 행 배치, 가로 overflow 없음
- 기능 구성 브라우저 확인: 오른쪽 `문서 요약`·`보고서 초안` 2개, 중앙 `선택 문서 대화` 유지
- 버전 확인 브라우저 확인: DB 검색 후보 2건에 버튼 표시, 파일 미선택 상태에서 버전 탭 즉시 열림
- 프로젝트 품질: 85/100, 목표 달성, hard gate 통과
- 실측 evidence: 현재 수직 흐름의 반복 baseline이 아직 없어 15점은 `skipped`
- 실제 저장소 smoke: PostgreSQL·MinIO 연결, Neo4j 연결/degraded
- 멀티스토어 파일 검색: 3개 후보, 741.7ms, PostgreSQL·MinIO·Neo4j 조회, 로컬 LLM 확장 성공
- 단계 지연: LLM 630.4ms, PostgreSQL 86.6ms, MinIO 40.9ms, Neo4j 100.5ms, 8초 예산 이내
- 선택 문서 채팅: 정상 응답, Citation 6개, 2104.9ms
- LLM 구조화 응답: 256 token 절단 실패를 확인해 간결성 지시와 500 token 상한으로 보완
- 로그 경계: `httpx`/`httpcore` INFO를 차단해 내부 endpoint가 서비스 로그에 남지 않도록 수정
- Docker image `automation-smb:local`: build·healthy·UID/GID 10001 실행·image 내 `.env` 부재 확인
- Docker 저장소 smoke: PostgreSQL·MinIO·Neo4j 모두 연결, Neo4j graph-space fallback만 degraded warning
- Docker 수직 흐름: 자연어 검색 4개 후보·2798.6ms, 세 저장소+로컬 LLM 사용, 버전 graph 6개 노드·10개 관계,
  선택 문서 채팅 Citation 6개·2831.4ms
- Docker 브라우저: 검색→선택→파일별 버전 확인→중앙 채팅 통과, console error 0건
- LAN HTTP 호환성: secure context 밖에서 `crypto.randomUUID`가 없는 브라우저도 UI ID fallback으로 검색·선택 통과
- LAN host 주소 smoke: HTTP 200 확인. 별도 물리 PC 접속과 방화벽 규칙 적용은 미검증
- 설정·첨부 API: traversal 422, 금지 확장자 415, 비덮어쓰기·실제 byte 크기·명시적 활성화 단위 테스트 통과
- Docker 공개 설정: 업로드 enabled/configured, 상대 경로는 container 재시작 뒤 유지, host·username property 미노출
- SMB 연결: 자격증명·경로를 출력하지 않는 read-only 확인에서 설정된 대상 폴더 존재 확인
- 최종 브라우저: 검색 결과 3건, 선택·해제 feedback 불변, 제거 대상 상태 문구 0건, 파일 첨부·설정 버튼 활성,
  연결 상태 4개 표시
- 실제 SMB 쓰기: 공유폴더에 테스트 파일을 남기지 않기 위해 미실행. 사용자가 선택한 파일로 화면에서 실행 가능

현재 Playground는 Figma 초안의 정보 구조를 기준으로 자연어 DB 검색 → 파일 첨부 → 후보 선택 → 대화 참고 파일 → 하단
설정 순서로 정리했다. 파일 버전 확인은 오른쪽 공통 기능이 아니라 각 검색 후보의 직접 동작이며, 오른쪽 실행 기능은 문서
요약과 보고서 초안만 제공한다. 범용 관리자 메뉴와 대화 이력은 추가하지 않았다.

현재 Docker 검증 프로세스는 `http://127.0.0.1:8013/playground`에서 실행 중이다. 8011에는 이전 Uvicorn reloader가
Windows 프로세스 목록에서 해제되지 않은 채 포트를 점유해 host port만 8013으로 바꿨다. image 내부와 기본 Compose
설정은 계속 8011이며, 기존 프로세스를 사용자 터미널에서 종료한 뒤 기본 명령으로 다시 띄우면 된다.

현재 기능 흐름은 실제 저장소와 연결되어 동작한다. 다음 검증 과제는 합성 질의셋을 고정해 retrieval·end-to-end p95와
Hit@5·no-answer·groundedness를 반복 측정하는 것이다.
