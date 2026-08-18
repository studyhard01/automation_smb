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

### 2026-08-14 — 절대 deadline은 모든 단계와 되돌릴 수 없는 commit 경계를 함께 다룬다
- 상황: 상위 요청은 timeout을 반환했지만 선택 검증·DB 연결·SMB 읽기·worker가 계속됐고, 반대로 SMB exclusive write가 성공한 뒤 deadline을 검사해 504로 바꾸면 삭제할 수 없는 파일과 registry 상태가 어긋났다.
- 교훈: 하나의 monotonic deadline을 admission·connect·query·read·model·render·write 시작 전까지 전달하고, timeout된 worker의 slot과 종료도 추적해야 한다. 다만 되돌릴 수 없는 create-only commit이 성공한 뒤에는 실패로 뒤집지 말고 bookkeeping을 마쳐야 한다.
- 다음 적용: 느린 fake로 연속 포화·shutdown·zero-work를 검증하고, 외부 쓰기는 각 chunk 시작 전 예산과 남은 I/O timeout을 적용한다. 최종 byte와 close 성공 뒤에는 안전한 over-budget 집계만 남기고 성공 상태를 일관되게 기록한다.

### 2026-08-11 — 근거 없는 문장은 선택 정보 삭제와 필수 정보 질문으로 분리한다
- 상황: 생성 prompt에 추측 금지만 적어서는 LLM이 만든 각 문장의 근거 여부를 구분할 수 없었고, 실제 모델은 검증 prompt를 보강한 뒤에도 명시적으로 요청된 참가비 누락을 `completed`로 판정했다.
- 교훈: 문단·목록 항목·표 행을 독립 주장으로 재검증하고 선택 정보는 삭제하되 필수 누락·충돌만 최대 3개 질문으로 바꿔야 한다. 2차 품질 편집도 그럴듯한 배경을 새로 만들 수 있으므로 기존 사실 주장은 불변으로 유지하고, 검증 citation 재구성과 유형별 결정론적 안전망을 마지막에 적용한다.
- 다음 적용: 사람 평가는 전체 문장 일치가 아닌 사실 key·값·출처·누락 action으로 기록한다. 실제 모델 smoke에서 질문 전 workbook 미생성과 답변 후 완성을 확인하고, 명시 기대효과·행사 결정 사실·구매 배경처럼 위험도가 높은 항목은 원문 문장과 유형·사용자 의도에 한정해 보정한다.

### 2026-08-11 — 피드백·평가 완료 기준은 실제 생성 경로와 배포 산출물까지다
- 상황: 기안 코드와 정답 복사 oracle은 통과했지만 실제 Excel·근거 선별 결함이 드러났고, 문자열 일치 점수는 읽기 좋은 표현을 과소평가했으며 미생성 XLSX의 기본 참 레이아웃 값이 부분 점수를 만들 수 있었다.
- 교훈: oracle은 계약 상한일 뿐 제품 점수가 아니다. 실제 입력·packed 근거·생성본만 보는 고정 온프레미스 judge로 의미 품질을 세분화하고, 산출물 존재 같은 선행 조건을 통과한 뒤에만 세부 속성 점수를 합산해야 한다.
- 다음 적용: judge model·rubric·temperature를 고정해 manifest에 묶고 사람 검토 test split으로 보정한다. gold 비노출 live hard gate와 파일 존재 guard를 확인한 뒤 Docker health·UI·합성 smoke까지 같은 버전으로 검증한다.

### 2026-08-11 — OOXML 보존과 출력 제약은 최종 직렬화 결과로 검사한다
- 상황: XML namespace 결함은 Excel 개방을 막았고, 개방 가능한 생성물도 일반 본문 병합·행 높이 또는 템플릿의 `pageBreakPreview`가 남아 편집성과 첫 화면을 해쳤다.
- 교훈: XLSX 성공 조건에는 ZIP·namespace·최종 셀 제약뿐 아니라 편집성과 workbook view도 포함한다. 일반 본문은 독립 셀과 기본 행 높이를 유지하고 표 논리 셀에만 병합을 허용하며, 배포본은 `Normal View`로 열어야 한다.
- 다음 적용: 일반 본문과 표의 geometry를 분리해 회귀하고, Excel read-only 개방·렌더에서 본문 병합·사용자 지정 행 높이와 `ActiveWindow.View`를 함께 검사한다.

### 2026-08-07 — 환경 파일의 식별자 값은 승인 없이 보정하지 않는다
- 상황: source key가 다른 재적재에서 같은 물리 파일이 별도 source/doc 계보로 활성화돼 검색 결과가 두 건씩 보였다.
- 교훈: 환경 파일 값은 데이터 정체성 계약이므로 추정 수정하지 않고, 읽기 소비자는 source 계보가 아니라 비공개 물리 URI로 중복을 접어야 한다.
- 다음 적용: 환경 파일은 승인 전 읽기만 허용하고 source key를 고정한다. 검색은 물리 URI 우선 identity로 통합하되 동명 다른 경로는 보존한다.

### 2026-08-06 — 로컬 개발 실행은 runtime 경계를 명령에 직접 드러낸다
- 상황: Backend·Frontend wrapper가 일상 실행 흐름을 가렸고, 저장소 루트의 `uv run --no-sync`가 기존 `backend/.venv` 대신 빈 루트 `.venv`를 만들어 Ruff·pytest를 찾지 못했다.
- 교훈: 설치 위치는 `backend/.venv`와 `frontend/node_modules`로 격리하고, 실행·품질 명령은 `UV_PROJECT_ENVIRONMENT=backend/.venv` 또는 명시적 interpreter로 같은 runtime을 사용해야 한다.
- 다음 적용: README와 hook에서 runtime 경계를 명령에 고정하고, Backend는 직접 `uvicorn --reload`, Frontend는 `npm run dev`로 실행하며 health·proxy를 smoke 검증한다.

### 2026-08-05 — 읽기 서비스에 쓰기를 더할 때는 경계를 별도 계약으로 고정한다
- 상황: DB 조회 서비스에 SMB 첨부와 별도 인증 저장소를 추가했고, `smbclient`의 동명 충돌이 일반 `SMBOSError`로 반환돼 연결 장애 503으로 오인될 수 있었다.
- 교훈: SMB 쓰기는 share 하위 최종 이름의 exclusive 신규 생성으로 제한하고 안전한 상태 코드로 충돌을 구분한다. 인증은 별도 schema·계정·실패 격리를 유지하며 외부 token과 서비스 session을 분리한다.
- 다음 적용: 완성 내용을 먼저 검증한 뒤 SMB 파일을 `xb`로 한 번만 만들고 충돌을 409로 반환한다. 새 쓰기·외부 인증은 최소 권한·실패 범위·감사 주체와 로컬 이름 충돌 금지를 함께 시험한다.

### 2026-08-05 — 컨테이너와 LAN HTTP는 localhost의 실행 조건을 공유하지 않는다
- 상황: 사내 SSL 검사·container DNS·비보안 LAN Browser API 차이뿐 아니라 Compose 재생성 때 셸 전용 포트 값이 빠져 기존 8013이 기본 8011로 바뀌었다.
- 교훈: build trust, container endpoint, secure-context API와 배포 시점의 일시 환경변수까지 하나의 재현 가능한 실행 계약으로 확인해야 한다.
- 다음 적용: 공개 wheel host opt-in은 빌드 프로세스에만 두고, 재생성 전후 `docker compose config/ps`의 port·endpoint를 비교한 뒤 non-root·read-only·health·LAN 화면을 검증한다.

### 2026-08-04 — 멀티스택 저장소는 물리 경계와 설정 소유권을 함께 드러낸다
- 상황: Vue와 FastAPI의 물리 경계가 불명확했고, 제품 목표에서 제외한 실험 코드도 같은 package에 남아 현재 실행 경로와 향후 후보를 구분하기 어려웠다.
- 교훈: 멀티스택 저장소는 `frontend/`·`backend/` 경계를 대칭적으로 두고, 새 수직 슬라이스를 먼저 연결·검증한 뒤 레거시의 import·dependency·문서·파일을 순서대로 제거해야 한다.
- 다음 적용: 구조 이동과 제품 경계 변경 시 package discovery, pytest, Vite outDir, reload, dependency, 품질 rubric, canonical 문서를 함께 검증한다. 비활성 테스트는 glob 없이 제거 후보와 일치하는 exact list·고정 개수·pytest header로 드러내고, 새 실패를 자동 격리하거나 삭제 모델을 복원하지 않는다. 영구 삭제는 명시 승인을 받는다.

### 2026-08-04 — DB 확장 문법은 Client placeholder와 Driver 실행 계층까지 검증한다
- 상황: PostgreSQL `pg_trgm`의 `%` 연산자가 psycopg placeholder parser와 충돌했고 Neo4j `Query` 객체가 managed transaction의 `run` 계약과 맞지 않았다.
- 교훈: DB 문법이 서버에서 유효해도 Client의 placeholder 규칙과 session/transaction별 API 계약에서 실패할 수 있다.
- 다음 적용: Driver나 확장 설치 전에 버전과 실행 계층을 확인하고, escape된 실제 SQL과 Query·timeout 지원을 최소 smoke로 검증한다.

### 2026-08-04 — 로컬 LLM의 구조화 출력은 실제 모델 호환성과 서버 검증을 함께 둔다
- 상황: thinking token 절단과 모델별 schema 지원 차이뿐 아니라, 검증 schema의 `\d` 정규식이 Ollama grammar parser에서 즉시 400으로 거절됐지만 일반 연결 오류로 표시됐다.
- 교훈: provider의 구조화 출력은 endpoint·모델뿐 아니라 JSON Schema 방언까지 확인하고, provider 호환 schema 뒤 애플리케이션 Pydantic 검증으로 필드 계약을 닫아야 한다.
- 다음 적용: `think=false`와 실제 schema를 모델에 smoke하고, ASCII 문자 클래스처럼 동등한 호환 표현을 우선한다. grammar 생성 오류는 연결 장애와 구분하고 필수·추가 필드·길이·invalid JSON 회귀를 유지한다.

### 2026-08-03 — Frontend 원본·스타일 계약·build 산출물을 함께 검사한다
- 상황: 남은 설정 JavaScript가 Vite 설정을 가로챌 수 있었고, Vue class를 바꾼 뒤 레거시 CSS selector가 남아 중앙 화면 높이와 배치가 무너졌다.
- 교훈: 원본은 `noEmit`으로 검사하고 production 산출물은 Vite만 생성하게 하며, 컴포넌트 class와 CSS selector의 계약은 기준 viewport에서 확인한다.
- 다음 적용: typecheck·test·build 뒤 중복 설정과 stale asset을 검사하고, Figma 기준/현재 viewport의 열 너비·높이·overflow·browser 오류를 smoke 검증한다.

## 새 항목 템플릿

```markdown
### YYYY-MM-DD — 짧은 제목
- 상황: 재발 가능한 문제를 한 문장으로 적는다.
- 교훈: 원인 또는 판단 원칙을 한 문장으로 적는다.
- 다음 적용: 다음 작업에서 먼저 할 확인이나 예방 조치를 적는다.
```
