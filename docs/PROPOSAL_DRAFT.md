# 기안 초안 생성 계약

기준일: 2026-08-19

## 사용자에게 보이는 동작

사용자가 참고 문서를 선택하고 기안 목적을 입력하면 온프레미스 LLM이 근거가 있는 내용만 구조화한다. 필수 정보가
충분하면 XLSX를 생성하고, 부족하면 파일을 만들지 않고 최대 3개의 확인 질문을 반환한다. 완성본은 지정된 SMB 하위
경로에 새 파일로만 저장하고 대화 카드에서 다운로드한다.

```text
선택 문서 + 사용자 설명
  → 활성 Revision 재검증
  → 기안 관련 근거 선별·문맥 예산 적용
  → ProposalDocumentV2 생성
  → 품질 편집
  → 주장별 근거 검증
  ├─ 필수 정보 부족: needs_clarification
  └─ 완료: XLSX render → SMB create-only → download card
```

## 입력 계약

- 참고 문서는 검색 또는 첨부 결과의 `(doc_id, revision_id)`를 사용한다.
- 최대 선택 수와 설명 길이는 Pydantic 계약으로 제한한다.
- `proposal_type`은 자동 또는 지원 유형을 선택한다.
- 수정 요청은 원본 `draft_id`, 원본 참고 문서와 사용자 feedback을 사용한다.
- 확인 답변은 서버가 발급한 `question_id` 전체에 대응해야 한다.

## 생성과 검증 원칙

1. `proposal_evidence.py`가 기안 목적과 관련 있는 문서·Chunk만 남긴다.
2. `proposal_context.py`가 전체·문서별 문자 예산과 Citation 수를 제한한다.
3. JSON LLM Gateway가 strict 구조의 `ProposalDocumentV2`를 생성한다.
4. 품질 편집은 기존 사실 주장을 바꾸거나 새 사실을 추가할 수 없다.
5. 검증기는 문단·목록 항목·표 행을 독립 주장으로 확인한다.
6. 선택 정보의 근거가 없으면 삭제하고 필수 정보가 없거나 충돌하면 질문으로 전환한다.
7. 검증이 완료된 문서만 OOXML workbook으로 직렬화한다.

LLM 응답이 JSON Schema 또는 Pydantic 계약을 벗어나면 임의 보정해 저장하지 않고 안전한 오류를 반환한다.

## 완료 상태

| 상태 | 의미 | XLSX |
|---|---|---|
| `completed` | 필수 정보와 근거 검증 완료 | 생성 |
| `needs_clarification` | 필수 정보 누락·충돌 | 생성하지 않음 |

질문 답변 후에도 필수 정보가 충족되지 않으면 다시 질문 상태를 유지할 수 있다. 원본 문서 Revision이 바뀌었으면 409로
중단하고 새 초안을 만들도록 안내한다.

## XLSX와 SMB 경계

- 템플릿: `backend/src/smb_finder/playground/templates/proposal_draft.xlsx`
- 일반 본문은 독립 셀과 기본 행 높이를 유지한다.
- 표 구조에 필요한 셀만 병합하고 workbook은 Normal View로 연다.
- ZIP·OOXML namespace·수식·관계 파일의 무결성을 검사한다.
- SMB 최종 파일은 `xb` 방식으로 한 번만 생성한다.
- 동명이면 409이며 기존 파일의 수정·덮어쓰기·이동·삭제는 수행하지 않는다.
- 되돌릴 수 없는 파일 생성이 성공한 뒤에는 deadline 초과를 실패로 뒤집지 않는다.

## 공개 API

| Method | Endpoint | 목적 |
|---|---|---|
| POST | `/api/playground/drafts/proposal` | 새 초안 생성 또는 질문 반환 |
| POST | `/api/playground/drafts/proposal/{draft_id}/clarifications` | 필수정보 답변 후 완성 |
| POST | `/api/playground/drafts/proposal/{draft_id}/revisions` | 원본 snapshot 기반 수정본 생성 |
| GET | `/api/playground/drafts/proposal/{draft_id}` | 완성 XLSX 다운로드 |

빈 템플릿 다운로드 API와 구 XLSX 필드 삽입 호환 경로는 제거됐다.

## 지연과 오류

- 요청 전체에 하나의 monotonic deadline을 적용한다.
- 근거 검색, 생성, 편집, 검증, render, SMB 쓰기 시간을 `timings_ms`로 반환한다.
- 로컬 LLM unavailable·timeout, 근거 부족, context 초과, stale Revision을 서로 다른 오류 코드로 구분한다.
- 내부 endpoint, 원문 경로, 자격증명과 예외 문자열을 응답에 포함하지 않는다.

## 구현 위치

- `backend/src/smb_finder/playground/proposal_draft.py` — 계약·생성·검증·render·registry
- `backend/src/smb_finder/playground/proposal_context.py` — 근거 문맥 예산
- `backend/src/smb_finder/playground/proposal_evidence.py` — 관련 근거 선별
- `backend/src/smb_finder/playground/upload_api.py` — HTTP router
- `backend/src/smb_finder/playground/upload_service.py` — SMB create-only writer
- `frontend/src/App.vue` — 생성·질문·수정 UI orchestration

## 검증

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv run --no-sync pytest backend/tests/test_proposal_draft.py backend/tests/test_upload_api.py -q
npm.cmd --prefix frontend run test
```

XLSX 변경은 ZIP 검사만으로 완료하지 않고 실제 Excel 개방 또는 렌더 결과까지 확인한다.
