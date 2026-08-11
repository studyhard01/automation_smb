# 운영 아키텍처

이 문서는 현재 기능 검증 범위와 온프레미스 운영 전환 시 지켜야 할 경계를 정의한다. 현재 제품은 기존 데이터셋 구축
파이프라인을 다시 구현하지 않고, 구축된 DB 저장소를 읽기 전용으로 소비한다. 쓰기는 사용자가 명시적으로 추가한 새 파일을
승인된 SMB share의 제한된 하위 폴더에 최종 이름으로 exclusive 생성하는 경로만 허용한다.

## 현재 제품 경계

```text
사용자
  └─ Vue Playground
       └─ FastAPI
            ├─ PostgreSQL/pgvector ─ 문서 메타데이터·chunk·활성 문서 기준
            ├─ MinIO               ─ artifact object key·존재 확인, 원본·미리보기
            ├─ Neo4j               ─ 문서·revision 명칭 검색과 version 관계
            ├─ SMB upload folder   ─ 사용자 첨부 파일 저장(명시적 활성화·비덮어쓰기)
            └─ Ollama              ─ 파일 검색어 확장과 검색된 근거 답변 생성
```

로컬/LAN 기능 검증 배포에서는 Vue production bundle과 FastAPI를 하나의 non-root Docker image로 묶는다. 저장소와
Ollama는 별도 서비스로 유지하고 `.env`를 container runtime에만 주입한다. image는 `0.0.0.0:8011`을 listen하며
Compose가 host port를 게시한다. build context는 allowlist 방식이라 `.env`, Git metadata와 로컬 cache가 들어가지 않는다.

- 왼쪽 패널의 자연어 파일 찾기는 온프레미스 LLM으로 질의를 확장하고 세 저장소를 병렬 조회한다.
- 사용자가 선택한 `(doc_id, revision_id)`는 채팅 요청의 검색 범위를 제한한다.
- 채팅은 선택 범위의 Hybrid/RRF 검색 결과가 있을 때만 로컬 Ollama를 호출하고, 답변과 citation을 함께 반환한다.
- PostgreSQL은 최종 활성 UUID와 공개 메타데이터의 기준이며 MinIO·Neo4j 후보도 PostgreSQL에서 재검증한다.
- PostgreSQL·MinIO·Neo4j 접근은 읽기 전용이다. 이 서비스는 SMB 전체 순회·문서 변환·embedding 생성·DB 적재를
  수행하지 않는다.
- SMB 쓰기는 웹 설정에 저장된 share 기준 상대 경로로 제한하고, 최종 이름의 신규 생성만 허용한다. 기존 항목의 수정,
  덮어쓰기, 이름 변경, 이동, 삭제와 SMB 내부 임시 파일 정리는 수행하지 않으며 자격증명·전체 경로는 API와 로그에 노출하지 않는다.
- 첨부 성공 응답은 `indexed=false`이며, 별도 ingestion 전까지 검색 결과에 포함됐다고 간주하지 않는다.

## 이번 단계에서 제외한 구성

- Langflow, LangGraph, MCP
- 요청 시 SMB 전체 순회와 로컬 SQLite 인덱싱
- Tool Lab, Skills CRUD, QC·핵형·NGS 보고서 데모
- 외부 OpenAI provider와 외부 검색 API
- MLflow 기반 평가·artifact 파이프라인
- PostgreSQL·MinIO·Neo4j에 대한 쓰기 API
- 첨부 파일의 자동 변환·인덱싱·버전 관계 생성

제외된 기능은 사용 근거와 운영 책임이 합의되기 전까지 실행 경로와 배포 dependency에 포함하지 않는다.

## 저장소별 책임과 장애 동작

| 구성 | 책임 | 장애 시 동작 |
|---|---|---|
| PostgreSQL/pgvector | 기준 후보 검색, 외부 후보 hydrate, active revision 검증, 선택 범위 chunk 검색 | 파일 검색과 채팅을 unavailable로 반환한다. |
| MinIO | Artifact Registry/Object key 검색·존재 확인, 미리보기·원본 조회 | `minio_search_degraded`와 PostgreSQL·Neo4j 부분 결과를 반환한다. |
| Neo4j | 문서/최신 Revision label 검색, revision/version 관계 조회 | `neo4j_search_degraded`와 PostgreSQL·MinIO 부분 결과를 반환한다. |
| Ollama | 파일 검색어 확장, 검색 근거 답변 생성 | 기본 정규화 검색어로 fallback하고 `search_llm_unavailable`을 표시한다. |
| SMB 첨부 폴더 | 사용자가 선택한 합성 파일을 제한된 상대 경로에 비덮어쓰기 저장 | 기능을 비활성화하고 명시적 오류를 반환한다. DB 검색은 유지한다. |

모든 외부 호출에는 timeout을 두고 비밀번호, 내부 endpoint, 실제 문서 경로·본문을 로그에 남기지 않는다.

## 지연 목표

| 경로 | 목표 |
|---|---:|
| 멀티스토어+LLM 파일 검색 | p50 1.5초 미만, p95 3초 미만, hard budget 8초 |
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
7. LAN 공개 전 reverse proxy 인증과 Windows/Linux 방화벽 허용 범위
8. SMB 업로드 권한 분리, 사용자별 감사 로그, 악성 파일 검사와 보존·삭제 정책

이 승인이 끝나기 전에는 합성 데이터 기능 검증 범위를 운영 준비 완료로 간주하지 않는다.

## Container 운영 경계

- `/health` 성공은 FastAPI process liveness다. PostgreSQL·MinIO·Neo4j readiness는
  `/api/playground/stores/status`와 실제 검색 smoke로 별도 판정한다.
- runtime은 UID/GID 10001, read-only root filesystem, capability 제거, `no-new-privileges`로 실행한다.
- 비밀이 아닌 업로드 상대 경로만 별도 runtime volume에 저장한다. SMB 자격증명은 계속 runtime env로만 주입한다.
- host loopback endpoint는 container loopback과 다르므로 필요할 때만 `.env.docker`에서
  `host.docker.internal` 또는 승인된 LAN endpoint로 바꾼다.
- 같은 LAN 공개도 인증 없는 서비스 노출이다. 합성 데이터 테스트 범위를 벗어나면 방화벽만으로 운영하지 않는다.
- 상세 실행 절차와 제한된 방화벽 규칙은 [Docker 배포 문서](DOCKER_DEPLOYMENT.md)를 따른다.
