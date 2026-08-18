# 기안 초안 생성 계약

## 목적과 범위

기안 생성은 사용자가 선택한 문서의 검색 근거를 온프레미스 LLM에 전달하고, 구조화된 기안 문서와 기존 Excel 초안을
함께 만드는 기능이다. 기존 화면과 API가 사용하는 `title`, `fields`, 파일 저장·다운로드 필드는 유지한다. 신규 품질 평가와
고도화는 손실이 적은 `proposal-document-v2`를 기준으로 한다.

공유폴더에는 완성한 XLSX를 새 최종 파일명으로 한 번만 추가한다. 기존 파일의 덮어쓰기, 이름 변경, 이동, 삭제는 하지 않는다.

## 처리 흐름

1. 선택한 파일의 활성 Revision과 업로드 참조 유효성을 서버가 다시 확인한다.
2. 선택 범위에서 기안 지시와 관련된 근거 chunk를 검색한다.
3. 명시 선택 또는 보수적 규칙으로 구매·박람회/행사 참석·일반 유형을 확정한다.
4. 유형별 문서 선별기가 영수증·결제 내역·참석 확인 같은 사후 증빙을 문서 단위로 제외한다.
5. 컨텍스트 패커가 남은 근거의 중복과 문서 편중을 제거하고 정해진 예산 안에서 LLM 입력을 만든다.
6. 온프레미스 LLM이 strict `proposal-document-v2` JSON을 출력한다. JSON 구조가 잘못되면 오류와 원래 지시를 함께 주어 한 번만 복구 생성한다.
7. 같은 온프레미스 LLM이 유형별 완결성 checklist로 품질 편집을 한 번 더 수행한다. 첫 생성의 사실 주장과 인용은 유지하고, 근거에서 확인되는 요청·예산 구역만 보완한다.
8. 서버가 Pydantic으로 추가 필드, 타입, 표 열 수, 인용 ID와 유형별 section 계약을 검증·정규화한다.
9. 별도 온프레미스 검증 호출이 문단·목록 항목·표 행을 `supported`, `derived`, `unsupported`, `conflicting`으로 판정한다.
10. 검증된 주장에 실제로 사용된 citation만 section 인용으로 다시 구성한다. 근거 없는 선택 내용은 삭제하고, 결재에 필수인 누락·충돌만 서로 겹치지 않는 최대 3개 확인 질문으로 바꾼다.
11. 유형별 결정론적 안전망이 표준 결재 문구, 행사 일시·장소·참석자·참가비, 명시된 기대효과를 원문 근거로 보완한다. 구매 배경은 필요성·문제 같은 배경 근거가 명시된 경우에만 유지한다.
12. 질문이 있으면 XLSX 생성과 SMB 저장을 보류한다. 사용자가 한 번에 답하면 답변을 사용자 근거로 재작성·재검증한다.
13. 확인이 끝난 V2만 기존 세 필드로 투영하고 XLSX에 렌더링한 뒤 SMB에 신규 추가하고 다운로드 ID를 발급한다.

근거가 없으면 LLM을 호출하지 않는다. 구조 검증 실패를 임의의 문장으로 보정하지 않는다.

## 요청과 하위 호환 응답

`POST /api/playground/drafts/proposal`

```json
{
  "instruction": "행사 목적, 예산과 일정을 포함해 작성해 줘.",
  "proposal_type": "event_attendance",
  "selected_files": [
    {
      "source": "llmops",
      "doc_id": "UUID",
      "revision_id": "UUID",
      "file_name": "synthetic-reference.xlsx",
      "title": "합성 참고자료"
    }
  ]
}
```

`proposal_type`은 `auto`, `purchase`, `event_attendance`, `general` 중 하나이며 생략하면 `auto`다. 응답은 기존 필드를
제거하거나 의미를 변경하지 않고 유형과 구조화 문서 정보를 추가한다.

| 필드 | 의미 |
|---|---|
| `title` | 기존 카드 표시용 제목. `fields.title`과 같다. |
| `fields` | 기존 `title`, `approval_request`, `body` 계약 |
| `document` | 품질 평가와 향후 렌더링의 원본인 strict V2 문서 |
| `proposal_type` | 최종 확정한 `purchase`, `event_attendance`, `general` 중 하나 |
| `proposal_type_source` | `user`, `rule`, `fallback` 중 하나인 판정 출처 |
| `evidence_filter` | 원문·파일명·UUID 없이 입력/포함/제외 문서·citation 수와 제외 사유 수를 담은 집계 |
| `context_usage` | 원문·경로가 없는 입력 크기와 축소 여부 |
| `completion` | 근거 판정 집계, `completed`/`needs_clarification`/`completed_with_omissions`, 최대 3개 질문 |
| `file_name`, `download_url` | 신규 생성된 XLSX 이름과 현재 프로세스의 다운로드 URL. 확인 대기이면 `null` |
| `destination_label`, `saved_to_smb` | 내부 경로를 숨긴 저장 결과 |
| `model_used`, `elapsed_ms`, `timings_ms` | 모델 식별자와 단계별 지연 |

기존 3필드 JSON을 반환하는 전환기 로컬 모델도 계속 받을 수 있다. 서버는 이를 `details` 단일 섹션 V2 문서로 승격한다.
신규 프롬프트와 평가는 V2를 기준으로 한다.

## 근거 판정과 필수 확인 질문

근거 판정은 완성 기안의 문장을 정답 문서와 똑같이 만드는 검사가 아니다. 문단 하나, 목록 항목 하나, 표 행 하나를 주장 단위로
펼친 뒤 참고 문서와 사용자 입력이 그 의미를 뒷받침하는지 확인한다. 합계처럼 입력 값에서 직접 계산할 수 있는 값만 `derived`로
유지한다. 한 block에는 가능한 한 사실 하나만 쓰도록 생성 prompt도 제한한다.

- `supported`, `derived`: 본문에 유지한다.
- `unsupported`이며 선택 정보: 질문하지 않고 본문에서 제외한다.
- `unsupported`이며 결재 필수 정보: `missing` 질문으로 바꾼다.
- 참고 근거끼리 충돌하고 결재에 필수: `conflict` 질문으로 바꾼다.
- 질문 후보는 우선순위와 `field_key`로 중복 제거한 뒤 최대 3개만 공개한다.
- 로컬 모델이 필수 금액 누락을 놓치는 경우를 막기 위해, 구매 기안 또는 사용자가 비용 승인을 명시한 행사 기안은 선별된
  근거·사용자 답변에 실제 금액이 있는지 서버가 다시 확인한다. 값이 없으면 각각 `total_amount`, `participation_fee` 질문을
  우선 추가하고, 근거에 없는 금액을 `supported`로 잘못 판정한 경우에도 본문에서 제거한다. `무료`·`무상`은 유효한 값이다.
- 숫자 계산이 아닌 서술형 `derived` 판정은 정규화한 문장이 원천 근거에 실제로 있을 때만 유지한다. 검증 뒤 section citation은
  남은 주장이 참조한 근거로 교체하므로 삭제된 주장만 뒷받침하던 인용은 XLSX에 표시되지 않는다.

질문이 하나라도 있으면 생성 응답은 `saved_to_smb=false`이고 파일 관련 세 필드는 `null`이다. provisional `document`는 화면
상태를 위한 것이며 다운로드할 수 없다. 따라서 답변 전 잘못된 초안이 공유폴더에 남지 않는다.

`POST /api/playground/drafts/proposal/{draft_id}/clarifications`

```json
{
  "answers": [{"question_id": "Q001", "answer": "승인 금액은 1,200,000원입니다."}],
  "selected_files": [{"source": "llmops", "doc_id": "UUID", "revision_id": "UUID", "file_name": "synthetic.xlsx", "title": "합성 근거"}]
}
```

표시된 질문 ID를 빠짐없이 한 번씩 답해야 하며 선택 문서 snapshot도 최초 요청과 정확히 같아야 한다. 서버는 질문 문구와 답변을
사용자 근거로 넣어 초안을 다시 작성하고, 두 번째 검증에서는 추가 질문을 만들지 않고 남은 비근거 선택 내용을 제거한다. 성공
응답에는 `clarification_of_draft_id`, `answered_question_count`가 추가되고 이때 처음으로 XLSX와 다운로드 URL이 생긴다.

질문 `field_key`는 평가 데이터와 맞추기 위해 유형별 공통 이름을 우선한다. 구매는 `purpose`, `purchase_item`, `quantity`,
`unit_price`, `total_amount`, `requested_action`, 행사 참석은 `participation_purpose`, `participation_content`,
`event_main_content`, `participation_fee`, 일반은 `purpose`, `requested_action`을 사용한다.

## `proposal-document-v2` 정확한 스키마

최상위와 모든 하위 모델은 `extra="forbid"`다. 명시하지 않은 키가 하나라도 있으면 실패한다.

```json
{
  "schema_version": "proposal-document-v2",
  "proposal_type": "purchase",
  "title": "합성 장비 구매 계획",
  "approval_request": "관련 내용을 검토 후 재가하여 주시기 바랍니다.",
  "sections": [
    {
      "heading": "구매 목적",
      "semantic_role": "purpose",
      "citations": ["E001"],
      "blocks": [
        {"type": "paragraph", "text": "합성 자동화 범위를 개선합니다."},
        {"type": "list", "ordered": true, "items": ["요건 확인", "도입 검토"]},
        {
          "type": "table",
          "headers": ["구분", "금액"],
          "rows": [["합성 품목", "10,000원"]]
        }
      ],
      "missing_information": []
    }
  ],
  "missing_information": ["최종 집행일 확인"]
}
```

`semantic_role`은 다음 값만 허용한다.

- `purpose`, `background`, `request`, `details`, `budget`, `schedule`
- `expected_effect`, `attachments`, `notes`, `other`

block은 `paragraph`, `list`, `table` 중 하나다. 표의 모든 데이터 행은 `headers`와 열 수가 같아야 한다. 각 section은
최소 한 개의 block과 최소 한 개의 `E001` 형식 인용을 가져야 한다. LLM이 패킹된 컨텍스트에 없는 ID를 인용하면
`proposal_llm_response_invalid`로 실패한다. 근거로 확정할 수 없는 값은 만들지 않고 section 또는 문서의
`missing_information`에 기록한다.

`document.proposal_type`은 이전 V2 JSON을 계속 읽기 위해 입력 계약에서는 선택값이다. 실제 생성 응답에서는 서버가 확정한
유형을 항상 주입하며, top-level `proposal_type`과 같다.

## 기안 유형 판정과 작성 profile

사용자가 `purchase`, `event_attendance`, `general`을 선택하면 그대로 적용하고 출처를 `user`로 남긴다. `auto`이면 먼저
사용자 지시에서 다음 표식을 보수적으로 확인한다. 지시에 표식이 없을 때만 강한 사후자료 제목을 제거한 나머지 근거 제목을
보조 판정에 사용한다. 따라서 영수증 제목에 있는 `구매`가 명시적인 행사 참석 지시를 `general`로 바꾸지 않는다.

- 행사 명칭 표식과 참석·참가·방문·등록 표식이 함께 있으면 행사 참석 후보
- 구매·구입·도입·교체·견적·발주·비품·장비 표식이 있으면 구매 후보
- 두 후보가 동시에 검출되거나 모두 없으면 임의 확정하지 않고 `general`, `fallback`

유형 판정 때문에 LLM을 한 번 더 호출하지 않는다. 유형별 권장 구조는 다음과 같지만 근거가 없는 빈 section을 강제로 만들지
않고 `missing_information`에 남긴다.

| 유형 | 우선 구조 |
|---|---|
| `purchase` | 목적 → 배경·필요성 → 구매 요청 → 품목 표 → 예산 → 납기·유지보수 → 기대효과 → 첨부 |
| `event_attendance` | 참가 목적 → 참가 내용 → 행사 주요 내용 |
| `general` | 목적 → 배경 → 요청·세부내용 → 일정 → 기대효과 → 첨부 |

행사 참석 유형의 최종 section heading은 `참가 목적`, `참가 내용`, `행사 주요 내용`만 허용하며 이 순서로 정규화한다.
행사 명칭·비용·일시·장소·참석자는 별도 개요나 비용 section을 만들지 않고 `참가 내용` block에 둔다. 시간대별 전체 일정과
별도 `행사 개요`, `전체 일정` section은 최종 V2와 XLSX에서 제거한다. 발표·전시·세션의 핵심 주제만 `행사 주요 내용`으로
남긴다. 근거가 없어 비어 있는 범주를 만들기 위해 내용을 추측하지 않으므로 세 heading 중 일부는 생략될 수 있다.

## 첨부 문서 선별 계약

`filter_proposal_evidence`는 같은 `(doc_id, revision_id)`의 citation을 하나의 문서로 묶고, 문서 전체를 포함하거나 제외한다.
따라서 제외한 영수증의 다른 chunk가 LLM context로 섞이지 않는다.
자동 제외는 문서 제목과 section metadata의 강한 표식으로만 판정한다. 행사 안내 excerpt에 `영수증 발급`이 한 번 등장했다는
이유만으로 안내 문서 전체를 제외하지 않는다.

- 행사 참석: 영수증·카드/승인 전표 같은 `receipt`, 결제 내역·결제 완료 같은 `payment_confirmation`, 참석/수료 확인증·출입증
  같은 `attendance_confirmation`, 행사 후 정산·결과 보고인 `post_event_report`를 기본 제외한다. 등록·참가신청 확인은 사전
  참고자료로 유지한다.
- 구매: `receipt`, `payment_confirmation`과 납품·검수 완료인 `purchase_completion`을 기본 제외한다.
- 일반: 문서 역할을 임의 확정하지 않고 기존 선택 근거를 유지한다.
- 사용자가 지시나 수정 피드백에서 `결제 영수증`, `참석 확인증`처럼 제외 대상 종류를 직접 지정하면 그 종류만 다시 허용한다.
- 모두 제외되면 임의 문서를 되살리거나 LLM을 호출하지 않고 `422 proposal_relevant_evidence_unavailable`을 반환한다.

생성·수정 응답의 공개 집계는 다음과 같다. 이 값에는 제목, 파일명, 경로, UUID와 본문이 없다.

```json
{
  "schema_version": "proposal-evidence-filter-v1",
  "input_document_count": 4,
  "included_document_count": 2,
  "excluded_document_count": 2,
  "input_citation_count": 9,
  "included_citation_count": 5,
  "excluded_citation_count": 4,
  "excluded_reason_counts": {"attendance_confirmation": 1, "receipt": 1},
  "fallback_used": false
}
```

## 피드백 기반 수정 API

`POST /api/playground/drafts/proposal/{draft_id}/revisions`

```json
{
  "feedback": "참가 목적을 더 간결하게 하고 전체 일정은 제외해 줘.",
  "selected_files": [
    {
      "source": "llmops",
      "doc_id": "UUID",
      "revision_id": "UUID",
      "file_name": "synthetic-reference.xlsx",
      "title": "합성 참고자료"
    }
  ]
}
```

`selected_files`는 원본 생성 요청의 snapshot과 순서·값이 같아야 한다. 다르면 문서 범위를 조용히 바꾸지 않고
`409 proposal_revision_scope_mismatch`를 반환한다. 서버는 snapshot의 활성 Revision/업로드 참조를 다시 검증하고, 원래 설명과
피드백을 검색어로 근거를 다시 가져온 뒤 같은 문서 선별과 context 제한을 적용한다. 기안 유형과 판정 출처는 원본에서 상속한다.

응답은 생성 응답 전체를 유지하면서 다음 필드를 추가한다.

| 필드 | 의미 |
|---|---|
| `revision_of_draft_id` | 직접 수정한 부모 초안 ID |
| `revision_number` | 같은 기안 계보의 `1.1`, `1.2` minor 버전 |
| `revision_summary` | LLM 자유문구가 아닌 V2 전후 diff로 만든 결정적 변경 요약 |

수정본은 원본 파일명·날짜 계보를 유지한 `_v1.1.xlsx`, `_v1.2.xlsx` 이름으로 SMB에 exclusive 신규 생성한다. 원본 registry
record와 XLSX bytes는 변경하지 않으며 기존 SMB 파일도 덮어쓰거나 이름 변경·삭제하지 않는다. 수정 응답의 `draft_id`와
`download_url`은 새 수정본을 가리킨다. 재수정 시 가장 최근 `draft_id`를 사용한다.

## LLM 초안 출력 방식

system prompt는 다음 원칙을 강제한다.

- 사용자 지시와 제공된 근거만 사용한다.
- 최상위 키와 section/block 키를 정확히 제한한다.
- 설명은 paragraph, 열거는 list, 행·열 관계는 table로 출력한다.
- 문장 또는 표가 사용한 근거 ID를 section의 `citations`에 남긴다.
- 확정할 수 없는 날짜, 금액, 대상은 추측하지 않고 `missing_information`에 기록한다.
- section과 최상위 `missing_information`은 항상 문자열 배열이며 확인할 내용이 없으면 `[]`로 반환한다.
- 재가 문구는 검토와 재가를 요청하는 1~2문장으로 작성한다.

Ollama에는 `format=json`, `temperature=0`, `think=false`를 사용한다. 모델별 JSON schema 객체 호환성 차이가 있어
provider에는 JSON 모드만 요청하고 최종 strict 검증은 서버 Pydantic이 수행한다.

수정 호출도 같은 JSON·token·timeout 계약을 사용한다. user message에는 현재 strict V2 JSON, 피드백, 다시 선별·패킹한 근거를
넣고, system prompt는 요청한 부분만 바꾸고 근거로 다시 확인할 수 없는 사실을 제거하거나 `missing_information`으로 옮기도록
지시한다. 행사 참석 수정 결과에도 동일한 세 section 정규화를 다시 적용한다.

## 컨텍스트 패킹 방식

검색 결과를 그대로 이어 붙이지 않는다. `pack_proposal_context`가 다음 순서로 제한한다.

1. 공백과 대소문자를 정규화한 excerpt가 같으면 한 건만 남긴다.
2. 검색의 RRF 점수와 사용자 지시의 단어 일치 수를 합산해 관련도를 계산한다.
3. 관련도가 같으면 문서·Revision·chunk UUID 순서로 정렬해 입력 순서와 무관한 결과를 만든다.
4. 전체 문자 예산, 문서별 문자 예산, 최대 citation 수를 동시에 적용한다.
5. excerpt를 줄여야 하면 표 머리글, 수치·금액·날짜가 있는 행, 질의 단어가 있는 행, 일반 행 순으로 선택한다.
6. 선택한 행은 원래 문서 순서로 다시 조립한다.

기본값은 다음과 같다.

| 설정 키 | 기본값 | 의미 |
|---|---:|---|
| `PROPOSAL_LLM_TIMEOUT_MS` | 120000 | 기안 전용 LLM HTTP timeout |
| `PROPOSAL_LLM_NUM_CTX` | 24576 | 현재 머신에서 합성 smoke가 성공한 최대 검증 창; 출력·prompt 여유 포함 |
| `PROPOSAL_LLM_MAX_TOKENS` | 4096 | 최대 출력 token |
| `PROPOSAL_CONTEXT_MAX_CHARS` | 30000 | 첫 시도 근거 문자 예산 |
| `PROPOSAL_CONTEXT_PER_DOCUMENT_CHARS` | 10000 | 한 문서가 차지할 수 있는 최대 근거 문자 |
| `PROPOSAL_CONTEXT_MAX_CITATIONS` | 30 | 최대 근거 chunk 수 |

한국어와 표는 tokenizer별 차이가 크므로 문자 수를 token 수로 단정하지 않는다. 합성 실측에서 2자당 1 token 추정이
실제보다 낮아, 서버의 `estimated_input_tokens`는 여유를 둔 **1.6자당 1 token**을 비교값으로 사용한다. 실제 Ollama
응답의 `prompt_eval_count`를 함께 기록한다.

2026-08-11 합성 allocation smoke에서 같은 모델의 `num_ctx` 8,192·16,384·24,576은 HTTP 200이었고 28,672는
입력이 거의 없어도 HTTP 500이었다. 모델 metadata의 262,144는 이 머신에서 실제 할당 가능한 창을 뜻하지 않는다.
따라서 검증된 24,576을 기본값으로 두고, prompt 1,024와 출력 4,096을 제외한 reference 예산 19,456 token보다
여유 있게 낮도록 첫 근거 문자 예산을 30,000자로 제한한다.

### 컨텍스트 제한 오류 재시도

HTTP 오류 또는 Ollama 오류 본문에 `context length`, `context window`, `maximum context`, `prompt too long`,
`too many tokens`, `token limit`, `num_ctx`, `request too large` 표식이 있을 때만 재시도한다.
HTTP 413과 `request entity too large`, `payload too large`도 같은 제한 오류로 취급한다.

- 재시도는 정확히 한 번만 한다.
- 전체 컨텍스트 예산과 문서별 예산을 각각 절반으로 줄인다.
- 두 번째도 같은 오류면 `422 proposal_context_limit_exceeded`를 반환한다.
- 연결 실패, timeout, 잘못된 JSON은 컨텍스트 문제로 추측해 재시도하지 않는다.

따라서 한 사용자 요청이 무제한 LLM 호출로 이어지지 않는다. 본문 생성은 컨텍스트 오류일 때 최대 2회, 성공한 본문의 근거
검증은 1회이므로 최초 요청당 최대 3회다. 확인 답변을 보내면 재작성과 재검증이 각각 별도 1회 수행된다.

## `context_usage` 기록 계약

```json
{
  "schema_version": "proposal-context-usage-v1",
  "source_citation_count": 12,
  "packed_citation_count": 8,
  "deduplicated_citation_count": 2,
  "source_document_count": 4,
  "packed_document_count": 4,
  "context_budget_chars": 30000,
  "context_chars": 23140,
  "estimated_input_tokens": 12600,
  "truncated": true,
  "retry_count": 0,
  "first_attempt_context_chars": null,
  "prompt_eval_count": 11842
}
```

이 메타데이터와 로그에는 excerpt, 제목, 파일명, 상대·절대 경로, UUID 목록을 넣지 않는다. 문서별 실제 컨텍스트 한계
통계는 평가 실행 결과에서 `context_chars`, `estimated_input_tokens`, `prompt_eval_count`, `retry_count`, 최종 오류 코드를
case 단위로 모아 산출한다. `prompt_eval_count`가 없는 provider 응답은 `null`로 둔다.

## V2에서 기존 `body`를 만드는 방식

기존 화면과 XLSX는 33행 문자열 본문을 요구하므로 V2를 다음과 같이 결정적으로 투영한다.

- section heading: `1. 제목`, `2. 제목`
- paragraph: 내부 CR/LF와 기타 줄 경계를 공백으로 합쳐 한 행
- ordered list: `1) 항목`; unordered list: `- 항목`. 항목 내부 줄 경계도 공백으로 변환
- table: 머리글과 각 행을 ` | `로 연결하고 각 셀 내부 줄 경계를 공백으로 변환
- missing information: `[확인 필요] 내용`. 내부 줄 경계는 공백으로 변환

33행을 넘으면 section heading, 표 머리글, 수치가 있는 행, 일반 행 순으로 보존하되 최종 출력은 원래 순서로 정렬한다.
마지막 행에 `※ 구조화 초안의 일부 세부 행은 엑셀 표시 범위로 인해 생략되었습니다.`를 남긴다. 완전한 내용은 응답의
`document`에 유지된다. 단일 투영 행이 1,000자를 넘거나 전체 본문이 6,000자를 넘는 경우에도 같은 고지를 남기고,
우선순위가 낮은 긴 행부터 제외한다. 최종 `fields.body`는 내부 줄 경계 정규화 후 항상 33행·6,000자 이내이며 같은 V2
입력은 항상 같은 결과를 만든다.

## Excel 매핑과 보존 범위

| 기존 필드 | Excel 위치 | 방식 |
|---|---|---|
| `title` | `기안지!C8` | 한 줄 inline literal string |
| `approval_request` | 병합 영역 `A10:Z11`의 `A10` | inline literal string |
| V2 section·paragraph·list | 본문 `A15` 이후 | A열의 독립 셀, 병합·자동 줄바꿈·명시 행 높이 없음 |
| 2~6열 V2 table | 본문 `A15` 이후 | 폭을 균등 분할한 실제 bordered matrix, 표 논리 셀만 병합 |
| 7열 이상 또는 남은 본문 행을 넘는 table | `세부내용` worksheet | 전체 header/row matrix 보존, 본문에는 시트 참조 표시 |

일반 본문은 사람이 각 행을 바로 고칠 수 있도록 병합하지 않는다. 긴 문장은 한글 2·ASCII 1의 표시폭을 기준으로 단어
경계에서 여러 기본 높이 행으로 나누고 각 행의 A열에만 기록한다. 자동 줄바꿈과 사용자 지정 행 높이는 사용하지 않는다.
V2 본문은 자연스러운 페이지 구분을 위해 최대 120행까지 사용할 수 있으며, legacy `fields.body`의 33행·6,000자 계약은
그대로 유지한다. 표만 열 폭 확보를 위해 논리 셀을 병합하고 자동 줄바꿈·계산 행 높이를 적용한다. 표를 `값1 | 값2`
문자열로 출력하지 않는다. `I13:S13`의 `*** 아 래 ***` 구분선과 기존 workbook ZIP
항목은 보존한다. renderer style을 `xl/styles.xml`에 추가하며 appendix가 필요할 때만 workbook 관계, content type과 새
worksheet를 추가한다. `=`, `+`, `-`, `@`로 시작하는 값도 수식이 아닌 `inlineStr`로 기록한다.

기존 외부 호출자를 위한 `insert_proposal_fields`는 3필드·33행 계약을 계속 제공한다. 실제 생성 서비스와 평가기는
`render_proposal_workbook`을 사용하므로 legacy `fields.body`의 ` | ` 투영 문자열이 새 XLSX 표로 사용되지는 않는다.

## 오류 계약

| code | status | 조건 |
|---|---:|---|
| `proposal_evidence_unavailable` | 422 | 패킹 가능한 검색 근거 없음 |
| `proposal_relevant_evidence_unavailable` | 422 | 선택 근거가 모두 현재 기안 유형의 사후 증빙으로 제외됨 |
| `proposal_context_limit_exceeded` | 422 | 절반 예산 재시도까지 컨텍스트 제한 |
| `proposal_llm_response_invalid` | 502 | strict V2/legacy 검증 실패 또는 알 수 없는 인용 |
| `proposal_llm_unavailable` | 503 | 로컬 LLM 연결·timeout 실패 |
| `proposal_verification_unavailable` | 503 | 별도 근거 검증 LLM 연결·timeout 실패 |
| `proposal_verification_response_invalid` | 502 | 주장별 판정이 누락·중복되거나 허용되지 않은 근거를 사용 |
| `proposal_body_too_long` | 422 | 외부 legacy 호출자가 직접 33행 초과 필드를 삽입 |
| `proposal_workbook_generation_failed` | 503 | XLSX 생성 실패 |
| `file_already_exists` | 409 | 동일한 최종 SMB 대상이 이미 존재 |
| `proposal_revision_scope_mismatch` | 409 | 수정 요청의 선택 문서 snapshot이 원본과 다름 |
| `proposal_clarification_scope_mismatch` | 409 | 확인 답변의 선택 문서 snapshot이 최초 요청과 다름 |
| `proposal_clarification_answers_incomplete` | 422 | 표시된 질문 중 누락되거나 알 수 없는 질문 ID가 있음 |
| `proposal_clarification_required` | 409 | 확인 대기 초안의 다운로드·수정을 먼저 요청 |
| `proposal_draft_not_found` | 404 | 생성/수정본 registry record가 만료됐거나 없음 |

## 검증 명령

```powershell
.\.venv\Scripts\ruff.exe check backend/src/smb_finder/playground/proposal_context.py backend/src/smb_finder/playground/proposal_evidence.py backend/src/smb_finder/playground/proposal_draft.py backend/tests/test_proposal_draft.py backend/tests/test_upload_api.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_proposal_draft.py backend/tests/test_upload_api.py -q -p no:cacheprovider
```

테스트 데이터는 실제 의료·업무 내용을 닮지 않은 합성 문구만 사용한다. 실제 SMB 통합 쓰기 테스트는 별도 승인 없이는
실행하지 않는다.
