# Dataset DB 연동 계획

- 작성 기준일: 2026-08-04
- 검토 기준: `mageAI_project`의 LLMOps Data Pipeline DB·챗봇 연동·버전 비교·파일 명명 문서
- 적용 대상: `automation_smb`의 RAG 검색, Playground API/tool, 문서·버전 조회 경계
- 상태: LLM 멀티스토어 검색·선택 범위 Citation 대화·MinIO Preview·Neo4j 버전 관계 수직 슬라이스 구현

## 1. 결정 요약

`automation_smb`는 LLMOps Data Pipeline이 만든 데이터셋을 새로 적재하거나
변경하지 않고, 별도의 read-only adapter를 통해 조회한다. PostgreSQL
`llmops` Schema를 문서 상태와 최종 활성 UUID의 기준 시스템으로 삼는다. 파일 찾기에서는 온프레미스 Ollama가 검색어를
확장하고 PostgreSQL 메타데이터/Chunk, MinIO Artifact key, Neo4j 문서/Revision label을 함께 조회한다.

Playground의 현재 파일 선택 hot path는 다음처럼 유지한다.

```text
좌측 파일 찾기 → local LLM 질의 확장
               → PostgreSQL·MinIO·Neo4j read-only 후보 병렬 조회
               → PostgreSQL active UUID hydrate·중복 통합 → 결과 선택
               → selected_files(doc_id, revision_id)
               → 선택 UUID 범위 Hybrid/RRF Chunk 검색 → Citation 대화
명시적 보기    → read-only MinIO Preview/Canonical 중계
명시적 버전    → read-only Neo4j 버전 관계
```

- Langflow 연동, Langflow source 동기화, Langflow tool import는 범위에서 제외한다.
- 파일 검색 요청은 세 저장소를 bounded parallel query로 조회하며 저장소별 timeout과 부분 실패 경고를 반환한다.
- 기존 `search_rag_chunks`는 즉시 삭제하지 않고 호환 진입점으로 유지한 뒤,
  내부 구현을 데이터셋 adapter로 교체한다.
- 제공된 설정을 메모리에만 읽어 PostgreSQL·MinIO·Neo4j의 read-only 연결과 계약을 검증했다.
  자격증명·내부 주소는 저장소나 문서에 기록하지 않았고 Schema Migration은 수행하지 않는다.
- 이 저장소의 합성 데이터 전용 원칙을 유지한다. 실제 의료·환자·검사 데이터가
  포함된 환경 연결은 이 계획의 승인이 아니라 별도 운영 전환 검토 대상이다.

## 2. 검토한 데이터셋 계약

상위 데이터셋 문서에서 확인한 기준은 다음과 같다.

| 영역 | 확인된 계약 | 이 프로젝트의 해석 |
|---|---|---|
| 기준 원장 | PostgreSQL DB `llmops_document_bot`, Schema `llmops` | 검색·문서 상태·버전·비교 Metadata의 기준 |
| Object | MinIO Bucket `llmops-document-bot` | Raw/Canonical/Preview/Diff를 Backend가 읽고 Browser에는 URI를 숨김 |
| Graph | Neo4j `space_id=llmops_document_bot` | `LATEST`, `SUPERSEDES`, Section/Chunk 구조의 보조 조회 |
| 활성 검색 | `documents.deleted_at IS NULL`, `document_chunks.active=true` | 모든 Vector/Lexical 후보에 동일 적용 |
| 검색 | pgvector + FTS + `pg_trgm`, RRF 결합 | 현재 vector-only SQL의 목표 교체 계약 |
| 문서 식별 | `document_key`, `doc_id` | 논리 문서 Identity. 날짜·업무 Version과 분리 |
| Source 파일 | `source_file_id`, `source_version`, `document_date` | NAS의 물리 파일과 사용자 업무 Version |
| 처리 이력 | `revision_id`, `revision_number`, `supersedes_revision_id` | Parser/Embedding 재처리까지 포함한 기술 Revision |
| 근거 | `chunk_id`, `element_ids`, `section_path`, `citation_anchor` | 답변 Citation의 기준. 임의 경로 문자열을 Citation으로 사용하지 않음 |
| 비교 | `revision_comparisons` + MinIO Diff JSON | 저장된 비교를 우선하며 Revision 원본은 불변 |

상위 문서의 PostgreSQL Table·Extension, MinIO Preview Object, Neo4j Revision Graph를 승인된 read-only 연결로
재확인했다. Neo4j는 구성된 논리 Database 이름이 서버에 없을 때 `graph_space` 모드의 기본/home Database를 사용하며
`neo4j_logical_database_ignored_for_graph_space` 경고를 반환한다.

## 3. 현재 계약과 목표 계약

| 항목 | 현재 `automation_smb` | 목표 |
|---|---|---|
| DB 설정 | `RAG_DB_*`, 기본 DB명이 기존 로컬 RAG DB에 고정 | `POSTGRES_*` 연결 + `LLMOPS_POSTGRES_DB`/`LLMOPS_POSTGRES_SCHEMA`, 명시적 feature flag |
| ID Type | `document_id`, `chunk_id`가 `int` | `doc_id`, `revision_id`, `chunk_id`가 UUID |
| SQL 대상 | Schema 없는 `documents`, `document_chunks` | `llmops.documents`, `llmops.document_revisions`, `llmops.document_chunks` |
| 공개 상태 | `documents.deleted_at IS NULL`만 검사 | 삭제되지 않은 문서 + Active Revision + Active Chunk를 함께 검사 |
| 검색 방식 | pgvector cosine 단일 정렬 | Vector/FTS/Trigram 후보에 동일 Scope 적용 후 RRF 결합 |
| ACL | 없음 | Principal Scope를 SQL 후보 생성 전에 적용. 문서 상세·버전·Artifact에도 동일 적용 |
| Citation | 여러 평면 위치 필드와 `file_path` | `section_path: list[str]` + 구조화된 `citation_anchor`; 내부 경로는 비공개 |
| 응답 Score | `similarity` 한 개 | `vector`, `lexical`, `trigram`, `rrf`와 공개 가능한 최종 Score |
| 문서 Version | 검색 응답에 없음 | 업무 `source_version`과 기술 `revision_number`를 분리해 반환 |
| Artifact | 없음 | ACL 확인 후 Backend가 MinIO에서 read-only 조회, Object URI는 미노출 |
| Graph | 없음 | 파일 후보 label 검색과 선택 버전 기능. 장애 시 다른 저장소 부분 결과 유지 |
| Playground | 좌측 파일 검색 결과를 선택해 `selected_files`로 채팅에 전달 | 선택 범위 Hybrid/RRF, 구조화 Citation/검색 요약, Artifact/Graph link를 반환 |
| 오류 | 넓은 `rag_db_unavailable` | 잘못된 요청·권한·timeout·dependency·계약 불일치를 구분한 안전한 오류 |

LLMOps 선택 문서 경로는 별도 adapter로 분리되어 있으며 모든 PostgreSQL transaction은 읽기 전용이다. DB·Embedding·
MinIO·Neo4j 호출에는 각각 timeout을 두고, 저장소 연결 실패를 로컬 검색 성공으로 숨기지 않는다. bounded connection
pool은 warm 반복 측정에서 연결 비용이 병목으로 확인될 때 도입한다.

## 4. Table과 ID Mapping

### 4.1 Table 책임

| PostgreSQL Table | adapter가 읽을 필드 | 사용 기능 |
|---|---|---|
| `llmops.documents` | `doc_id`, `document_key`, 조직/도메인, `title`, `active_revision_id`, `acl`, `deleted_at` | 문서 Identity, Scope, ACL, 최신 Revision |
| `llmops.document_source_files` | `source_file_id`, `doc_id`, `source_version`, Version 숫자, `document_date`, `file_name`, `status` | 물리 파일 이력과 업무 Version 선택 |
| `llmops.document_revisions` | `revision_id`, `doc_id`, `revision_number`, `supersedes_revision_id`, 업무 Version, 품질·Parser Metadata, Artifact URI | Revision 목록과 Active 상태 검증 |
| `llmops.canonical_elements` | `element_id`, `revision_id`, `element_order`, `element_type`, `section_path`, `location` | 단일 Version Snapshot과 비교 근거 |
| `llmops.document_chunks` | `chunk_id`, `revision_id`, `doc_id`, `chunk_order`, `element_type`, `section_path`, `citation_anchor`, `content`, `search_text`, `acl`, `embedding`, `active` | Hybrid 검색과 Citation |
| `llmops.revision_comparisons` | Revision pair, 알고리즘 버전, 분류별 건수, 유사도, `diff_artifact_uri` | 저장된 Version 비교 요약 |
| `llmops.acl_entries` | 초기 단계에는 사용하지 않음 | 상위 문서상 Runtime은 현재 JSON ACL을 사용하므로 성급히 전환하지 않음 |

`sources`, `source_sync_state`, `processing_runs`, `processing_errors`,
`graph_outbox`, `artifacts`는 초기 사용자 질의 hot path에 Join하지 않는다.
운영 상태 화면이 필요할 때 별도 endpoint와 시간 예산으로 추가한다.

### 4.2 현재 모델에서 목표 모델로의 Mapping

| 현재 필드 | 목표 필드 | 변환 원칙 |
|---|---|---|
| `document_id: int` | `doc_id: UUID` | 숫자 호환 변환 금지. DB UUID를 그대로 사용 |
| `chunk_id: int` | `chunk_id: UUID` | Pydantic에서 UUID 검증 |
| 없음 | `revision_id: UUID` | 모든 근거가 어느 Revision인지 필수 반환 |
| `chunk_index` | `chunk_order` | DB 순번을 그대로 반환 |
| `file_name` | `title`, `source_file_name` | 화면 제목과 물리 파일명을 분리 |
| `file_path` | `source_ref`, `open_url` | 내부 SMB/MinIO URI를 반환하지 않고 opaque reference와 Backend route 사용 |
| `section_path: str` | `section_path: list[str]` | PostgreSQL `TEXT[]`를 보존 |
| `page_*`, `slide_*`, `sheet_name` | `citation_anchor` | page/slide/sheet/cell/line/bounding box의 구조 보존 |
| `similarity` | `scores.vector` | Vector Score와 RRF 최종 Score를 혼용하지 않음 |
| 없음 | `source_version`, `revision_number` | 사용자 Version과 기술 Revision을 동시에 표시 |
| 없음 | `document_key`, `identity_strategy` | 조직 계층/호환 Key/URI 전략을 UI에 설명 가능하게 제공 |

## 5. read-only adapter 경계

현재 구현 모듈 경계는 다음과 같다.

```text
backend/src/smb_finder/
  models.py             # 저장소 독립 Pydantic 요청/응답
  llmops_search.py      # PostgreSQL 파일 후보 검색
  llmops_retrieval.py   # 선택 UUID 범위 Hybrid/RRF와 Citation
  llmops_artifacts.py   # MinIO get/stat 전용 adapter
  llmops_graph.py       # Neo4j read-only Graph adapter
  playground/api.py    # 안전한 domain error → HTTP error Mapping
  playground/agent.py  # no-answer, 근거 합성, Citation 조립
```

adapter가 지켜야 할 규칙:

1. PostgreSQL Session에 `default_transaction_read_only=on`과
   `statement_timeout`을 강제한다.
2. SQL은 코드에 정의한 매개변수화된 Allowlist Query만 실행한다. 사용자 질문,
   필터 또는 LLM 출력으로 SQL/Cypher를 만들지 않는다.
3. `SET LOCAL hnsw.iterative_scan = strict_order`는 pgvector Version 확인 후
   검색 Transaction 안에서만 적용한다.
4. MinIO는 `stat/get`만 허용하고 Put/Delete, Presigned URL, Credential·Object
   URI 노출을 금지한다.
5. Neo4j는 read-only session query와 필수 `space_id` 조건을 사용한다. Graph 실패는
   검색 실패로 승격하지 않는다.
6. 모든 저장소 adapter는 application 시작/종료 시 자원을 재사용·정리하고,
   동시 실행 수를 bounded pool/semaphore로 제한한다.
7. Browser와 LLM에는 내부 Source URI, 절대 경로, ACL 원문, Credential을
   전달하지 않는다. LLM에는 선택된 Chunk 본문과 Citation 표시값만 전달한다.

## 6. 제안 Pydantic 계약

아래 모델명과 필드는 구현 시의 목표 Shape다. 기존 `ChatResponse` 필드는 한
단계 동안 유지해 UI와 테스트의 호환성을 보장한다.

```python
class PrincipalContext(BaseModel):
    subject_id: str
    groups: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    review_all_synthetic: bool = False


class DatasetSearchFilters(BaseModel):
    doc_ids: list[UUID] = Field(default_factory=list, max_length=100)
    document_types: list[str] = Field(default_factory=list, max_length=20)
    approval_statuses: list[str] = Field(default_factory=list, max_length=20)


class DatasetSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=20)
    candidate_k: int = Field(default=40, ge=1, le=200)
    filters: DatasetSearchFilters = Field(default_factory=DatasetSearchFilters)


class CitationAnchor(BaseModel):
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    bounding_box: tuple[float, float, float, float] | None = None


class RetrievalScores(BaseModel):
    vector: float | None = None
    lexical: float | None = None
    trigram: float | None = None
    rrf: float


class DatasetCitation(BaseModel):
    index: int
    doc_id: UUID
    revision_id: UUID
    chunk_id: UUID
    title: str
    source_version: str | None = None
    revision_number: int
    section_path: list[str] = Field(default_factory=list)
    location: CitationAnchor
    excerpt: str
    open_url: str | None = None
    scores: RetrievalScores


class DatasetSearchResponse(BaseModel):
    trace_id: UUID
    query: str
    hits: list[DatasetCitation]
    result_count: int
    candidate_count: int
    grounded: bool
    timings_ms: dict[str, float]
    elapsed_ms: float
    over_budget: bool
    degraded_dependencies: list[str] = Field(default_factory=list)
```

Version 기능에는 별도 `DocumentVersionSummary`, `DocumentSourceFileSummary`,
`CanonicalSnapshot`, `RevisionComparisonResponse` 모델을 둔다.

- `DocumentVersionSummary`: `revision_id`, `revision_number`, `source_version`,
  `document_date`, `source_file_name`, `status`, `published_at`
- `DocumentSourceFileSummary`: `source_file_id`, `source_version`,
  Version 숫자 구성요소, `document_date`, `file_name`, `status`
- `RevisionComparisonResponse`: 기준/대상 UUID와 순번, 업무 Version, 분류별 건수,
  `similarity_score`, `comparison_source`, `persisted`, 변경 Element 배열
- `ChatResponse` 추가 필드: `citations: list[DatasetCitation]`,
  `retrieval: DatasetSearchResponse | None`; 기존 `rag_grounding`은 호환 기간 유지

## 7. 제안 API와 tool 계약

| Method/Path | 목적 | 의존 저장소 | 실패 시 동작 |
|---|---|---|---|
| `POST /api/search` | 구조화된 Hybrid 검색 | PostgreSQL + Embedding | 근거 없음은 200/빈 hits, 의존성 장애는 503/504 |
| `POST /api/playground/chat` | 기존 Playground 실행 | 검색 + 선택한 LLM | 검색 근거가 부족하면 답변 보류, 구조화 Citation 포함 |
| `GET /api/documents/{doc_id}/versions` | Revision 목록 | PostgreSQL | 권한 없음과 미존재를 같은 404로 처리 |
| `GET /api/documents/{doc_id}/source-files` | 물리 파일/업무 Version 이력 | PostgreSQL | 실패를 Versions 없음으로 바꾸지 않음 |
| `GET /api/documents/{doc_id}/versions/{revision_id}/snapshot` | 단일 Canonical Snapshot | PostgreSQL | `revision_id`의 문서 소속 검증 |
| `GET /api/documents/{doc_id}/compare` | 최신 저장 비교 또는 선택 pair | PostgreSQL, 선택적으로 MinIO | Phase 2는 저장 pair만, Artifact 장애는 안전한 503 |
| `GET /api/documents/{doc_id}/preview/{revision_id}` | 권한 검증된 Preview | PostgreSQL → MinIO | URI 대신 Backend streaming 응답 |
| `GET /api/documents/{doc_id}/graph` | Version/구조 Graph | Neo4j | 503 또는 `degraded`, 채팅 검색에는 영향 없음 |

Playground tool은 장기적으로 `search_dataset_chunks`를 표준 ID로 삼되,
`search_rag_chunks` 요청을 같은 handler로 Mapping해 기존 저장된 UI 설정과
negative API 회귀 테스트를 깨지 않는다. 삭제된 Langflow endpoint는 계속
404여야 하며 재도입하지 않는다.

공통 오류 응답은 다음 필드를 가진다.

```json
{
  "code": "dataset_query_timeout",
  "message": "문서 검색 시간 예산을 초과했습니다.",
  "retryable": true,
  "trace_id": "uuid",
  "elapsed_ms": 901.2
}
```

권장 오류 Mapping:

| HTTP | code | 의미 |
|---|---|---|
| 400/422 | `invalid_dataset_query`, `invalid_revision_pair` | 입력·Pydantic 계약 오류 |
| 404 | `document_not_found` | 문서 미존재 또는 비인가. 열거 방지 위해 구분하지 않음 |
| 409 | `dataset_contract_mismatch` | 예상 Schema/Embedding 차원/API 계약 불일치 |
| 503 | `dataset_db_unavailable`, `artifact_store_unavailable`, `graph_unavailable` | 의존성 장애 |
| 504 | `dataset_query_timeout` | 전체 검색 deadline 초과 |

오류 message와 로그에는 SQL, 연결 문자열, 내부 host, SMB/MinIO 경로, ACL
Principal 전체를 포함하지 않는다.

## 8. 지연과 timeout 예산

멀티스토어+LLM 파일 검색 목표는 warm 상태 p50 1.5초 미만, p95 3초 미만이며 hard budget은 8초다. 단순
PostgreSQL fallback 검색은 기존 p95 1초 목표를 유지한다. 모든 단계는 bounded timeout과 상위 N건 제한을 둔다.

| 단계 | 권장 hard timeout | 관측 필드 |
|---|---:|---|
| Pool 획득/DB 연결 | 200ms | `pool_wait_ms`, `db_connect_ms` |
| 질의 Embedding | 400ms | `embedding_ms` |
| 검색어 확장 local LLM | 6,000ms hard | `llm_query_expansion` |
| PostgreSQL 파일 Query | 1,500ms | `postgresql` |
| MinIO Registry/Object 확인 | 1,000ms | `minio` |
| Neo4j label Query | 1,000ms | `neo4j` |
| 결과/Citation 조립 | 100ms | `assembly_ms` |
| 파일 검색 전체 | 8,000ms hard | `elapsed_ms`, `over_budget` |
| MinIO Preview/Diff | 500ms | `artifact_ms` |
| Neo4j Graph | 400ms | `graph_ms`, `degraded_dependencies` |

- `candidate_k`와 `top_k`에 상한을 두고 결과 본문 글자 수도 제한한다.
- 파일 검색은 문서 본문 없이 사용자 질의만 온프레미스 LLM으로 확장하고, 실패하면 기본 정규화 검색어로 fallback한다.
- 채팅은 검색 fast path를 먼저 실행하고, 근거가 없으면 생성 LLM을 호출하지
  않는다. 생성이 예산을 넘길 가능성이 있으면 extractive 답변 또는 후속
  streaming 계약을 별도 결정한다.
- 파일 검색에서는 Graph label과 Artifact Object key/존재 여부만 조회하며, 본문·원본은 사용자가 화면을 열 때만 읽는다.
- timeout 뒤 실행이 계속 누적되지 않도록 pool과 동시성 상한을 함께 검증한다.

## 9. ACL과 Citation 규칙

### ACL

1. 인증 계층이 만든 `PrincipalContext`만 service에 전달한다. Browser Header를
   운영 신원으로 직접 신뢰하지 않는다.
2. Vector, FTS, Trigram 후보 모두 같은 ACL과 `doc_ids` Scope를 SQL 후보 생성
   전에 적용한다.
3. 문서 목록·버전·Snapshot·비교·Preview·Graph가 같은 문서 접근 함수를
   공유한다.
4. JSON ACL과 정규화 `acl_entries` 중 어느 것이 Runtime 기준인지 adapter
   설정으로 갈라 두지 않는다. 상위 문서의 현재 계약대로 초기에는
   `documents.acl`/`document_chunks.acl`을 사용하고, 전환은 별도 Migration
   확인 후 진행한다.
5. 합성 데이터 전체 검수 mode가 필요하면 명시적 로컬 시작 option으로만
   허용하고 응답에 `review_mode=true`를 표시한다. 운영 기본은 fail-closed다.

### Citation

1. 모든 답변 근거에는 `doc_id`, `revision_id`, `chunk_id`를 포함한다.
2. `section_path`와 `citation_anchor`를 구조적으로 보존한다.
3. 기본 검색은 ACTIVE Revision만 반환한다. Version 차이 질문은 일반 검색으로
   추정하지 않고 비교 endpoint/tool을 우선한다.
4. 내부 `source_uri`, MinIO Object URI, 절대 경로는 응답·Prompt에서 제거한다.
5. 원문 열기는 opaque ID 기반 Backend route를 사용하고 그 route에서 ACL을
   다시 확인한다.
6. 주요 주장에 근거 번호를 연결할 수 없으면 `grounded=false`와 no-answer를
   반환한다.

## 10. 단계별 구현 계획

### Phase 0 — 계약 승인과 fixture 동결 (완료)

- 이 문서의 ID/API/ACL 노출 정책을 승인한다.
- 실제 연결 없이 UUID, 업무 Version, Revision, ACL, Citation, 비교 pair를 가진
  일반 합성 fixture와 expected response를 만든다.
- 기존 `/api/playground/chat`, `search_rag_chunks`, 삭제된 Langflow route의
  negative regression을 동결한다.

### Phase 1 — 파일 검색 모델과 PostgreSQL read adapter (완료)

- `DocumentSearchRequest/Hit/Response`와 `LlmopsFileSearcher`를 추가했다.
- `POST /api/playground/files/search`가 Active Revision과 Active Chunk를 읽기 전용으로 검색한다.
- 선택한 UUID `doc_id`·`revision_id`를 채팅 전에 다시 검증한다.
- DB가 구성되지 않았거나 응답하지 않으면 `503`을 반환하며 로컬 fixture를 성공 결과로 사용하지 않는다.
- 파일 검색은 파일명·제목·본문 신호를 PostgreSQL에서 조회하고 안정적인 UUID를 반환한다.

### Phase 1.5 — LLM 멀티스토어 후보 통합 (완료)

- 온프레미스 Ollama가 원문 질의만 받아 파일명·부서명·문서 종류 동의어를 구조화 JSON으로 확장한다.
- PostgreSQL·MinIO·Neo4지를 bounded thread pool로 병렬 조회하고 `queried_stores`, `matched_stores`, 단계별 지연을 반환한다.
- MinIO·Neo4j 후보는 PostgreSQL active `(doc_id, revision_id)`로 다시 hydrate하며 내부 Object URI는 응답하지 않는다.
- 선택 저장소 장애는 경고와 부분 결과로 반환하고 PostgreSQL 기준 원장 장애는 명시적 503으로 처리한다.

### Phase 2 — Playground 선택 문서 근거 대화 (완료)

- 좌측 선택 결과를 중앙 `selected_files` 컨텍스트로 전달하는 연결은 완료했다.
- 선택된 1~5개 `(doc_id, revision_id)`를 Vector·FTS·Trigram 후보 모두에 강제하고 RRF로 결합한다.
- `ChatResponse`에 구조화 Citation, 검색 요약, Artifact/Graph link를 추가했다.
- 선택 범위 밖 근거는 응답 전 다시 차단하며 근거가 없으면 생성 LLM을 호출하지 않는다.

### Phase 3 — MinIO Artifact read adapter (완료)

- 활성 문서·Revision 검증 뒤 Preview/Canonical만 읽는다.
- Object 크기와 허용 Artifact 종류를 제한한다.
- Upload/Delete/Presigned URL은 구현하지 않는다.

### Phase 4 — 선택적 Neo4j Graph (완료)

- `graph_space` mode에서 필수 `space_id`가 있는 read query만 실행한다.
- 버전 Graph API와 파일 상세 UI에만 연결했다.
- Neo4j 장애·논리 Database 경고는 PostgreSQL 검색을 막지 않는 degraded 상태로 반환한다.

### Phase 5 — 운영 전환

- SSO Principal Mapping, 감사 로그 최소화, 비밀 주입, 운영 인증서를 검토한다.
- 실제 데이터 연결 전 별도의 보안·개인정보·부하 검증과 사용자 승인을 받는다.
- 합성 검수 bypass를 비활성화하고 Unauthorized Retrieval 0건을 hard gate로
  검증한다.

## 11. 테스트 계획

### Unit/contract

- UUID ID와 Version 숫자 정렬, `source_version`/`revision_number` 분리
- Active/deleted/ACL/doc Scope가 Vector/FTS/Trigram SQL 모두에 존재
- Citation JSON의 page/slide/sheet/cell/line/bounding box 변환
- `file_path`, `source_uri`, `diff_artifact_uri`, Credential이 API/tool/로그에
  노출되지 않음
- cutoff 이하 후보의 no-answer와 생성 LLM 미호출
- timeout, pool busy, embedding 차원, Schema 불일치의 error code
- 비인가와 미존재 문서가 동일 404
- 비교 pair의 같은 `doc_id` 소속과 순서 검증
- MinIO/Neo4j/검색 LLM 장애 시 허용된 degraded/fallback 동작

### API regression

- 기존 Playground 요청/응답 필드와 `search_rag_chunks` 호환
- unknown tool은 400 유지
- 삭제된 Langflow route는 404 유지
- 잘못된 UUID/필터/Version pair는 422 또는 정의된 400
- Source file 이력 실패를 빈 Version 이력으로 오인하지 않음
- 오류 응답이 내부 예외/host/path를 반사하지 않음

### Integration/성능

- 합성 전용 PostgreSQL fixture DB에서 `-m integration`으로 Hybrid 결과와 ACL 검증
- 합성 fixture 단위 테스트와 제공된 개발 저장소의 read-only smoke를 함께 검증
- warm 30회 이상에서 검색 p50/p95와 단계별 지연 기록
- DB/Embedding/MinIO/Neo4j 각각의 강제 timeout·중단 fault injection
- bounded pool 포화 시 요청이 무한 대기하지 않는지 검증

## 12. 위험, 미결정 사항, 승인 필요 항목

| 항목 | 위험/선택 | 권장안과 필요한 승인 |
|---|---|---|
| 데이터 분류 | 현재 단계는 제공된 합성 데이터 테스트 설정만 사용 | 실제 데이터 전환 전 데이터 소유자 확인과 별도 승인 필요 |
| DB 연결 | 세 저장소 read-only smoke는 완료했으나 운영 계정·권한 계약은 아님 | 운영 전 읽기 전용 계정·허용 Table/Bucket/Graph를 재승인 |
| ACL 검수 mode | 상위 Testbot은 전체 검수 mode를 사용하지만 운영 계약은 아님 | 합성 로컬에서만 명시적 허용, 운영 기본 fail-closed 승인 |
| 경로 노출 | 상위 예시 응답에는 `source_uri`가 있으나 내부 SMB 경로 노출 위험 | Browser/LLM에는 opaque reference만 제공하는 축소 계약 승인 |
| API 호환 | 기존 int ID/vector-only 모델과 새 UUID/Hybrid 모델은 직접 호환 불가 | handler alias는 유지하되 payload는 versioned model로 전환 승인 |
| Version 비교 | 임의 pair 즉시 계산은 CPU·응답 크기를 늘림 | Phase 2는 저장된 비교만, on-demand 계산은 비동기 설계 후 승인 |
| MinIO/Neo4j | 검색 hot path에서 지연과 장애면적이 증가 | 30건 상한·병렬 timeout·부분 결과를 적용하고 원본/본문 읽기는 명시적 요청으로 분리 |
| 외부 LLM | 검색 Chunk가 외부로 전송될 수 있음 | 현재는 로컬 provider만 사용. 외부 전송은 데이터 범위를 밝힌 별도 승인 필요 |
| Schema drift | 상위 문서와 실제 Migration 상태가 다를 수 있음 | 연결 승인 후 read-only startup contract check부터 수행 |

현재 승인 범위는 합성 데이터의 read-only 조회까지다. Schema 변경, Bucket/Graph 생성, Object/Graph 쓰기,
원본 파일 이동·삭제, 실제 의료자료 사용, 외부 전송은 포함되지 않는다.
