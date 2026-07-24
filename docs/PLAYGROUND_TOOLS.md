# Playground tool 기능 계약

`/playground`는 실제 의료 환경과 닮지 않은 합성 데이터로 챗봇과 tool 호출을 검증하는 테스트 화면이다.
의료데이터 입력 탐지, provider별 합성 데이터 허용 토글, provider별 tool 차단은 사용하지 않는다.

## API

| Method | Path | 역할 |
|---|---|---|
| `GET` | `/api/playground/tools` | 현재 등록된 tool 목록 |
| `POST` | `/api/playground/attachments` | 원시 body로 PDF/Markdown 한 건을 로컬 임시 저장 |
| `DELETE` | `/api/playground/attachments/{attachment_id}` | 로컬 임시 첨부 삭제 |
| `GET`, `POST` | `/api/playground/skills` | 설치된 skill 목록 조회, 사용자 skill 생성 |
| `PUT`, `DELETE` | `/api/playground/skills/{skill_id}` | 사용자 skill 수정·삭제 |
| `POST` | `/api/playground/chat` | 선택한 tool 범위에서 agent 채팅 실행 |
| `POST` | `/api/playground/karyotype-summary` | 선택한 provider로 LLM-backed 요약 tool 직접 실행 |
| `POST` | `/api/playground/tool-draft` | 합성 테스트용 tool manifest 초안 생성 |
| `POST` | `/api/playground/llm-status` | provider/model 연결 확인 |

`ToolDefinition`의 주요 필드는 다음과 같다.

| 필드 | 설명 |
|---|---|
| `id` | tool 고유 ID |
| `display_name` | UI 표시 이름 |
| `description` | agent가 읽는 기능 설명 |
| `category` | UI 대분류: `smb`, `database`, `report`, `skill` |
| `permission` | `read`, `write` 또는 `admin` |
| `execution_type` | `code` 또는 `llm` |
| `enabled` | 현재 실행 가능 여부 |
| `default_selected` | UI 최초 선택 여부 |
| `requires_admin` | 관리자 경로 전용 여부 |
| `timeout_ms` | 단일 tool 제한 시간 |
| `input_schema` | LLM이 만들 인자 설명 |
| `origin`, `source_id` | 내장 tool 호환 metadata. 현재 각각 `builtin`, 빈 문자열 |

`provider_availability`와 `external_provider_allowed`는 계약에서 제거됐다. `enabled=true`인 일반 tool은 local/OpenAI
provider에서 같은 방식으로 선택할 수 있다.

## 분류 UI

화면은 처음에 `SMB 직접 접근`, `DB 접근`, `보고서 관련`, `Skill 관리` 대분류 카드만 보여준다. 각 카드를
클릭하면 해당 분류의 tool 체크박스가 펼쳐지고, 다시 클릭하면 접힌다. 최초 진입과 목록 새로고침 후에는 모두
접힌 상태다.

채팅 작성 영역의 `PDF/MD 첨부`는 한 번에 한 파일을 받는다. 파일명은 표시용 메타데이터로만 보존하고 실제 저장
경로에는 임의 UUID를 사용한다. 허용 확장자는 `.pdf`, `.md`, `.markdown`이며 기본 크기 상한은 10MiB다.

## 공통 catalog와 MCP 경계

`find_folder`, `search_content`는 Pydantic 입출력 모델, handler, timeout과 공개 surface를 공통 `ToolCatalog`에서
관리한다. Playground는 같은 `ToolExecutor`를 프로세스 안에서 직접 호출한다. 자기 서비스의 `/mcp`를 다시
호출하거나 외부 tool schema를 동기화하지 않는다.

`/mcp`는 외부 MCP Host용 adapter다. 현재 catalog에서 `mcp` surface가 승인된 읽기·멱등 검색 두 개만 공개하며,
관리자·쓰기·인덱싱·RAG·QC·보고서 tool은 공개하지 않는다. 다른 내장 tool은 기존 Playground registry에 남아 있고
tool별 Pydantic 계약과 회귀 검증을 추가하면서 공통 catalog로 단계적으로 이관한다. 검색 두 개의 Playground 표시용
`ToolDefinition.input_schema`도 현재 호환 registry에 남은 수동 metadata이며, 후속 이관에서 Pydantic schema로
파생한다.

## 등록 tool

| ID | 분류 | 방식 | 기본 선택 | 동작 |
|---|---|---|---:|---|
| `draft_qc_report` | 보고서 관련 | llm | 예 | 합성 측정값 판정·LLM 비수치 서술·Markdown 조립·재감사를 한 번에 실행 |
| `audit_qc_report` | 보고서 관련 | code | 예 | 첨부 추출·합성 SOP 대조·근거 포함 판정을 한 번에 반환 |
| `extract_uploaded_document` | 보고서 관련 | code | 아니오 | 텍스트형 PDF/Markdown의 로컬 본문과 제한된 미리보기 반환 |
| `search_sop_knowledge` | DB 접근 | code | 아니오 | 로컬 합성 SOP의 코드·별칭·판정 기준 검색 |
| `find_folder` | SMB 직접 접근 | code | 예 | 인메모리 폴더 인덱스 검색 |
| `search_content` | SMB 직접 접근 | code | 아니오 | 로컬 FTS5 내용 인덱스 검색 |
| `refresh_content` | SMB 직접 접근 | code | 아니오 | 관리자 전용 인덱싱 안내, Playground에서는 비활성 |
| `search_rag_chunks` | DB 접근 | code | 아니오 | 질의 임베딩과 pgvector cosine 검색으로 근거 chunk 반환 |
| `cytogenetics_karyotype_summary` | 보고서 관련 | llm | 아니오 | 선택한 provider/model로 테스트 입력 요약 |
| `cytogenetics_report` | 보고서 관련 | code | 아니오 | 후보 템플릿과 체크리스트 생성 |
| `ngs_report` | 보고서 관련 | code | 아니오 | 후보 템플릿과 체크리스트 생성 |
| `create_playground_skill` | Skill 관리 | code | 아니오 | 검증된 사용자 `<skill-id>/SKILL.md`를 로컬에 생성 |

`create_playground_skill`은 일반 채팅에서 항상 노출되는 기본 tool이 아니다. 요청의 `selected_skill_ids`에
`skill-creator`가 있으면 agent가 registry의 이 tool을 `selected_tool_ids`에 자동 추가한다. 사용자가 별도 tool
체크를 하지 않아도 생성할 수 있으며, 생성에 성공한 skill은 같은 응답의 다음 agent 판단부터 활성화된다.
`skill-creator`를 선택하지 않은 요청에는 자동 추가되지 않는다.

### DB 접근 tool

`search_rag_chunks`의 입력은 `query`와 선택적인 `limit`이다. 실행 흐름은 다음과 같다.

1. `RAG_EMBEDDING_BASE_URL`의 OpenAI 호환 `/embeddings` endpoint에서 질의를 임베딩한다. 모델은 원격
   온프레미스 Linux에서 `127.0.0.1:18080`에만 바인딩하고, Windows에서는 PuTTY 저장 세션과
   `scripts/start_remote_embedding_tunnel.ps1`로 `http://127.0.0.1:18080/v1`에 연결한다.
2. `document_chunks.embedding <=> query_vector` cosine distance로 상위 chunk를 조회한다.
3. chunk 본문·문서 위치·유사도와 `embedding_ms`, `db_ms`, `elapsed_ms`를 반환한다.
4. agent가 상위 hit별로 균등 배분한 압축 근거를 받아 최종 답변을 합성한다. 기본 입력 상한은
   `RAG_SYNTHESIS_EVIDENCE_CHARS=1800`, 출력 상한은 `RAG_SYNTHESIS_MAX_TOKENS=256`이다.

현재 로컬 DB 계약은 database `rag_db_local_20260713`, 모델 `nomic-embed-text-v2-moe`, 차원 768이다.
원격 서버에 설치할 GGUF 변형은 DB 적재에 사용한 것과 같은지 먼저 확인하며, 서버 CPU/RAM/디스크 점검 없이
임의로 큰 모델을 설치하지 않는다.
임베딩 endpoint가 꺼져 있거나 다른 차원을 반환하면 `embedding_unavailable` 또는
`embedding_dimension_mismatch`로 즉시 표시한다. DB 연결·질의에는 각각 timeout이 있고 SQL 세션은 읽기 전용이다.
LLM 합성 HTTP client는 서비스 수명 동안 keep-alive 연결을 재사용하고 종료 시 닫는다.

## 채팅 실행

`POST /api/playground/chat`은 다음 순서로 동작한다.

1. tool·skill·첨부 ID가 현재 로컬 registry/저장소에 있는지 확인한다.
2. 첨부가 있고 `audit_qc_report`가 선택됐으면 provider/model 연결 전에 로컬 QC fast path를 실행한다.
3. 일반 요청은 provider/model 연결을 확인한다.
4. 요청한 skill을 로드하고 `skill-creator`가 활성화된 경우에만 `create_playground_skill`을 tool 범위에 자동 추가한다.
5. disabled/admin tool 요청을 즉시 오류로 반환한다.
6. LLM은 최종 선택된 tool schema와 활성 skill 지침만 받고 다음 행동을 JSON으로 반환한다.
7. tool 결과를 observation으로 전달해 답변을 합성한다.
8. `assistant_message`, `tool_calls`, `agent_steps`, `active_skill_ids`, `elapsed_ms`, `over_budget`,
   `rag_grounding`, `token_usage`를 반환한다.

QC fast path는 `audit_qc_report` 한 번만 호출하며 LLM과 외부 provider를 사용하지 않는다. 내부 순서는
`extract_uploaded_document`와 같은 추출기 → 로컬 SOP 규칙 로드/검색 → 결정론적 판정이다. 판정은
`PASS`, `WARNING`, `FAIL`, `UNVERIFIABLE` 중 하나이고 각 finding에 기준과 SOP 근거 ID가 포함된다.

`draft_qc_report`는 일반 LLM agent 경로에서 실행한다. 입력은 `temperature_c`, `recovery_rate_pct`,
`self_check_status` 세 필수 합성 값과 선택적 `operator_notes`다. tool은 LLM 호출 전에 기존 SOP 감사기로 예상 판정을
계산한다. LLM은 숫자를 포함하지 않는 `summary`, `interpretation`, `follow_up`만 생성하고, 관찰값 표·판정·criterion·
evidence는 코드가 Markdown에 삽입한다. 조립 결과를 기존 감사기로 다시 검사해 사전 판정과 다르면
`qc_draft_verification_failed`로 차단한다. 성공 결과도 `DRAFT`이며 사람 승인 전에는 최종 보고서가 아니다.

기능 안정성을 위한 제한은 유지한다.

- agent 최대 단계: `PLAYGROUND_AGENT_MAX_STEPS`
- 요청당 최대 tool 호출: `PLAYGROUND_AGENT_MAX_TOOL_CALLS`
- 전체 시간 예산: `PLAYGROUND_AGENT_BUDGET_MS`
- 동일 tool/인자 반복 호출 차단
- 각 tool의 `timeout_ms`

## 디버그

- `debug_trace=true`: 응답에 agent 단계 표시
- `debug_raw_llm=true`: 해당 요청의 raw LLM debug 표시
- `PLAYGROUND_DEBUG_PREVIEW_CHARS`: raw preview 길이 제한

raw LLM debug는 별도 서버 허용 gate 없이 요청 토글만으로 작동한다. 반복 평가와 보존할 trace는 서비스 경로와
분리된 MLflow 평가 runner에서 기록한다.

## 오류 계약

- 알 수 없는 tool: HTTP `400`, `unknown_tool`
- 비활성 tool: chat 응답 `tool_disabled`
- 관리자 tool: chat 응답 `admin_api_only`
- 알 수 없는 첨부: HTTP `400`, `unknown_attachment`
- 금지 확장자·손상 PDF·빈 첨부: HTTP `400`, `attachment_*`
- 이미지형/본문 없는 PDF: tool 응답 `document_text_unavailable` 또는 감사 `UNVERIFIABLE`
- QC 초안 필수값 누락·형식 오류: `qc_draft_invalid_input`
- QC 초안 LLM 실패·형식/숫자 위반: `qc_draft_llm_failed`, `qc_draft_invalid_response`
- QC 초안 재감사 불일치: `qc_draft_verification_failed`
- RAG 임베딩 endpoint 미가동: chat 응답 `embedding_unavailable`
- RAG DB 연결/SQL 실패: chat 응답 `rag_db_unavailable`
- RAG similarity 기준 미달: 오류가 아닌 `rag_grounding.decision=insufficient_evidence`,
  warning `rag_insufficient_evidence`, 추가 LLM 호출 0회
- 입력 또는 LLM 설정 오류: HTTP `400`
- karyotype 입력 길이 오류: HTTP `422`
- LLM 호출/응답 오류: HTTP `502`

provider 정책에 따른 `403`은 더 이상 없다.

## 합성 smoke test 예시

```json
{
  "message": "프로젝트 오로라의 디자인 에셋 폴더를 찾아줘",
  "selected_tool_ids": ["find_folder"],
  "provider": "local",
  "model": "local-model",
  "debug_trace": true,
  "debug_raw_llm": true
}
```

QC 초안은 agent가 자연어에서 아래 형태의 tool 인자를 구성하며 `debug_trace=true`에서 확인할 수 있다.

```json
{
  "temperature_c": 21.0,
  "recovery_rate_pct": 95.0,
  "self_check_status": "PASS",
  "operator_notes": "합성 기능 시험 메모"
}
```
