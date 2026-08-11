# 개발 교훈

반복해서 도움이 되는 문제와 해결 원칙만 짧게 유지한다. 작업을 시작할 때 `현재 교훈`을 읽고, commit/push 전에는
이번 변경에서 다시 쓸 만한 교훈이 생겼는지 확인한다. 새 교훈이 없으면 형식적인 항목을 추가하지 않는다.

## 기록 규칙

- 최신 항목을 위에 두고 최대 12개만 유지한다. 같은 원인의 항목은 새로 늘리지 말고 기존 항목을 합친다.
- 한 항목은 `상황`, `교훈`, `다음 적용` 세 줄로 쓰고 700자를 넘기지 않는다.
- 자격증명, 내부 주소, 실제 SMB 경로·파일 목록, 환자·검체 식별자는 기록하지 않는다.
- 일회성 명령 실패보다 다른 작업에서도 재발할 수 있는 원인과 다음 확인 순서를 남긴다.
- 형식 검사는 `uv run --no-sync python scripts/check_development_lessons.py`로 실행한다.

## 현재 교훈

### 2026-08-11 — OOXML 보존과 출력 제약은 최종 직렬화 결과로 검사한다
- 상황: 셀 치환 범위 오류는 ZIP이 열려도 인접 행을 지웠고, 표 셀 내부 개행은 중간 candidate에서 1행이었지만 최종 `splitlines()`에서는 여러 행이 되어 33행 제한을 넘겼다.
- 교훈: XLSX가 열리고 목표 셀 값이나 중간 행 수가 맞는 것만으로는 성공이 아니며, 변경 허용 ZIP 항목과 최종 직렬화의 행·문자 제약을 함께 검사해야 한다.
- 다음 적용: 셀 패턴을 분리하고 목표 외 ZIP byte·인접 셀을 비교한 뒤, 내부 개행·긴 셀·대형 표를 포함해 최종 33행·6,000자·생략 고지와 workbook 생성을 회귀한다.

### 2026-08-07 — 환경 파일의 식별자 값은 승인 없이 보정하지 않는다
- 상황: source key가 다른 재적재에서 같은 물리 파일이 별도 source/doc 계보로 활성화돼 검색 결과가 두 건씩 보였다.
- 교훈: 환경 파일 값은 데이터 정체성 계약이므로 추정 수정하지 않고, 읽기 소비자는 source 계보가 아니라 비공개 물리 URI로 중복을 접어야 한다.
- 다음 적용: 환경 파일은 승인 전 읽기만 허용하고 source key를 고정한다. 검색은 물리 URI 우선 identity로 통합하되 동명 다른 경로는 보존한다.

### 2026-08-05 — 읽기 서비스에 쓰기를 더할 때는 경계를 별도 계약으로 고정한다
- 상황: DB 조회 서비스에 SMB 첨부를 추가할 때 임시 파일의 rename·실패 시 삭제까지 허용했지만, 이후 저장 경계가 읽기와 신규 추가만으로 더 엄격해졌다.
- 교훈: 쓰기는 명시적 활성화·share 내부 상대 경로·크기/확장자 제한뿐 아니라 최종 이름의 exclusive 신규 생성으로 좁혀야 하며, 저장 완료·인덱싱·즉시 대화 준비를 서로 다른 계약으로 표시해야 한다.
- 다음 적용: 완성 내용을 메모리나 로컬에서 검증한 뒤 SMB 최종 파일을 `xb`로 한 번만 만들고, rename·remove·overwrite 없이 충돌을 실패시킨다. 저장명·traversal·크기·UUID와 제3자 SMB 로그 수준을 회귀 테스트한다.

### 2026-08-05 — 컨테이너와 LAN HTTP는 localhost의 실행 조건을 공유하지 않는다
- 상황: 사내 SSL 검사·container DNS·비보안 LAN Browser API 차이뿐 아니라 Compose 재생성 때 셸 전용 포트 값이 빠져 기존 8013이 기본 8011로 바뀌었다.
- 교훈: build trust, container endpoint, secure-context API와 배포 시점의 일시 환경변수까지 하나의 재현 가능한 실행 계약으로 확인해야 한다.
- 다음 적용: 공개 wheel host opt-in은 빌드 프로세스에만 두고, 재생성 전후 `docker compose config/ps`의 port·endpoint를 비교한 뒤 non-root·read-only·health·LAN 화면을 검증한다.

### 2026-08-04 — 멀티스택 저장소는 물리 경계와 설정 소유권을 함께 드러낸다
- 상황: Vue와 FastAPI의 물리 경계가 불명확했고, 제품 목표에서 제외한 실험 코드도 같은 package에 남아 현재 실행 경로와 향후 후보를 구분하기 어려웠다.
- 교훈: 멀티스택 저장소는 `frontend/`·`backend/` 경계를 대칭적으로 두고, 새 수직 슬라이스를 먼저 연결·검증한 뒤 레거시의 import·dependency·문서·파일을 순서대로 제거해야 한다.
- 다음 적용: 구조 이동과 제품 경계 변경 시 package discovery, pytest, Vite outDir, reload, dependency, 품질 rubric, canonical 문서를 함께 검증하고 영구 삭제 대상은 명시 승인을 받는다.

### 2026-08-04 — DB 확장 문법은 Client placeholder와 Driver 실행 계층까지 검증한다
- 상황: PostgreSQL `pg_trgm`의 `%` 연산자가 psycopg placeholder parser와 충돌했고 Neo4j `Query` 객체가 managed transaction의 `run` 계약과 맞지 않았다.
- 교훈: DB 문법이 서버에서 유효해도 Client의 placeholder 규칙과 session/transaction별 API 계약에서 실패할 수 있다.
- 다음 적용: Driver나 확장 설치 전에 버전과 실행 계층을 확인하고, escape된 실제 SQL과 Query·timeout 지원을 최소 smoke로 검증한다.

### 2026-08-04 — 로컬 LLM의 구조화 출력은 실제 모델 호환성과 서버 검증을 함께 둔다
- 상황: thinking token 절단은 native format으로 줄였지만, 다른 모델은 schema 객체 처리 중 vocabulary 로딩에 실패했고 JSON 문자열 모드만 지원했다.
- 교훈: provider의 구조화 출력 지원은 endpoint가 아니라 실제 모델별로 확인하고, 호환 JSON 모드 뒤 애플리케이션 schema 검증으로 필드 계약을 닫아야 한다.
- 다음 적용: `think=false`와 최소 JSON 모드를 실제 모델로 smoke하고, Pydantic의 필수·추가 필드·길이 검증과 invalid/truncated JSON 회귀 테스트를 함께 둔다.

### 2026-08-03 — Frontend 원본·스타일 계약·build 산출물을 함께 검사한다
- 상황: 남은 설정 JavaScript가 Vite 설정을 가로챌 수 있었고, Vue class를 바꾼 뒤 레거시 CSS selector가 남아 중앙 화면 높이와 배치가 무너졌다.
- 교훈: 원본은 `noEmit`으로 검사하고 production 산출물은 Vite만 생성하게 하며, 컴포넌트 class와 CSS selector의 계약은 기준 viewport에서 확인한다.
- 다음 적용: typecheck·test·build 뒤 중복 설정과 stale asset을 검사하고, Figma 기준/현재 viewport의 열 너비·높이·overflow·browser 오류를 smoke 검증한다.

### 2026-08-03 — 선택 상태는 화면 표시와 실행 계약을 함께 연결한다
- 상황: 좌측 검색 문장을 중앙 입력으로 복사하는 것만으로는 대화 범위를 보장할 수 없었고, 결과별 버전 확인이 파일 선택 동작과 섞일 수 있었다.
- 교훈: 안정적인 ID와 선택 상태를 API까지 전달하되, 선택 없이 가능한 보조 동작은 카드의 독립 이벤트로 분리하고 서버에서 대상을 다시 검증해야 한다.
- 다음 적용: 선택형 UI는 결과 표시·선택/해제·카드별 보조 동작·중앙 범위·downstream 요청을 contract test와 화면 smoke로 검증한다.

### 2026-08-03 — 구축 데이터 schema는 복제하지 않고 소비 계약으로 경계 짓는다
- 상황: 문서·Revision·Chunk·Artifact·Graph가 여러 저장소에 분산돼 한 저장소만 검색하면 버전명이나 Object key 단서를 놓칠 수 있었다.
- 교훈: 저장소별 read-only 후보를 병렬 수집하되 최종 ID·활성 Revision은 PostgreSQL 기준 원장에서 hydrate해 중복과 stale 대상을 차단한다.
- 다음 적용: 멀티스토어 검색은 저장소별 timeout·부분 실패·매칭 출처·단계 지연을 계약에 포함하고 실제 연결 smoke에서 모두 조회됐는지 확인한다.

### 2026-07-24 — 문서 구조를 바꿔도 canonical 계약을 보존한다
- 상황: README의 파일별 구조 표를 책임별 그룹으로 줄이면서 품질 rubric이 요구하는 핵심 경로 표식이 빠져 hard gate가 실패했다.
- 교훈: canonical 문서는 설명 자료이면서 자동 검사 계약이므로 표현을 단순화해도 필수 주장·경로·링크는 유지해야 한다.
- 다음 적용: README·설계 문서 개편 전 rubric의 `canonical_documents`를 확인하고 변경 직후 전체 품질 평가를 실행한다.

### 2026-07-24 — 도구 계약 원본과 공개 adapter를 분리한다
- 상황: 외부 tool schema 동기화와 로컬 registry가 함께 존재해 계약 원본이 나뉘고 원격 지연·장애가 fast path로 전파될 수 있었다.
- 교훈: Python 도메인 코드와 Pydantic catalog를 단일 원본으로 두고 Playground는 in-process, 외부 client만 승인된 MCP adapter를 사용한다.
- 다음 적용: 새 업무 tool은 handler·timeout·권한·surface를 catalog에 먼저 고정하고 MCP 공개 여부와 지연을 별도로 검증한다.

## 새 항목 템플릿

```markdown
### YYYY-MM-DD — 짧은 제목
- 상황: 재발 가능한 문제를 한 문장으로 적는다.
- 교훈: 원인 또는 판단 원칙을 한 문장으로 적는다.
- 다음 적용: 다음 작업에서 먼저 할 확인이나 예방 조치를 적는다.
```
