# Playground constrained agentic loop implementation plan

## 2026-07-09 provider update

Playground provider scope changed from local-only to `local` plus `openai`.

- `local` remains the default and keeps the internal URL guard.
- `openai` uses `OPENAI_BASE_URL` on the server, defaulting to `https://api.openai.com/v1`.
- The browser Settings button accepts an OpenAI API key for the current tab only.
- The UI sends that key through `X-Playground-OpenAI-Key`; it is not stored in localStorage, sessionStorage, cookies, repo files, responses, warnings, or debug payloads.
- The server may still use `OPENAI_API_KEY` as an operator fallback, but UI users are asked to provide a key through Settings.
- Agent/tool orchestration is shared by both providers. Only the LLM connection resolver changes by provider.
- When `openai` is selected, the user's message, selected tool specs, and selected tool observations may be sent to OpenAI. SMB credentials, admin tokens, and raw debug secrets remain redacted.
- OpenAI is not allowed to choose unselected, disabled, unknown, or duplicate tool calls; the existing server-side allowlist remains authoritative.

## 목표

`automation_smb` 자체 Playground에만 적용되는 제한형 agent loop를 만든다. 현재
`POST /api/playground/chat`은 local LLM이 한 번 tool 호출 목록을 고르고 실행한 뒤 답변을 합성한다.
다음 단계는 이를 `plan -> tool -> observe -> replan/final` 루프로 확장하되, 선택된 tool allowlist,
local/on-prem LLM 제한, 지연 예산, 디버그 정보 노출 제한을 유지하는 것이다.

비목표:

- Langflow, LangGraph, Dify, n8n 통합 변경 없음.
- 새로운 SMB 쓰기/이동/삭제 tool 추가 없음.
- 외부 LLM/OpenAI 호출 허용 없음.
- Playground 밖의 `/find`, `/search-content`, `/refresh*` API 계약 변경 없음.

## 사용자 동작

- Playground 채팅 화면에 토글 2개를 추가한다.
- `Agent trace 보기`: 켜면 agent step, tool 실행 요약, 관측 결과 요약을 메시지 하단에 보여준다. 기본값은 꺼짐.
- `Raw LLM debug 보기`: 테스트용 raw LLM 요청/응답 디버그를 보여준다. 기본값은 꺼짐이고, 서버 환경변수
  `PLAYGROUND_DEBUG_RAW_LLM=true`일 때만 동작한다. 서버 gate가 꺼져 있으면 UI는 비활성 상태로 보이거나,
  요청해도 응답에는 `debug_raw_llm_denied` warning만 포함한다.
- 사용자가 선택하지 않은 tool은 LLM이 요청해도 실행하지 않고 trace/warning에 차단 사실만 남긴다.
- agent는 제한된 step 수 안에서만 재계획한다. 예: 최대 3 step, step당 tool 1개, 전체 LLM/tool 시간 예산 초과 시
  부분 결과 또는 명확한 중단 메시지를 즉시 반환한다.

## API 계약 계획

`src/smb_finder/playground/models.py`

- `ChatRequest`에 `debug_trace: bool = False`, `debug_raw_llm: bool = False`를 추가한다.
- `ChatResponse`에 `agent_steps: list[AgentStepTrace] = []`, `debug: PlaygroundDebug | None = None`를 추가한다.
- 기존 `tool_calls`는 호환성을 위해 유지한다. 최종 실행된 tool trace의 축약 목록으로 계속 사용한다.
- `AgentStepTrace` 후보 필드:
  - `step: int`
  - `phase: Literal["plan", "tool", "observe", "final", "blocked", "error"]`
  - `tool_id: str = ""`
  - `status: str = ""`
  - `elapsed_ms: float = 0.0`
  - `summary: str = ""`
- `LlmDebugCall` 후보 필드:
  - `step: int`
  - `purpose: Literal["plan", "replan", "final"]`
  - `request_preview: str`
  - `response_preview: str`
  - `elapsed_ms: float`
  - `truncated: bool = False`

`src/smb_finder/config.py`

- `playground_debug_raw_llm: bool = Field(default=False)`를 추가하고, `.env`의 `PLAYGROUND_DEBUG_RAW_LLM`로 제어한다.
- gate가 false이면 raw LLM 요청/응답 본문은 모델 객체에 생성하지 않는다.

`src/smb_finder/playground/api.py`

- `/api/playground/chat`만 변경한다.
- 별도 config endpoint는 필수 아님. 프론트는 토글을 보낼 수 있고, 서버가 gate 결과를 응답 warning으로 알려도 된다.
  UI에서 선제 비활성화가 필요하면 `GET /api/playground/debug-config`를 아주 작은 read-only endpoint로 추가한다.

## Backend 책임

`src/smb_finder/playground/agent.py`

- 현재 `_plan_tool_calls` + `_compose_answer` 흐름을 제한형 loop로 분리한다.
- loop 상태는 메모리에만 둔다. `history`는 현재 요청 컨텍스트로만 사용하고 서버 세션 저장소를 만들지 않는다.
- 매 step:
  1. LLM에 현재 사용자 메시지, 선택된 tool spec, 이전 observation 요약만 전달한다.
  2. LLM 응답은 JSON 객체만 허용한다. 기존 `response_format`과 malformed JSON repair를 재사용한다.
  3. 응답 schema는 `{ "action": "tool" | "final", "tool_call": {...}, "answer": "..." }`처럼 단일 action으로 제한한다.
  4. `tool` action이면 selected+enabled handler allowlist에 있는지 서버에서 재검증한다.
  5. tool 결과는 길이 제한 및 redaction 후 observation으로만 다음 LLM 호출에 전달한다.
  6. `final` action 또는 step limit 도달 시 종료한다.
- 권장 제한:
  - `MAX_AGENT_STEPS = 3`
  - `MAX_TOOL_CALLS_PER_STEP = 1`
  - tool result observation LLM 전달 길이 `<= 2000` chars
  - trace summary `<= 1000` chars
  - raw debug preview request/response 각각 `<= 4000` chars
- 모든 LLM 호출과 tool 실행 elapsed_ms를 기록한다.
- 전체 `ChatResponse.elapsed_ms`가 기존 지연 예산을 크게 넘기면 warning에 `playground_agent_over_budget`를 남긴다.
- public URL 차단은 기존 `_is_internal_http_url`을 계속 사용한다. host allow 규칙을 완화하지 않는다.

Redaction:

- raw debug와 trace 모두 같은 redaction helper를 통과한다.
- 최소 패턴:
  - `SMB_PASSWORD`, `LLM_API_KEY`, `OPENAI_API_KEY`, `ADMIN_API_TOKEN` 값 마스킹
  - URL userinfo 마스킹
  - bearer/basic token 마스킹
  - Windows/UNC/SMB 경로는 trace에서는 tool 결과와 동일한 축약 정책을 따르고, raw debug에서는 길이 제한 후 필요 시
    `[PATH_REDACTED]`로 치환한다.
- raw debug는 테스트 편의를 위한 기능이므로 운영 기본값 false, README/.env.example에 위험 문구를 명시한다.

## Frontend 책임

`src/smb_finder/web/index.html`

- 채팅 header 또는 composer 근처에 토글 2개를 추가한다.
- 라벨은 정확히 `Agent trace 보기`, `Raw LLM debug 보기`를 사용한다.
- Raw debug 토글 옆에는 운영 기능처럼 보이는 설명 문구를 길게 두지 않는다. gate가 꺼져 있으면 disabled 상태를 권장한다.

`src/smb_finder/web/assets/playground.js`

- `sendChat()` payload에 `debug_trace`, `debug_raw_llm`을 포함한다.
- `debug_trace`가 켜졌을 때만 `data.agent_steps`를 렌더링한다.
- `debug_raw_llm`이 켜졌고 응답의 `data.debug.llm_calls`가 있을 때만 접을 수 있는 `<details>` 영역에 표시한다.
- HTML 삽입 시 현재 `escapeHtml` 사용 원칙을 유지한다. raw debug도 `innerHTML`에 직접 넣지 않는다.
- 기존 `tool_calls` 자동 표시 동작은 토글 기준으로 바꾼다.

`src/smb_finder/web/assets/playground.css`

- 기존 8px radius, 조용한 작업 도구 톤을 유지한다.
- trace/debug 영역은 채팅 카드 내부 하단에 작게 배치하고, 모바일에서 메시지 폭을 넘지 않게 `overflow:auto`를 둔다.

## Test 책임

Backend tests: `tests/test_playground.py`

- selected tool allowlist 위반 시 loop 중에도 tool이 실행되지 않고 warning/trace만 남는지 검증한다.
- LLM이 `tool -> final`을 반환하는 2 step 성공 경로를 monkeypatch로 검증한다.
- step limit 도달 시 마지막 observation 기반 부분 응답 또는 중단 메시지를 반환하는지 검증한다.
- `debug_raw_llm=True`이지만 `PLAYGROUND_DEBUG_RAW_LLM` false이면 raw debug가 비어 있고 warning만 있는지 검증한다.
- gate true이면 raw debug가 포함되되 secret/token/path 후보가 redaction되고 길이 제한되는지 검증한다.
- public LLM URL 차단 테스트는 유지한다.
- JSON response_format / malformed JSON repair 기존 테스트는 유지 또는 loop parser에 맞춰 보강한다.

API contract tests: `tests/test_api_contracts.py`

- `/api/playground/chat` response model에 신규 필드가 OpenAPI에 반영되는지 확인한다.
- 기존 operationId는 바꾸지 않는다.

Frontend smoke:

- 서버 실행 후 `/playground`에서 두 토글이 보이는지 확인한다.
- `Agent trace 보기` off일 때 tool trace가 렌더링되지 않고, on일 때만 렌더링되는지 확인한다.
- `Raw LLM debug 보기`는 gate false에서 raw 내용이 화면에 나오지 않는지 확인한다.

## 리스크

- Replan loop는 LLM 왕복이 늘어 지연이 커질 수 있다. step limit와 timeout을 코드 상수로 먼저 고정하고, 측정 후 조정한다.
- raw LLM debug는 prompt와 observation이 노출될 수 있다. 서버 gate, redaction, length limit, 기본 off가 acceptance 조건이다.
- tool 결과를 다음 LLM observation으로 넘기면 경로/본문 노출 범위가 넓어질 수 있다. 기존 tool result 요약을 재사용하고,
  파일 본문 snippet 전체를 넘기지 않는다.
- UI 토글이 client-only이면 네트워크 응답에는 trace가 계속 실릴 수 있다. trace는 반드시 request flag에 따라 서버에서
  포함 여부를 결정한다.
- 현재 pending local changes가 `_chat_json` response_format과 JSON repair를 이미 만지고 있으므로, 구현자는 그 변경을
  보존하고 loop parser에 재사용해야 한다.

## 완료 기준

- `POST /api/playground/chat`이 최대 step 수 안에서 `plan -> tool -> observe -> final` 흐름을 수행한다.
- 선택되지 않은 tool, 비활성 tool, registry에 없는 tool은 어떤 step에서도 실행되지 않는다.
- LLM base URL은 localhost/private/on-prem 규칙을 통과해야만 호출된다.
- `Agent trace 보기` off 기본값에서는 응답/화면에 상세 agent trace가 나오지 않는다.
- `Raw LLM debug 보기`는 `PLAYGROUND_DEBUG_RAW_LLM=true`와 request flag가 동시에 true일 때만 redacted/truncated preview를
  반환한다.
- raw debug와 trace에는 비밀번호, API key, admin token, URL userinfo가 평문으로 남지 않는다.
- 기존 Playground API operationId와 기존 `tool_calls` 호환 필드는 유지된다.
- `pytest tests/test_playground.py tests/test_api_contracts.py -m "not integration"`가 통과한다.

## 개발자 핸드오프

Backend:

- 편집 범위: `src/smb_finder/playground/agent.py`, `src/smb_finder/playground/models.py`,
  `src/smb_finder/playground/api.py`, `src/smb_finder/config.py`, `.env.example`, `tests/test_playground.py`,
  `tests/test_api_contracts.py`.
- 구현 순서: 모델 필드 추가 -> redaction/length helper -> bounded loop -> raw debug gate -> tests.
- product/security ambiguity가 생기면 raw data를 더 보여주는 방향으로 추측하지 말고 planner에게 되돌린다.

Frontend:

- 편집 범위: `src/smb_finder/web/index.html`, `src/smb_finder/web/assets/playground.js`,
  `src/smb_finder/web/assets/playground.css`.
- 구현 순서: 토글 DOM 추가 -> chat payload 연결 -> trace 렌더 조건 변경 -> raw debug `<details>` 렌더 -> 모바일 overflow 확인.
- raw debug는 HTML escape와 접힘 UI를 반드시 유지한다.

Shared:

- 신규 필드명은 backend/frontend/test에서 동일하게 맞춘다.
- README에는 새 env `PLAYGROUND_DEBUG_RAW_LLM`과 테스트 전용 경고를 짧게 추가한다.
