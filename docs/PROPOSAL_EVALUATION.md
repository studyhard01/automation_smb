# 기안 생성 도구 평가 계약

## 목적과 안전 경계

이 평가는 참고 문서를 근거로 만든 기안 초안과 XLSX 결과, 생성 도구의 전체 흐름을 한 번에 채점한다. 평가기는
`documents.jsonl`과 `proposal_cases.jsonl`을 **읽기만** 한다. `preflight`, `oracle_contract_check`, `predictions`는 SMB와 LLM을
호출하지 않는다. `live`만 실제 공개 core 함수와 온프레미스 `LocalProposalDraftGenerator`를 호출하며 SMB 서비스·업로드
관리자는 생성하지 않는다. live는 생성 뒤 실제 주장별 `assess`와 근거 없는 block 제거까지 수행한다. 필수 확인 질문이 나오면
production과 동일하게 XLSX 생성·저장을 건너뛰고 질문 field key와 집계만 checkpoint한다.

최종 JSON 보고서에는 원문, 제목, 재가 문구, 본문, prompt, LLM 응답, 상대·절대 경로, 파일명, 내부 주소를 넣지 않는다.
case/group/artifact/chunk ID, 점수, 건수, 문자·token 길이, SHA-256, 시간, 제한된 status만 남긴다. 실제 dataset JSONL과
prediction JSONL은 민감할 수 있으므로 저장소 밖의 승인된 로컬 위치에서 관리한다.

live 생성 입력은 case의 `instruction`, `reference_artifact_ids`, `excluded_post_event_artifact_ids`와 해당 artifact의
`file_name`·`role`·chunk `text`·`anchor`뿐이다. `expected`와 target artifact는 유형 판정, filter, LLM 입력 객체에 넣지 않는다.
stable artifact/chunk ID는 고정 namespace UUID로 바꿔 core `DocumentCitation`에 넣으며 UUID나 원문은 보고서에 남기지 않는다.

## 평가 단위

`proposal_cases.jsonl` 한 줄의 기안 sheet를 case 한 건으로 본다. 같은 업무의 초안·수정본은 `group_id`가 같으므로 split과
통계를 group 단위로도 확인한다. 법인카드 case는 계약이 달라 이 평가의 분모에 넣지 않는다.

case가 참조한 artifact가 `documents.jsonl`에 없거나, reference ID가 비었거나, 모든 reference에 추출 chunk가 없으면
`skipped_reference_missing`으로 기록한다. 이 case는 모델을 호출하거나 점수 평균의 분모에 넣지 않는다. 일부 reference
ID만 누락돼도 잘못된 근거로 평가하지 않도록 skip한다.

## 100점 채점표

| 범주 | 점수 | 결정론 검사 |
|---|---:|---|
| 근거·context | 25 | legacy는 recall 10·chunk 5·누출 5·budget 5. filter 계약 노출 시 recall 8·chunk 4·사후자료 제외 recall 3·오제외 방지 2·누출 방지 3·budget 5 |
| LLM 내용 | 35 | 제목 5, 재가 문구 4, legacy 본문 투영 7, V2 section 역할·제목·유형 profile 7, 표·목록·문단 block 7, citation 5 |
| XLSX 결과 | 25 | legacy는 기존 3필드 계약, V2는 생성·OOXML·3필드·본문 레이아웃·실제 표·appendix를 합산 |
| 도구 흐름 | 15 | evidence·LLM·workbook·save stage 성공 각 3, 정확한 순서 3 |

문자열 유사도는 NFKC 정규화와 공백 통합 후 `SequenceMatcher`로 계산한다. V2 구조 점수는 dataset의
`body_sections` 역할·제목 순서와 `body_rows`의 다중 셀 표, 목록 marker를 `document.sections[].blocks`와 비교한다.
`E###` citation 문법과 packed citation 범위를 3점, `citation_map`이 실제 사용 chunk ID를 가리키는지를 2점으로 검사한다.
외부 judge나 임베딩 모델을 사용하지 않으므로 같은 입력은 같은 점수를 낸다. 현재 XLSX 계약은 `기안지` sheet의 `C8`,
`A10`, `A15` 이후 본문 행을 검사한다. dataset의 `source_sheet`는 이전 양식명을 보조 탐색할 때만 사용한다.

### coverage와 제품 품질 eligibility

`candidate`, `needs_review`, `unassigned` case의 점수는 evaluator·도구 진단일 뿐 제품 품질 점수가 아니다. 보고서의
`product_quality_eligible`은 평가된 모든 case가 `review_status=reviewed`, `split=test`일 때만 참이다. oracle은 label을 그대로
출력에 복사하는 계약 검사이므로 review/split과 무관하게 항상 거짓이다.

새 검토 label은 전체 문장 대신 `fact_expectations`의 `fact_key`, `expectation(include|exclude)`, `expected_values`, `aliases`,
`source_artifact_ids`, `missing_action(omit|ask)`로 기록한다. 금액·날짜의 공백과 구분 기호 차이는 허용하지만 실제 문자·숫자는
일치해야 한다. reviewed test이고 label이 하나 이상 있을 때만 근거 사실의 포함, 누락, 비근거 추가를 결정론적으로 세고
`scored_points / possible_points`를 0~100으로 정규화한다. `include+ask` 값이 사용자 요청과 reference에 없으면 본문에
추측해 넣는 대신 같은 `fact_key`의 확인 질문을 만든 경우 정답으로 센다. 구 `required_fact_labels`,
`unsupported_addition_labels`는 기존 dataset의 진단 호환용으로만 유지하며 product-quality eligible 분모에는 넣지 않는다.
reviewed test라도 구조화 `fact_expectations`가 없으면 `scoring_basis=diagnostic`이다. 전체 line을 atomic fact로 자동 승격하지
않고, 원자 사실의 key·값·근거·누락 action을 다시 검토한 뒤에만 제품 점수로 인정한다. label이 없거나 검토 전이면 0점이
아니라 `not_scored`다.

### hard gate

다음 조건은 총점과 별도로 모두 통과해야 한다.

1. reference가 실제로 존재하고 추출 chunk가 있다.
2. prediction이 성공했고 legacy `fields`와 `proposal-document-v2`, `proposal-context-usage-v1`을 모두 가진다.
3. 사용 근거에 target 기안서나 `excluded_post_event_artifact_ids`가 섞이지 않는다.
4. 실제 packed context가 runtime input budget 이하다.
5. XLSX가 정상 ZIP이고 필수 part와 세 출력 영역이 일치하며, V2면 본문·표·appendix 렌더 계약도 통과한다.
6. `evidence → llm → workbook → save` 네 stage가 순서대로 성공한다.
7. evidence filter가 노출된 경우 사후자료 제외 recall 100%, 정상자료 오제외 0건, 제외 citation/context 누출 0건이며 공개 집계가 일관된다.
8. `event_attendance`인 경우 compact heading 세 개와 순서가 정확하고 별도 `행사 개요`·`전체 일정` section이 없다.
9. revision인 경우 metadata, feedback 기대 hash, 변경·보존 필드와 base 문서/workbook hash 불변성이 일치한다.
10. 필수 사실이 실제 입력에 없어 `missing_action=ask`인 case는 정확한 fact key만 최대 3개 질문하고 workbook/save를 건너뛰어야 한다.

10번 case는 답변 전 파일을 만들지 않는 것이 올바른 종료다. `completion.status=needs_clarification`이고 비식별
`question_field_keys` 집합이 기대 집합과 정확히 같으면 XLSX·save hard gate와 해당 25점·15점을 충족한 것으로 대체한다.
질문 prompt와 답변 원문은 평가 보고서에 저장하지 않는다. 필수 질문이 없는 일반 case는 기존처럼 실제 XLSX와 네 stage를 모두
통과해야 한다.

`xlsx_contract=legacy-v1`은 기존 A열 projection과 점수를 그대로 유지한다. `proposal-xlsx-v2`는 다음 직렬화 계약을 추가로
검사하며 하나라도 깨지면 `xlsx_render_contract_valid` hard gate가 실패한다.

- 일반 본문은 한 물리 행에 한 줄만 두고, 가로 병합·자동 줄바꿈·명시적 행 높이를 사용하지 않는다.
- 긴 일반 문장은 단어 경계에서 여러 기본 높이 행으로 나누며 각 행은 독립적으로 편집할 수 있어야 한다.
- 2~6열 표는 본문에 실제 셀 matrix와 사방 border로 렌더링한다. 셀 폭 확보를 위한 표 논리 셀 병합과 표 안의
  자동 줄바꿈·명시적 행 높이만 허용한다.
- 본문 한 셀에 `열1 | 열2`처럼 평탄화한 가짜 표를 두지 않는다.
- 6열 초과 또는 본문 잔여 행 부족 표는 `세부내용` appendix에 실제 matrix로 보존한다.
- expected/rendered content block 수, expected/rendered/inline/appendix 표 수, 구조 손실·생략 여부를 본문 없이 진단값으로 남긴다.

### 기안 유형과 section profile

별도 공개 `section_profile` 필드는 두지 않고 `proposal_type`을 profile ID로 사용한다. dataset case에
`proposal_type_label`이 있을 때만 요청 선택값, 최종 해결값, 해결 출처와 존재하는 canonical 역할의 상대 순서를 2점 범위에서
결정론적으로 채점한다. 역할은 근거가 있을 때만 생성하므로 누락 자체는 실패가 아니다.

- `purchase`: purpose → background → request → details → budget → schedule → expected_effect → attachments
- `event_attendance`: purpose → background → details → schedule → budget → expected_effect → attachments
- `general`: purpose → background → request → details → schedule → expected_effect → attachments → notes → other

기존 dataset에는 이 label이 없으므로 `not_labeled`로 기록하고 종전 35점 산식을 그대로 적용한다.

### evidence filter 평가

core의 `proposal-evidence-filter-v1` 공개 응답은 문서·citation의 입력/포함/제외 건수, 제외 이유별 건수와 fallback 여부만 가진다.
이 집계에는 경로·파일명·문서 ID가 없으므로 실제 제외 recall을 계산할 때 prediction adapter가 다음 stable ID를 평가 입력에만
추가한다.

- `excluded_evidence_artifact_ids`: filter가 제외한 artifact ID
- `excluded_evidence_chunk_ids`: filter가 제외한 chunk ID

평가기의 기대 제외 집합은 case의 `excluded_post_event_artifact_ids`, 기대 포함 집합은 `reference_artifact_ids`다. 제외 recall,
정상 reference 오제외, 제외 artifact/chunk가 `evidence_*` 또는 `citation_map`을 통해 다시 context로 들어왔는지를 검사한다.
최종 보고서에는 reason key, 파일명, 경로, citation 원문을 복사하지 않고 건수·recall·status만 남긴다. 기존 prediction이 filter
계약을 전혀 노출하지 않으면 `not_exposed` 중립으로 처리해 구버전 baseline 호환을 유지한다.

### 행사 참석 compact 구조

최종 해결 유형이 `event_attendance`이면 label 유무와 관계없이 section heading은 정확히 다음 세 개여야 한다.

1. `참가 목적`
2. `참가 내용`
3. `행사 주요 내용`

누락·추가·순서 위반을 각각 집계하며 `행사 개요`, `전체 일정`을 별도 section으로 만들면 hard gate가 실패한다. 비용·일시·장소·
참석자처럼 필요한 값은 `참가 내용` 내부 block에 두는 core 계약을 따른다.

### revision 평가

수정 피드백 원문은 보고서에 저장하지 않는다. revision golden label은 `revision_expectation`에 feedback SHA-256, base 문서 SHA-256,
기대 수정 문서 SHA-256, 변경해야 할 필드와 보존해야 할 필드만 둔다. 문서 hash는 UTF-8 JSON을 key 정렬·공백 없는 형식으로
직렬화한 SHA-256이다. prediction adapter는 공개 응답의 `revision_of_draft_id`, `revision_number`, `revision_summary`와 함께
수정 전 base document, 수정 전후 base document/workbook SHA-256을 기록한다.

평가기는 수정 문서 전체 hash로 feedback 적용을, 필드별 전후 비교로 변경·보존을, base hash의 before/after 일치로 원본 불변성을
검사한다. 보고서에는 revision summary 원문 대신 길이와 SHA-256, revision minor 숫자, count/status만 남긴다. label 없이 revision
metadata만 노출된 경우에는 공개 metadata와 제공된 base hash 불변성만 검사한다.

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

### 2. oracle_contract_check

expected 출력과 reference를 oracle prediction으로 합성해 evaluator 자체의 상한과 현재 workbook 계약의 수용 범위를 검사한다.
LLM·SMB는 호출하지 않으며 `render_proposal_workbook`으로 V2 workbook을 메모리에서 생성·검사한다. 본문 공간을 넘는 표는
`세부내용` appendix까지 검증한다. 공개 renderer가 없는 과도기 환경에서만 legacy wrapper로 후퇴한다. `simulated` stage는
oracle 보고서에서만 성공으로 간주한다. 과거 `baseline` 이름은 deprecated alias로만 허용하며 보고서에
`deprecated_baseline_alias_used=true`를 남긴다. 두 이름 모두 `evaluation_kind=oracle_contract_check`이고 제품 품질 점수로
사용할 수 없다.

```powershell
uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode oracle_contract_check `
  --documents "<외부-dataset>/documents.jsonl" `
  --proposal-cases "<외부-dataset>/proposal_cases.jsonl" `
  --scope all `
  --output ".runtime/proposal_evaluation/oracle_contract_check.json"
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
    "proposal_type": "purchase",
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
  "requested_proposal_type": "auto",
  "resolved_proposal_type": "purchase",
  "proposal_type_source": "rule",
  "evidence_filter": {
    "schema_version": "proposal-evidence-filter-v1",
    "input_document_count": 2,
    "included_document_count": 1,
    "excluded_document_count": 1,
    "input_citation_count": 2,
    "included_citation_count": 1,
    "excluded_citation_count": 1,
    "excluded_reason_counts": {"<제한된-core-reason>": 1},
    "fallback_used": false
  },
  "evidence_artifact_ids": ["art-<20 hex>"],
  "evidence_chunk_ids": ["chk-<16 hex>"],
  "excluded_evidence_artifact_ids": ["art-<20 hex>"],
  "excluded_evidence_chunk_ids": ["chk-<16 hex>"],
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
  "xlsx_contract": "proposal-xlsx-v2",
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

revision prediction은 위 공통 필드에 다음을 더한다. `revision_base_document`의 실제 문자열은 입력 JSONL에서만 읽고 최종
보고서에는 남기지 않는다.

```json
{
  "revision_of_draft_id": "<uuid>",
  "revision_number": "1.1",
  "revision_summary": "<core 공개 수정 요약>",
  "revision_base_document": {"schema_version": "proposal-document-v2", "...": "<수정 전 문서>"},
  "revision_base_document_sha256_before": "<64 hex>",
  "revision_base_document_sha256_after": "<64 hex>",
  "revision_base_workbook_sha256_before": "<64 hex>",
  "revision_base_workbook_sha256_after": "<64 hex>"
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

### 4. live

`live`는 다음 수직 경로만 호출한다.

`DocumentCitation 구성 → resolve_proposal_type → filter_proposal_evidence → LocalProposalDraftGenerator.generate →
normalize_proposal_document → project_document_to_legacy_fields → render_proposal_workbook`

filter 전 후보에는 reference와 `excluded_post_event_artifact_ids`를 함께 넣어 실제 선별을 시험한다. 생성 직전 target ID 또는
`is_target=true` artifact를 발견하면 실패한다. 기대 제외 후보가 filter 뒤에 남아도 oracle label로 실행을 막지 않고 그대로
generator에 전달한 뒤 scorer가 제외 recall·누출 hard gate 실패로 기록한다. 추출 chunk가 없는 reference는 LLM 없이 skip하고,
추출할 수 없는 기대 제외 후보는 `unavailable` 진단으로 남긴다. gold `expected`는 생성 경로에서 읽지 않는다.

checkpoint JSONL은 생성 본문과 로컬 workbook 상대 경로가 들어가는 제한 자료다. 최종 evaluation JSON과 live case/artifact
보고서만 비식별 보고서다. CLI는 `--confirm-local-sensitive-data`를 요구하고 raw checkpoint와 XLSX 디렉터리가 저장소 내부면
거부한다. workbook, live 보고서, 최종 evaluation 보고서는 `create-new/exclusive`로 저장해 같은 이름을 덮어쓰지 않는다.
`--resume`은 유효한 checkpoint의 성공·실패·skip case를 건너뛰며 workbook과 checkpoint를 다시 쓰지 않는다.
`--max-cases`와 전체 `--deadline-seconds`를 함께 쓸 수 있고 deadline은 새 case 시작 전에 검사한다. 이미 진행 중인 한 번의
로컬 LLM 호출은 core timeout을 따른다.

checkpoint 옆 manifest v2는 dataset fingerprint, scope, 모든 scope case의 생성 입력 fingerprint, template SHA-256과
`generation_config_fingerprint`를 결속한다. 생성 설정 fingerprint에는 provider·model·LLM endpoint identity와 core 구현,
`num_ctx`·출력 token·context 문자/문서/citation 예산·timeout·temperature·prompt reserve·schema/XLSX 계약을 넣되 model과
endpoint 원문은 저장하지 않는다. instruction/reference/제외 후보/target/type, template 또는 생성 설정이 달라지거나 manifest가
없는 구 checkpoint는 `--resume`로 재사용하지 않고 `checkpoint_manifest_mismatch`로 거부한다.

checkpoint별 exclusive run lock은 PID, process 시작/run identity와 host hash를 기록하고 전체 invocation 동안 유지한다. 같은 host에서
owner PID가 종료됐음이 명확할 때만 stale lock을 복구하며 active, 다른 host, 손상되어 소유자를 판정할 수 없는 lock은 모두 거부한다.
XLSX는 같은 디렉터리의 hash-addressed pending 파일에 먼저 쓰고, 저장소 밖 checkpoint 옆 raw journal에 prediction 한 줄,
manifest/case fingerprint와 workbook hash를 exclusive+fsync로 고정한 뒤 checkpoint append와 신규 target hard-link publish를 한다.
append 실패 뒤 `--resume`은 journal의 동일 prediction과 workbook을 사용하므로 LLM을 다시 호출하지 않는다. publish 완료는 별도
create-new marker로 남기며 기존 target hash가 다르거나 pending 복구가 끝나지 않으면 최종 비식별 live/evaluation 보고서를 만들지
않고 실패한다. raw journal과 pending workbook도 checkpoint와 같은 제한 자료이며 저장소 밖 로컬 평가 경로에서만 관리한다.

성공 prediction은 core의 최종 retry 기준 `packed_citation_map`과 `packed_context_sha256`을 필수로 요구한다. map 건수는
`context_usage.packed_citation_count`와 같아야 하고 모든 source UUID가 filter 입력 citation으로 역매핑되어야 하며 document의
citation이 map 범위 안에 있어야 한다. 누락·불일치는 `generation_error` checkpoint로 남긴다.

```powershell
uv run --no-sync python scripts/run_proposal_evaluation.py `
  --mode live `
  --documents "<외부-dataset>/documents.jsonl" `
  --proposal-cases "<외부-dataset>/proposal_cases.jsonl" `
  --checkpoint "<저장소-밖-승인된-로컬>/predictions.jsonl" `
  --artifact-directory "<저장소-밖-승인된-로컬>/xlsx" `
  --live-report ".runtime/proposal_live/live_report.json" `
  --output ".runtime/proposal_live/evaluation.json" `
  --max-cases 3 `
  --deadline-seconds 600 `
  --confirm-local-sensitive-data
```

중단 뒤에는 동일 경로와 `--resume`을 사용한다. checkpoint와 이미 저장된 XLSX는 로컬 평가 범위에서만 읽고 SMB로 저장하거나
업로드하지 않는다.

generation reference eligibility는 preflight와 live가 같은 함수로 판정한다. 조건은 reference ID가 하나 이상이고, 모든 ID가
존재하며, 각 artifact가 `ok/partial` 추출 상태이고 chunk를 하나 이상 가지는 것이다. 제외 후보만으로는 생성하지 않는다.
live 보고서의 reference 누락은 두 집계로 나눈다. `expected_reference_skip_case_count`는 이 canonical preflight가
`skipped_reference_missing`으로 판정한 case 수다. `unexpected_live_reference_failure_case_count`는 canonical ready였지만 live에서
추가로 reference를 구성하지 못한 수이며 정상 계약에서는 0이어야 한다. 기존
`skipped_reference_missing_case_count`는 둘을 합친 실제 live skip 수이므로 단독으로 dataset 분모와 비교하지 않는다.

지연도 두 종류를 분리한다. evaluator의 `latency_p50_ms/p95_ms`는 생성 실패와 수 ms짜리 reference skip까지 포함하는 전체 실행
진단이다. 제품 생성 지연으로 해석할 값은 네 tool stage가 모두 `ok`인 결과만 모은
`generated_completed_latency_p50_ms/p95_ms`다. live run 보고서에는 같은 의미의
`completed_latency_p50_ms/p95_ms`를 둔다.

live의 `completed_case_count`는 runner가 LLM·workbook·save까지 끝낸 수이고, evaluator의
`generated_completed_case_count`는 canonical 평가 분모 안에서 네 stage가 모두 `ok`인 수다. v2 SMB에서 전자는 12, 후자는
11이었던 이유는 runner 완료 1건을 당시 evaluator가 reference skip으로 제외한 availability 불일치였다. canonical readiness를
공유하는 v3에서는 완료 prediction이 evaluator skip으로 빠지면 회귀 실패로 본다.

## 해석 순서

1. `skipped_reference_missing_count`를 먼저 줄여 평가 분모를 확정한다.
2. runtime 초과 case와 bucket 분포로 운영 context budget을 정한다.
3. oracle contract check에서 `current_tool`과 `structure_only`를 나눠 현재 workbook 구조의 수용 범위를 확인한다.
4. predictions에서 hard gate 실패 원인을 먼저 고친 뒤 범주별 평균을 비교한다.
5. 실제 `proposal_context_limit_exceeded`가 있으면 preflight 추정이 아니라 관측된 실패 token 최소·중앙·최댓값을 기준으로 packing 정책을 조정한다.

## 2026-08-11 dataset 스냅샷 결과

현재 운영 기본값은 runtime 24,576 token, prompt reserve 1,024, output reserve 4,096이며 실제 reference 입력 예산은
19,456 token이다. 현재 모델 metadata 최대치 262,144 token은 runtime과 별도 통계로만 사용했다.

| 소스 | 전체 / ready / skip | raw runtime 초과 | model 최대 초과 | raw token 최소 / 중앙 / 최대 |
|---|---:|---:|---:|---:|
| 로컬 | 18 / 14 / 4 | 6 | 0 | 854 / 11,988 / 45,025 |
| SMB 2026 | 41 / 16 / 25 | 6 | 0 | 854 / 11,574 / 45,025 |

canonical eligibility 통일 전 V2 renderer oracle baseline은 로컬 16/16(평균 95.3926점), SMB 33/33(평균 95.3887점)이 XLSX 렌더 hard gate를
통과했다. `body_limit`은 두 corpus 모두 0건이다. 본문 33행 표시 범위 때문에 생략 고지가 들어간 case는 로컬 12건
(논리 block 151개), SMB 26건(논리 block 417개)이며 보고서에 내용 없이 진단값으로 남긴다. 표는 본문 실제 matrix 또는
`세부내용` appendix에 모두 보존됐다. 초기 projection 검사에서 표 셀 내부 개행 때문에 복잡 case 22건이 33행을 넘던 결함은
block 텍스트의 단일 행 정규화와 최종 33행·6,000자 불변 조건으로 해소됐다.

같은 oracle에서 evidence filter는 로컬 16 case의 기대 제외 artifact 56개와 SMB 33 case의 119개를 모두 제외해 recall 1.0,
오제외 0건, 제외 citation/context 누출 0건, 공개 집계 불일치 0건이었다. 이는 evaluator 선별 계산의 상한이며 실제 core 실행
품질 점수가 아니다. 현재 dataset에는 행사 compact heading golden label과 revision feedback/hash label이 없어 두 supplemental
평가의 실제 corpus 적용 건수는 0건이고, 합성 contract test로 정상·실패 경로만 검증했다.

### canonical eligibility 통일 전 실제 live v2 실행 결과

아래 값은 실제 core와 온프레미스 LLM을 실행했지만 preflight와 live availability 정의가 달랐던 v2 진단 기록이다. 새 checkpoint
manifest와 canonical eligibility를 적용한 v3 품질 결과로 재사용하지 않는다. oracle 계약 상한과 달리 제품 품질 실패를 그대로 포함한다. 두 corpus
모두 case가 candidate/unassigned이고 사실 label은 0건이므로 `product_quality_eligible=false`다.

| 소스 | 전체 / evaluator 평가 / expected skip | live 완료 / 생성 오류 / live reference 누락 | unexpected reference 실패 | 평균 점수 / pass | 완료 생성 p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|
| 로컬 | 18 / 16 / 2 | 9 / 7 / 2 | 0 | 43.4246 / 0 | 약 6.57초 / 13.25초 |
| SMB 2026 | 41 / 33 / 8 | 12 / 12 / 17 | 9 | 29.9331 / 0 | 약 6.60초 / 12.86초 |

v2 SMB에서는 evaluator skip 8건과 live reference 누락 17건이 포함관계가 아니었다. evaluator skip 8건은 live에서 생성 오류
7건·완료 1건이었고, live reference 누락 17건은 evaluator에서 모두 evaluated였다. 원인은 빈 reference에서 제외 후보만으로
생성한 결함과 preflight의 any-chunk 판정이었다. canonical 계약을 적용한 새 preflight는 로컬 4건, SMB 25건을 skip하고
각각 14건·16건만 usable로 본다. 기존 checkpoint는 manifest mismatch로 거부하므로 v3는 새 경로에서 실행해야 한다.

v3 canonical 계약의 로컬 1-case smoke는 최초와 `--resume` 모두 completed 1, failed/skip 0, 완료 생성 p50/p95
6,850.3119ms로 같았고 resume count만 0에서 1로 증가했다. checkpoint fingerprint와 evaluator의
`generated_completed_case_count=1`, p50/p95도 동일하게 보존됐다.

### canonical eligibility·manifest v2 적용 실제 live v4 결과

v4 전체 실행은 새 checkpoint에서 실제 core와 온프레미스 LLM을 다시 호출했다. 원격 SMB 호출·저장은 0회였고, 최초 실행과
전체 `--resume`의 checkpoint fingerprint, 완료·실패·skip 수, 완료 생성 p50/p95와 최종 점수가 모두 같았다.

| 소스 | 전체 / 평가 / canonical skip | 완료 XLSX / 생성 오류 | 평균 점수 / pass | 완료 생성 p50 / p95 |
|---|---:|---:|---:|---:|
| 로컬 | 18 / 14 / 4 | 9 / 5 | 47.7710 / 0 | 약 6.63초 / 13.32초 |
| SMB 2026 | 41 / 16 / 25 | 11 / 5 | 47.9246 / 0 | 약 6.61초 / 12.46초 |

두 corpus 모두 canonical ready와 live reference 판정이 일치해 `unexpected_live_reference_failure_case_count=0`이었다. 실제
context 제한 오류도 0건이었지만 raw runtime 예산 초과는 각각 6건이었다. 근거 filter는 완료 결과 20건 중 8건이 hard gate를
실패했고, 기대 제외 artifact 47개를 정확히 제외한 건수는 0개였다. 생성 오류 10건은 strict V2 `ValidationError`로 분류됐다.
따라서 현재 우선순위는 dataset의 사람 검토·test split 확정과 함께 evidence filter 판정 및 구조화 응답 안정화를 고치는 것이다.
모든 case가 아직 `candidate/needs_review`, `split=unassigned`이고 사실 label이 0건이므로 이 점수도 결함 진단값이며 승인된 제품
품질 baseline은 아니다.

checkpoint manifest v2는 dataset·scope·case 생성 입력·template뿐 아니라 provider/model/endpoint, core 구현, context/token/timeout,
packing과 schema 계약을 원문 없는 `generation_config_fingerprint`로 결속한다. 성공 결과는 외부 raw pending journal과 workbook
hash를 먼저 fsync한 뒤 checkpoint append와 create-new XLSX publish를 수행한다. append/publish가 중단되면 resume이 journal을
사용해 LLM 재호출 없이 복구하며, 복구 전에는 비식별 최종 보고서를 만들지 않는다. run lock은 같은 host에서 소유 PID가
종료된 사실이 명확할 때만 회수한다.

v2 evidence filter는 로컬 완료 9건을 평가해 실패 3건, 기대 제외 12개 중 정확 제외 0개, 정상 reference 오제외 12개,
제외 citation/context 누출 3건이었다. SMB는 완료 결과 중 11건을 평가해 실패 5건, 기대 제외 35개 중 정확 제외 0개,
정상 reference 오제외 12개, 누출 5건이었다. 이 값은 현재 후보 label과 core metadata 기반 filter 사이의 불일치를 보여주며,
oracle의 recall 1.0을 실제 제품 품질로 해석하면 안 된다.

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
