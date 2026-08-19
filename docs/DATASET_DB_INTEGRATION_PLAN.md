# 데이터 저장소 연동 계약

기준일: 2026-08-19

## 결정 요약

이 서비스는 upstream 데이터 파이프라인이 적재한 PostgreSQL·MinIO·Neo4j를 소비하는 **read-only adapter**다. 문서 수집,
변환, Chunk 생성, Embedding 적재와 활성 Revision 결정은 이 저장소의 책임이 아니다.

```text
자연어 파일 검색
  ├─ PostgreSQL: 문서·활성 Revision·Chunk metadata
  ├─ MinIO: Preview/Canonical artifact와 registry
  └─ Neo4j: Document/Revision 관계

선택 문서 대화·기안
  └─ PostgreSQL llmops.document_chunks Hybrid/RRF 검색
```

Langflow는 현재 실행 경로에 포함하지 않는다. Langflow·LangGraph 없이 FastAPI adapter가 저장소를 직접 읽는다.

## ID 계약

| ID | 의미 | 사용 위치 |
|---|---|---|
| `doc_id` | 논리 문서 UUID | 검색 결과, 선택 범위, graph |
| `revision_id` | 특정 문서 버전 UUID | 활성성 검증, artifact, Chunk 범위 |
| `chunk_id` | 인용 가능한 Chunk UUID | Hybrid/RRF 결과와 Citation |

Frontend가 보낸 UUID를 그대로 신뢰하지 않는다. 대화·기안 직전에 `(doc_id, revision_id)`가 현재 활성 Revision인지
PostgreSQL에서 다시 확인하며 stale이면 409를 반환한다.

## 저장소별 책임

### PostgreSQL

- 파일 후보와 활성 Revision 조회
- `llmops.document_chunks`의 lexical·vector 후보 조회
- 선택 UUID 검증과 metadata 조회
- transaction에 `default_transaction_read_only=on` 적용

단순 파일 찾기는 metadata 검색을 우선한다. 선택 문서 내용 검색은 lexical과 vector 결과를 RRF로 합치고 상위 N개만
답변·기안 근거로 전달한다.

### MinIO

- Preview/Canonical object를 read-only로 반환
- 검색 후보 보강에 필요한 artifact registry 조회
- 최대 응답 크기와 connect/read timeout 적용

object를 생성·수정·삭제하지 않는다. artifact가 없으면 문서 검색 전체를 실패시키지 않고 가능한 저장소 결과를 반환한다.

### Neo4j

- Document와 Revision 관계 조회
- 버전 화면과 검색 후보 보강
- read access session과 query timeout 적용

Neo4j 장애는 PostgreSQL 검색과 선택 문서 대화를 막지 않는 degraded 경로다.

## 요청 흐름

### 파일 검색

1. 입력을 정규화하고 명백한 키워드 질의는 fast path로 처리한다.
2. 필요한 경우 온프레미스 Ollama가 검색어를 짧게 확장한다.
3. PostgreSQL·MinIO·Neo4j 후보를 제한된 concurrency로 조회한다.
4. 물리 파일 identity로 중복을 접고 일관된 score로 정렬한다.
5. 상위 `limit`과 저장소별 warning·elapsed_ms를 반환한다.

### 선택 문서 검색

1. 선택 UUID와 활성 Revision을 검증한다.
2. query embedding을 생성한다.
3. 선택 Revision으로 범위를 고정해 lexical·vector 후보를 조회한다.
4. RRF 결과를 Citation과 함께 반환한다.
5. 근거가 부족하면 LLM 추측 대신 `rag_insufficient_evidence`를 반환한다.

## 지연과 실패 계약

- 파일 검색 목표: 인덱스 준비 상태에서 p50 < 300ms, p95 < 1s
- 인메모리 또는 DB fast path를 우선하고 요청마다 SMB 전체를 순회하지 않는다.
- DB·Embedding·MinIO·Neo4j·LLM에 절대 deadline의 남은 예산을 전달한다.
- 선택 저장소가 실패하면 warning과 부분 결과를 반환한다.
- PostgreSQL 또는 선택 Revision 검증이 불가능하면 안전하게 503을 반환한다.

## 구현 위치

- `backend/src/smb_finder/llmops_search.py` — PostgreSQL 파일 검색·Revision 검증
- `backend/src/smb_finder/llmops_multistore_search.py` — 검색어 확장·후보 통합
- `backend/src/smb_finder/llmops_retrieval.py` — 선택 범위 Hybrid/RRF
- `backend/src/smb_finder/llmops_artifacts.py` — MinIO artifact
- `backend/src/smb_finder/llmops_graph.py` — Neo4j graph
- `backend/src/smb_finder/models.py` — 공개 Pydantic 계약

## 검증

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv run --no-sync pytest `
  backend/tests/test_llmops_search.py `
  backend/tests/test_llmops_retrieval.py `
  backend/tests/test_llmops_stores.py `
  backend/tests/test_llmops_api_contracts.py -q
```

운영 전에는 합성 dataset으로 검색 Hit@5, no-answer 정확도, groundedness, cold/warm 지연을 다시 측정한다.
