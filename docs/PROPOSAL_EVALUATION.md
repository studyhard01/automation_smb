# 기안 생성 도구 평가 계약

## 목적과 안전 경계

이 평가는 참고 문서를 근거로 만든 기안 초안과 XLSX 결과, 생성 도구의 전체 흐름을 한 번에 채점한다. 평가기는
`documents.jsonl`과 `proposal_cases.jsonl`을 **읽기만** 하며 SMB를 순회하거나 LLM을 호출하지 않는다. 실제 실행 결과는 별도
`predictions.jsonl`로 전달한다.

최종 JSON 보고서에는 원문, 제목, 재가 문구, 본문, prompt, LLM 응답, 상대·절대 경로, 파일명, 내부 주소를 넣지 않는다.
case/group/artifact/chunk ID, 점수, 건수, 문자·token 길이, SHA-256, 시간, 제한된 status만 남긴다. 실제 dataset JSONL과
prediction JSONL은 민감할 수 있으므로 저장소 밖의 승인된 로컬 위치에서 관리한다.

## 평가 단위

`proposal_cases.jsonl` 한 줄의 기안 sheet를 case 한 건으로 본다. 같은 업무의 초안·수정본은 `group_id`가 같으므로 split과
통계를 group 단위로도 확인한다. 법인카드 case는 계약이 달라 이 평가의 분모에 넣지 않는다.

case가 참조한 artifact가 `documents.jsonl`에 없거나, reference ID가 비었거나, 모든 reference에 추출 chunk가 없으면
`skipped_reference_missing`으로 기록한다. 이 case는 모델을 호출하거나 점수 평균의 분모에 넣지 않는다. 일부 reference
ID만 누락돼도 잘못된 근거로 평가하지 않도록 skip한다.

## 100점 채점표

| 범주 | 점수 | 결정론 검사 |
|---|---:|---|
| 근거·context | 25 | reference artifact recall 10, 유효 chunk precision 5, 정답·사후자료 누출 방지 5, packed context budget 준수 5 |
| LLM 내용 | 35 | 제목 5, 재가 문구 4, legacy 본문 투영 7, V2 section 역할·제목 7, 표·목록·문단 block 7, citation 5 |
| XLSX 결과 | 25 | 생성 3, ZIP 정상 5, 필수 OOXML part 4, 제목 셀 4, 재가 셀 4, 본문 행 5 |
| 도구 흐름 | 15 | evidence·LLM·workbook·save stage 성공 각 3, 정확한 순서 3 |

문자열 유사도는 NFKC 정규화와 공백 통합 후 `SequenceMatcher`로 계산한다. V2 구조 점수는 dataset의
`body_sections` 역할·제목 순서와 `body_rows`의 다중 셀 표, 목록 marker를 `document.sections[].blocks`와 비교한다.
`E###` citation 문법과 packed citation 범위를 3점, `citation_map`이 실제 사용 chunk ID를 가리키는지를 2점으로 검사한다.
외부 judge나 임베딩 모델을 사용하지 않으므로 같은 입력은 같은 점수를 낸다. 현재 XLSX 계약은 `기안지` sheet의 `C8`,
`A10`, `A15` 이후 본문 행을 검사한다. dataset의 `source_sheet`는 이전 양식명을 보조 탐색할 때만 사용한다.

### hard gate

다음 조건은 총점과 별도로 모두 통과해야 한다.

1. reference가 실제로 존재하고 추출 chunk가 있다.
2. prediction이 성공했고 legacy `fields`와 `proposal-document-v2`, `proposal-context-usage-v1`을 모두 가진다.
3. 사용 근거에 target 기안서나 `excluded_post_event_artifact_ids`가 섞이지 않는다.
4. 실제 packed context가 runtime input budget 이하다.
5. XLSX가 정상 ZIP이고 필수 part와 세 출력 영역이 legacy projection과 일치한다.
6. `evidence → llm → workbook → save` 네 stage가 순서대로 성공한다.

표 matrix 자체는 현재 XLSX가 33행 A열 projection이므로 hard gate로 요구하지 않는다. 대신 expected/predicted section 수,
표 행 수, 목록 항목 수, block type 수, projection 원본·출력·생략 행 수와 `truncated`를 비식별 진단값으로 남긴다.

hard gate 하나라도 실패하면 `hard_gate_score_cap` 기본값인 49점으로 최종 점수를 제한한다. 모든 gate를 통과하고 최종 점수가
기본 80점 이상일 때만 `passed`다. raw 점수도 함께 남겨 품질 점수는 높지만 실행 완결성이 깨진 경우를 구분한다.

## context preflight

### runtime budget과 모델 최대치

두 값을 섞지 않는다.

- `runtime_context_window_tokens`: 운영에서 한 요청에 허용할 budget. 기본 24,576 token이다.
- `model_context_limit_tokens`: 모델 metadata가 밝힌 이론상 최대치. 알 수 없으면 생략한다.
- `effective_input_budget_tokens`: runtime window에서 prompt reserve와 output reserve를 뺀 실제 reference 입력 한도다.

예를 들어 모델 metadata 한도가 262,144여도 runtime budget을 24,576으로 두었다면 운영 예산을 기준으로 packing하고 채점한다.
기본 prompt reserve 1,024와 output reserve 4,096을 빼면 reference에 쓸 수 있는 input budget은 19,456 token이다.
모델 한도는 별도 `over_model_limit` 통계에만 사용하며 evaluator에 특정 모델 값을 hardcode하지 않는다.

### 길이 추정과 고정 bucket

한국어·혼합 문서의 합성 실측에서 2자/token이 실제보다 낮아, 여유를 둔 `ceil(문자 수 × 5 / 8)`을 token 추정치로 사용한다. 모델 최대치와 관계없이
raw reference 길이를 다음 고정 구간으로 집계한다.

- `lte_8k`: 8,192 이하
- `8k_to_16k`: 8,193~16,384
- `16k_to_32k`: 16,385~32,768
- `32k_to_64k`: 32,769~65,536
- `gt_64k`: 65,537 이상

보고서에는 raw/packed 문자 수의 최솟값·중앙값·최댓값, raw token 최솟값·중앙값·최댓값, packed token 최댓값, bucket별 건수,
runtime/model 초과 case ID와 group ID만 남긴다. 실제 실행이 `proposal_context_limit_exceeded`를 반환하면 관측된 packed token의
최솟값·중앙값·최댓값과 해당 ID를 별도로 모은다.

### 최소 손실 packing

raw context가 input budget을 넘으면 다음 순서로 줄인다.

1. 동일 텍스트 SHA-256의 중복 chunk를 제거한다.
2. reference artifact별 chunk를 round-robin으로 선택해 첫 문서가 budget을 독점하지 않게 한다.
3. 원본 reference 순서와 artifact 내부 chunk 순서는 유지한다.
4. 마지막 남은 budget에는 마지막 chunk의 앞부분만 넣고 즉시 중단한다.
5. packed context의 내용 대신 SHA-256만 보고서에 기록한다.

이 정책은 모든 자료를 무작정 요약해 사실을 바꾸지 않는 최소 조치다. 초과 case가 많으면 다음 실험에서 section·page 단위
retrieval, 낮은 우선순위 reference 제외, map-reduce 요약을 별도 후보로 비교하되 golden 정답과 사후 문서는 입력하지 않는다.

## 실행 모드

### 1. preflight

dataset 유효성, reference 누락, raw/packed context 길이만 계산한다. SMB와 LLM, workbook 생성기를 호출하지 않는다.

```powershell
uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode preflight `
  --documents "<외부-dataset>/documents.jsonl" `
  --proposal-cases "<외부-dataset>/proposal_cases.jsonl" `
  --runtime-context-window-tokens 24576 `
  --model-context-limit-tokens 262144 `
  --output ".runtime/proposal_evaluation/preflight.json"
```

### 2. baseline

expected 출력과 reference를 oracle prediction으로 투영해 evaluator 자체의 상한과 현재 workbook 계약의 수용 범위를 검사한다.
LLM·SMB는 호출하지 않으며 workbook은 저장하지 않고 메모리에서 생성·검사한다. `structure_only` case가 현재 출력 길이·행 구조를
넘으면 workbook hard gate가 실패하는 것이 정상이다. `simulated` stage는 baseline 보고서에서만 성공으로 간주한다.

```powershell
uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode baseline `
  --documents "<외부-dataset>/documents.jsonl" `
  --proposal-cases "<외부-dataset>/proposal_cases.jsonl" `
  --scope all `
  --output ".runtime/proposal_evaluation/baseline.json"
```

### 3. predictions

실제 도구 실행 결과를 JSONL로 받아 채점한다. workbook 경로는 prediction JSONL 기준 상대 경로 또는 명시한 로컬 절대 경로를
읽으며, 보고서에는 경로를 복사하지 않는다.

```json
{
  "case_id": "prp-<20 hex>",
  "status": "ok",
  "fields": {
    "title": "<생성 제목>",
    "approval_request": "<생성 재가 문구>",
    "body": "<생성 본문>"
  },
  "document": {
    "schema_version": "proposal-document-v2",
    "title": "<생성 제목>",
    "approval_request": "<생성 재가 문구>",
    "sections": [
      {
        "heading": "1. 목적",
        "semantic_role": "purpose",
        "citations": ["E001"],
        "blocks": [{"type": "paragraph", "text": "<문단>"}],
        "missing_information": []
      }
    ],
    "missing_information": []
  },
  "evidence_artifact_ids": ["art-<20 hex>"],
  "evidence_chunk_ids": ["chk-<16 hex>"],
  "citation_map": {"E001": "chk-<16 hex>"},
  "context": {
    "raw_chars": 0,
    "packed_chars": 0,
    "estimated_tokens": 0,
    "context_sha256": "<64 hex>"
  },
  "context_usage": {
    "schema_version": "proposal-context-usage-v1",
    "source_citation_count": 0,
    "packed_citation_count": 0,
    "deduplicated_citation_count": 0,
    "source_document_count": 0,
    "packed_document_count": 0,
    "context_budget_chars": 0,
    "context_chars": 0,
    "estimated_input_tokens": 0,
    "truncated": false,
    "retry_count": 0
  },
  "projection": {
    "source_line_count": 0,
    "output_line_count": 0,
    "omitted_line_count": 0,
    "truncated": false
  },
  "tool_events": [
    {"stage": "evidence", "status": "ok", "elapsed_ms": 0},
    {"stage": "llm", "status": "ok", "elapsed_ms": 0},
    {"stage": "workbook", "status": "ok", "elapsed_ms": 0},
    {"stage": "save", "status": "ok", "elapsed_ms": 0}
  ],
  "workbook_path": "result.xlsx",
  "timings_ms": {
    "evidence_ms": 0,
    "llm_ms": 0,
    "workbook_ms": 0,
    "save_ms": 0,
    "total_ms": 0
  },
  "failure_code": "none"
}
```

실패 결과는 `status=error`와 함께 `proposal_context_limit_exceeded`, `generation_error`, `workbook_error`, `tool_error`, `unknown` 중
하나의 제한된 `failure_code`를 쓴다. 오류 메시지와 provider 응답은 넣지 않는다.

```powershell
uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode predictions `
  --documents "<외부-dataset>/documents.jsonl" `
  --proposal-cases "<외부-dataset>/proposal_cases.jsonl" `
  --predictions "<로컬-실행결과>/predictions.jsonl" `
  --output ".runtime/proposal_evaluation/result.json" `
  --fail-on-quality
```

## 해석 순서

1. `skipped_reference_missing_count`를 먼저 줄여 평가 분모를 확정한다.
2. runtime 초과 case와 bucket 분포로 운영 context budget을 정한다.
3. baseline에서 `current_tool`과 `structure_only`를 나눠 현재 workbook 구조의 수용 범위를 확인한다.
4. predictions에서 hard gate 실패 원인을 먼저 고친 뒤 범주별 평균을 비교한다.
5. 실제 `proposal_context_limit_exceeded`가 있으면 preflight 추정이 아니라 관측된 실패 token 최소·중앙·최댓값을 기준으로 packing 정책을 조정한다.

## 2026-08-11 dataset 스냅샷 결과

현재 운영 기본값은 runtime 24,576 token, prompt reserve 1,024, output reserve 4,096이며 실제 reference 입력 예산은
19,456 token이다. 현재 모델 metadata 최대치 262,144 token은 runtime과 별도 통계로만 사용했다.

| 소스 | 전체 / ready / skip | raw runtime 초과 | model 최대 초과 | raw token 최소 / 중앙 / 최대 |
|---|---:|---:|---:|---:|
| 로컬 | 18 / 16 / 2 | 6 | 0 | 735 / 10,426 / 45,025 |
| SMB 2026 | 41 / 33 / 8 | 19 | 0 | 735 / 26,780 / 45,025 |

수정된 V2→legacy projection oracle baseline은 로컬 16/16(평균 96.1426점), SMB 33/33(평균 96.1766점)이 hard gate를
통과했다. 첫 실행에서 표 셀 내부 개행 때문에 복잡 case 22건이 33행을 넘는 결함을 평가기가 발견했고, block 텍스트의
단일 행 정규화와 최종 33행·6,000자 불변 조건을 추가한 뒤 모두 해소됐다.

이 결과는 evaluator와 projection/XLSX 도구의 수용 상한이다. 실제 LLM 품질이나 사람이 승인한 golden 점수가 아니다.
초기 합성 smoke의 HTTP 500은 28,672 창 allocation 문제였고, 24,576으로 내린 뒤 strict V2 생성이 성공했다. 실제
`proposal_context_limit_exceeded` 관측치는 아직 0건이다. 원문 없는 최종 JSON 보고서는 dataset 작업공간의 `evaluation`
폴더에 두고 Git에는 추가하지 않는다.

추가 allocation smoke에서는 24,576 창까지 성공하고 28,672 창부터 작은 입력도 HTTP 500임을 확인했다. 이는 원문 길이
초과가 아니라 runner가 요청 창을 할당하지 못한 관측치다. 운영 기본값은 성공이 확인된 24,576으로 낮췄으며, raw reference가
그 입력 예산을 넘는 case는 로컬 6건·SMB 19건이다.

token 추정 보정 전 2자/token 값은 실제보다 낮았다. 짧은 합성 prompt는 근거 82자에서 `prompt_eval_count=368`, 긴 합성
prompt는 근거 9,998자에서 6,239였다. 고정 prompt 비용을 빼면 근거는 약 1.69자/token이므로 평가기는 여유를 둔
1.6자/token으로 다시 집계했다. 최종 raw token bucket은 로컬 `5/5/0/6/0`, SMB `8/6/13/6/0`이다.
