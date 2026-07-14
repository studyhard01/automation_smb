# LangGraph 통합 — 합성 챗봇 테스트

LangGraph와 LangGraph Studio로 `smb_finder`의 agent/tool 호출을 시각화하고 디버깅한다. 현재는 실제 의료 환경과
무관한 합성 데이터만 사용하는 단일 기능 테스트 프로필이다.

```text
[Studio / Agent Server]
          ↓
 [smb_agent graph] ⇄ [local OpenAI-compatible LLM]
          ↓ HTTP
 [smb-finder :8010] → /find · /search-content · /admin/content-index-jobs
```

Playground의 완료 이벤트는 별도 observer graph에서 볼 수 있다.

```text
[/playground] → [PlaygroundAgent/ToolExecutor] → 사용자 응답
                         └─ 비동기 실행 메타데이터
                                      ↓
                         [playground_observer]
```

## 구성

| 경로 | 역할 |
|---|---|
| `smb_agent/graph.py` | `create_react_agent` 기반 챗봇 graph |
| `smb_agent/observer_graph.py` | Playground 완료 이벤트를 표시하는 무LLM graph |
| `smb_agent/tools.py` | `find_folder`·`search_content`·`refresh_content` HTTP tool |
| `smb_agent/config.py` | `.env.studio` 기반 LLM·smb-finder·시간 예산 설정 |
| `langgraph.json` | 두 graph와 `.env.studio`를 등록하는 CLI 진입점 |
| `.env.example` | 합성 테스트 설정 예시 |
| `start_local_studio.ps1` | `.env.studio` 생성 후 로컬 Agent Server 실행 |

이전의 다중 안전 검사·전용 trace 프로필 구성은 제거됐다. Studio는 import 시점 검사 없이 하나의
`.env.studio` 경로로 시작한다.

## 실행

루트 API와 사용할 local LLM을 먼저 실행한다.

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010
```

다른 PowerShell에서 Studio Agent Server를 실행한다.

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb\integrations\langgraph
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_local_studio.ps1
```

최초 실행 시 `.env.example`이 `.env.studio`로 복사된다. 다음 값을 사용하는 로컬 환경에 맞춰 바꾼다.

```dotenv
SMB_FINDER_URL=http://localhost:8010
LLM_BASE_URL=http://localhost:8080/v1
LLM_MODEL=local-model
LLM_API_KEY=local-no-key
TOOL_TIMEOUT_MS=1500
```

Agent Server 주소는 `http://127.0.0.1:2024`다. Studio에서 `smb_agent`를 선택한 뒤 다음처럼 일반 합성 문장으로
호출 흐름을 확인한다.

```text
프로젝트 오로라의 디자인 에셋 폴더를 찾아줘
로봇 경주 규칙 문서에서 다음 경기 일정을 찾아줘
```

## LangSmith trace

LangSmith는 별도 프로필 없이 `.env.studio`의 표준 환경변수로 선택적으로 켠다. 합성 테스트 기본값은 input/output을
숨기지 않으므로 agent의 전체 LLM call을 바로 확인할 수 있다.

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<your-key>
LANGSMITH_PROJECT=automation-smb-langgraph
LANGSMITH_HIDE_INPUTS=false
LANGSMITH_HIDE_OUTPUTS=false
```

키 값은 `.env.studio`에만 넣고 예제 파일에는 넣지 않는다.

## Playground observer

루트 `.env`에서 observer를 켜고 API를 재시작한다.

```dotenv
LANGGRAPH_STUDIO_OBSERVER_ENABLED=true
LANGGRAPH_STUDIO_OBSERVER_URL=http://127.0.0.1:2024
LANGGRAPH_STUDIO_OBSERVER_GRAPH_ID=playground_observer
LANGGRAPH_STUDIO_OBSERVER_TIMEOUT_MS=300
LANGGRAPH_STUDIO_OBSERVER_QUEUE_SIZE=100
```

observer 전송은 비동기 fail-open이다. Studio가 꺼져 있거나 timeout이 나도 Playground 응답은 기다리지 않는다.

## 지연과 실행 한계

- `TOOL_TIMEOUT_MS` 기본값은 1500ms다.
- 검색 결과는 `FIND_LIMIT`, `CONTENT_LIMIT` 상위 N건만 반환한다.
- 검색 요청마다 SMB 전체를 순회하지 않고 이미 구축된 인덱스를 사용한다.
- `refresh_content`는 관리자 토큰이 있을 때만 API job을 만들며 SMB 원본 파일을 수정하지 않는다.
- agent LLM 왕복은 검색 자체보다 느리므로 단순 검색 성능 측정은 `/find`와 `/search-content`를 직접 사용한다.
