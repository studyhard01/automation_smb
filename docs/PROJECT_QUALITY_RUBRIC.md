# 프로젝트 품질 rubric

이 문서는 `automation_smb`의 코드·설정·문서 변경을 같은 기준으로 반복 평가하기 위한 저장소 전용 rubric이다.
현재 단계는 실제 의료 환경과 무관한 **합성 데이터 전용 기능 검증**이며, 운영 전환 보안 심사를 대신하지 않는다.

## 판정 원칙

- 총점은 **100점**이며 권장 기준은 **85점 이상**이다.
- 점수는 개선 우선순위를 보여주는 advisory 지표다. 기본 실행은 85점 미만을 보고하되 score만으로 실패하지 않는다.
- **hard gate는 점수와 별개**다. 하나라도 실패하면 점수가 높아도 품질 판정은 실패하고 명령은 non-zero로 종료한다.
- `--strict-score`를 사용하면 hard gate와 함께 85점 기준도 blocking gate가 된다.
- 외부 LLM judge처럼 네트워크·비용·비결정성이 있는 근거는 advisory다. 실행하지 못한 근거는 `skipped`로
  명시하고, 이를 hard gate 통과나 새 실측값으로 간주하지 않는다.

## 100점 배점

| 영역 | 배점 | 이 저장소에서 확인할 내용 |
|---|---:|---|
| 보안·데이터 경계 | 25 | 자격증명 비커밋, 합성 fixture, 실제 데이터 외부 전송 금지, SMB read-only, 외부 provider 명시 opt-in |
| 기능·회귀 | 20 | 비통합 테스트, API/tool/skill 계약, 오류·timeout·부분 실패, optional 계층의 fail-open |
| 지연·성능 | 20 | 인덱스 우선 fast path, 요청 중 SMB 전체 순회 금지, 상위 N건, 단계별 ms, 저장된 합성 지연 근거 |
| 코드·계약 | 15 | Ruff, Python/uv 규약, Pydantic API·read-only tool 계약, 저장된 합성 RAG 품질 근거 |
| 문서·맥락 일치 | 10 | `AGENTS.md` 기준의 목표·현재 단계·구현 상태, 상대 링크, 실행 명령, README 구조 설명 |
| 운영·관측 가능성 | 10 | request/trace 상관관계, 예산 초과·오류 관측, 재현 가능한 평가, hook/CI와 롤백 가능한 변경 |
| **합계** | **100** | |

세부 배점과 차단 여부는 다음과 같다.

| 영역 | 세부 기준 |
|---|---|
| 보안·데이터 경계 | 산출물 위생 7(H), 민감 값 비노출 10(H), read-only 구조 8(H) |
| 기능·회귀 | 비통합 pytest 10(H), 합성 fixture 계약 5(H), candidate 검토 진행률 5(A) |
| 지연·성능 | 인덱스 우선 4(H), timeout·elapsed 계약 4(H), 실측 freshness 2(A), retrieval p95 3(A), E2E p95 7(A) |
| 코드·계약 | Ruff 4(H), Python/uv 규약 2(H), API/tool 계약 2(H), RAG 품질 실측 7(A) |
| 문서·맥락 | Markdown 상대 링크 4(H), 핵심 문서 정합성 6(H) |
| 운영·관측성 | hook/CI 자동화 6(H), 안전한 관측성 계약 4(H) |

`H`는 hard gate, `A`는 advisory다. 저장된 실측 metric은 목표 충족 시 해당 점수를 모두 얻고, 미달하면 0점이다.
RAG 품질 7점은 Hit@5·no-answer·groundedness 세 목표의 충족 비율로 나누며, candidate 검토 5점은 실제 검토
완료 비율만 반영한다. 이 방식은 크게 미달한 수치가 비례 점수로 거의 만점에 가까워지는 것을 막는다.

점수가 낮아도 기능을 숨기거나 근거 없는 수치로 보정하지 않는다. 특히 현재 no-answer accuracy와 end-to-end 지연은
알려진 품질 부채로 점수에 반영하고 개선 대상으로 유지한다.

## Hard gate

다음 항목은 advisory 점수로 상쇄할 수 없다.

1. **저장소 경계**
   - 실제 자격증명, API key, 내부 IP/호스트, 실제 UNC/SMB 경로, 생성 cache/index/database가 커밋 대상에 없다.
   - `.env`는 추적하지 않고 `.env.example`에는 비어 있거나 명백한 placeholder만 둔다.
2. **데이터·외부 전송 경계**
   - fixture와 smoke 입력은 일반 합성 데이터다.
   - 실제 환자·검사 파일의 본문·경로·목록을 외부 LLM, judge, trace, 검색 API나 저장소로 보내는 경로가 없다.
3. **SMB read-only**
   - 명시 승인 없이 SMB 파일을 쓰기·이동·삭제하지 않는다.
   - 사용자 요청 경로에서 SMB 전체를 실시간 순회하지 않는다.
4. **결정적 회귀 검사**
   - 비통합 `pytest`, Ruff, 추적 Markdown 상대 링크 검사가 통과한다.
   - 실패나 실행 누락을 성공으로 보고하지 않는다.

`--static-only`는 Ruff/pytest 명령을 실행하지 않으므로 해당 실행만으로 전체 hard gate를 새로 증명하지 않으며
완료 판정은 non-zero로 종료한다. 빠른 정적 점검에만 사용하고, 커밋·push·PR 전에는 기본 또는 strict 평가를 실행한다.

## 로컬 실행

기본 실행은 결정적 로컬 검사를 모두 수행하고, 저장된 합성 품질·지연 근거의 상태와 freshness를 함께 읽는다.

```powershell
uv run --no-sync python scripts/evaluate_quality.py
```

선택 인자는 다음과 같다.

```powershell
# 사람이 읽는 출력과 같은 평가 결과를 JSON으로 저장
uv run --no-sync python scripts/evaluate_quality.py --json-output .tmp/project-quality.json

# hard gate 또는 85점 미만이면 실패
uv run --no-sync python scripts/evaluate_quality.py --strict-score

# Ruff/pytest 실행 없이 저장소 정적 검사와 저장된 근거만 확인
uv run --no-sync python scripts/evaluate_quality.py --static-only
```

`--json-output PATH`는 부모 디렉터리를 필요에 따라 만들고 동일 판정 결과를 저장한다. JSON의 주요 필드는
`rubric_id`, `rubric_version`, `evaluated_on`, `score`, `max_score`, `target_score`, `target_met`,
`hard_gates_passed`, `hard_gate_failures`, `skipped_hard_gates`, `categories`, `criteria`다. 기본 JSON 저장 경로는
없으며, 예시의 `.tmp/` 결과는 로컬 산출물이므로 커밋하지 않는다.

## 변경 시 자동 실행

현재 clone에서는 추적된 hook을 한 번 활성화한다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\enable_quality_hook.ps1
```

이 명령은 저장소의 hook 경로를 설정한다. 이후 코드·설정·문서 커밋 전에 evaluator가 실행되며 `--no-verify`로
건너뛰지 않는다. GitHub workflow는 push와 pull request에서 전체 evaluator를 실행해 hard gate를 강제하고 85점
기준은 advisory로 표시한다. 외부 서비스가 필요한 현재 측정 근거를 score 때문에 임의로 재실행하지 않으며,
release-readiness 판단 때 `--strict-score`를 명시적으로 실행한다. CI에서는 실제 SMB, 로컬 embedding/DB, 외부
judge 자격증명을 요구하지 않는다.

에이전트와 개발자는 hook 실행 여부와 무관하게 변경 후 evaluator를 직접 실행하고, 완료 보고에 다음을 남긴다.

- 총점과 category별 주요 감점
- hard gate 통과/실패
- 실행한 검사와 실패 원인
- 저장된 합성 evidence의 provenance·freshness와 목표 미달 항목

## 저장된 합성 evidence

rubric의 machine-readable source of truth는 [`config/project_quality_rubric.json`](../config/project_quality_rubric.json)이다.
외부 서비스를 매 커밋마다 호출하지 않으며, 품질·지연 점수는 이 파일의 `measured_evidence`에 저장된 검증된 합성
dataset 측정 기록을 읽는다. 현재 기준 근거는 2026-07-16 MLflow Phase 3 결과다.

| 근거 | 현재 값 | 목표/판정 |
|---|---:|---|
| validated golden | 15건 | candidate 25건은 사람 승인 전까지 제외 |
| Retrieval Hit@5 | 83.3% | 90% 이상 |
| no-answer accuracy | 0% | 95% 이상 |
| Groundedness judge | 92.3% | advisory, 외부 judge 자체는 release gate 아님 |
| retrieval p95 | 195.6ms | 1초 미만 |
| end-to-end p95 | 4.91초 | 절대값과 baseline 대비 악화를 함께 추적 |

상세 dataset/run ID와 scorer 계약은
[`MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md`](MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md)를 기준으로 한다.

## 합성 evidence 갱신

측정 근거는 기능이나 dataset이 바뀌었다고 자동으로 새 값이 되지 않는다. 다음 절차로 갱신한다.

1. `document_chatbot_golden.jsonl`의 `validated` 항목만 사용하고 dataset version/fingerprint를 기록한다.
2. 로컬 embedding endpoint와 PostgreSQL/pgvector를 준비한 뒤 retrieval-only baseline을 재실행한다.
3. end-to-end 또는 외부 judge가 필요하면 먼저 1건 smoke를 수행하고, 합성 질문·답변·chunk만 전송되는지 확인한다.
4. 실행 명령과 근거를 확인한 뒤 `config/project_quality_rubric.json`의 `measured_evidence`에 `kind`, `advisory`,
   `recorded_on`, `max_age_days`, `source`, `run_fingerprint`, `dataset_fingerprint`, `metrics`를 함께 반영한다.
   비밀값, 내부 endpoint, 실제 경로·본문은 기록하지 않는다.
5. evaluator 기본 실행과 `--strict-score`를 다시 실행해 점수와 freshness를 확인한다.

retrieval-only 예시는 다음과 같다.

```powershell
uv run --no-sync python .\scripts\run_mlflow_doc_eval.py `
  --mode retrieval `
  --dataset .\data\evaluation\document_chatbot_golden.jsonl `
  --top-k 5 `
  --no-mlflow
```

외부 judge는 `--judge`, `--include-trace-content`, `--confirm-external-judge-data`와 외부 provider 선택을 모두
명시한 합성 평가에서만 실행한다. 실행하지 않았다면 기존 근거의 측정일을 유지하고 `skipped` 사유를 보고한다.
