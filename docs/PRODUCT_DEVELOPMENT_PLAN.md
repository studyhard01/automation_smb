# 제품 개발 계획

## 1. 개발 목표

이 서비스는 팀 공유 문서의 위치와 내용을 자연어로 찾고, 사용자가 선택한 문서만 근거로 대화하는 온프레미스
Playground다. 현재 성공 기준은 다음 수직 흐름의 실제 연결이다.

1. 왼쪽 파일 찾기에 `진검파트 WBS 찾아줘` 같은 자연어를 입력한다.
2. 온프레미스 Ollama가 검색어를 확장하고 PostgreSQL·MinIO·Neo4j가 병렬로 활성 문서 후보를 찾는다.
3. 검색 후보의 `버전 확인`으로 선택 전에도 Neo4j 버전 관계를 바로 조회한다.
4. 사용자가 후보를 선택하면 `(doc_id, revision_id)`가 중앙 대화 범위에 들어간다.
5. 중앙 질문은 선택 Revision의 Chunk만 Hybrid/RRF로 검색하고 온프레미스 Ollama가 Citation과 함께 답한다.
6. 오른쪽 `기안 초안 작성`은 선택 문서를 근거로 사용자 설명을 받은 뒤 LLM의 제목·결재 요청·본문 JSON을 XLSX로 생성한다.
7. 필요할 때 MinIO 미리보기를 조회한다.
8. 사용자가 파일을 첨부하면 승인된 SMB share의 설정된 상대 경로에 비덮어쓰기 저장하고 `indexed=false`를 알린다.

규칙 기반 가짜 결과, fixture fallback, 매 요청 SMB 전체 순회는 성공으로 간주하지 않는다.

## 2. 제품 경계

### 현재 포함

- Vue 3 파일 검색·선택·채팅 UI
- FastAPI/Pydantic API
- 온프레미스 LLM 검색어 확장과 PostgreSQL·MinIO·Neo4j 통합 파일 후보 검색
- 활성 Revision 재검증
- 선택 문서 범위 Hybrid/RRF 검색
- 온프레미스 Ollama 근거 답변
- Citation, 저장소 상태, 지연 측정
- 검색 결과별 Neo4j 버전 관계 read-only 조회
- 중앙 선택 문서 대화와 별도 실행 기능 `문서 요약`, `기안 초안 작성`
- 원본 서식을 보존한 LLM 제목·결재 요청·본문 기안 XLSX 생성·SMB 신규 저장·대화창 다운로드와 상대 경로 설정
- MinIO Preview/Canonical read-only 조회
- 명시적으로 활성화한 SMB 파일 첨부와 비밀 없는 상대 경로 설정
- 로그인·회원가입·관리자 사용자 관리와 PostgreSQL `auth` schema 권한 저장
- 관리자·최고 관리자 분리와 전체 또는 서비스별 접근 권한 모델

### P0로 편입

- 활성 `DocumentRuntime` 계약을 사용하는 FastAPI·LangGraph 최소 Flow와 결정론적 Router
- 외부 DB·LLM 없이 실행되는 Mock Retriever/Model 프로필
- 공통 Model Gateway와 Citation·Policy·오류 계약
- 활성 FastAPI lifespan에 연결되는 read-only MCP Metadata Tool
- 서버 소유 다중 턴 상태가 생길 때만 재개하는 Session Cache 보류 기준
- 위 항목의 상세 순서와 완료 기준은 [Bot Main Core 구현 계획](BOT_MAIN_CORE_IMPLEMENTATION_PLAN.md)을 따른다.

### 현재 제외

- Langflow와 운영용 remote MCP/OAuth·SSO
- LangGraph Studio의 운영 관측·평가 저장소 사용
- 로컬 SQLite/SMB 직접 인덱싱과 `/find`, `/search-content`
- 범용 Tool Lab, Skill CRUD
- 첨부 파일 자동 변환·DB 인덱싱·버전 관계 생성
- QC 감사, 핵형·NGS 등 별도 업무 데모
- 외부 OpenAI provider
- 문서 저장소 DB schema/object/graph 생성·수정·삭제. 별도 `auth` schema의 인증 쓰기는 예외
- 실제 의료데이터 운영

데이터 수집·변환·Chunk·Embedding 적재는 upstream Mage 파이프라인이 소유한다. 이 저장소는 해당 계약을 복제하지
않고 read-only consumer로 동작한다.

## 3. 현재 우선순위

### P0-A — Bot Main Core 계약 복구

- 현재 FastAPI와 레거시 MCP·LangGraph 실험의 실행 경계를 먼저 고정
- FastAPI + 최소 graph + Mock Retriever/Model 수직 슬라이스
- 공통 Model Gateway로 검색어 확장·근거 답변의 timeout·오류 계약 통합
- read-only `get_document_metadata` 한 개와 bounded timeout/concurrency를 활성 FastAPI lifespan에 연결
- Session Cache는 서버가 복원할 상태·authenticated principal·허용 schema·worker miss 의미가 확정될 때까지 보류
- 추가 metadata tool은 실제 소비 흐름이 승인된 뒤 별도 수직 슬라이스로 검토
- 삭제된 `FolderIndex`·SQLite 계약을 되살려 레거시 테스트만 통과시키는 방식은 사용하지 않음

### P0-B — 수직 흐름 안정화

- 실제 저장소 연결에서 검색 → 선택 → 채팅 회귀 테스트
- 검색 결과 정확도와 활성 Revision 검증
- DB/Embedding/LLM timeout과 부분 실패 메시지
- 외부 전송 없는지 검증

### P1 — 지연과 검색 품질

- 멀티스토어+LLM 파일 검색 p50 1.5초 미만, p95 3초 미만, hard budget 8초
- 선택 문서 검색 단계별 지연 기록
- Ollama 모델·프롬프트·근거 길이 조정으로 end-to-end 지연 개선
- 합성 질의셋으로 Hit@5, no-answer, groundedness 측정

### P2 — 운영 준비

- 현재 계정·서비스 권한을 Playground와 업무 API에 강제 적용
- 사내 SSO 또는 reverse proxy 연동, 사용자/그룹·문서 ACL, 감사 로그
- 운영 read-only 계정과 secret 주입
- 장애·재시도·degraded 상태 관측
- 합성 단계 완료 후 의료데이터 보안 검토와 운영 승인

### P3 — 업무 기능 확장

- 사용자가 승인한 문서 형식의 실제 산출물 생성
- 생성물 저장 위치·검토·승인·버전 정책
- 필요성이 확인된 기능만 별도 vertical slice로 추가

## 4. 승인 Gate

- 실제 의료데이터 연결 또는 외부 LLM 전송
- DB/MinIO/Neo4j 쓰기 권한
- 보고서 파일 자동 저장·배포
- 운영 배포와 원격 push
- 기존 기능군의 대량 영구 삭제

## 5. 완료 정의

- DB 미연결 시 가짜 결과 없이 명시적으로 실패한다.
- 채팅 전 선택 UUID가 활성 Revision인지 재검증한다.
- 검색된 Citation이 모두 선택 scope 안에 있다.
- 문서 내용은 온프레미스 Ollama 외부로 전송되지 않는다.
- Frontend 테스트·build, Backend Ruff·pytest, 프로젝트 품질 검사가 통과한다.
