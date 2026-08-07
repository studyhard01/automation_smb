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

### 2026-08-06 — 로컬 개발 실행은 runtime 경계를 명령에 직접 드러낸다
- 상황: Backend와 Frontend를 분리 실행할 때 wrapper가 reload 자식 프로세스 관리까지 떠안으면서 개발자가 기대한 직접 실행 흐름이 가려졌다.
- 교훈: 설치 위치는 `backend/.venv`와 `frontend/node_modules`로 격리하되, 일상 실행은 얇은 `main.py` 진입점의 `uvicorn --reload`와 `npm run dev`를 전경에서 직접 실행해야 종료와 로그 확인이 단순하다.
- 다음 적용: README의 직접 명령을 표준 경로로 유지하고 wrapper는 선택 기능으로만 두며, Vite는 포트 충돌 시 자동 대체 포트를 사용하고 health·proxy를 smoke 검증한다.

### 2026-08-05 — 읽기 서비스에 쓰기를 더할 때는 경계를 별도 계약으로 고정한다
- 상황: DB 조회만 하던 LAN Playground에 제한된 SMB 첨부, 로그인·사용자 권한, 외부 계정 연동처럼 서로 다른 쓰기와 신원 책임이 추가됐다.
- 교훈: SMB 쓰기는 명시적 활성화·share 하위 경로·비덮어쓰기로 좁히고, 인증은 별도 schema·계정·실패 격리를 유지한다. 외부 access token은 검증 뒤 폐기하고 provider·subject로 로컬 계정을 연결해 서비스 자체 session과 분리한다.
- 다음 적용: 새 쓰기·외부 인증은 대상 저장소·최소 권한·실패 범위·감사 주체를 먼저 고정하고, 로컬 이름 충돌 자동 연결 금지와 기존 기능 회귀를 함께 시험한다.

### 2026-08-05 — 컨테이너와 LAN HTTP는 localhost의 실행 조건을 공유하지 않는다
- 상황: 사내 SSL 검사와 container DNS 차이 외에도 localhost에서 되던 `crypto.randomUUID`가 LAN의 비보안 HTTP 브라우저에서는 제공되지 않아 DB 검색 성공 뒤 UI가 실패로 표시됐다.
- 교훈: build trust·container endpoint·adapter readiness뿐 아니라 secure-context 전용 Browser API도 LAN 배포 계약에 포함하고, UI 비핵심 ID는 안전한 fallback을 제공해야 한다.
- 다음 적용: secure default와 runtime 비밀 경계를 유지하고 non-root·LAN bind·멀티스토어 검색을 검증한 뒤, secure-context API를 제거한 브라우저 회귀 테스트로 실제 원격 HTTP 흐름까지 확인한다.

### 2026-08-04 — 멀티스택 저장소는 물리 경계와 설정 소유권을 함께 드러낸다
- 상황: Vue와 FastAPI의 물리 경계가 불명확했고, 제품 목표에서 제외한 실험 코드도 같은 package에 남아 현재 실행 경로와 향후 후보를 구분하기 어려웠다.
- 교훈: 멀티스택 저장소는 `frontend/`·`backend/` 경계를 대칭적으로 두고, 새 수직 슬라이스를 먼저 연결·검증한 뒤 레거시의 import·dependency·문서·파일을 순서대로 제거해야 한다.
- 다음 적용: 구조 이동과 제품 경계 변경 시 package discovery, pytest, Vite outDir, reload, dependency, 품질 rubric, canonical 문서를 함께 검증하고 영구 삭제 대상은 명시 승인을 받는다.

### 2026-08-04 — DB 확장 문법은 Client placeholder와 Driver 실행 계층까지 검증한다
- 상황: PostgreSQL `pg_trgm`의 `%` 연산자가 psycopg placeholder parser와 충돌했고 Neo4j `Query` 객체가 managed transaction의 `run` 계약과 맞지 않았다.
- 교훈: DB 문법이 서버에서 유효해도 Client의 placeholder 규칙과 session/transaction별 API 계약에서 실패할 수 있다.
- 다음 적용: Driver나 확장 설치 전에 버전과 실행 계층을 확인하고, escape된 실제 SQL과 Query·timeout 지원을 최소 smoke로 검증한다.

### 2026-08-04 — 로컬 LLM의 구조화 출력은 native Schema와 thinking 제어를 우선한다
- 상황: Qwen3의 OpenAI 호환 JSON 응답이 thinking에 token을 소진해 상한을 늘려도 본문이 잘리고 parse에 실패했다.
- 교훈: token 상한만 키우지 말고 provider가 지원하는 native JSON Schema와 thinking 비활성화를 사용해야 지연과 형식을 함께 통제할 수 있다.
- 다음 적용: 구조화 호출은 native format 지원을 먼저 확인하고 `think=false`, 작은 schema, invalid/truncated JSON 회귀 테스트를 함께 둔다.

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

### 2026-07-22 — LLM 보고서는 사실과 서술의 소유권을 분리한다
- 상황: QC 보고서 전체를 LLM에 맡기면 입력 측정값·판정·SOP 근거까지 자연스럽게 바뀐 초안이 반환될 수 있었다.
- 교훈: 수치·판정·근거는 코드가 고정하고 LLM은 비수치 서술만 만들며, 조립 결과를 같은 감사기로 다시 검증한다.
- 다음 적용: 새 생성형 업무 tool도 입력 사실, LLM 허용 필드, 기계 검증, 사람 승인 전 상태를 계약에 함께 명시한다.

## 새 항목 템플릿

```markdown
### YYYY-MM-DD — 짧은 제목
- 상황: 재발 가능한 문제를 한 문장으로 적는다.
- 교훈: 원인 또는 판단 원칙을 한 문장으로 적는다.
- 다음 적용: 다음 작업에서 먼저 할 확인이나 예방 조치를 적는다.
```
