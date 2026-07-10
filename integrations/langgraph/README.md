# LangGraph 통합 — smb-finder를 LangGraph Studio로 관리하기

[LangGraph](https://github.com/langchain-ai/langgraph) + **LangGraph Studio**(로컬)로,
지금까지 만든 `smb_finder` 서비스 호출들을 **코드 그래프**로 관리·시각화·디버깅한다.
Langflow flow(`smbtest-*`)가 하던 "Agent + SMB 도구 + Chat" 구성을 LangGraph로 옮긴 것이다.

```
[LangGraph Studio]  ──붙음──▶  [langgraph dev (로컬 서버)]  ──그래프──▶  [에이전트 LLM ⇄ 도구]
                                                                              │ HTTP
                                                                              ▼
                                          POST /find · /search-content · /admin/content-index-jobs
                                                              │
                                                              ▼
                                          [smb-finder 서비스]  (localhost:8010, 사내망)
                                                              │ 인메모리/FTS5 인덱스
                                                              ▼
                                                            결과
```

> **Langflow ↔ LangGraph는 자동 변환되지 않는다.** flow JSON 포맷이 호환되지 않아 그대로
> 불러올 수 없다. 대신 **진짜 로직은 전부 smb-finder(HTTP)에 있고** Langflow 컴포넌트도
> LangGraph 도구도 그 엔드포인트를 부르는 얇은 래퍼라, **같은 일을 LangGraph로 다시 짜는 것**은
> 쉽다. 이 폴더가 그 LangGraph 외피다. `smb_finder` 코드와 Langflow 외피는 그대로 둔다.

## 구성

| 경로 | 역할 |
|---|---|
| `smb_agent/graph.py` | `create_react_agent` 기반 에이전트 그래프 (Studio가 불러갈 `graph`) |
| `smb_agent/tools.py` | 도구 3종 — `find_folder`/`search_content`/`refresh_content` (→ smb-finder HTTP) |
| `smb_agent/config.py` | `.env` 설정 (smb-finder 주소·LLM·예산). smb_finder와 같은 LLM 키 재사용 |
| `langgraph.json` | LangGraph CLI/Studio가 읽는 진입점 (`smb_agent` 그래프 등록) |
| `.env.example` | 설정 예시 (값 비움). **LangSmith 트레이싱 OFF 포함** |
| `requirements.txt` | langgraph / langgraph-cli / langchain-openai 등 |

## 🔒 보안 — LangGraph Studio 쓸 때 반드시 (CLAUDE.md 최우선)

- **LangSmith 트레이싱을 끈다.** 켜면 노드 입출력(= 환자/검사 **폴더 경로·파일 본문 스니펫**)이
  LangSmith **클라우드로 업로드**된다 → 외부 전송 금지 위반. `.env.example`에 `LANGSMITH_TRACING=false`,
  `LANGCHAIN_TRACING_V2=false`로 꺼 뒀다. 꼭 추적이 필요하면 **사내 self-hosted LangSmith**만.
- **LLM은 온프레미스만.** `LLM_BASE_URL`을 OpenAI 등 외부로 바꾸지 말 것(기본 `localhost:8080/v1`).
- **smb-finder는 사내 localhost만.** `SMB_FINDER_URL`을 외부로 바꾸지 말 것.
- **로컬 dev 서버만 사용.** `langgraph dev`는 localhost에 뜬다. 사내망 밖으로 노출하지 않는다.
- 도구는 **read-only** — 폴더 찾기/내용 검색/인덱싱(로컬 인덱스만 갱신). 공유폴더 파일을 쓰지 않는다.

## 지연 (CLAUDE.md)

- 도구는 `TOOL_TIMEOUT_MS`(기본 1500ms)로 smb-finder 호출에 timeout을 강제하고, 초과 시 짧은
  상태 메시지로 즉시 반환한다. 결과는 상위 N건만 → LLM 입력 토큰도 줄인다.
- 단, **에이전트 LLM 자체가 가장 큰 지연원**이다(ReAct 루프 = LLM 왕복 여러 번). 단순 키워드
  질의를 빠르게 처리하려면 Langflow처럼 도구를 직접 호출하거나, smb-finder의 fast-path를 쓰는 게
  더 빠르다. LangGraph는 **에이전트가 도구를 골라야 하는 모호한 질의**에서 가치가 크다.

## 실행

### 1) smb-finder 서비스 먼저 (호스트)
```bash
cd ../..                                   # automation_smb 루트
.venv/Scripts/uvicorn smb_finder.api:app --port 8010
```

### 2) (선택) 온프레미스 LLM 띄우기
에이전트가 쓸 OpenAI 호환 로컬 LLM(llama.cpp 등)을 `LLM_BASE_URL`에 맞춰 실행한다.

### 3) LangGraph 외피 설치 · 실행
```bash
cd integrations/langgraph
cp .env.example .env                       # 값 확인 (smb-finder 주소·LLM·트레이싱 OFF)

# 의존성 (사내망 SSL이면 아래 둘 중 하나)
pip install -r requirements.txt --trusted-host pypi.org --trusted-host files.pythonhosted.org
# uv pip install --native-tls -r requirements.txt

# 로컬 Studio 서버 — 외부로 노출하지 않음
$env:PYTHONIOENCODING="utf-8"   # Windows cp949 인코딩 오류 방지
$env:PYTHONUTF8="1"
langgraph dev
```
- 콘솔에 뜨는 Studio URL(`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024` 형태)로
  접속하면 브라우저 UI는 LangChain이 호스팅하지만 **그래프 실행·데이터는 localhost(127.0.0.1:2024)
  에만** 머문다. 트레이싱을 꺼 두면 데이터가 클라우드로 가지 않는다. 사내 보안정책상 외부 UI 접속도
  막아야 하면, Studio 없이 `langgraph dev` API(`http://127.0.0.1:2024`)를 직접 호출해도 된다.

### 4) Studio에서 사용
- 좌측에서 `smb_agent` 그래프 선택 → 채팅 입력에 "OO검사 결과 폴더 찾아줘" / "BRCA1 변이 보고서
  내용 검색" 입력 → 에이전트가 도구를 골라 smb-finder를 호출하는 과정을 노드 단위로 본다.
- 내용 검색이 "인덱스 비어 있음"이면 "검사결과/2026/OO검사 폴더 DB화해줘"로 `refresh_content` 먼저.
  `ADMIN_API_TOKEN`이 설정된 운영 환경에서는 admin job 생성 후 상태를 짧게 polling한다.

## 버전 메모

- 검증 기준: `create_react_agent`의 `prompt=` 인자(LangGraph 0.2.x+). 더 낮은 버전은
  `state_modifier=`일 수 있다 — 그땐 `graph.py`의 해당 인자만 바꾼다.
- `langgraph dev`는 `langgraph-cli[inmem]`이 있어야 동작한다(requirements에 포함).

## LangSmith token usage 확인

Playground의 OpenAI provider 호출은 루트 서비스에서 `usage`를 추출해 응답의 `token_usage`에 포함한다.
이 호출 trace는 LangGraph Studio 실행 목록이 아니라 LangSmith의 Tracing Projects에서
`automation-smb-playground` project로 확인한다. LangGraph Studio URL은 `smb_agent` 그래프를 직접 실행할 때의
디버깅 화면이다.

LangSmith에 모델별 token/cost 로그까지 남기려면 아래처럼 명시적으로 opt-in한다.

```bash
# 루트 서비스 또는 integrations/langgraph/.env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=<LangSmith API key>
LANGSMITH_PROJECT=automation-smb-playground
LANGSMITH_HIDE_INPUTS=true
LANGSMITH_HIDE_OUTPUTS=true
```

trace에는 `ls_provider=openai`, `ls_model_name=<model>`, `usage_metadata.input_tokens`,
`usage_metadata.output_tokens`, `usage_metadata.total_tokens`가 들어가므로 LangSmith trace tree/project stats에서
모델별 사용량을 볼 수 있다. raw prompt, tool 결과, SMB 경로/본문은 숨긴다. 실제 환자/검사 데이터로 cloud
LangSmith를 켜지 말고, 필요한 경우 self-hosted `LANGSMITH_ENDPOINT`를 사용한다.
