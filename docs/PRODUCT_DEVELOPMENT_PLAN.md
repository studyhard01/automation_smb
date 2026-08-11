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

### 현재 제외

- Langflow, LangGraph Studio, MCP
- 로컬 SQLite/SMB 직접 인덱싱과 `/find`, `/search-content`
- 범용 Tool Lab, Skill CRUD
- 첨부 파일 자동 변환·DB 인덱싱·버전 관계 생성
- QC 감사, 핵형·NGS 등 별도 업무 데모
- 외부 OpenAI provider
- DB schema/object/graph 생성·수정·삭제
- 실제 의료데이터 운영

데이터 수집·변환·Chunk·Embedding 적재는 upstream Mage 파이프라인이 소유한다. 이 저장소는 해당 계약을 복제하지
않고 read-only consumer로 동작한다.

## 3. 현재 우선순위

### P0 — 수직 흐름 안정화

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

- 사내 인증, 사용자/그룹 ACL, 감사 로그
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
