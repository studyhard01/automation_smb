# automation_smb — 공유 폴더 찾기 서비스

텍스트(이후 음성) 명령으로 **SMB 공유폴더를 즉시 찾아주는 챗봇 서비스**다.
노코딩 자동화 툴(→ [`OS.md`](./OS.md))의 첫 성공 케이스이자 L4 도구다.
현재 개발 단계와 작업 규칙·지연 요구는 [`AGENTS.md`](./AGENTS.md) 참고.

> **현재는 합성 데이터 전용 기능 테스트 환경이다.** 실제 의료 환경과 닮지 않은 더미 폴더·파일·문장만 사용해
> 챗봇 대화, tool 호출, trace, 지연을 검증한다. 의료데이터 입력 탐지나 provider별 합성 데이터 gate는 두지 않으며,
> 운영용 보안 검토는 기본 기능 테스트 이후 별도 단계에서 진행한다.

## 핵심 설계 — 지연 최소화

매 요청마다 SMB를 도는 대신, **폴더 트리를 미리 인덱싱(메모리+JSON 캐시)** 하고 **인메모리로 검색**한다.
자연어는 규칙 기반 fast-path로 키워드를 뽑고, 모호할 때만 로컬 LLM을 태운다(timeout 강제).

```
텍스트 질의 → intent.normalize(규칙/LLM) → FolderIndex.search(인메모리) → 결과(+소요 ms)
                                              ▲ 미리 빌드된 인덱스 (SMB 순회는 시작/갱신 시에만)
```

## 두 가지 검색

| 검색 | 무엇으로 찾나 | 엔드포인트 | 저장소 |
|---|---|---|---|
| **폴더 찾기** | 폴더 이름·경로 | `POST /find` | 인메모리 인덱스 + JSON 캐시 |
| **내용 찾기** | 파일 **본문** 키워드 | `POST /search-content` | SQLite **FTS5(trigram)** |

내용 검색은 trigram 토크나이저라 **한국어·영문·검사코드 모두 부분일치**가 되고, 형태소 분석기 같은
**외부 의존성이 없다**(온프레미스·SSL프록시 환경에 적합). 3글자 미만 질의는 `LIKE`로 폴백한다.
`docx`/`xlsx`는 stdlib `zipfile`로, `pdf`는 선택 의존성(`pypdf`)으로 본문을 뽑는다.

테스트용 내용 인덱스(`.cache/content.fts.db`)에는 합성 fixture 본문만 적재한다. `.cache/`는 실행 중 생성되는
로컬 캐시이므로 커밋하지 않는다.

## API 계약 · 관측

`POST /find`와 `POST /search-content` 응답은 결과 목록과 함께 `result_count`, `elapsed_ms`,
`over_budget`을 반환한다. `POST /refresh`, `POST /refresh-content`, `GET /health`도 명시적인
Pydantic 응답 모델을 사용해 OpenAPI 도구 계약을 고정한다.

- `/health`: `ready`, `indexed_folders`, `indexed_files`, 인덱스 로드 여부를 반환한다.
- `/refresh`: 폴더 인덱스 갱신 후 폴더 수와 소요 시간을 반환한다.
- `/refresh-content`: 내용 인덱싱 통계와 소요 시간을 반환하되, SMB host/share 실제 값은 노출하지 않는다.
- `/admin/content-index-jobs`: 내용 인덱싱을 백그라운드 job으로 실행한다. `X-Admin-Token` 필요.

로그에는 요청 ID, 질의 길이, 결과 수, 소요 시간과 예산 초과 여부를 기록해 기능과 지연을 확인한다.

## MCP 로컬 MVP

기존 FastAPI 프로세스의 검색 runtime을 그대로 사용해 `http://127.0.0.1:8010/mcp`에
Streamable HTTP MCP를 선택적으로 제공한다. 기본값은 `MCP_ENABLED=false`라 endpoint가 마운트되지 않는다.
활성화된 MCP가 공개하는 도구는 읽기 전용 검색 두 개뿐이다.

- `find_folder`: 사전 구축된 인메모리 폴더 인덱스 검색
- `search_content`: 사전 구축된 로컬 FTS5 내용 인덱스 검색

관리자·인덱스 갱신·보고서·핵형요약 도구는 MCP catalog에 등록하지 않는다. 결과에서도 원문 query, 본문
`snippet`, 절대경로, SMB host/share, 내부 IP와 자격증명을 제외한다. MCP 요청이 SMB를 직접 순회하거나
인덱스를 갱신하지도 않는다.

로컬 테스트에서만 아래 값을 `.env`에 추가한다. `MCP_API_TOKEN`은 `ADMIN_API_TOKEN`과 다른 임의값을 쓰고,
서비스는 계속 `--host 127.0.0.1`로 바인딩한다.

```powershell
MCP_ENABLED=true
MCP_API_TOKEN=<별도로 생성한 충분히 긴 임의값>
MCP_ALLOW_REMOTE=false
MCP_ALLOWED_HOSTS=127.0.0.1,localhost,[::1]
MCP_ALLOWED_ORIGINS=
```

Origin 없는 로컬 서버형 client는 허용한다. Origin 헤더가 있는 client는 `MCP_ALLOWED_ORIGINS`에 정확한 값을
명시해야 하며 wildcard는 사용하지 않는다. 기본 CORS는 꺼져 있어 브라우저의 직접 교차 출처 연결은 이번 MVP에서
지원하지 않는다. 요청에는 `Authorization: Bearer <MCP_API_TOKEN>`이 필요하고, 본문은 64KiB로 제한된다.
사내 다중 사용자 공개와 OAuth/SSO는 이 로컬 MVP의 범위가 아니다.

## 구조

| 파일 | 역할 |
|---|---|
| `src/smb_finder/config.py` | `.env` 설정 로더 (SMB·인덱스·LLM·시간예산·내용검색) |
| `src/smb_finder/models.py` | 입출력 Pydantic 모델 (`Find*`/`ContentSearch*`) |
| `src/smb_finder/smb_client.py` | SMB 세션 + 트리 순회 (폴더 `walk_folders`, 파일 `walk_files`) |
| `src/smb_finder/index.py` | 폴더 인메모리 인덱스 + 빠른 검색 + JSON 캐시 |
| `src/smb_finder/indexer.py` | SMB 순회로 폴더 인덱스 빌드 (시작/백그라운드) |
| `src/smb_finder/intent.py` | 자연어 → 키워드 (규칙 우선, LLM 선택) |
| `src/smb_finder/finder.py` | 폴더 검색 오케스트레이터 (정규화→검색→응답, 시간 측정) |
| `src/smb_finder/extract.py` | 파일 본문 추출 (텍스트/`docx`/`xlsx`/`pdf`, cp949 폴백) |
| `src/smb_finder/content_index.py` | 내용 FTS5(trigram) 저장 + 검색 (bm25·snippet) |
| `src/smb_finder/content_indexer.py` | SMB 파일 순회→추출→FTS5 적재 (관리/백그라운드) |
| `src/smb_finder/content_search.py` | 내용 검색 오케스트레이터 (토큰화→검색, 시간 측정) |
| `src/smb_finder/rag_search.py` | 로컬 PostgreSQL 질의 임베딩→pgvector chunk 검색 (단계별 시간 측정) |
| `src/smb_finder/reports/` | 검사 보고서 업무 보조 tool — LLM 기반 ISCN 요약 + 로컬 후보 문서 검색/체크리스트 |
| `src/smb_finder/tooling/` | 검색 도구 공통 Pydantic 계약·불변 catalog·timeout/동시 실행/감사 로그 executor |
| `src/smb_finder/mcp_server.py` | 읽기 전용 MCP 검색 도구 등록과 HTTP transport 설정 |
| `src/smb_finder/api.py` | FastAPI 앱 — `POST /find`·`/search-content`·`/refresh*` (OpenAPI 도구) |
| `src/smb_finder/playground/` | 자체 챗봇 Playground — 선택한 tool만 호출하는 local LLM 기반 채팅 API |
| `src/smb_finder/playground/studio_observer.py` | Playground 실행 메타데이터를 로컬 LangGraph Studio 관찰 graph로 보내는 비동기·fail-open observer |
| `src/smb_finder/web/` | `/playground` 정적 UI — tool 선택, 채팅, Tool Lab 초안 화면 |
| `docs/PLAYGROUND_TOOLS.md` | `/playground` 등록 tool별 입력·동작·테스트 계약 |
| `docs/LLM_REPORT_PROCESS_FOR_AUTOMATION_SMB.md` | 세포유전 LLM 보고서 프로세스 이관 가이드 |
| `docs/TOOL_MCP_LANGCHAIN_ARCHITECTURE.md` | 공통 ToolSpec을 중심으로 REST·Playground·MCP·LangChain을 연결하는 목표 아키텍처와 흐름도 |
| `docs/MCP_IMPLEMENTATION_PLAN.md` | 검색 도구 2개부터 시작하는 MCP MVP의 작업 순서·테스트·롤백 계획 |
| `docs/playground_agentic_plan.md` | Playground 제한형 agent loop와 debug 토글 구현 계획 |
| `integrations/langflow/` | 노코드 외피 — Langflow 컴포넌트 + `folder_search` 워크플로우 자동 생성기 ([README](integrations/langflow/README.md)) |
| `integrations/langgraph/` | LangGraph 외피 — 같은 HTTP 호출을 LangGraph Studio(로컬)로 관리·디버깅 ([README](integrations/langgraph/README.md)) |

## 노코드 외피 (Langflow)

코딩 없이 워크플로를 짜는 외피로 [Langflow](https://github.com/langflow-ai/langflow)를 쓴다.
`smb_finder` 코드는 그대로 두고, `integrations/langflow/`의 커스텀 컴포넌트가 `POST /find`를
**사내 localhost로** 호출해 캔버스에 끌어다 쓸 수 있게 감싼다(외부 전송 없음). 자세한 실행은
[`integrations/langflow/README.md`](integrations/langflow/README.md).

첫 자동 생성 템플릿은 `folder_search`다. 자연어 요구사항을 입력하면 기존 `SMBFolderFinder`
컴포넌트를 재사용하는 Langflow flow를 생성하고, Langflow API에 바로 등록할 수 있다. 기본은
규칙/로컬 LLM이며, OpenAI는 명시 설정이 있을 때만 사용한다.

## 자체 챗봇 Playground

Langflow 없이 `smb_finder` 안에서 바로 쓰는 챗봇/tool UI를 제공한다.

- 화면: `GET /playground`
- tool 목록: `GET /api/playground/tools`
- skill 목록/생성: `GET|POST /api/playground/skills`
- skill 수정/삭제: `PUT|DELETE /api/playground/skills/{skill_id}`
- ISCN 핵형 요약: `POST /api/playground/karyotype-summary`
- 채팅 실행: `POST /api/playground/chat`
- Tool Lab 초안: `POST /api/playground/tool-draft`
- local LLM 확인: `POST /api/playground/llm-status`

기본 tool은 `find_folder`, `search_content`, `search_rag_chunks`, `cytogenetics_karyotype_summary`,
`cytogenetics_report`, `ngs_report`, `refresh_content`다. UI는 `SMB 직접 접근`, `DB 접근`, `보고서 관련` 세 분류만
먼저 표시하고, 분류를 클릭하면 내부 tool 선택지가 열린다.

채팅 입력창 아래 `Skills` 버튼에서는 실제 agent와 같은 `<skill-id>/SKILL.md` 형식의 스킬을 선택·조회·추가·수정·삭제한다.
기본 스킬은 코드와 함께 제공되는 읽기 전용 문서이며, UI에서 만든 사용자 스킬은
`PLAYGROUND_SKILLS_DIR`(기본 `.cache/playground-skills`) 아래에 같은 디렉터리 구조로 저장된다. 선택한 SKILL.md의
frontmatter와 본문 지침은 채팅 요청의 agent system prompt에 실제로 주입되고, 응답의 `active_skill_ids`에서 적용 여부를
확인할 수 있다. `/tools`는 모든 tool 설명을, `/skills`는 설치된 모든 skill 설명을 LLM 호출 없이 즉시 반환한다.

기본 제공 skill은 다음과 같다.

- `tools`, `skills`: `/tools`, `/skills` slash command 사용법과 목록 출력
- `rag-grounded-answer`: PostgreSQL vector chunk 근거를 먼저 찾고 문서 위치와 함께 답변
- `smb-navigation`: 폴더명 검색과 파일 본문 검색 중 가장 작은 tool을 선택
- `report-workflow`: 핵형·세포유전·NGS 요청을 알맞은 보고서 tool로 라우팅
- `latency-first`: 불필요한 재호출을 줄이고 시간 예산 안에서 짧게 응답

`search_rag_chunks`는 `rag_db_local_20260713.document_chunks`를 cosine 유사도로 검색한다. DB에 저장된
`nomic-embed-text-v2-moe`와 동일한 768차원 모델 endpoint가 필요하며, 질의에는 모델 권장
`search_query: ` prefix를 자동으로 붙인다. 연결은 `.env`의 `RAG_DB_*`, `RAG_EMBEDDING_*`로 바꿀 수 있다.
찾은 chunk는 agent observation으로 전달되고 선택한 LLM이 근거 기반 최종 답변을 작성한다.
보고서 tool은 문서를 자동 확정하지 않고, 로컬 내용 인덱스에서 관련 템플릿 후보를 찾은 뒤
작성 체크리스트와 다음 행동을 제시한다. 결과 trace에는 구조화 payload가 포함되어 UI가 후보 문서와
체크리스트를 별도 블록으로 표시한다.

Playground는 local/on-prem OpenAI 호환 LLM과 OpenAI API provider를 둘 다 지원한다.
local provider는 `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT_MS` 환경변수를 사용한다.
OpenAI provider는 UI의 Settings에 임시 API key를 넣거나 서버 `.env`의 `OPENAI_API_KEY`를 사용한다. UI key가 비어 있으면
서버 `.env` 값을 자동 사용하므로 재시작·새로고침마다 다시 입력할 필요가 없다. 실제 key는 `.env.example`이나 커밋에 넣지 않는다.
현재는 합성 데이터 전용 테스트이므로 활성화된 tool은 local/OpenAI provider에서 같은 방식으로 선택·실행된다.
별도 합성 데이터 토글이나 provider별 tool 차단은 없다. LLM 모델이 비어 있으면 UI는 tool을 실행하지 않고
설정 필요 메시지를 반환한다.
화면에서 Base URL과 모델명을 임시 입력해 `.env` 수정 없이 Ollama/LM Studio/llama.cpp 같은 로컬 서버를 확인할 수 있다.
예: `http://127.0.0.1:11434/v1`, `qwen2.5-coder:7b`.
Qwen3 계열은 기본 thinking이 JSON 응답 예산을 소진할 수 있어 Playground의 JSON 전용 호출에 `/no_think`를 자동 적용한다.
OpenAI-compatible 응답에 `usage`가 있으면 채팅 응답과 연결 확인 결과에 `token_usage`가 포함되어
모델별 input/output/total token과 호출 수를 볼 수 있다.

## LangGraph Studio · LangSmith trace

LangGraph Studio 연동은 서로 다른 두 graph를 제공한다.

- `smb_agent`: Studio에서 직접 질문을 실행하는 기존 Agent graph다. 로컬 LLM과 `smb_finder` REST 도구를 호출한다.
- `playground_observer`: `/playground`에서 이미 완료된 요청의 실행 메타데이터를 받는 관찰 graph다.
  LLM·SMB·MCP를 호출하지 않으며 Studio가 꺼져도 Playground 응답에는 영향을 주지 않는다.

observer에는 요청 ID, provider/model 식별자, 선택·실행된 tool ID, 상태·오류 코드, 지연·토큰 수치를 보낸다.
기본값은 OFF이며, 켜면 Playground 응답과 무관한 fail-open 방식으로 동작한다.

```powershell
# 1. Studio 의존성 설치
uv sync --native-tls --extra dev --extra studio

# 2. LangGraph 로컬 Agent Server 실행
# 최초 실행 시 .env.example을 .env.studio로 복사한다.
cd integrations/langgraph
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_local_studio.ps1
```

루트 `.env`에서 observer를 명시적으로 켠 뒤 `smb_finder`를 재시작한다.

```dotenv
LANGGRAPH_STUDIO_OBSERVER_ENABLED=true
LANGGRAPH_STUDIO_OBSERVER_URL=http://127.0.0.1:2024
LANGGRAPH_STUDIO_OBSERVER_GRAPH_ID=playground_observer
LANGGRAPH_STUDIO_OBSERVER_TIMEOUT_MS=300
LANGGRAPH_STUDIO_OBSERVER_QUEUE_SIZE=100
LANGSMITH_TRACING=false
```

Studio에서 `playground_observer` graph를 선택하면 Playground 응답 하단의 요청 ID와 같은 run을 찾을 수 있다.
로컬 Agent Server의 in-memory run은 개발 서버 재시작 시 사라진다. LangSmith tracing은 같은 `.env.studio`에서
표준 환경변수로 선택적으로 켤 수 있으며, 별도 안전 검사나 `synthetic_trace` 프로필 분기는 없다.

## 설치 · 실행

```powershell
# uv.lock 기준으로 Python 3.11 가상환경과 개발 의존성을 한 번에 맞춘다.
uv sync --python 3.11 --native-tls --extra dev
Copy-Item .env.example .env                  # SMB 자격증명 입력 (실제 값은 ../automation/.env)

# 서버 실행
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010 --reload

# 별도 터미널: DB 적재 때 사용한 동일 GGUF 모델을 embeddings 서버로 실행
llama-server -m <nomic-embed-text-v2-moe.gguf> --embeddings --port 8081

# 폴더 찾기 요청 (이름·경로)
curl -s -X POST http://localhost:8010/find \
  -H 'Content-Type: application/json' \
  -d '{"query": "demo project alpha 폴더 찾아줘"}'

# 내용 찾기 요청 (파일 본문)
curl -s -X POST http://localhost:8010/search-content \
  -H 'Content-Type: application/json' \
  -d '{"query": "orange widget revision"}'

# 폴더 인덱스 갱신 (이름 검색용)
curl -s -X POST http://localhost:8010/refresh

# 내용 DB화 — 원하는 폴더만 인덱싱 (path 지정, 관리자 토큰 필요)
curl -s -X POST http://localhost:8010/refresh-content \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>' \
  -d '{"path": "demo/projects/alpha"}'

# 운영 권장: 관리자용 백그라운드 job으로 내용 DB화
curl -s -X POST http://localhost:8010/admin/content-index-jobs \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>' \
  -d '{"path": "demo/projects/alpha"}'

curl -s http://localhost:8010/admin/content-index-jobs/<job_id> \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>'
```

> 내용 인덱스는 추출이 무거워 **시작 시 자동 빌드하지 않는다.** 공유 전체를 한 번에 돌지 않고,
> `/refresh-content`에 **폴더 경로(path)**를 줘서 원하는 폴더만 DB화한다(여러 폴더는 누적, 같은 폴더는
> 갱신, `path` 생략 시 전체). 같은 파일은 `size/mtime`이 같으면 본문 읽기를 건너뛰고,
> 이번 범위에서 사라진 기존 인덱스만 정리한다. 이후 `/search-content`는 떠 있는 인덱스에서 즉시 검색한다.
> (`PDF` 본문까지 쓰려면 `uv pip install -e ".[pdf]"`).
> 운영 자동화와 관리자 UI는 동기 `/refresh-content` 대신 `/admin/content-index-jobs`를 사용한다.
> `ADMIN_API_TOKEN`이 비어 있으면 `/refresh`, `/refresh-content`, `/admin/*` 인덱싱 엔드포인트는 닫힌다.
> host/share override는 `SMB_ALLOWED_HOSTS`, `SMB_ALLOWED_SHARES` 또는 기본 `SMB_HOST`/`SMB_SHARE_NAME`에
> 포함된 대상만 허용한다.

## 테스트 · 린트

```bash
.venv/Scripts/python -m pytest tests/ -m "not integration"   # 라이브 SMB 불필요
.venv/Scripts/python -m ruff check src tests integrations/langflow
```

## 다음 단계

- **의미 검색(벡터) — 같은 SQLite에 추가**: 현재 키워드(FTS5) 위에, 표현이 달라도 의미가 가까운
  검색을 위해 **로컬 임베딩 + 벡터**를 얹어 하이브리드(RRF)로 융합. 외부 전송 금지 원칙상 임베딩은
  온프레미스 로컬 모델로. 실제 필요성 확인 후 진행.
- `hwp`/`hwpx` 본문 추출 추가 (진단검사실에 흔함 — OLE/zip 파서, 선택 의존성)
- 음성(STT) 입력 추가 — L1, 로컬 모델 (`OS.md` 8장 미확정 항목)
- 실측으로 시간 예산(`FIND_BUDGET_MS`/`CONTENT_SEARCH_BUDGET_MS`) 조정
