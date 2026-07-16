# MLflow 문서 챗봇 평가 최종 개발 계획

> 상태: Phase 0·1 구현 및 MLflow 실측 완료, Phase 2 진행 예정
> 기준일: 2026-07-16
> 대상: `search_rag_chunks`를 사용하는 Playground 문서 챗봇
> 실행 환경: 실제 의료 환경과 무관한 합성·더미 데이터 전용 로컬 테스트

## 1. 결론

MLflow는 현재 서비스의 필수 런타임이 아니라 **선택 설치하는 오프라인 평가·실험 관리 계층**으로 도입한다.
정상 Playground 요청은 MLflow가 없거나 중단되어도 기존과 동일하게 동작해야 하며, 1단계 평가 결과 화면은
별도 UI를 만들지 않고 `http://127.0.0.1:5000`의 MLflow UI를 사용한다.

첫 구현 목표는 다음 수직 슬라이스다.

1. 합성 golden dataset으로 현재 문서 챗봇을 반복 실행한다.
2. 검색 품질, tool 선택, 답변 품질, 지연과 token을 하나의 MLflow evaluation run에 기록한다.
3. 각 평가 case에서 `root → agent → tool → retriever/LLM` trace를 확인한다.
4. gpt-4.1-mini, top-k, 활성 skill 조합을 같은 dataset으로 비교한다.
5. 평가 실패는 보고만 하고 서비스 응답, 배포, CI를 차단하지 않는다.

## 2. 목표와 비목표

### 목표

- MLflow 3.x Tracking, Tracing, GenAI Evaluation을 선택적으로 사용한다.
- 로컬 SQLite tracking backend와 로컬 artifact 디렉터리를 사용한다.
- `Recall@K`, `MRR`, tool 정확도, no-answer 정확도처럼 재현 가능한 코드 지표를 우선한다.
- `Correctness`, `RetrievalGroundedness`, `RelevanceToQuery` judge를 선택적으로 실행한다.
- 모델·검색·prompt·skill·corpus 버전을 run parameter와 tag로 남긴다.
- 기존 `embedding_ms`, `db_ms`, agent/tool elapsed, token usage를 재사용한다.
- telemetry는 기본 OFF이며 서비스 경로에서 항상 fail-open이다.

### 비목표

- MLflow 점수로 요청이나 배포를 자동 차단하지 않는다.
- 이번 단계에서 Playground 평가 버튼, dashboard, iframe을 만들지 않는다.
- 기존 LangSmith 연동을 제거하거나 대체하지 않는다.
- 모델 재학습, 온라인 A/B 테스트, 운영 자동 모니터링은 포함하지 않는다.
- 실제 환자·검사·SMB 원문을 평가 dataset, trace, artifact 또는 외부 judge에 넣지 않는다.

## 3. 현재 시스템과 MLflow의 역할

| 현재 구성 | 현재 기록 | MLflow 추가 역할 |
|---|---|---|
| `PlaygroundAgent` | agent step, tool trace, 전체 elapsed | case별 root/agent/tool span과 설정 비교 |
| `RagVectorSearcher` | similarity, `embedding_ms`, `db_ms` | retrieval 지표와 `RETRIEVER` span |
| OpenAI 호환 LLM | token usage, model | 생성 span과 답변 품질 평가 |
| LangSmith | OpenAI LLM 호출 trace | 유지. LLM 호출 디버깅에 사용 |
| MLflow | 없음 | 문서 RAG 전체 trace, golden evaluation, run 비교 |

LangSmith와 MLflow는 독립 플래그로 제어한다. 오프라인 평가에서는 MLflow를 켜고 LangSmith는 필요할 때만 켠다.
동일 LLM 호출을 두 시스템이 중복 wrapping하지 않도록 MLflow는 전체 app의 논리 span을 수동 계측한다.

## 4. 목표 평가 구조

```text
합성 Golden Dataset
  → document evaluation runner
  → PlaygroundAgent
      → agent decision
      → search_rag_chunks tool
          → query embedding
          → PostgreSQL/pgvector retrieval
      → final answer LLM
  → deterministic scorers + optional LLM judges
  → MLflow experiment/run/traces/artifacts
```

### MLflow 객체 매핑

- Experiment: `automation-smb-doc-chatbot`
- Evaluation run: dataset과 한 가지 실행 설정의 조합
- Trace: 질문 한 건의 전체 실행
- Span: agent 판단, tool 실행, retrieval, LLM 호출
- Parameters: provider, model, top-k, embedding model, prompt/skill hash, corpus fingerprint
- Metrics: aggregate retrieval, answer, latency, token 지표
- Artifacts: 입력 내용이 없는 설정 snapshot, case별 평가 결과, 실패 case 요약

## 5. 데이터 계약

초기 합성 smoke dataset은 최소 12건으로 시작하고, 첫 baseline 전 40건으로 확장한다.

```json
{
  "case_id": "synthetic-single-hop-001",
  "inputs": {
    "question": "프로젝트 오로라의 합성 일정은?",
    "top_k": 5
  },
  "expectations": {
    "expected_document_keys": ["docs/aurora.md"],
    "expected_chunk_keys": ["docs/aurora.md#3"],
    "expected_tool_calls": [{"name": "search_rag_chunks"}],
    "expected_facts": ["일정은 7월이다"],
    "reference_answer": "프로젝트 오로라의 일정은 7월입니다.",
    "should_answer": true
  },
  "tags": {
    "synthetic": "true",
    "category": "single-hop",
    "difficulty": "easy"
  }
}
```

정답 key는 합성 corpus의 정규화된 `file_path#chunk_index`를 사용한다. 각 run에 문서 수, chunk 수, embedding 모델,
최종 변경 식별자를 조합한 `corpus_fingerprint`를 기록한다. 경로나 chunk 규칙이 바뀌면 golden dataset도 새 버전으로 올린다.

평가 `predict_fn`은 기존 API를 변경하지 않고 내부적으로 다음 구조를 반환한다.

```json
{
  "answer": "프로젝트 오로라의 일정은 7월입니다.",
  "tool_calls": ["search_rag_chunks"],
  "retrieved_document_keys": ["docs/aurora.md"],
  "retrieved_chunk_keys": ["docs/aurora.md#3"],
  "error_code": "",
  "elapsed_ms": 820.4,
  "embedding_ms": 120.1,
  "db_ms": 23.2,
  "over_budget": false,
  "token_usage": {"total_tokens": 120}
}
```

Dataset 구성은 다음을 모두 포함한다.

- 단일 chunk 정답
- 복수 chunk 조합
- 유사 문서 구분
- 답이 없는 질문
- 추가 질문이 필요한 모호한 요청
- RAG tool을 호출하지 않아야 하는 일반 요청

## 6. Trace 계약

### Root span: `document_chatbot.evaluate_case`

- 입력: `case_id`, 질문, provider/model, top-k, 활성 tool/skill ID
- 출력: 최종 답변, error code, over-budget 여부
- tag: request ID, dataset version, corpus fingerprint, Git SHA
- metric: 전체 elapsed와 token usage

### Agent span: `playground.agent`

- agent step 수
- 선택한 tool ID와 공개 가능한 인자 요약
- 중복 호출·단계 제한·시간 예산 결과
- 활성 skill ID와 SKILL.md hash

### Tool span: `playground.tool.search_rag_chunks`

- span type: `TOOL`
- query 길이, top-k, status, error code, elapsed
- 실제 DB 비밀번호, endpoint key, 전체 내부 경로는 기록하지 않는다.

### Retriever span: `rag.vector_search`

- span type: `RETRIEVER`
- 입력: 합성 query, top-k, embedding model
- metadata: document/chunk ID, similarity, `embedding_ms`, `db_ms`
- 출력: MLflow 문서 규격의 `page_content`, `doc_uri`, `chunk_id`

`rag.vector_search` 아래에는 `query_embedding` span을 두고 span type `EMBEDDING`, embedding model, dimension,
elapsed와 오류 코드만 기록한다.

`RetrievalGroundedness` 평가에는 검색된 본문이 필요하다. 따라서 **오프라인 evaluation runner가 합성 데이터로
실행될 때만** retriever span에 합성 chunk 본문을 포함한다. 정상 서비스 tracing은 기본 OFF이고, 후속 실시간 tracing은
ID·지연만 기록하는 별도 모드로 유지한다.

### LLM span: `playground.llm_generation`

- provider, model, purpose
- prompt/completion/total token
- elapsed와 응답 상태
- 오프라인 평가에서만 합성 입력·출력을 기록한다.

## 7. 평가 지표

### 코드 기반 필수 지표

| 구분 | 지표 |
|---|---|
| Retrieval | Hit@K, Recall@K, MRR, 선택적으로 nDCG |
| Agent | expected tool exact match, tool call count, duplicate call rate |
| Answer | no-answer accuracy, expected fact coverage, citation document match |
| Reliability | success/error/over-budget rate |
| Latency | embedding, DB, retrieval, tool, agent, end-to-end p50·p95 |
| Cost | prompt/completion/total token 평균과 합계 |

코드 지표는 judge API와 무관하게 완전 로컬에서 실행되어야 하며 regression의 1차 기준으로 사용한다.

### 선택적 LLM judge

- `Correctness(model="openai:/gpt-4.1-mini")`
- `RetrievalGroundedness(model="openai:/gpt-4.1-mini")`
- `RelevanceToQuery(model="openai:/gpt-4.1-mini")`
- 후속: `RetrievalRelevance`, `RetrievalSufficiency`, `ToolCallCorrectness`, `ToolCallEfficiency`
- custom `Guidelines`: 답변에 근거 문서·위치가 표시되는지, 근거가 없을 때 추측하지 않는지

judge model은 MLflow 기본값에 의존하지 않고 항상 명시한다. judge는 비결정적이고 네트워크·비용 영향을 받으므로
초기 release gate로 사용하지 않는다. 20개 표본에 대한 사람 판정과 비교해 scorer 방향을 먼저 보정한다.

## 8. 첫 비교 실험

| Run | 생성 모델 | top-k | `rag-grounded-answer` | 목적 |
|---|---|---:|---:|---|
| baseline | gpt-4.1-mini | 5 | ON | 현재 기준선 |
| retrieval-k3 | gpt-4.1-mini | 3 | ON | 검색 폭과 지연 비교 |
| skill-off | gpt-4.1-mini | 5 | OFF | skill의 grounded answer 영향 |

첫 평가에서는 `MLFLOW_GENAI_EVAL_MAX_WORKERS=1`로 고정한다. 지연 baseline이 안정된 후 별도 throughput run에서만
worker 수를 늘린다.

top-k 비교는 먼저 retrieval-only runner에서 `RagVectorSearcher.search(question, top_k)`로 강제한다. end-to-end Agent가
tool 인자의 `limit`을 다시 선택할 수 있으므로, retrieval 변수가 고정됐는지 확인한 뒤 전체 챗봇 비교로 확장한다.

## 9. 구현 단계와 담당

### Phase 0. 기반과 optional dependency — 완료

Backend:

- `evaluation = ["mlflow[genai]>=3.9,<4"]` optional extra 추가 후 `uv.lock`에 실제 검증 버전 고정
- MLflow 설정 모델과 `.env.example` 추가
- local Tracking Server 실행·health preflight 문서화
- MLflow 미설치 환경에서 import 가능한 no-op adapter 정의

완료 기준: 기본 설치와 전체 기존 테스트가 MLflow 없이 통과한다.

### Phase 1. 계약과 retrieval-only 평가 — 완료

Backend/Test:

- `GoldenCase`, `CaseEvaluationResult`, aggregate metric Pydantic 모델
- synthetic JSONL loader와 15-case smoke dataset
- Hit@K, Recall@K, MRR, latency aggregate scorer
- `RagVectorSearcher`를 호출하는 retrieval-only runner

완료 기준: 고정 fixture의 지표가 수기로 계산한 값과 정확히 일치하고 MLflow UI에 run이 보인다.

실측 baseline(top-k 5):

- 15 case 중 retrieval 평가 13건, non-RAG 기대 case 2건은 retrieval-only runner에서 제외
- Hit@5 83.3%, Recall@5 79.2%, MRR 69.4%
- warm baseline retrieval p50 188.9ms, p95 204.3ms, 오류율 0%, over-budget 0%
- no-answer accuracy 0%: 유사도 cutoff가 없어 답이 없는 질문에도 상위 chunk를 반환하는 현재 한계를 확인
- MLflow run `499673fd79be4b1285edbf87b09ef318`에서 metric 19개, parameter 12개와 JSON artifact 2개 확인

### Phase 2. 전체 문서 챗봇 trace

Backend:

- root/agent/tool/retriever/LLM span adapter
- 기존 PlaygroundAgent를 재사용하는 end-to-end `predict_fn`
- request ID, skill hash, corpus fingerprint, Git SHA tag
- telemetry 예외 격리, 짧은 timeout, 비동기 export 또는 circuit breaker

완료 기준: 한 case에서 span 부모·자식 관계, retrieval 결과, 답변, token과 지연을 확인한다.

### Phase 3. GenAI judge와 비교 run

Backend/Test:

- MLflow `Correctness`, `RetrievalGroundedness`, `RelevanceToQuery` 연결
- judge enabled/disabled/skipped 상태 기록
- 40-case golden dataset 확장
- baseline/top-k/skill 비교 실행

완료 기준: 코드 지표는 judge 실패와 무관하게 완료되고 세 run을 MLflow UI에서 비교할 수 있다.

### Phase 4. 후속 선택 기능

- 사람 평가/피드백을 trace에 연결
- 실패 trace를 evaluation dataset에 승격
- 승인된 threshold 기반 수동 release checklist
- 필요할 때만 Playground에 작은 MLflow 외부 링크 또는 trace 링크 추가
- 운영 전환 보안·보존·접근통제 별도 설계

Frontend는 Phase 0~3에서 변경하지 않는다. MLflow UI를 평가 결과 화면으로 사용하고 `/api/playground/chat`과
`ChatResponse` 계약을 그대로 유지한다.

## 10. 예상 파일

```text
pyproject.toml
uv.lock
.env.example
README.md
src/smb_finder/config.py
src/smb_finder/evaluation/__init__.py
src/smb_finder/evaluation/models.py
src/smb_finder/evaluation/tracing.py
src/smb_finder/evaluation/datasets.py
src/smb_finder/evaluation/scorers.py
src/smb_finder/evaluation/document_chatbot.py
scripts/run_mlflow_doc_eval.py
scripts/start_local_stack.ps1
data/evaluation/document_chatbot_golden.jsonl
tests/test_mlflow_tracing.py
tests/test_document_chatbot_scorers.py
tests/test_document_chatbot_evaluation.py
docs/MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md
```

## 11. 설정과 실행 계약

예상 환경변수:

```dotenv
MLFLOW_EVALUATION_ENABLED=false
MLFLOW_TRACING_ENABLED=false
MLFLOW_TRACKING_URI=http://127.0.0.1:5000
MLFLOW_EXPERIMENT_NAME=automation-smb-doc-chatbot
MLFLOW_JUDGE_ENABLED=false
MLFLOW_JUDGE_MODEL=openai:/gpt-4.1-mini
MLFLOW_TRACE_INCLUDE_CONTENT=false
```

예상 로컬 실행:

```powershell
uv sync --native-tls --extra dev --extra evaluation

# 통합 실행기가 구현된 현재 단계에서는 MLflow UI만 단독 또는 전체 스택으로 시작할 수 있다.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Start -Profile Mlflow

uv run --extra evaluation mlflow server `
  --host 127.0.0.1 `
  --port 5000 `
  --backend-store-uri sqlite:///./.cache/mlflow/mlflow.db `
  --artifacts-destination ./.cache/mlflow/artifacts

$env:MLFLOW_GENAI_EVAL_MAX_WORKERS="1"
uv run --extra evaluation python scripts/run_mlflow_doc_eval.py `
  --mode retrieval `
  --dataset data/evaluation/document_chatbot_golden.jsonl `
  --provider openai `
  --model gpt-4.1-mini `
  --top-k 5
```

평가 runner는 시작 시 Tracking Server를 preflight한다. 서버에 연결할 수 없으면 명확한 CLI 오류로 종료하되,
Playground 서비스에는 영향을 주지 않는다.

## 12. 완료 기준

1. MLflow 미설치 기본 환경에서 앱 import, 서비스 실행, 기존 테스트가 그대로 통과한다.
2. `evaluation` extra 설치 후 loopback SQLite Tracking Server와 평가 runner가 실행된다.
3. 한 평가 실행이 하나의 evaluation run과 case별 trace를 만든다.
4. trace에서 `root → agent → tool → retriever/LLM` 관계를 확인한다.
5. retriever span에 document/chunk ID, similarity, `embedding_ms`, `db_ms`가 기록된다.
6. 합성 평가 모드에서만 `RETRIEVER` 문서 본문이 기록되고 built-in RAG judge가 동작한다.
7. 고정 fixture의 Recall@K, MRR, tool accuracy, no-answer accuracy가 기대값과 일치한다.
8. judge가 꺼져 있거나 실패해도 코드 기반 평가 run은 성공한다.
9. MLflow 서버가 중단된 상태의 채팅 요청 100건이 telemetry 때문에 실패하지 않는다.
10. telemetry OFF의 LLM 없는 retrieval 요청은 `p50 < 1s`, `p95 < 3s`를 목표로 한다.
11. telemetry ON·endpoint 장애 시 p95 증가분이 baseline 대비 50ms 이하이다.
12. 기존 `ChatResponse`와 Playground UI 계약이 변경되지 않는다.
13. trace와 artifact에 key, 비밀번호, 내부 endpoint, 실제 의료데이터가 없다.
14. 평가 결과가 서비스 응답, 배포, CI를 자동 차단하지 않는다.

초기 품질 목표는 baseline 측정 후 확정하되 다음 값을 출발점으로 사용한다.

- Retrieval Hit@5: 90% 이상
- Tool exact-match accuracy: 95% 이상
- no-answer accuracy: 95% 이상
- Groundedness pass rate: 90% 이상
- 검색 오류율: 1% 미만
- retrieval p95: 1초 미만
- end-to-end p95: 절대값과 함께 baseline 대비 20% 이상 악화 금지

## 13. 위험과 대응

| 위험 | 대응 |
|---|---|
| MLflow export가 요청을 지연 | 기본 OFF, 평가 runner 분리, async/fail-open, 짧은 timeout과 circuit breaker |
| judge 비용·비결정성 | 명시 opt-in, worker 1, code scorer 우선, 사람 표본으로 보정 |
| DB 재적재·경로 변경으로 정답 key 변경 | corpus fingerprint와 dataset version을 함께 변경 |
| LangSmith와 역할 중복 | LangSmith=LLM 디버깅, MLflow=RAG 전체 평가로 문서화 |
| MLflow UI에서 요청 식별 어려움 | `request_id`, case ID, run naming을 tag로 통일 |
| 합성/실제 데이터 경계 혼동 | 파일명·tag에 synthetic 명시, offline include-content 옵션 기본 false |
| 기존 미커밋 변경 충돌 | MLflow 구현은 별도 커밋으로 진행하고 skill-creator 변경을 되돌리지 않음 |

## 14. 역할별 최종 결정

- Project planner: optional/offline/fail-open 평가 계층과 단계별 완료 기준 확정
- Backend developer: optional dependency, tracing adapter, dataset/runner/scorer, 운영 명령과 테스트 담당
- Frontend developer: Phase 0~3 변경 없음. 기존 API·오류·loading 동작 회귀 확인만 담당
- Orchestrator: 계약 통합, 기존 변경 보호, 전체 테스트·지연·장애 검증과 최종 보고 담당

## 15. 공식 참고 자료

- [MLflow GenAI Tracing](https://mlflow.org/docs/latest/genai/tracing)
- [MLflow Evaluation Datasets](https://mlflow.org/docs/latest/genai/datasets/)
- [MLflow GenAI Evaluation Examples](https://mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/eval-examples/)
- [MLflow Built-in Judges](https://mlflow.org/docs/latest/genai/eval-monitor/scorers/llm-judge/predefined/)
- [MLflow Custom Code Scorers](https://mlflow.org/docs/latest/genai/eval-monitor/scorers/custom/)
- [MLflow Retriever Span Schema](https://mlflow.org/docs/latest/genai/concepts/span/)
- [MLflow Tracking Server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)
