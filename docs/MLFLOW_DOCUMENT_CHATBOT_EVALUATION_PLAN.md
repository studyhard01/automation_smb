# MLflow 문서 챗봇 평가 최종 개발 계획

> 상태: Phase 0~2 완료, Phase 3 Dataset 등록·judge smoke·baseline/top-k 비교 실측 완료, skill-off 재실행 대기
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
- 서비스 요청마다 별도 관측 시스템에 trace를 중복 기록하지 않는다.
- 모델 재학습, 온라인 A/B 테스트, 운영 자동 모니터링은 포함하지 않는다.
- 실제 환자·검사·SMB 원문을 평가 dataset, trace, artifact 또는 외부 judge에 넣지 않는다.

## 3. 현재 시스템과 MLflow의 역할

| 현재 구성 | 현재 기록 | MLflow 추가 역할 |
|---|---|---|
| `PlaygroundAgent` | agent step, tool trace, 전체 elapsed | case별 root/agent/tool span과 설정 비교 |
| `RagVectorSearcher` | similarity, `embedding_ms`, `db_ms` | retrieval 지표와 `RETRIEVER` span |
| OpenAI 호환 LLM | token usage, model | 생성 span과 답변 품질 평가 |
| 구조화 서비스 로그 | 요청 ID, agent/tool 단계, 지연, 오류 코드 | 온라인 장애 진단과 SLO 집계의 원본 |
| MLflow | 평가 runner의 logical span | 문서 RAG 전체 trace, golden evaluation, run 비교 |

MLflow를 평가·실험 비교·보존 trace의 단일 기준점으로 사용한다. 온라인 요청은 구조화 서비스 로그만 남기고,
MLflow는 서비스 경로와 분리된 runner에서 전체 app의 논리 span을 수동 계측한다.

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

### 5.1 기존 15건 검토와 dataset 분리 결정

2026-07-16 로컬 corpus의 6개 문서·115개 chunk를 읽기 전용으로 대조했다. 기존 15건은 답변 가능한 RAG 12건,
no-answer RAG 1건, clarification/general behavioral 2건으로 구성되어 있었다.

- 12건의 positive RAG case마다 기대 chunk의 `content_hash`를 provenance에 고정했다.
- Parser Router와 chunking을 함께 묻는 case는 실제 2개 chunk 근거로 교정했다.
- 로컬 실행환경 case는 구성요소를 직접 열거한 PPT `#1`로 기대 chunk를 교정했다.
- Skills Registry case는 역할 정의가 직접 포함된 아키텍처 문서 `#2`로 교정했다.
- no-answer·clarification·general case는 corpus hash를 가장하지 않고 `source_kind=behavioral`로 구분했다.

물리 파일과 사용 목적은 다음처럼 고정한다.

| 파일 | 상태 | 건수 | 기본 baseline 사용 |
|---|---|---:|---:|
| `document_chatbot_golden.jsonl` | `validated`, `synthetic-golden-v2` | 15 | 사용 |
| `document_chatbot_candidates.jsonl` | `candidate`, `synthetic-candidates-v1` | 25 | 사용하지 않음 |

candidate는 corpus chunk에서 질문·기대 사실·참고 답변을 만든 검토 대기 항목이다. 다음 조건을 모두 만족한 행만 golden으로
이동하고 `review_status=validated`로 바꾼다: source hash 재확인, 질문-근거 단독 답변 가능성 확인, 기대 사실 표현 검토,
유사 문서 오답 가능성 확인, retrieval smoke 결과 확인. 승격 시 golden version과 fingerprint를 함께 갱신한다.
사람 검토는 `scripts/review_evaluation_candidate.py`로 `provenance.review_decision`에
`approve`·`revise`·`reject` 결정을 먼저 기록한다. 이 결정은 진행률과 검토 근거일 뿐 baseline 승격이 아니며,
승인 또는 수정 완료 case를 golden으로 옮기는 변경은 별도 검증한다.

```powershell
# 전체 queue와 현재 결정을 확인
uv run --no-sync python .\scripts\review_evaluation_candidate.py

# 한 case의 질문·기대 근거·참고 답변 확인
uv run --no-sync python .\scripts\review_evaluation_candidate.py `
  --case-id synthetic-candidate-skills-packaging-022

# 사람이 근거를 확인한 뒤 결정만 기록
uv run --no-sync python .\scripts\review_evaluation_candidate.py `
  --case-id synthetic-candidate-skills-packaging-022 `
  --decision revise `
  --notes "기대 chunk가 top-5에 들지 않는 원인을 확인하고 질문 범위를 좁힐 것"
```

첫 검토 순서는 기존 retrieval에서 top-5를 놓친 `skills-packaging-022`, `golden-design-023`,
`scope-risk-026` 세 건이다. 이후 multi-fact 18건, single-hop 3건 순으로 진행한다.

2026-07-16 자동 근거 사전 검토에서는 candidate 25건 모두 source chunk가 존재했고 저장된 `content_hash`와 일치했으며,
질문·기대 사실·reference answer가 해당 합성 chunk 본문에 근거했다. 별도 candidate retrieval run(top-k 5)은 Hit@5/Recall@5
88.0%, MRR 75.5%, retrieval p50 173.8ms, p95 188.3ms였다. `synthetic-candidate-skills-packaging-022`,
`synthetic-candidate-golden-design-023`, `synthetic-candidate-scope-risk-026`은 기대 chunk가 top-5에 들지 않아 어려운 실패
후보로 유지한다. 이 검사는 사람 검토를 대체하지 않으므로 25건 모두 `candidate` 상태이며 golden v3 승격은 보류한다.
MLflow run은 `d167fde4f82b463a81c643e5cd269764`다.

교정된 golden v2의 warm retrieval-only 실측(top-k 5)은 Hit@5 83.3%, Recall@5 75.0%, MRR 67.4%,
retrieval p50 174.8ms, p95 195.6ms, 오류율 0%, over-budget 0%였다. MLflow run은
`4af63b2a27dc49599469399f718c7d8e`다. 기존 v1보다 Recall/MRR이 낮아진 것은 잘못된 단일 chunk 정답을 더 엄격한
2-chunk 근거로 교정한 영향이며, no-answer accuracy 0% 문제는 similarity cutoff가 없어 그대로 남아 있다.

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

| Run | top-k | Skill | Hit/Recall/MRR | Correctness/Relevance/Groundedness | p50/p95 | 상태 |
|---|---:|---:|---|---|---|---|
| baseline `481644c3daca4c8b99012a98b4259620` | 5 | ON | 83.3/75.0/67.4% | 73.3/93.3/92.3% | 3.09/4.91s | 완료 |
| retrieval-k3 `e5b0dd945bbc4d858307dcd70881a50c` | 3 | ON | 75.0/66.7/65.3% | 66.7/93.3/92.3% | 3.86/5.79s | 완료 |
| skill-off | 5 | OFF | - | - | - | 실행 환경 분리 후 재실행 |

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
- no-answer accuracy 0%: 당시 유사도 cutoff가 없어 답이 없는 질문에도 상위 chunk를 반환한 baseline 한계를 확인
- MLflow run `499673fd79be4b1285edbf87b09ef318`에서 metric 19개, parameter 12개와 JSON artifact 2개 확인

### Phase 2. 전체 문서 챗봇 trace — 완료

Backend:

- root/agent/tool/retriever/LLM span adapter
- 기존 PlaygroundAgent를 재사용하는 end-to-end `predict_fn`
- request ID, skill hash, corpus fingerprint, Git SHA tag
- telemetry 예외 격리, 짧은 timeout, 비동기 export 또는 circuit breaker

완료 기준: 한 case에서 span 부모·자식 관계, retrieval 결과, 답변, token과 지연을 확인한다.

구현·실측 결과:

- 서비스 core에는 MLflow를 import하지 않고 `TraceObserver` 계약과 기본 no-op 구현만 추가했다.
- 실제 `PlaygroundAgent`와 `search_rag_chunks`를 재사용하는 `--mode end-to-end` runner를 추가했다.
- `document_chatbot.evaluate_case → playground.agent → playground.tool.search_rag_chunks → rag.vector_search` 아래에
  `query_embedding`, `rag.pgvector_query`를 기록하고, 답변 생성은 agent 아래 `playground.llm_generation`으로 기록한다.
- root span에 request/case ID, skill fingerprint, corpus fingerprint를 남기며 MLflow가 Git commit/branch tag를 자동 기록한다.
- trace 본문은 기본 미포함이고 `--include-trace-content`를 명시한 합성 평가에서만 포함한다.
- telemetry 실패는 `trace_errors`로 수집하고 실제 agent 실행은 계속한다. 서비스 runtime과 분리된 runner에서만
  MLflow trace를 기록한다.
- 2026-07-16 최종 합성 1-case smoke run `45f2e20b81904310bd418aea187a04d5`: trace 1개, span 7개,
  Hit@5/Recall@5/MRR/tool exact/fact coverage/citation match 모두 1.0, end-to-end 3,023.3ms,
  LLM 1회, 1,406 token.

### Phase 3. GenAI judge와 비교 run — Dataset/judge/baseline/top-k 실측 완료

Backend/Test:

- MLflow `Correctness`, `RetrievalGroundedness`, `RelevanceToQuery` 연결
- judge enabled/disabled/skipped 상태 기록
- 40-case golden dataset 확장
- baseline/top-k/skill 비교 실행

완료 기준: 코드 지표는 judge 실패와 무관하게 완료되고 세 run을 MLflow UI에서 비교할 수 있다.

현재 구현:

- validated golden과 candidate를 별도 MLflow Evaluation Dataset으로 idempotent 등록한다.
- MLflow reserved expectation key인 `expected_facts`, `expected_response`와 기존 retrieval/tool 기대값을 함께 보존한다.
- `Correctness`, `RelevanceToQuery`, `RetrievalGroundedness`를 scorer별로 격리해 실패/건너뜀/완료 상태를 기록한다.
- judge는 `end-to-end + judge + trace content + external data confirmation` 네 조건이 모두 있어야 실행한다.
- 외부 judge 전송 범위는 합성 질문·답변·reference/expected facts와 retriever trace의 합성 chunk 본문이다.
- 직접 provider judge에 필요한 `litellm==1.79.1`을 evaluation extra에 고정했다. 세 scorer는 timeout 30초, 최대 256 output
  token, retry 0으로 실행해 scorer 한 건이 수분 동안 재시도하는 것을 막는다.
- `MLFLOW_GENAI_EVAL_MAX_WORKERS=1`을 기본으로 두며 judge 결과는 release gate로 사용하지 않는다.

실측 결과:

- golden/candidate Dataset은 각각 15/25 record로 등록됐고 동일 입력 재등록 시 갱신되는 것을 확인했다.
- 1-case judge smoke `2449d8f5b69c4f9387f729e413ce1928`에서 세 scorer와 trace assessment 연결이 완료됐다.
- baseline과 retrieval-k3는 validated 15건 모두 성공했고 위 비교표의 코드·judge 지표를 기록했다.
- gpt-4.1-mini의 2026-07-16 공식 단가(input $0.40/1M, output $1.60/1M token)를 기준으로 실행 전 전체 작업을
  $0.20~$0.60, 운영 상한 $1.00, 사용자 hard cap $2.00로 잡았다. MLflow usage/cost 메타데이터와 직접 진단 호출을 합친
  기록 기반 비용은 약 $0.0857이며, 사용량이 남지 않은 timeout 시도를 보수적으로 포함해도 $0.10 미만으로 추정한다.
  가격 기준: https://developers.openai.com/api/docs/models/gpt-4.1-mini

남은 순서:

1. 현재 tool sandbox와 외부 실행 권한을 분리한 뒤 skill-off 15건을 재실행한다.
2. candidate 25건을 사람이 검토해 approve/revise/reject를 확정한다.
3. 승인된 candidate만 별도 변경으로 golden v3에 승격하고 Dataset fingerprint를 갱신한다.
4. 구현된 similarity cutoff를 실제 embedding/DB가 준비된 상태에서 재측정하고 3초대 end-to-end 지연을 최적화한다.

### Phase 4. RAG 품질·지연 안정화 — cutoff·합성 지연 최적화 구현 완료, 재측정 대기

- `RAG_SIMILARITY_CUTOFF`를 `0.0~1.0` 설정으로 추가하고 기본값을 `0.4`로 두었다.
- Phase 1 저장 artifact에서 답변 가능 case의 최고 similarity 최솟값은 `0.490`, no-answer case의 최고값은
  `0.284`였다. `0.4` replay는 Hit@5 83.3%·Recall@5 79.2%를 유지하고 no-answer 1건을 모두 거절했다.
- 검색 결과에는 `candidate_count`, `rejected_count`, `similarity_cutoff`, `top_similarity`, `no_answer`를 기록한다.
- Playground 응답에는 additive `rag_grounding` 메타데이터를 추가한다. cutoff 미달은 오류로 처리하지 않으며
  fast path와 일반 agent 경로 모두 추가 LLM 호출 없이 `insufficient_evidence`로 끝난다.
- 저장된 Phase 3 baseline의 RAG 13건은 평균 입력 1,150.6 token, 출력 185.2 token, end-to-end 3,597.3ms였고
  최대 출력 357 token case는 6,407.0ms였다. 이를 근거로 합성 입력은 `RAG_SYNTHESIS_EVIDENCE_CHARS=1800`
  안에서 상위 hit별로 균등 배분하고 출력은 `RAG_SYNTHESIS_MAX_TOKENS=256`으로 제한했다.
- RAG 전용 system prompt에서 builtin skill의 중복 지침을 제거했다. 검색 payload와 서버 조립 인용 목록은 그대로
  유지하며 LLM에 전달하는 본문만 압축한다.
- 같은 Playground agent는 LLM HTTP client를 재사용해 keep-alive를 적용한다. API router도 요청마다 agent/client를
  새로 만들지 않고 공유하며 서비스 종료 시 연결을 닫는다.
- 2026-07-20 로컬 재측정은 embedding endpoint가 준비되지 않아 13건 모두 `embedding_unavailable`로 종료됐다.
  따라서 저장된 품질 evidence와 rubric의 no-answer 0%는 새 성공 run 전까지 갱신하지 않는다.

다음 순서:

1. embedding endpoint와 pgvector를 준비해 retrieval/end-to-end 15건을 재실행한다.
2. no-answer 95% 이상, Hit@5 90% 이상, Groundedness 유지와 입력·출력 token 감소 여부를 확인한다.
3. end-to-end p95 1초에 미달하면 생성 model/서빙 설정과 hybrid/RRF 검색을 분리 실험한다.

### Phase 5. 후속 선택 기능

- 사람 평가/피드백을 trace에 연결
- 실패 trace를 evaluation dataset에 승격
- 승인된 threshold 기반 수동 release checklist
- 필요할 때만 Playground에 작은 MLflow 외부 링크 또는 trace 링크 추가
- 운영 전환 보안·보존·접근통제 별도 설계

Frontend 화면은 Phase 0~4에서 별도 변경하지 않는다. MLflow UI를 평가 결과 화면으로 사용하고
`/api/playground/chat`의 기존 필드는 유지하면서 Phase 4에 optional `rag_grounding`만 추가했다.

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
src/smb_finder/evaluation/managed_datasets.py
src/smb_finder/evaluation/judges.py
scripts/run_mlflow_doc_eval.py
scripts/register_mlflow_eval_dataset.py
scripts/start_local_stack.ps1
data/evaluation/document_chatbot_golden.jsonl
data/evaluation/document_chatbot_candidates.jsonl
tests/test_mlflow_tracing.py
tests/test_document_chatbot_scorers.py
tests/test_document_chatbot_evaluation.py
docs/MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md
```

## 11. 설정과 실행 계약

예상 환경변수:

```dotenv
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

# MLflow Evaluation Dataset 등록
uv run --extra evaluation python scripts/register_mlflow_eval_dataset.py --tier golden
uv run --extra evaluation python scripts/register_mlflow_eval_dataset.py --tier candidate

# 외부 judge 1-case smoke: 전송 범위를 확인한 경우에만 실행
uv run --extra evaluation python scripts/run_mlflow_doc_eval.py `
  --mode end-to-end `
  --provider openai `
  --model gpt-4.1-mini `
  --top-k 5 `
  --max-cases 1 `
  --judge `
  --include-trace-content `
  --confirm-external-judge-data
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
| 관측 시스템 역할 중복 | 서비스 로그=온라인 진단, MLflow=오프라인 평가·보존 trace로 역할을 고정 |
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
