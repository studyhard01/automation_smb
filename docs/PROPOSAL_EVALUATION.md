# 기안 평가

기준일: 2026-08-19

## 목적

기안 생성 변경이 근거성·내용 품질·XLSX 구조·지연을 악화시키지 않는지 합성 데이터로 확인한다. 평가 코드는 서비스
runtime에서 호출되지 않는 개발 품질 도구지만 기안 변경의 회귀 방지를 위해 유지한다.

평가 입력과 결과는 일반 합성 문서만 사용한다. 실제 공유폴더 내용이나 의료 식별자를 저장소·외부 서비스로 보내지 않는다.

## 구성

- `documents.jsonl` — 합성 문서와 Chunk
- `proposal_cases.jsonl` — 요청, 선택 문서, 기대 사실·구조
- predictions/checkpoint JSONL — 평가 대상 생성 결과
- XLSX artifact directory — live 실행 생성물
- evaluation JSON — case별 점수·hard gate·지연

dataset은 이 저장소에 내장하지 않고 별도 로컬 workspace에서 전달한다. 과거 문서 챗봇 golden/candidate JSONL은 현재
기안 평가와 관계가 없어 제거했다.

## 실행 모드

| Mode | 목적 |
|---|---|
| `preflight` | dataset 구조·참조·중복·분할 검증 |
| `oracle_contract_check` | 정답 계약이 evaluator 상한을 만족하는지 확인 |
| `baseline` | 결정론적 기본 예측으로 scorer 동작 확인 |
| `predictions` | 미리 생성한 prediction JSONL 평가 |
| `live` | 현재 기안 생성 경로와 온프레미스 Judge 실행 |

`live`는 checkpoint, XLSX 디렉터리, 비식별 live report 경로가 필요하다. raw 출력은 저장소 밖의 새 경로에 exclusive로
생성하며 `--resume`은 완료 case만 건너뛴다.

## 평가 축

| 영역 | 확인 내용 |
|---|---|
| 근거·내용 | 기대 사실, 누락·환각, Citation, 필수 질문 |
| 구조 | 유형별 section, 목록·표, 수정 계약 |
| XLSX | 파일 존재, ZIP·OOXML, 셀 geometry, workbook view |
| 안전 | gold 비노출, 실제 데이터 금지, 외부 Judge 금지 |
| 지연 | 단계별·전체 elapsed와 deadline |

상세 점수와 hard gate의 기준값은 `proposal_models.py`, `proposal_scoring.py`를 단일 진실 원천으로 사용한다. 문서에 숫자를
중복해 적지 않는다.

## 실행 예시

PowerShell에서 저장소 루트를 기준으로 실행한다.

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"

uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode preflight `
  --documents C:\path\to\documents.jsonl `
  --proposal-cases C:\path\to\proposal_cases.jsonl `
  --output C:\path\to\preflight.json
```

현재 생성 경로를 한 case만 smoke할 때:

```powershell
uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode live `
  --documents C:\path\to\documents.jsonl `
  --proposal-cases C:\path\to\proposal_cases.jsonl `
  --scope current_tool `
  --checkpoint C:\outside-repo\predictions.jsonl `
  --artifact-directory C:\outside-repo\xlsx `
  --live-report C:\outside-repo\live-report.json `
  --output C:\outside-repo\evaluation.json `
  --max-cases 1 `
  --deadline-seconds 180 `
  --confirm-local-sensitive-data
```

실제 입력을 쓸 수 있다는 의미가 아니다. `--confirm-local-sensitive-data`는 합성·비식별 로컬 입력 경계를 실행자가 확인했다는
명시적 표시다.

## 구현 위치

- `scripts/run_proposal_evaluation.py` — CLI와 안전한 출력 경로 검사
- `backend/src/smb_finder/evaluation/proposal_runner.py` — dataset·case orchestration
- `backend/src/smb_finder/evaluation/proposal_live_runner.py` — 현재 생성 경로 실행·checkpoint
- `backend/src/smb_finder/evaluation/proposal_content_judge.py` — 온프레미스 내용 Judge
- `backend/src/smb_finder/evaluation/proposal_scoring.py` — XLSX·구조·지연 점수
- `backend/src/smb_finder/evaluation/proposal_models.py` — 평가 계약

## 자동 검증

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv run --no-sync pytest `
  backend/tests/test_proposal_evaluation.py `
  backend/tests/test_proposal_live_runner.py `
  backend/tests/test_proposal_content_judge.py -q
```

평가 결과가 좋더라도 최종 XLSX는 실제 Excel 또는 렌더 화면으로 확인한다. oracle 점수는 제품 품질 점수가 아니며,
gold 비노출 live 결과와 사람 검토를 최종 판단에 사용한다.
