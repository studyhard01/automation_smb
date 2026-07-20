# 운영 아키텍처

이 문서는 합성 데이터 기능 검증을 마친 `automation_smb`를 온프레미스 운영으로 옮길 때의 목표 구조와
아직 확정하지 않은 경계를 정리한다. 현재 저장소에 이 구성이 배포됐다는 뜻은 아니다.

## 현재 상태와 운영 목표

| 구분 | 현재 합성 테스트 | 운영 목표 |
|---|---|---|
| API | Windows 개발 머신의 단일 FastAPI 프로세스 | 사내 Linux 서버의 인증된 API, 우선 단일 노드 pilot |
| 폴더/본문 검색 | 메모리+JSON, 로컬 SQLite FTS5 | 암호화된 영속 볼륨의 index snapshot/FTS5, 백그라운드 갱신 |
| 문서 RAG | 온프레미스 PostgreSQL/pgvector와 원격 embedding endpoint | 접근 통제·백업되는 PostgreSQL/pgvector |
| LLM/embedding | 로컬 또는 원격 OpenAI 호환 endpoint | 사내 GPU 서버의 Ollama/OpenAI 호환 endpoint |
| MLflow metadata | 로컬 SQLite | 운영 PostgreSQL의 별도 database/schema |
| MLflow artifact | 로컬 `.cache/mlflow/artifacts` | 온프레미스 MinIO의 전용 bucket |
| 인증·권한 | 기능 테스트 수준 | reverse proxy/SSO, 역할 기반 API·SMB 접근, 감사 로그 |

현재 구현은 단일 개발 환경의 기능과 지연을 확인하는 단계에는 맞지만, 다중 사용자·재배포·장애 복구를 견디는
운영 토폴로지는 아직 미구축이다. 특히 로컬 SQLite와 `.cache` artifact는 해당 머신이 사라지면 함께 손실되므로
운영 공유 저장소로 간주하지 않는다.

평가·실험 비교·보존 trace의 기준점은 MLflow 하나로 둔다. 온라인 요청은 구조화 서비스 로그와 metric으로 진단하고,
LangGraph Studio는 `smb_agent` graph를 수동 실행하는 개발 도구로만 필요할 때 켠다.

## 권장 운영 토폴로지

```text
사용자
  │
  ▼
사내 reverse proxy / SSO
  │
  ▼
automation_smb API ── fast path ── 메모리 index / FTS5 snapshot
  │       │
  │       ├── read-only ── SMB 공유폴더
  │       ├── RAG ─────── PostgreSQL / pgvector
  │       └── 추론 ────── 사내 GPU Ollama endpoint
  │
  └── 비동기 갱신/평가
          ├── indexer worker / scheduler
          └── MLflow tracking server
                 ├── metadata ── PostgreSQL
                 └── artifact ── MinIO
```

온라인 요청의 기본 경로는 계속 **인덱스 우선**이다. SMB 전체 순회, index 갱신, 평가 artifact 업로드는
요청 hot path 밖에서 수행한다. MinIO 장애 때문에 폴더 검색 응답이 늦어지거나 실패해서는 안 된다.

초기 운영 pilot은 API와 로컬 index snapshot을 한 Linux 노드에서 실행하는 구성이 현실적이다. 실제 부하와
복구 목표를 측정하기 전에 API를 여러 replica로 늘리면 FTS5/index 배포와 갱신 일관성 문제가 먼저 생긴다.
다중 노드는 indexer가 불변 snapshot을 발행하고 API가 원자적으로 교체하는 계약을 설계한 뒤 진행한다.

## MinIO 판단

**결론:** MinIO는 현재 검색 기능을 실행하는 데 필수는 아니지만, 운영 pilot에서 MLflow를 팀 공용으로 유지하려면
도입을 권장한다. 다중 노드 또는 재배포 가능한 MLflow를 운영하기 전에는 사실상 필수 조건으로 본다.
현재 코드·dependency·로컬 stack에는 MinIO 연결이 구현돼 있지 않다.

MLflow는 작은 run/metric metadata를 backend store에, 모델·이미지·JSON 같은 큰 파일을 artifact store에 나눠
보관한다. 공식 MLflow 문서도 운영 동시성이 높을 때 PostgreSQL 같은 database backend와 S3 호환 artifact store를
분리하는 구성을 지원하며, MinIO는 `MLFLOW_S3_ENDPOINT_URL`로 연결하는 S3 호환 저장소다.

- [MLflow Tracking Server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)
- [MLflow Artifact Stores](https://mlflow.org/docs/latest/self-hosting/architecture/artifact-store/)

권장 연결은 client가 MinIO 자격증명을 직접 갖지 않는 **MLflow proxy 방식**이다. Tracking Server만
`MLFLOW_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`를 secret으로 받고
`--artifacts-destination s3://<dedicated-bucket>/mlflow`를 사용한다. metadata는 별도 PostgreSQL
`--backend-store-uri`에 둔다. 실제 endpoint와 자격증명은 저장소나 문서에 기록하지 않는다.

### MinIO에 넣는 것과 넣지 않는 것

| 분류 | 기본 정책 |
|---|---|
| 합성 evaluation 결과 JSON, chart, model/eval artifact | 허용 |
| 배포 manifest와 재현에 필요한 비민감 artifact | 검토 후 허용 |
| 실제 SMB 원본 파일·원본 경로 목록 | 금지 |
| 환자·검체 식별자나 원문이 포함된 trace/evaluation artifact | 별도 승인 전 금지 |
| 파생 chunk/index snapshot | 데이터 분류·암호화·보존기간·삭제 절차 승인 후 결정 |

MinIO는 사내망에 있다는 이유만으로 원본 SMB의 복제 저장소가 되지 않는다. 원본의 source of truth는 계속
read-only SMB이며, MinIO bucket은 MLflow 전용 service account, TLS/사내 CA, 저장 암호화, lifecycle/retention,
backup과 복원 테스트를 갖춘 별도 운영 자산으로 관리한다.

## 장애 시 동작

| 장애 | 사용자 요청의 기대 동작 |
|---|---|
| SMB 일시 장애 | 안전한 마지막 index snapshot으로 제한된 검색을 제공하고 freshness를 표시 |
| PostgreSQL/pgvector 장애 | RAG만 unavailable 처리하고 폴더/FTS fast path는 유지 |
| Ollama/embedding 장애 | 규칙 기반 폴더/키워드 검색은 유지하고 LLM/RAG만 빠르게 timeout |
| MinIO 장애 | 온라인 검색은 유지하고 MLflow artifact 기록은 실패 또는 제한된 재시도 |
| index 갱신 실패 | 기존 snapshot을 유지하며 readiness와 운영 alert에 실패를 노출 |

모든 dependency 호출에는 timeout과 단계별 지연을 둔다. 실패 시 내부 endpoint, SMB 경로, 자격증명, 문서 본문을
오류 응답이나 중앙 로그에 남기지 않는다.

## 운영 전환 순서

1. **단일 노드 pilot** — Linux 서비스 계정, reverse proxy/SSO, read-only SMB, 영속 index 볼륨과 backup을 준비한다.
2. **공용 데이터 계층** — PostgreSQL/pgvector의 접근 통제·backup·복원과 원격 Ollama의 timeout/keep-alive를 검증한다.
3. **MLflow 영속화** — metadata를 PostgreSQL로, 허용된 artifact만 MinIO로 옮기고 신규 experiment에서 proxy
   artifact URI가 적용되는지 확인한다.
4. **운영 검증** — 합성 데이터로 dependency별 장애, cold/warm 지연, disk/bucket quota, restore를 점검한다.
5. **실데이터 전환 심사** — 인증/RBAC, 감사, 보존·삭제, trace redaction, 파생 데이터 분류를 별도 승인한다.
6. **필요할 때만 확장** — 실제 동시성·SLO 근거가 생긴 뒤 worker 분리와 API replica/index 배포 계약을 구현한다.

## 아직 확정하지 않은 결정

- 인증 제공자와 사용자/팀별 SMB 권한 위임 방식
- index snapshot의 암호화, 보존 기간, 다중 노드 배포 방식
- pgvector의 실제 문서 chunk 보존·삭제·재색인 정책
- MinIO 가용성 구성, bucket quota, lifecycle 기간과 backup 대상
- 운영 모니터링/alert 제품과 장애 대응 책임자

이 결정은 사내 인프라 담당자와 데이터 보호 책임자의 요구를 확인한 뒤 확정한다.
