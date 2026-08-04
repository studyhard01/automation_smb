# 구현 현황

기준일: 2026-08-04

## 한눈에 보기

| 영역 | 상태 | 현재 구현 |
|---|---|---|
| Frontend/Backend 분리 | 완료 | `frontend/`, `backend/src`, `backend/tests` |
| Figma Playground 정렬 | 완료 | 헤더 71px, 3열 288/816/336px, 중앙 대화·하단 Composer 구조 |
| 자연어 파일 검색 | 완료 | PostgreSQL 활성 문서 메타데이터+Chunk 검색 |
| 파일 후보 선택 | 완료 | 최대 5개, UUID pair를 채팅 요청에 전달 |
| 선택 문서 검증 | 완료 | 채팅 직전 활성 Revision 재검증, stale이면 409 |
| 선택 범위 검색 | 완료 | Hybrid/RRF, scope 위반 차단, Citation 반환 |
| 근거 답변 | 완료 | 온프레미스 Ollama native JSON, 근거 없으면 LLM 미호출 |
| 오른쪽 문서 기능 | 완료 | 문서 요약·보고서 초안만 제공, Q&A는 중앙 채팅으로 통합 |
| MinIO 보기 | 완료 | Preview/Canonical read-only, Object URI 미노출 |
| Neo4j 버전 관계 | 완료 | 각 검색 결과의 버전 확인 버튼에서 즉시 조회 |
| 저장소 상태 | 완료 | PostgreSQL·MinIO·Neo4j 연결/degraded 상태 표시 |
| 지연 목표 | 미달 | 검색은 목표권, LLM 포함 채팅은 추가 개선 필요 |
| 인증·ACL·감사 | 미구현 | 운영 전 별도 설계·승인 필요 |
| 실제 보고서 Artifact | 미구현 | 현재는 선택 문서 기반 텍스트 초안만 생성 |

## 활성 Backend 경로

- `api.py`: app lifespan, Vue bundle, health
- `llmops_search.py`: 파일 후보 검색과 활성 Revision 검증
- `llmops_retrieval.py`: 선택 범위 Hybrid/RRF Chunk 검색
- `llmops_artifacts.py`: PostgreSQL ACL/Revision 확인 후 MinIO 조회
- `llmops_graph.py`: Neo4j 버전 관계 조회
- `playground/document_api.py`: 현재 공개 API
- `playground/document_chat.py`: 근거 검색과 로컬 LLM 합성
- `playground/document_models.py`: 채팅 API 계약

## 현재 API

- `POST /api/playground/files/search`
- `POST /api/playground/chat`
- `GET /api/playground/files/{doc_id}/revisions/{revision_id}/artifacts/{artifact_type}`
- `GET /api/playground/files/{doc_id}/graph`
- `GET /api/playground/stores/status`
- `GET /health`
- `GET /playground`

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

- Backend 활성 Ruff·pytest: 통과, 총 30개 테스트 통과
- Backend DB 수직 흐름 집중 테스트: 13개 통과
- Frontend 테스트: 10개 통과
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
- 자연어 파일 검색: 3개 후보, 51.5ms, `source=llmops`
- 선택 문서 채팅: 정상 응답, Citation 6개, 2104.9ms
- LLM 구조화 응답: 256 token 절단 실패를 확인해 간결성 지시와 500 token 상한으로 보완
- 로그 경계: `httpx`/`httpcore` INFO를 차단해 내부 endpoint가 서비스 로그에 남지 않도록 수정

현재 Playground는 Figma 초안의 정보 구조를 기준으로 정리하되, 제품 범위 밖인 파일 직접 첨부·관리자 메뉴·대화 이력은
추가하지 않았다. 왼쪽 패널의 자연어 DB 검색 → 후보 선택 → 대화 참고 파일 표시 → 중앙 선택 문서 대화 흐름과 오른쪽의
실제 실행 상태만 유지한다. 파일 버전 확인은 오른쪽 공통 기능이 아니라 각 검색 후보의 직접 동작이며, 오른쪽 실행 기능은
문서 요약과 보고서 초안만 제공한다.

현재 검증 프로세스는 `http://127.0.0.1:8012/playground`에서 실행 중이다. 8011에는 이전 Uvicorn reloader가 Windows
프로세스 목록에서 해제되지 않은 채 포트를 점유하고 있어 이번 세션에서 교체하지 못했다. 새 프로세스의 기본 실행 설정은
계속 8011이며, 기존 8011 프로세스를 사용자 터미널에서 종료한 뒤 기본 명령으로 다시 띄우면 된다.

현재 기능 흐름은 실제 저장소와 연결되어 동작한다. 다음 검증 과제는 합성 질의셋을 고정해 retrieval·end-to-end p95와
Hit@5·no-answer·groundedness를 반복 측정하는 것이다.
