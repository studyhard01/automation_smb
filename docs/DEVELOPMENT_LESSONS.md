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
- 상황: 별도 파이프라인이 만든 문서·Revision·Chunk 데이터셋을 현재 단순 RAG 모델에 바로 맞추면 최신본·인용·버전 계보가 손실될 수 있었다.
- 교훈: 외부 schema는 upstream 기준으로 존중하고 서비스 내부에는 읽기 전용 reader와 Pydantic 공개 계약을 두어 저장소 세부와 UI를 분리한다.
- 다음 적용: 실제 연결 전에 ID·활성 Revision·Citation·ACL·timeout 계약을 합성 fixture로 고정하고 저장소별 adapter를 단계적으로 검증한다.

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

### 2026-07-20 — 품질 지표에는 달성 가능한 상태 전이가 있어야 한다
- 상황: Candidate 파일은 `candidate` 상태만 허용했지만 진행률은 허용되지 않은 상태값을 세어 항상 0점이었다.
- 교훈: 점수식을 추가할 때 입력 계약에서 실제로 그 상태에 도달하고 다음 단계로 이동할 수 있는지 함께 검증한다.
- 다음 적용: 검토 결정과 baseline 승격을 분리하고 fixture·CLI·rubric 테스트로 같은 상태 전이를 고정한다.

### 2026-07-20 — Windows 검증은 workspace별 cache와 temp로 격리한다
- 상황: 사용자 공용 `uv` cache와 pytest temp의 소유권·잔여 상태 때문에 코드와 무관한 접근 거부가 재현됐다.
- 교훈: 공용 경로 재사용 실패를 dependency 문제로 오인하지 말고 저장소의 `.tmp` 아래 격리 경로로 원인을 분리한다.
- 다음 적용: 검증 명령에 필요하면 `UV_CACHE_DIR`와 `--basetemp`를 workspace 경로로 지정하고 산출물은 커밋하지 않는다.

### 2026-07-20 — MLflow 저장소와 업무 원본의 경계를 분리한다
- 상황: 개발용 MLflow metadata와 artifact가 로컬 SQLite와 `.cache`에 있어 재배포·다중 노드 운영 시 내구성이 없다.
- 교훈: 운영 MLflow는 PostgreSQL metadata와 MinIO artifact를 분리하되 MinIO를 원본 SMB 복제소로 사용하지 않는다.
- 다음 적용: 운영 pilot 전에 MLflow proxy 방식과 전용 bucket을 검증하고 허용 artifact 목록부터 승인한다.

## 새 항목 템플릿

```markdown
### YYYY-MM-DD — 짧은 제목
- 상황: 재발 가능한 문제를 한 문장으로 적는다.
- 교훈: 원인 또는 판단 원칙을 한 문장으로 적는다.
- 다음 적용: 다음 작업에서 먼저 할 확인이나 예방 조치를 적는다.
```
