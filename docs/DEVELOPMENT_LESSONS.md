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

### 2026-07-20 — 품질 지표에는 달성 가능한 상태 전이가 있어야 한다
- 상황: Candidate 파일은 `candidate` 상태만 허용했지만 진행률은 허용되지 않은 상태값을 세어 항상 0점이었다.
- 교훈: 점수식을 추가할 때 입력 계약에서 실제로 그 상태에 도달하고 다음 단계로 이동할 수 있는지 함께 검증한다.
- 다음 적용: 검토 결정과 baseline 승격을 분리하고 fixture·CLI·rubric 테스트로 같은 상태 전이를 고정한다.

### 2026-07-20 — 관측 도구는 하나의 기준점을 먼저 정한다
- 상황: MLflow, 외부 tracing, Studio observer가 같은 LLM·tool 실행을 서로 다른 형태로 중복 기록하고 있었다.
- 교훈: 온라인 진단은 구조화 로그, 오프라인 평가와 보존 trace는 MLflow, graph 디버깅은 Studio로 역할을 분리한다.
- 다음 적용: 새 관측 의존성은 기존 도구로 충족되지 않는 요구와 데이터 경계를 먼저 문서화한 뒤 추가한다.

### 2026-07-20 — Windows 검증은 workspace별 cache와 temp로 격리한다
- 상황: 사용자 공용 `uv` cache와 pytest temp의 소유권·잔여 상태 때문에 코드와 무관한 접근 거부가 재현됐다.
- 교훈: 공용 경로 재사용 실패를 dependency 문제로 오인하지 말고 저장소의 `.tmp` 아래 격리 경로로 원인을 분리한다.
- 다음 적용: 검증 명령에 필요하면 `UV_CACHE_DIR`와 `--basetemp`를 workspace 경로로 지정하고 산출물은 커밋하지 않는다.

### 2026-07-20 — MLflow 저장소와 업무 원본의 경계를 분리한다
- 상황: 개발용 MLflow metadata와 artifact가 로컬 SQLite와 `.cache`에 있어 재배포·다중 노드 운영 시 내구성이 없다.
- 교훈: 운영 MLflow는 PostgreSQL metadata와 MinIO artifact를 분리하되 MinIO를 원본 SMB 복제소로 사용하지 않는다.
- 다음 적용: 운영 pilot 전에 MLflow proxy 방식과 전용 bucket을 검증하고 허용 artifact 목록부터 승인한다.

### 2026-07-20 — 설치 전에 원격 runtime의 정확한 상태를 확인한다
- 상황: 모델 설치 요청을 점검하니 필요한 embedding 모델과 Ollama 서비스가 원격 서버에 이미 준비돼 있었다.
- 교훈: 이름 추정으로 다시 다운로드하지 말고 version, model 목록, API 응답, 실제 dimension을 먼저 확인한다.
- 다음 적용: 원격 변경 전 read-only inventory와 합성 smoke test를 실행하고 부족한 항목만 설치한다.

### 2026-07-20 — cold start와 warm latency를 분리한다
- 상황: 같은 embedding 요청도 첫 호출과 연결·모델이 준비된 뒤의 호출 시간이 크게 달랐다.
- 교훈: 단일 측정값으로 SLO를 판단하지 말고 cold/warm을 나눠 p50/p95와 initialization 원인을 기록한다.
- 다음 적용: 배포 smoke에 prewarm과 연속 호출 측정을 포함하고 hot path는 세션/HTTP 연결을 재사용한다.

### 2026-07-20 — 설치·실행·권한은 서로 다른 점검 항목이다
- 상황: 실행 파일과 서비스가 존재해도 현재 계정의 container/service 접근 권한이 없어 관리 명령이 실패할 수 있었다.
- 교훈: binary version, service 상태, 사용자 권한, 실제 API 호출을 각각 확인해야 “사용 가능”이라고 판정할 수 있다.
- 다음 적용: 운영 체크리스트에 네 항목을 분리하고 권한 부족을 재설치로 우회하지 않는다.

### 2026-07-20 — 큰 모델 작업 전에 용량과 중복을 확인한다
- 상황: GPU 여유와 별개로 root disk 사용률이 높아 추가 모델 다운로드가 서비스 안정성을 해칠 수 있었다.
- 교훈: GPU memory만 보지 말고 disk 여유, 기존 blob 중복, 예상 download 크기, rollback 공간을 함께 계산한다.
- 다음 적용: model pull 전 inventory를 남기고 용량 임계치 초과 시 설치 대신 정리·별도 volume 계획을 먼저 승인받는다.

### 2026-07-20 — 저장된 SSH 세션은 자동 접속 계약이 아니다
- 상황: GUI의 한글 PuTTY 세션명이 registry에서는 ANSI percent-encoding되어 `plink -load`가 host를 찾지 못했다.
- 교훈: 세션 존재와 비대화식 접속 가능 여부를 분리하고 escaped key의 host·port와 최초 신뢰·인증 조건을 확인한다.
- 다음 적용: tunnel 스크립트는 저장값을 로그에 남기지 않는 host fallback과 session·인증·port 오류 구분을 유지한다.

## 새 항목 템플릿

```markdown
### YYYY-MM-DD — 짧은 제목
- 상황: 재발 가능한 문제를 한 문장으로 적는다.
- 교훈: 원인 또는 판단 원칙을 한 문장으로 적는다.
- 다음 적용: 다음 작업에서 먼저 할 확인이나 예방 조치를 적는다.
```
