# MCP 문서 메타데이터 도구 구현 계획

> 상태: **첫 수직 슬라이스 구현 완료**
>
> 기준일: 2026-08-14
>
> 상위 계획: [Bot Main Core 구현 계획](BOT_MAIN_CORE_IMPLEMENTATION_PLAN.md)

## 1. 목적과 현재 결정

현재 MCP의 목적은 챗봇이 선택한 문서의 식별자와 revision 상태를 안전하고 빠르게 확인하는 것이다. 공개 도구는
`get_document_metadata` 하나로 제한한다. 사용되지 않는 범용 catalog, REST 대응 endpoint, 원격 인증 체계 또는
미래 도구용 추상화는 만들지 않는다.

Session Cache는 구현하지 않는다. 현재 대화 이력은 프런트엔드가 요청마다 전달하고, 서버 graph에는 복원할 상태와
인증된 사용자 주체가 없기 때문이다. 지금 cache를 추가하면 사용되지 않는 코드이거나 이력의 이중 진실 원천이 된다.

## 2. 구현 범위

- 공식 MCP Python SDK v2의 Streamable HTTP를 기존 FastAPI lifespan에 연결한다.
- `MCP_ENABLED=false`가 기본이며 이때 `/mcp`는 404다.
- 활성화할 때는 별도 bearer token이 필요하고 loopback 요청만 허용한다.
- `get_document_metadata(doc_id, revision_id?)`는 PostgreSQL의 현재 `documents`와 `document_revisions`를 한 번의
  read-only query로 조회한다.
- `revision_id`를 생략하면 active revision을 해석하고, 지정하면 해당 revision의 존재와 active 여부를 반환한다.
- bounded executor가 timeout과 최대 동시 실행 수를 집행한다.
- `/mcp`만 정확히 노출하며 `/mcp/` redirect와 다른 하위 경로는 허용하지 않는다.

범위 밖:

- 삭제된 `FolderHit`, `ContentSearchRequest`, SQLite/직접 SMB 검색 계약 복원
- `find_folder`, `search_content`, write/admin/index/download 도구
- REST parity endpoint와 프런트엔드 MCP 관리 화면
- remote MCP, OAuth/SSO, 사용자별 ACL
- 범용 provider/도구 registry, retry framework, feature flag 계층

## 3. 공개 계약

입력:

- `doc_id`: UUID, 필수
- `revision_id`: UUID, 선택

출력은 다음 필드만 허용한다.

- `source`
- `doc_id`
- `revision_id`
- `is_active`
- `revision_status`
- `extension`
- `size_bytes`
- `modified_at`
- `elapsed_ms`
- `over_budget`
- `degraded_dependencies`

filename, title, path, URI, object key, 본문, snippet, excerpt, 원본 query, credential은 SQL SELECT와 출력에서 모두
제외한다. 오류는 `isError=true`와 안전한 메시지, namespaced `_meta`의 `code`, `retryable`, `elapsed_ms`로 반환하며
내부 예외 상세는 노출하지 않는다.

## 4. 설정과 lifecycle

| 설정 | 기본값 | 역할 |
|---|---:|---|
| `MCP_ENABLED` | `false` | `/mcp` 활성화 여부 |
| `MCP_API_TOKEN` | 빈 값 | 활성화 시 필수인 전용 bearer token |
| `MCP_METADATA_TIMEOUT_MS` | `1500` | metadata 전체 실행 예산 |
| `MCP_METADATA_MAX_CONCURRENCY` | `2` | 동시 metadata worker 상한 |

`.env`와 `.env.*`는 이 구현에서 수정하지 않는다. 활성화 시 FastAPI lifespan이 MCP session manager와 bounded
executor를 소유하며, 종료 시 새 작업을 막고 실행 중 작업을 정리한 뒤 PostgreSQL adapter를 닫는다.

## 5. Session Cache 보류 조건

다음 조건이 모두 확정될 때 별도 수직 슬라이스로 다시 설계한다.

1. 다음 요청에서 서버가 복원해야 하는 실제 multi-turn 상태가 생긴다.
2. 안정적인 authenticated principal 계약이 생긴다.
3. 저장을 허용할 state schema가 확정된다.
4. single/multi-worker cache miss 의미가 정해진다.

그전까지 `session_id`는 correlation ID이며 프로세스 재시작, worker 변경, cache miss가 응답 정확성에 영향을 주지
않아야 한다. cache 설정, metric, 관리 API와 UI도 추가하지 않는다.

## 6. 검증 기준

- 기본 `/mcp` 404, 활성화된 정확한 `/mcp`는 redirect 없이 initialize 성공
- `tools/list`에 `get_document_metadata` 정확히 하나
- UUID 입력 검증, active resolve, 지정 revision의 active/stale 상태, not-found 오류
- read-only SQL과 금지 필드 비선택·비노출
- loopback/Host/bearer 거절과 안전한 MCP 오류 직렬화
- timeout 후 worker 슬롯 누적 방지, 최대 동시 실행 수와 shutdown 정리
- 공개 REST/OpenAPI와 프런트엔드 계약 변화 없음
- legacy MCP/tooling 테스트를 현재 계약으로 교체하고 quarantine을 10개에서 8개로 축소

재현 명령:

```powershell
uv run --no-sync ruff check backend/src/smb_finder/mcp_server.py backend/src/smb_finder/tooling backend/src/smb_finder/llmops_search.py backend/tests/test_mcp_server.py backend/tests/test_tooling.py backend/tests/test_llmops_search.py
uv run --no-sync pytest backend/tests/test_mcp_server.py backend/tests/test_tooling.py backend/tests/test_llmops_search.py backend/tests/test_api_contracts.py
uv run --no-sync pytest -m "not integration" backend/tests
uv run --no-sync python scripts/evaluate_quality.py
```

## 7. 후속 확장 원칙

새 MCP 도구는 실제 소비자와 승인된 사용자 흐름이 생긴 뒤 하나씩 추가한다. 추가할 때마다 현재 목적에 필요한지,
기존 query/service를 재사용하는지, 민감 필드를 넓히지 않는지, 별도 설정·추상화·UI가 정말 필요한지를 code-reviewer가
확정 전 검토한다. Session Cache와 remote MCP는 위 조건이 충족되기 전에는 완료 항목으로 세지 않는다.
