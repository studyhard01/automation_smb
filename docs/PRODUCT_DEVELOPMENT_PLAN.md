# 제품 개발 계획

기준일: 2026-08-19

## 1. 제품 목표

이 서비스는 팀 공유 문서의 위치와 내용을 자연어로 찾고, 사용자가 선택한 문서만 근거로 대화·요약·기안 초안을
만드는 온프레미스 Playground다.

현재 성공 기준은 다음 수직 흐름의 실제 연결이다.

1. 왼쪽 파일 찾기에 자연어 질의를 입력한다.
2. 온프레미스 Ollama가 필요한 경우 검색어를 확장한다.
3. PostgreSQL·MinIO·Neo4j에서 활성 문서 후보를 병렬 조회한다.
4. 사용자가 최대 5개 문서를 선택한다.
5. Backend가 `(doc_id, revision_id)`의 활성 상태를 다시 검증한다.
6. 선택 Revision의 Chunk만 Hybrid/RRF로 검색한다.
7. 온프레미스 Ollama가 Citation이 있는 답변·요약을 만든다.
8. 기안 기능은 같은 근거를 선별·검증해 XLSX를 만들고 SMB에 새 파일로 저장한다.

가짜 검색 결과, fixture fallback, 매 요청 SMB 전체 순회는 성공으로 간주하지 않는다.

## 2. 현재 포함

- Vue 3 파일 검색·선택·채팅 UI
- FastAPI/Pydantic API와 안전한 오류 계약
- PostgreSQL·MinIO·Neo4j read-only 통합 파일 검색
- 선택 문서 활성 Revision 재검증
- 선택 범위 Hybrid/RRF 검색과 Citation
- 온프레미스 Ollama 검색어 확장·답변·기안 생성·검증
- 문서 Preview/Canonical과 Version Graph
- 문서 요약 프리셋
- 구조화 기안 생성·필수정보 확인·수정본·XLSX 다운로드
- 제한된 SMB 신규 파일 첨부와 기안 저장
- 로그인·회원가입·관리자 사용자·서비스 권한
- 기안 합성 데이터 평가와 품질 Gate

문서 수집·변환·Chunk·Embedding 적재는 upstream 데이터 파이프라인이 소유한다. 이 저장소는 문서 저장소를 수정하지
않는 consumer다.

## 3. 현재 제외

- 별도 시각형 workflow·graph orchestration runtime
- 로컬 SMB 직접 순회·SQLite 인덱싱
- 범용 Tool Lab과 Skill CRUD
- QC 감사·핵형·NGS 보고서 데모
- 외부 LLM provider
- 첨부 파일의 자동 DB 인덱싱·버전 관계 생성
- 문서 저장소 DB schema·MinIO object·Neo4j graph 쓰기
- 운영 SSO와 문서별 ACL 강제
- 실제 의료데이터 운영

## 4. 다음 우선순위

### P0 — 현재 수직 흐름 안정화

- 실제 합성 저장소에서 검색 → 선택 → 대화 → 요약 → 기안 회귀
- 선택 Revision 범위와 Citation 검증
- DB·Embedding·LLM·SMB deadline과 부분 실패 메시지 검증
- Frontend와 API 오류 코드의 사용자 안내 정합성 유지

### P1 — 지연과 검색 품질

- 검색·retrieval·LLM 단계별 p50/p95 측정
- 검색어 확장 fast path와 cold/warm 모델 지연 분리
- 합성 질의셋으로 Hit@5·no-answer·groundedness 평가
- 선택 문서 채팅 end-to-end p95 1초 목표의 현실성 재측정

### P2 — 운영 준비

- 현재 사용자·서비스 권한을 Playground API에 강제
- 사내 SSO 또는 reverse proxy 연동
- 문서별 ACL과 감사 로그 설계
- 운영 read-only 계정과 secret 주입
- 합성 단계 종료 후 의료데이터 보안 검토와 운영 승인

### P3 — 검증된 업무 기능 확장

- 실제 사용자가 승인한 산출물만 별도 vertical slice로 추가
- 생성물 저장 위치·검토·승인·버전 정책 확정
- 새 외부 연동은 명확한 consumer와 최소 권한 계약이 생긴 뒤 별도 수직 슬라이스로 추가

## 5. 승인 Gate

- 실제 의료데이터 연결 또는 외부 전송
- 문서 저장소의 쓰기 권한
- 실제 SMB 신규 파일 쓰기 smoke
- 운영 배포와 원격 push
- 기존 SMB 파일 수정·덮어쓰기·이동·삭제는 승인 대상이 아니라 금지 경계

## 6. 완료 정의

- DB 미연결 시 가짜 결과 없이 명시적으로 실패한다.
- 채팅과 기안 전에 선택 UUID가 활성 Revision인지 재검증한다.
- 모든 Citation이 선택 scope 안에 있다.
- 문서 내용은 온프레미스 Ollama 외부로 전송되지 않는다.
- SMB 파일은 create-only로 저장한다.
- Backend Ruff·pytest, Frontend typecheck·test·build, 프로젝트 품질 검사가 통과한다.
- Docker health·OpenAPI·UI·합성 read-only smoke가 같은 build에서 통과한다.
