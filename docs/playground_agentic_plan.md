# Playground 챗봇 기능 테스트 설계

## 현재 단계

Playground는 실제 의료 환경과 무관한 합성 데이터로만 기능을 검증한다. 현재 목표는 provider 연결, tool 선택,
agent loop, 결과 표시, trace와 지연 측정이 하나의 경로에서 동작하는지 빠르게 확인하는 것이다.

운영용 의료데이터 탐지, provider별 합성 데이터 허용 gate, 별도 안전 프로필은 기본 기능 테스트 이후에 다시
설계한다. 현재 기능 경로에는 이를 추가하지 않는다.

## 사용자 흐름

1. `/playground`에서 local 또는 OpenAI provider와 모델을 선택한다.
2. 서버가 반환한 활성 tool 중 챗봇이 사용할 항목을 고른다.
3. 일반 합성 문장을 입력한다.
4. agent가 선택된 tool 범위 안에서 제한된 횟수만 호출한다.
5. 답변, tool 결과, 단계 trace, token usage, 전체 소요 시간을 한 화면에서 확인한다.

## 계약

- `GET /api/playground/tools`
  - `provider_availability` 같은 provider별 정책 정보는 반환하지 않는다.
  - `enabled=true`이고 `execution_type`이 `code` 또는 `llm`인 tool만 UI에서 선택할 수 있다.
- `POST /api/playground/chat`
  - local/OpenAI provider가 같은 tool 계약을 사용한다.
  - 요청에서 선택하지 않은 tool은 실행하지 않는다.
  - 알 수 없는 tool ID는 `400 unknown_tool`로 거절한다.
  - 응답은 `assistant_message`, `tool_calls`, `agent_steps`, `elapsed_ms`, `over_budget`, `token_usage`를 제공한다.
- `POST /api/playground/karyotype-summary`
  - 별도 합성 데이터 토글이나 provider 정책 `403` 없이 선택한 provider로 실행한다.
  - 입력·LLM 설정 오류는 `400`, 길이 오류는 `422`, provider 호출/응답 오류는 `502`로 반환한다.

## 실행 경계

- agent step, tool call 수, timeout, 전체 시간 예산은 항상 제한한다.
- 같은 tool/인자의 반복 호출은 막는다.
- 내용 인덱싱처럼 무거운 관리자 동작은 Playground 자동 실행 대상이 아니다.
- SMB 원본은 읽기 전용이며, 검색 요청마다 전체 공유폴더를 순회하지 않는다.
- API key와 SMB 자격증명은 환경변수나 현재 브라우저 탭 메모리에서만 받는다.

## LangGraph Studio

Studio는 단일 `.env.studio`와 `langgraph.json` 경로를 사용한다. 실행 전 fail-closed 검사와 별도
`synthetic_trace` 프로필은 사용하지 않는다. LangSmith trace는 표준 환경변수로 선택적으로 켜며 Studio 시작을
차단하지 않는다.

## 완료 기준

- 두 provider에서 동일한 활성 tool 목록을 선택할 수 있다.
- 별도 테스트 토글 없이 합성 입력으로 LLM-backed tool을 실행할 수 있다.
- Studio가 단일 실행 스크립트로 시작된다.
- disabled/admin tool, timeout, step/tool-call 상한, 지연 측정은 유지된다.
- `pytest -m "not integration"`와 `ruff check`가 통과한다.
