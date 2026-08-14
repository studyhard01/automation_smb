# Bot Main Core 구현 계획

> 기준일: 2026-08-14
>
> 상태: D1(P0-1 + 최소 P0-2 scaffold) 구현 완료, P0-3 Model Gateway 대기
>
> 기준 실행 경로: `backend/src/smb_finder/api.py`의 FastAPI와 PostgreSQL·MinIO·Neo4j 기반 `DocumentRuntime`

## 1. 결론

WBS 네 항목은 활성 제품 기준으로 모두 완료 상태가 아니다. 1·2번은 **부분 구현**, 3·4번은 **미구현**이다.
최우선 작업은 예전 Finder/SQLite 코드를 되살리는 것이 아니라, 현재 `DocumentRuntime` 계약 위에 Bot Core, Model
Gateway, read-only MCP metadata adapter와 bounded Session Cache를 새로 연결하는 것이다.

> **금지:** 삭제된 `FolderHit`, `ContentSearchRequest`, `FindRequest`, `FolderIndex` 또는 SQLite 계약을 복원해 오래된
> 테스트만 통과시키지 않는다. 레거시 테스트는 현재 문서 모델 기반 contract test로 이관하거나 제거 사유를 명시한다.

## 2. WBS 구현 감사

| WBS | 활성 제품 판정 | 저장소 증거와 빠진 부분 |
|---|---|---|
| FastAPI·LangGraph 기본 Flow, Router, Mock Retriever/Model | **부분 구현** | FastAPI 앱과 문서 Router는 [`api.py`](../backend/src/smb_finder/api.py)의 `app`, `create_document_router()`로 활성화되어 있다. LangGraph는 [`integrations/langgraph/smb_agent/graph.py`](../integrations/langgraph/smb_agent/graph.py)에만 있으며 import 시 실제 `ChatOpenAI`를 생성하고, 활성 FastAPI에 연결되지 않는다. 도구도 삭제된 `/find`, `/search-content`, admin 인덱싱 API를 호출한다. 외부 DB·LLM 없이 실행되는 Mock Retriever/Model과 graph/router 회귀 테스트가 없다. |
| Model Gateway, Citation/Policy, 오류 처리 Flow | **부분 구현** | `DocumentCitation`, `RetrievalScope`, `RetrievalMetadata` 계약과 선택 Revision 범위 SQL은 [`models.py`](../backend/src/smb_finder/models.py), [`llmops_retrieval.py`](../backend/src/smb_finder/llmops_retrieval.py)에 있다. 근거가 없을 때 LLM을 호출하지 않는 처리, 내부 URL 제한, 안전한 오류 코드는 [`document_chat.py`](../backend/src/smb_finder/playground/document_chat.py)에 있다. 그러나 공통 Gateway가 없고 `DocumentChatService`, `LocalSearchQueryExpander`, `OllamaQueryEmbeddingClient`, proposal 생성기가 각각 `httpx.Client`와 오류·timeout 계약을 소유한다. |
| MCP Metadata Tool, Session Cache, timeout/fallback | **미구현** | [`mcp_server.py`](../backend/src/smb_finder/mcp_server.py)와 [`tooling/executor.py`](../backend/src/smb_finder/tooling/executor.py)에 MCP adapter와 bounded executor 초안은 있으나 활성 `api.py`에 마운트되지 않는다. catalog는 삭제된 모델을 import해 현재 수집도 실패한다. 전용 metadata tool과 애플리케이션 Session Cache가 없다. MCP SDK의 protocol session manager는 대화/graph 상태를 보관하는 Session Cache가 아니다. |
| MCP Metadata new tool 확장 | **미구현** | [`tooling/catalog.py`](../backend/src/smb_finder/tooling/catalog.py)의 공개 spec은 레거시 `find_folder`, `search_content` 두 개뿐이다. 현재 문서 metadata, 활성 Revision, 버전 관계, 저장소 상태, 선택 범위 Citation을 현재 계약으로 제공하는 MCP tool은 없다. |

검증 기준도 위 판정을 지지한다.

- 활성 계약: `test_llmops_api_contracts.py`, `test_llmops_search.py`, `test_llmops_retrieval.py`에서 **17 passed**.
- 레거시 묶음: `test_tooling.py`, `test_mcp_server.py`, `test_playground.py` 수집 중 삭제된
  `ContentSearchRequest`와 `FolderHit` import로 **3 collection errors**.
- `integrations/langgraph/`에는 graph/router/mock 자동 테스트가 없다.

## 3. 기존 계획과의 정렬

- 상위 [`PRODUCT_DEVELOPMENT_PLAN.md`](PRODUCT_DEVELOPMENT_PLAN.md)의 `P0 — Bot Main Core 계약 복구`를 이 문서의
  실행 순서와 완료 기준으로 구체화한다.
- [`MCP_IMPLEMENTATION_PLAN.md`](MCP_IMPLEMENTATION_PLAN.md)의 `M0–M2 완료` 표기는 현재 활성 코드와 테스트 기준으로
  재판정 대상이다. 보안·전송 설계는 참고하되 레거시 Finder runtime 완료 기록은 승계하지 않는다.
- [`playground_agentic_plan.md`](playground_agentic_plan.md)의 LangGraph Studio는 선택적 개발 도구로 유지한다. 운영
  실행·관측 저장소로 간주하지 않는다.
- [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)가 `mcp_server.py`, `tooling/`, `integrations/langgraph/`를
  비활성 실험으로 분류한 현재 경계는 P0 완료 전까지 유지한다.

## 4. 우선순위와 의존 순서

```text
P0-1 활성 계약/레거시 경계 고정
  -> P0-2 FastAPI + LangGraph + Mock 수직 슬라이스
      -> P0-3 Model Gateway 통합
          -> P0-4 MCP Metadata + Session Cache 연결
              -> P1 metadata tool 단계적 확장
```

P0-2와 P0-3의 protocol·fake 설계는 함께 검토할 수 있지만, 활성 runtime 계약이 고정되기 전 제품 연결은 하지 않는다.
P1 tool은 각각 독립적인 계약·권한·지연 gate를 통과한 뒤 하나씩 공개한다.

### P0-1 — 활성 계약과 레거시 경계 고정

- `DocumentRuntime`과 `DocumentSearchHit`/`DocumentCitation`/Revision UUID를 기준 계약으로 선언한다.
- 활성 FastAPI route와 비활성 MCP/LangGraph import 경계를 contract test로 고정한다.
- 오래된 Finder/ContentSearcher/SQLite test를 현재 adapter test로 이관하고, 대응 제품 기능이 없으면 삭제 사유를 남긴다.
- `mcp`, LangGraph, 모델 SDK 버전을 잠그기 전에 Python 3.11과 현재 FastAPI lifespan 호환성을 확인한다.

완료 기준:

- 활성 앱 import와 OpenAPI 생성이 DB·MinIO·Neo4j·LLM 연결 없이 성공한다.
- 레거시 모델을 복원하지 않고 Bot Core 대상 test가 수집된다.
- MCP 비활성 기본값에서는 `/mcp`가 404이며 기존 REST 계약이 변하지 않는다.

### P0-2 — FastAPI + LangGraph + Mock 수직 슬라이스

- `create_bot_graph(dependencies)` factory로 graph를 만들며 모듈 import 시 client·connection을 생성하지 않는다.
- graph state는 `request_id`, `session_key`, `message`, 선택 `doc_id/revision_id`, `route`, citation ID, 안전한
  error code, 단계별 `timings_ms`만 소유한다.
- 결정론적 Router가 명시적 metadata 요청과 선택 문서 Q&A fast path를 먼저 분기한다. 단순 분기에는 LLM을 쓰지 않는다.
- `RetrieverProtocol`, `ModelGatewayProtocol`, `MetadataToolProtocol`을 주입하고 deterministic fake를 제공한다.
- 첫 연결은 기존 `/api/playground/chat` 응답 계약을 유지하는 내부 orchestration으로 한다. 새 공개 endpoint가 필요하면
  OpenAPI/프런트 계약을 먼저 승인한다.

Mock 수직 슬라이스 인수 기준:

- DB·네트워크·LLM 없이 `metadata -> answer`, `retrieve -> grounded answer`, `no evidence`, `tool timeout`, `model
  timeout` 분기를 반복 가능한 값으로 테스트한다.
- 동일 입력은 동일 route/tool-call 순서를 만들며 step 수와 tool-call 수에 상한이 있다.
- graph import/startup에서 실제 `ChatOpenAI`를 생성하지 않는다.
- 각 node와 전체 `elapsed_ms`, `over_budget`가 응답 또는 내부 비민감 trace에 기록된다.

### P0-3 — Model Gateway 통합

공통 Gateway는 provider 호출과 안전한 구조화 응답만 책임지고, 검색 범위와 citation 선택은 서버 도메인 코드가 책임진다.

- 입력: `purpose`, 서버 설정에서 고른 `model`, system/user payload, Pydantic `response_schema`, `deadline_ms`, 출력
  token 상한. API 요청으로 base URL·API key·provider override를 받지 않는다.
- 출력: 검증된 payload, `provider`, `model`, token usage, `elapsed_ms`, warning. 원문 prompt와 응답 본문은 로그에
  남기지 않는다.
- 오류 코드: `model_not_configured`, `model_url_not_internal`, `model_timeout`, `model_unavailable`,
  `invalid_model_response`, `output_schema_invalid`, `model_budget_exhausted`.
- FastAPI lifespan에서 목적별 adapter가 하나의 재사용 HTTP transport를 공유하고 종료 시 닫는다.
- 우선 이관 순서: 검색어 확장 -> 문서 근거 답변 -> proposal 생성/검증. Embedding은 동일 transport·deadline 정책을
  쓰되 생성 모델과 별도 protocol로 유지할 수 있다.
- D1 배포 smoke에서 선택적 검색어 확장이 실패 fallback 전 6,007.9ms를 소비했고 전체 검색은 6,157.3ms였다.
  P0-3의 첫 인수 항목은 검색어 확장에 남은 절대 deadline을 적용해 즉시 rule-based fallback하고, 목표 초과 시
  `over_budget=true`가 되도록 예산 판정을 단일화하는 것이다.

Citation/Policy 계약:

- Citation은 retrieval 결과로 서버가 조립하고 모델이 UUID, 제목, 위치, excerpt를 새로 만들지 못한다.
- 반환 citation의 `(doc_id, revision_id)` 집합은 요청 시 서버가 재검증한 선택 Revision scope의 부분집합이어야 한다.
- 근거 0건이면 모델 호출 0회이며 `insufficient_evidence`를 반환한다.
- 외부 URL, 요청별 provider/model URL, 내부 예외 문자열, 자격증명은 입력·오류·trace에 노출하지 않는다.

### P0-4 — MCP Metadata Tool + Session Cache + timeout/fallback

#### 첫 tool: `get_document_metadata`

목적은 서버가 발급한 문서 ID로 PostgreSQL의 canonical 활성 Revision 기본 상태만 확인하는 것이다. 검색, 본문 조회,
SMB 순회, artifact 다운로드, 파일 쓰기를 수행하지 않는다.

- 입력: `doc_id: UUID`, `revision_id: UUID | None`. Revision을 생략하면 활성 Revision을 해석하고, 지정하면 활성
  여부를 함께 검증한다.
- 출력: `source="llmops"`, `doc_id`, resolved `revision_id`, `is_active`, `revision_status`, `extension`,
  `size_bytes`, `modified_at`, `elapsed_ms`, `over_budget`, `degraded_dependencies`.
- 제외: 파일명·제목, source URI/item ID, 절대/상대 SMB 경로, 내부 URL, object key, 본문·preview·snippet·excerpt,
  사용자 질의, 자격증명. 후속 필요성이 확인되기 전 이름 정보도 공개하지 않는다.
- 오류: `invalid_arguments`, `metadata_not_configured`, `document_not_found`, `revision_not_found`,
  `revision_stale`, `tool_timeout`, `internal_error`. 내부 예외 문자는 반환하지 않는다.
- annotation: read-only, idempotent, non-destructive, closed-world. MCP 전용 bearer token과 loopback 기본 정책을
  유지하며 remote MCP는 P0 범위 밖이다.
- REST와 MCP가 동일 metadata service/Pydantic 모델을 사용하고 identity/status 결과가 일치해야 한다.

#### Session Cache 범위

Session Cache는 모델 답변 캐시나 의료 문서 캐시가 아니라 **graph 제어 상태와 session 격리**를 위한 프로세스 내
bounded cache다.

- 생명주기: FastAPI lifespan에서 생성·종료하며 재시작 시 전부 사라진다. worker 간 일관성·영속성을 보장하지 않는다.
- key: `surface + 인증 principal fingerprint + session_id`의 서버 비밀 기반 digest. token과 session ID 원문은
  로그·metric label에 남기지 않는다.
- 값: 선택한 `doc_id/revision_id`, graph checkpoint/node, tool 중복 방지 digest, citation/chunk ID, 안전한 오류·지연
  요약만 저장한다. 사용자 문장, 대화 본문, 파일명·경로, title, excerpt, prompt/response, 자격증명은 저장하지 않는다.
- 기본 한도: sliding TTL 15분, absolute TTL 60분, 프로세스당 1,000 session, session당 32 state event 또는
  64 KiB 중 먼저 도달하는 한도, LRU eviction. 설정값에도 hard maximum을 둔다.
- 현재 shared MCP token은 다중 사용자 identity가 아니다. remote/다중 사용자 공개 전 OAuth/SSO 또는 사용자별 principal
  계약을 먼저 확정하며, 그전에는 loopback 단일 사용자 가정을 유지한다.

Session Cache 인수 기준:

- hit/miss, sliding/absolute TTL 만료, LRU/크기 eviction, session/principal 격리, shutdown clear가 clock-controlled
  unit test를 통과한다.
- cache dump/log/metric에 금지 필드가 없고, 같은 `session_id`라도 principal이 다르면 상태를 공유하지 않는다.
- multi-worker에서 cache miss가 정상 동작이며 correctness가 cache 존재에 의존하지 않는다.

#### 공통 timeout/fallback 계약

- 모든 request는 monotonic absolute deadline을 하나 만들고 Router -> retriever/tool -> Gateway로 남은 예산을 전파한다.
- downstream timeout은 `min(설정 timeout, 남은 deadline)`이며 deadline 소진 후 새 외부 호출을 시작하지 않는다.
- metadata/tool executor는 동시 실행 상한을 두고 timeout된 동기 worker가 무한 누적되지 않게 한다.
- 검색어 확장 실패/timeout: 원래 질의의 rule-based term으로 즉시 fallback하고 warning을 남긴다.
- metadata timeout: 부분 metadata를 만들지 않고 `tool_timeout`, `retryable=true`를 반환한다.
- retrieval timeout: 선택 scope가 완전히 검증된 citation만 부분 반환할 수 있다. 검증되지 않으면 근거 없음으로 처리하고
  모델을 호출하지 않는다.
- model timeout/invalid output: 답을 지어내지 않는다. 검증된 citation은 별도 필드로 유지할 수 있으나 안전한 안내와
  안정된 error code를 반환한다.
- fast path 목표는 기존 예산대로 end-to-end p50 300ms, p95 1s, metadata adapter 자체 p95 50ms 이하이다. 모델
  경로가 이를 넘으면 `over_budget=true`와 단계별 지연을 보고하고 숨기지 않는다.

### P1 — Metadata tool 단계적 확장

각 단계는 별도 Pydantic 계약, `allowed_surfaces`, 권한, 결과 최소화, REST parity, timeout과 합성 지연 evidence를
갖춘 뒤 다음 단계로 진행한다.

1. `get_document_metadata` 안정화
2. 활성 Revision 확인/목록: 내부 경로·원문 없이 서버 ID와 상태만 반환
3. 버전 관계 조회: 현재 `DocumentGraphResponse`의 허용된 node/edge만 반환
4. 저장소 상태: 주소 없이 configured/connected/degraded만 반환
5. 선택 범위 Citation 검색: 최대 N건, 서버 검증 scope, 최소 excerpt 또는 excerpt 비공개 profile을 명시

관리자, 인덱싱, 업로드, SMB 쓰기·이동·삭제, artifact 원문 다운로드 tool은 MCP에 공개하지 않는다. 강제로
`allowed_surfaces={"mcp"}`가 선언되어도 executor가 admin/destructive/non-idempotent spec을 차단해야 한다.

## 5. 역할별 책임

| 역할 | 책임 | 완료 산출물 |
|---|---|---|
| Backend | `DocumentRuntime` 기반 service/protocol, graph factory, deterministic fakes, Gateway, metadata service/tool, cache/lifespan, 안전한 error/deadline 계약 | 코드, Pydantic/OpenAPI 계약, latency/security log, rollback flag |
| Frontend | P0 화면 재설계 없음. 기존 citation, degraded/fallback, safe error code, loading/timeout 표시만 계약에 맞춰 점검 | 필요 시 `types.ts`, API client, Chat workspace의 최소 호환 수정과 component test |
| Shared/Product | tool ID·필드 공개 수준, fast path 의도 규칙, session identity 가정, feature flag/rollback, remote MCP 제외를 승인 | 계약 decision record와 WBS/상태 문서 동기화 |
| Test/QA | mock graph 분기, Gateway 오류, scope/citation, REST/MCP parity, cache isolation/eviction, tool 권한, 성능, Docker 합성 smoke | unit/contract/integration/performance evidence와 품질 평가 |

프런트에는 MCP token, endpoint, cache hit/key/capacity, 내부 provider URL을 노출하지 않는다. API 계약 변경은 backend와
frontend가 같은 OpenAPI snapshot을 기준으로 동시에 반영한다.

## 6. 단계별 최종 인수 기준

- 현재 FastAPI 앱·OpenAPI·health와 기존 문서 검색/대화 API가 하위 호환된다.
- mock profile은 외부 설정 없이 graph 전 분기와 Gateway 오류를 재현한다.
- 활성 LLM 호출이 승인된 공통 Gateway를 경유하며 연결 재사용과 단계별 지연 측정이 유지된다.
- citation scope 위반, 모델의 임의 citation, 외부 URL, 원문 log/cache 저장을 테스트가 차단한다.
- MCP 비활성 404, 활성 전용 token, 승인된 read-only tool 목록, 안전한 structured error가 검증된다.
- MCP metadata와 REST 결과의 ID/status parity, adapter p95 50ms 이하가 합성 fixture로 확인된다.
- cache TTL/용량/격리/privacy와 timeout worker 상한 테스트가 통과한다.
- `evaluate_quality.py` hard gate를 통과한 뒤 이미지를 build하고, 성공한 경우에만 healthy container를 교체한다.
- 실제 SMB 쓰기·실데이터·외부 LLM·registry push·원격 배포는 수행하지 않는다.

## 7. 구현 시 검증 명령

PowerShell에서 workspace-local uv cache를 사용한다.

```powershell
$env:UV_CACHE_DIR='.runtime\uv-cache-bot-core'
uv run --no-sync ruff check backend/src backend/tests integrations/langgraph
uv run --no-sync pytest -m "not integration" backend/tests
uv run --no-sync pytest backend/tests/test_llmops_api_contracts.py backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py
uv run --no-sync python scripts/check_development_lessons.py
uv run --no-sync python scripts/evaluate_quality.py
docker compose build automation-smb
docker compose config
```

P0 전용 테스트 파일명은 구현 시 `test_bot_core.py`, `test_model_gateway.py`, `test_document_metadata_tool.py`,
`test_session_cache.py`로 고정하고 위 전체 test 전에 먼저 실행한다. 실제 provider/저장소 검증은 `integration` marker로
분리하며 합성 fixture만 사용한다. build 또는 test 실패 시 기존 healthy container를 유지한다.

## 8. 주요 위험과 rollback

- **계약 역행:** 레거시 FolderIndex/SQLite 복구는 금지하고, 현재 UUID/Revision adapter에서만 기능을 추가한다.
- **지연 악화:** Gateway 계층 추가 전후 p50/p95와 connection 재사용을 비교한다. fast path에 동기 LLM을 강제하지 않는다.
- **민감정보 잔류:** cache·오류·trace에 query/path/excerpt가 들어가면 release gate를 실패시킨다.
- **MCP 과다 공개:** 기본 비활성 + loopback + 별도 token을 유지하고 tool 단위로 rollback한다.
- **기능 rollback:** Bot Core, Gateway, MCP를 독립 feature flag로 끌 수 있어야 하며 기존 REST document path는 유지한다.
