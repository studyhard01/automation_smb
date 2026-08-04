# 운영 아키텍처

이 문서는 현재 기능 검증 범위와 온프레미스 운영 전환 시 지켜야 할 경계를 정의한다. 현재 제품은 기존 데이터셋 구축 파이프라인을 다시 구현하지 않고, 구축된 저장소를 읽기 전용으로 소비하는 검색·대화 서비스다.

## 현재 제품 경계

```text
사용자
  └─ Vue Playground
       └─ FastAPI
            ├─ PostgreSQL/pgvector ─ 문서 메타데이터, 검색, 선택 문서 chunk 검색
            ├─ MinIO               ─ 선택 문서 원본·미리보기 artifact
            ├─ Neo4j               ─ 문서 revision·version 관계
            └─ Ollama              ─ 검색된 근거로 답변 생성
```

- 왼쪽 패널의 자연어 파일 찾기는 PostgreSQL 인덱스를 조회한다.
- 사용자가 선택한 `(doc_id, revision_id)`는 채팅 요청의 검색 범위를 제한한다.
- 채팅은 선택 범위의 Hybrid/RRF 검색 결과가 있을 때만 로컬 Ollama를 호출하고, 답변과 citation을 함께 반환한다.
- MinIO와 Neo4j는 선택 문서의 미리보기와 버전 관계를 제공한다.
- 모든 저장소 접근은 읽기 전용이다. 이 서비스는 SMB 순회·문서 변환·embedding 생성·DB 적재를 수행하지 않는다.

## 이번 단계에서 제외한 구성

- Langflow, LangGraph, MCP
- 요청 시 SMB 전체 순회와 로컬 SQLite 인덱싱
- Tool Lab, Skills CRUD, QC·핵형·NGS 보고서 데모
- 외부 OpenAI provider와 외부 검색 API
- MLflow 기반 평가·artifact 파이프라인
- PostgreSQL·MinIO·Neo4j에 대한 쓰기 API

제외된 기능은 사용 근거와 운영 책임이 합의되기 전까지 실행 경로와 배포 dependency에 포함하지 않는다.

## 저장소별 책임과 장애 동작

| 구성 | 책임 | 장애 시 동작 |
|---|---|---|
| PostgreSQL/pgvector | 문서 후보 검색, active revision 검증, 선택 범위 chunk 검색 | 파일 검색과 채팅을 unavailable로 반환하고 다른 저장소로 우회하지 않는다. |
| MinIO | 선택 문서의 미리보기·원본 artifact 조회 | 파일 검색·근거 채팅은 유지하고 미리보기만 unavailable로 표시한다. |
| Neo4j | 선택 문서 revision/version 관계 조회 | 파일 검색·근거 채팅은 유지하고 버전 그래프만 degraded로 표시한다. |
| Ollama | 검색된 근거를 인용하는 답변 생성 | 검색 결과는 유지하고 채팅만 빠르게 실패시킨다. |

모든 외부 호출에는 timeout을 두고 비밀번호, 내부 endpoint, 실제 문서 경로·본문을 로그에 남기지 않는다.

## 지연 목표

| 경로 | 목표 |
|---|---:|
| 저장소가 준비된 파일 검색 | p50 300ms 미만, p95 1초 미만 |
| 선택 문서 retrieval | p95 1초 미만 |
| 선택 문서 근거 채팅 | 현재 baseline 측정 후 확정, 우선 5초 초과를 개선 대상으로 기록 |

검색 결과와 채팅 응답에는 `elapsed_ms`를 기록한다. 저장소 연결·검색·LLM 호출이 실패하거나 예산을 넘기면 성공처럼 숨기지 않고 응답 상태와 품질 evidence에 남긴다.

## 운영 전환 전 승인 항목

1. SSO 또는 reverse proxy 인증과 사용자별 문서 접근 권한 모델
2. 실제 업무 데이터 사용 승인과 로그·citation·미리보기의 비식별/보존 정책
3. PostgreSQL, MinIO, Neo4j의 서비스 계정·TLS·backup·복구·retention 정책
4. Ollama model, GPU 용량, 동시성, timeout과 응답 지연 SLO
5. 선택 revision이 inactive가 되었을 때 사용자에게 재선택을 요구하는 운영 정책
6. 배포 대상 Linux 서버의 secret 주입, health/readiness, 모니터링과 장애 책임자

이 승인이 끝나기 전에는 합성 데이터 기능 검증 범위를 운영 준비 완료로 간주하지 않는다.
