# Bot Main Core 구현 계획

> 기준일: 2026-08-14
>
> 상태: D1(P0-1 + 최소 P0-2) 완료, D2(P0-3a 공통 Gateway·파일 검색 deadline) 완료,
> D3(P0-3b 문서 근거 답변·단일 deadline) 완료, D4(P0-3c proposal Gateway·단일 deadline) 완료,
> D5(P0-4 read-only MCP metadata·timeout) 완료, Session Cache는 소비 계약 확정 전 보류
>
> 기준 실행 경로: `backend/src/smb_finder/api.py`의 FastAPI와 PostgreSQL·MinIO·Neo4j 기반 `DocumentRuntime`

## 1. 결론

활성 제품 기준으로 기본 Flow scaffold, Model Gateway·Citation/오류 흐름, 첫 read-only MCP metadata tool과
timeout/concurrency를 구현했다. Session Cache는 현재 서버가 복원할 다중 턴 상태와 authenticated principal이 없어
사용되지 않는 장치가 되므로, 명시한 재개 조건이 충족될 때까지 구현하지 않는다.

> **금지:** 삭제된 `FolderHit`, `ContentSearchRequest`, `FindRequest`, `FolderIndex` 또는 SQLite 계약을 복원해 오래된
> 테스트만 통과시키지 않는다. 레거시 테스트는 현재 문서 모델 기반 contract test로 이관하거나 제거 사유를 명시한다.

## 2. WBS 구현 감사

| WBS | 활성 제품 판정 | 저장소 증거와 빠진 부분 |
|---|---|---|
| FastAPI·LangGraph 기본 Flow, Router, Mock Retriever/Model | **scaffold 완료** | [`bot_core/`](../backend/src/smb_finder/bot_core/)에 import 부작용 없는 graph factory, 결정론 Router, Retriever/Model/Metadata protocol과 합성 fake가 있고 `DocumentRuntime`의 optional runner로 연결된다. 기존 공개 ChatResponse 15필드는 유지한다. |
| Model Gateway, Citation/Policy, 오류 처리 Flow | **구현** | `DocumentCitation`, `RetrievalScope`, `RetrievalMetadata` 계약과 선택 Revision 범위 SQL은 [`models.py`](../backend/src/smb_finder/models.py), [`llmops_retrieval.py`](../backend/src/smb_finder/llmops_retrieval.py)에 있다. 파일 검색·문서 답변·proposal 생성/검증이 lifespan의 공통 JSON Gateway와 하나의 absolute deadline을 공유한다. Embedding은 별도 protocol을 유지하고 citation은 서버가 검증한다. |
| MCP Metadata Tool, Session Cache, timeout/fallback | **부분 완료** | 공식 MCP SDK v2의 loopback·token 보호 `/mcp`와 bounded executor를 활성 앱 lifespan에 연결했다. `get_document_metadata`는 완료했고 Session Cache는 실제 소비 상태·principal 계약이 생길 때까지 보류한다. |
| MCP Metadata new tool 확장 | **첫 tool 완료** | 공개 목록은 `get_document_metadata` 정확히 1개이며 레거시 `find_folder`/`search_content`와 삭제 모델을 복원하지 않았다. 추가 tool은 승인된 현재 사용 사례가 생길 때 별도 gate로 진행한다. |

검증 기준도 위 판정을 지지한다.

- MCP/tooling/LLMOps/API 집중 계약은 공식 SDK v2 wire, read-only SQL, exact `/mcp`, structured error와 기본 404를
  포함해 통과한다.
- `test_mcp_server.py`와 `test_tooling.py`는 현재 계약으로 이관했고, 나머지 레거시 제거 후보 8개 모듈만 exact list로
  수집하지 않는다.

## 3. 기존 계획과의 정렬

- 상위 [`PRODUCT_DEVELOPMENT_PLAN.md`](PRODUCT_DEVELOPMENT_PLAN.md)의 `P0 — Bot Main Core 계약 복구`를 이 문서의
  실행 순서와 완료 기준으로 구체화한다.
- [`MCP_IMPLEMENTATION_PLAN.md`](MCP_IMPLEMENTATION_PLAN.md)의 `M0–M2 완료` 표기는 현재 활성 코드와 테스트 기준으로
  재판정 대상이다. 보안·전송 설계는 참고하되 레거시 Finder runtime 완료 기록은 승계하지 않는다.
- [`playground_agentic_plan.md`](playground_agentic_plan.md)의 LangGraph Studio는 선택적 개발 도구로 유지한다. 운영
  실행·관측 저장소로 간주하지 않는다.
- [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md)는 현재 `mcp_server.py`/`tooling/`의 단일 metadata 계약과
  비활성 `integrations/langgraph/` 레거시 경계를 구분한다.

## 4. 우선순위와 의존 순서

```text
P0-1 활성 계약/레거시 경계 고정
  -> P0-2 FastAPI + LangGraph + Mock 수직 슬라이스
      -> P0-3 Model Gateway 통합
          -> P0-4 MCP Metadata + timeout 연결
              -> 실제 소비 계약 확인 후 Session Cache 또는 추가 metadata tool 재개
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

P0-3a 완료 범위:

- `OllamaModelGateway`가 내부 URL·절대 deadline·호출별 timeout·Pydantic strict output·token 집계와 안정된 오류 코드를
  공통 계약으로 제공한다. prompt·응답 본문·내부 예외 문자열은 로그·오류에 기록하지 않는다.
- 검색 전체 hard budget은 1,000ms, 선택적 LLM 검색어 확장은 최대 500ms로 제한한다. 같은 absolute
  deadline이 선택 저장소 대기와 PostgreSQL hydration까지 적용되며, 시간을 넘긴 선택 경로는 degraded
  warning과 부분 결과로 즉시 반환한다. 확장 실패·timeout은 원래 질의의 rule-based term으로 fallback한다.
- 단순 키워드 질의는 모델 호출 0회의 fast path로 처리하고, `비슷/유사/similar/related` 등 모호성 marker 또는 긴
  자연어 질의만 선택적 Gateway 확장을 시도한다.
- FastAPI lifespan에서 검색 확장기가 하나의 Gateway HTTP client를 재사용하고 종료 시 닫는다.
- 다음 이관은 proposal 생성/검증이며 별도 회귀와 지연 gate로 옮긴다.

P0-3b 완료 범위:

- API가 만든 하나의 monotonic absolute deadline을 선택 활성 Revision 검증, embedding, scoped PostgreSQL 검색,
  업로드 SMB 원본 읽기·텍스트 추출, 문서 답변 Gateway까지 전달한다.
- PostgreSQL 선택 검증과 scoped retrieval은 남은 시간이 1초 미만이면 새 연결을 시작하지 않는다. 연결 timeout은 남은
  예산과 설정 중 작은 값이며, 연결 직후 main query 전에 `statement_timeout`을 다시 계산한다.
- 업로드 근거 검색은 파일별 SMB 읽기와 추출 전후에 예산을 확인하고, SMB 연결·경로 확인·파일 열기 timeout을 남은
  예산과 설정 중 작은 값으로 제한한다. 검증되지 않은 부분 citation 대신 안전한 budget 오류를 반환한다.
- 근거 0건은 모델을 호출하지 않고, 근거가 있으면 서버가 조립한 citation을 보존한 채 공통 Gateway의
  `document_answer` 목적을 호출한다.

P0-3c 완료 범위:

- proposal 생성·수정·근거 검증은 lifespan의 공통 Gateway를 재사용하고 서버 설정 모델과 typed `num_ctx`만 전달한다.
- 요청 진입에서 만든 하나의 monotonic deadline을 선택 검증, 근거 검색, 생성·보정·검증, workbook 렌더와 SMB 신규
  생성까지 전달한다. 만료 후 되돌릴 수 없는 SMB exclusive commit 전의 registry·검색·모델·렌더·신규 생성은 시작하지
  않는다. exclusive write와 close가 이미 성공했다면 늦은 완료를 경고로 기록하고 성공 응답과 registry 기록을 마친다.
- provider의 413·알려진 context overflow는 본문을 노출하지 않는 안정된 오류로 변환한다. 생성은 context 절반 축소
  1회와 구조 복구 1회를 서로 독립적으로 허용하고, 검증은 재시도 없이 안전한 context 오류를 반환한다.

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
  `tool_timeout`, `internal_error`. 지정 Revision이 현재 active가 아니면 오류로 숨기지 않고 `is_active=false`와 상태를
  반환한다. 내부 예외 문자는 반환하지 않는다.
- annotation: read-only, idempotent, non-destructive, closed-world. MCP 전용 bearer token과 loopback 기본 정책을
  유지하며 remote MCP는 P0 범위 밖이다.
- 기본 `MCP_ENABLED=false`에서는 `/mcp`가 404이고 기존 OpenAPI에는 MCP route나 내부 error metadata가 나타나지 않는다.

#### Session Cache 보류 결정

현재 UI가 대화 history를 소유해 매 요청 전송하고, graph는 checkpointer 없이 compile되며 `session_id`를 복원 상태로
사용하지 않는다. 활성 API에는 사용자 principal 계약도 없다. 따라서 지금 cache를 만들면 소비자가 없는 dead code,
client history와의 이중 source of truth, 또는 shared token 사용자 간 상태 혼선이 된다.

아래 네 조건이 모두 정해질 때 별도 설계로 재개한다.

1. 서버가 다음 요청에서 복원해야 하는 다중 턴 상태가 생긴다.
2. 상태를 격리할 authenticated principal 계약이 생긴다.
3. 저장 허용 state schema와 금지 필드가 확정된다.
4. single/multi-worker cache miss semantics가 정해진다.

재개 전까지 `session_id`는 correlation ID이며 재시작·worker 변경·cache miss가 답변 정확성에 영향을 주지 않는다.
cache 파일·설정·metric·UI도 추가하지 않는다.

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

현재 승인된 P1 tool은 없다. 아래 후보는 실제 consumer와 공개 필드가 승인된 경우에만 하나씩 별도 Pydantic 계약,
결과 최소화, timeout과 합성 지연 evidence를 갖춘 뒤 진행한다. 범용 registry나 provider framework를 미리 만들지 않는다.

1. `get_document_metadata` 안정화
2. 활성 Revision 확인/목록: 내부 경로·원문 없이 서버 ID와 상태만 반환
3. 버전 관계 조회: 현재 `DocumentGraphResponse`의 허용된 node/edge만 반환
4. 저장소 상태: 주소 없이 configured/connected/degraded만 반환
5. 선택 범위 Citation 검색: 최대 N건, 서버 검증 scope, 최소 excerpt 또는 excerpt 비공개 profile을 명시

관리자, 인덱싱, 업로드, SMB 쓰기·이동·삭제, artifact 원문 다운로드 tool은 MCP에 공개하지 않는다.

## 5. 역할별 책임

| 역할 | 책임 | 완료 산출물 |
|---|---|---|
| Backend | `DocumentRuntime` 기반 service/protocol, graph factory, deterministic fakes, Gateway, metadata service/tool/lifespan, 안전한 error/deadline 계약 | 코드, Pydantic/MCP 계약, latency/security log, 기본 비활성 gate |
| Frontend | P0 화면 재설계 없음. 기존 citation, degraded/fallback, safe error code, loading/timeout 표시만 계약에 맞춰 점검 | 필요 시 `types.ts`, API client, Chat workspace의 최소 호환 수정과 component test |
| Shared/Product | tool ID·필드 공개 수준, fast path 의도 규칙, Session Cache 재개 조건, remote MCP 제외를 승인 | 계약 decision record와 WBS/상태 문서 동기화 |
| Test/QA | mock graph 분기, Gateway 오류, scope/citation, MCP wire·deadline·권한, 성능, Docker 합성 smoke | unit/contract/integration/performance evidence와 품질 평가 |

프런트에는 MCP token, endpoint, cache hit/key/capacity, 내부 provider URL을 노출하지 않는다. API 계약 변경은 backend와
frontend가 같은 OpenAPI snapshot을 기준으로 동시에 반영한다.

## 6. 단계별 최종 인수 기준

- 현재 FastAPI 앱·OpenAPI·health와 기존 문서 검색/대화 API가 하위 호환된다.
- mock profile은 외부 설정 없이 graph 전 분기와 Gateway 오류를 재현한다.
- 활성 LLM 호출이 승인된 공통 Gateway를 경유하며 연결 재사용과 단계별 지연 측정이 유지된다.
- citation scope 위반, 모델의 임의 citation, 외부 URL, 원문 log/cache 저장을 테스트가 차단한다.
- MCP 비활성 404, 활성 전용 token, 승인된 read-only tool 목록, 안전한 structured error가 검증된다.
- MCP metadata adapter p95 50ms 이하와 timeout worker 상한이 합성 fixture로 확인된다.
- Session Cache 파일·설정·metric·UI가 없고 답변 correctness가 cache 존재에 의존하지 않는다.
- `evaluate_quality.py` hard gate를 통과한 뒤 이미지를 build하고, 성공한 경우에만 healthy container를 교체한다.
- 실제 SMB 쓰기·실데이터·외부 LLM·registry push·원격 배포는 수행하지 않는다.

## 7. 구현 시 검증 명령

PowerShell에서 workspace-local uv cache를 사용한다.

```powershell
$env:UV_CACHE_DIR='.runtime\uv-cache-bot-core'
uv run --no-sync ruff check backend/src backend/tests
uv run --no-sync pytest -m "not integration" backend/tests
uv run --no-sync pytest backend/tests/test_llmops_api_contracts.py backend/tests/test_llmops_search.py backend/tests/test_llmops_retrieval.py
uv run --no-sync python scripts/check_development_lessons.py
uv run --no-sync python scripts/evaluate_quality.py
docker compose build automation-smb
docker compose config
```

P0 전용 계약은 `test_bot_core.py`, `test_model_gateway.py`, `test_mcp_server.py`, `test_tooling.py`에서 먼저 실행한다.
실제 provider/저장소 검증은 `integration` marker로 분리하며 합성 fixture만 사용한다. build 또는 test 실패 시 기존
healthy container를 유지한다.

## 8. 주요 위험과 rollback

- **계약 역행:** 레거시 FolderIndex/SQLite 복구는 금지하고, 현재 UUID/Revision adapter에서만 기능을 추가한다.
- **지연 악화:** Gateway 계층 추가 전후 p50/p95와 connection 재사용을 비교한다. fast path에 동기 LLM을 강제하지 않는다.
- **민감정보 잔류:** 오류·trace·MCP 결과에 query/path/excerpt가 들어가면 release gate를 실패시킨다.
- **MCP 과다 공개:** 기본 비활성 + loopback + 별도 token을 유지하고 tool 단위로 rollback한다.
- **기능 rollback:** MCP는 기본 비활성 gate로 즉시 분리할 수 있고 기존 REST document path와 OpenAPI는 유지한다.
