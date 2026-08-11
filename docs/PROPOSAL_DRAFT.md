# 기안 초안 생성 계약

## 목적과 범위

기안 생성은 사용자가 선택한 문서의 검색 근거를 온프레미스 LLM에 전달하고, 구조화된 기안 문서와 기존 Excel 초안을
함께 만드는 기능이다. 기존 화면과 API가 사용하는 `title`, `fields`, 파일 저장·다운로드 필드는 유지한다. 신규 품질 평가와
고도화는 손실이 적은 `proposal-document-v2`를 기준으로 한다.

공유폴더에는 완성한 XLSX를 새 최종 파일명으로 한 번만 추가한다. 기존 파일의 덮어쓰기, 이름 변경, 이동, 삭제는 하지 않는다.

## 처리 흐름

1. 선택한 파일의 활성 Revision과 업로드 참조 유효성을 서버가 다시 확인한다.
2. 선택 범위에서 기안 지시와 관련된 근거 chunk를 검색한다.
3. 컨텍스트 패커가 중복과 문서 편중을 제거하고 정해진 예산 안에서 LLM 입력을 만든다.
4. 온프레미스 LLM이 strict `proposal-document-v2` JSON을 출력한다.
5. 서버가 Pydantic으로 추가 필드, 타입, 표 열 수, 인용 ID를 검증한다.
6. V2 문서를 기존 `title`, `approval_request`, `body`로 결정적으로 투영한다.
7. 기존 XLSX 패키지에서 `기안지` worksheet만 수정한 뒤 SMB에 신규 추가하고 다운로드 ID를 발급한다.

근거가 없으면 LLM을 호출하지 않는다. 구조 검증 실패를 임의의 문장으로 보정하지 않는다.

## 요청과 하위 호환 응답

`POST /api/playground/drafts/proposal`

```json
{
  "instruction": "행사 목적, 예산과 일정을 포함해 작성해 줘.",
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

응답은 기존 필드를 제거하거나 의미를 변경하지 않고 `document`, `context_usage`를 추가한다.

| 필드 | 의미 |
|---|---|
| `title` | 기존 카드 표시용 제목. `fields.title`과 같다. |
| `fields` | 기존 `title`, `approval_request`, `body` 계약 |
| `document` | 품질 평가와 향후 렌더링의 원본인 strict V2 문서 |
| `context_usage` | 원문·경로가 없는 입력 크기와 축소 여부 |
| `file_name`, `download_url` | 신규 생성된 XLSX 이름과 현재 프로세스의 다운로드 URL |
| `destination_label`, `saved_to_smb` | 내부 경로를 숨긴 저장 결과 |
| `model_used`, `elapsed_ms`, `timings_ms` | 모델 식별자와 단계별 지연 |

기존 3필드 JSON을 반환하는 전환기 로컬 모델도 계속 받을 수 있다. 서버는 이를 `details` 단일 섹션 V2 문서로 승격한다.
신규 프롬프트와 평가는 V2를 기준으로 한다.

## `proposal-document-v2` 정확한 스키마

최상위와 모든 하위 모델은 `extra="forbid"`다. 명시하지 않은 키가 하나라도 있으면 실패한다.

```json
{
  "schema_version": "proposal-document-v2",
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

따라서 한 사용자 요청이 무제한 LLM 호출로 이어지지 않으며 최대 호출 횟수는 2회다.

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
| `body` | `A15:A47` | 줄마다 한 행의 inline literal string |

`I13:S13`의 `*** 아 래 ***` 구분선, 기존 병합·스타일, worksheet 이외 ZIP 항목은 보존한다. 생성 전후 ZIP 항목을
비교하는 테스트에서 변경 허용 항목은 `xl/worksheets/sheet1.xml` 하나뿐이다. `=`, `+`, `-`, `@`로 시작하는 값도
수식이 아닌 literal string으로 기록한다.

## 오류 계약

| code | status | 조건 |
|---|---:|---|
| `proposal_evidence_unavailable` | 422 | 패킹 가능한 검색 근거 없음 |
| `proposal_context_limit_exceeded` | 422 | 절반 예산 재시도까지 컨텍스트 제한 |
| `proposal_llm_response_invalid` | 502 | strict V2/legacy 검증 실패 또는 알 수 없는 인용 |
| `proposal_llm_unavailable` | 503 | 로컬 LLM 연결·timeout 실패 |
| `proposal_body_too_long` | 422 | 외부 legacy 호출자가 직접 33행 초과 필드를 삽입 |
| `proposal_workbook_generation_failed` | 503 | XLSX 생성 실패 |
| `file_already_exists` | 409 | 동일한 최종 SMB 대상이 이미 존재 |

## 검증 명령

```powershell
.\.venv\Scripts\ruff.exe check backend/src/smb_finder/playground/proposal_context.py backend/src/smb_finder/playground/proposal_draft.py backend/tests/test_proposal_draft.py backend/tests/test_upload_api.py
.\.venv\Scripts\python.exe -m pytest backend/tests/test_proposal_draft.py backend/tests/test_upload_api.py -q -p no:cacheprovider
```

테스트 데이터는 실제 의료·업무 내용을 닮지 않은 합성 문구만 사용한다. 실제 SMB 통합 쓰기 테스트는 별도 승인 없이는
실행하지 않는다.
