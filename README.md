# automation_smb — 공유 폴더 찾기 서비스

텍스트(이후 음성) 명령으로 **SMB 공유폴더를 즉시 찾아주는 챗봇 서비스**다.
노코딩 자동화 툴(→ [`OS.md`](./OS.md))의 첫 성공 케이스이자 L4 도구다.
현재 개발 단계와 작업 규칙·지연 요구는 [`AGENTS.md`](./AGENTS.md) 참고.

> **현재는 합성 데이터 전용 기능 테스트 환경이다.** 실제 의료 환경과 닮지 않은 더미 폴더·파일·문장만 사용해
> 챗봇 대화, tool 호출, trace, 지연을 검증한다. 의료데이터 입력 탐지나 provider별 합성 데이터 gate는 두지 않으며,
> 운영용 보안 검토는 기본 기능 테스트 이후 별도 단계에서 진행한다.

## 개발 환경 설정

처음 참여하는 개발자는 역할별 설치·실행·검증 절차를 정리한
[`Frontend/Backend 개발 환경 설정`](docs/DEVELOPMENT_SETUP.md)부터 확인한다. 현재 Frontend는 별도 Node 빌드가
없는 정적 UI이며, Backend와 함께 합성 데이터 모드로 시작하는 방법을 안내한다.

## 현재 개발 우선순위

1. **QC Report 감사·LLM 초안 수직 슬라이스**: 첨부 감사와 구조화 측정값 기반 검토용 Markdown 생성
2. **코드 우선 tool 계약**: Python 도메인 코드와 Pydantic 계약을 공통 `ToolCatalog`/`ToolExecutor`에서 관리
3. 실제 SOP compiler·규칙 버전·근거 위치와 초안 검토·승인 상태 계약
4. 합성 평가셋 정확도/지연 검증, 이미지형 PDF OCR, 표 구조 복원과 결과 내보내기

Playground는 같은 프로세스의 executor를 직접 호출하고, 외부 MCP Host만 `/mcp`에서 명시적으로 승인된 읽기 도구를
호출한다. QC 상세 범위와 단계는 [`docs/QC_REPORT_AUDIT_PLAN.md`](docs/QC_REPORT_AUDIT_PLAN.md)를 따른다.

## 핵심 설계 — 지연 최소화

매 요청마다 SMB를 도는 대신, **인덱스 우선**으로 폴더 트리를 미리 구성(메모리+JSON 캐시)하고
**인메모리로 검색**한다.
자연어는 규칙 기반 fast-path로 키워드를 뽑고, 모호할 때만 로컬 LLM을 태운다(timeout 강제).

```
텍스트 질의 → intent.normalize(규칙/LLM) → FolderIndex.search(인메모리) → 결과(+소요 ms)
                                              ▲ 미리 빌드된 인덱스 (SMB 순회는 시작/갱신 시에만)
```

## 두 가지 검색

| 검색 | 무엇으로 찾나 | 엔드포인트 | 저장소 |
|---|---|---|---|
| **폴더 찾기** | 폴더 이름·경로 | `POST /find` | 인메모리 인덱스 + JSON 캐시 |
| **내용 찾기** | 파일 **본문** 키워드 | `POST /search-content` | SQLite **FTS5(trigram)** |

내용 검색은 trigram 토크나이저라 **한국어·영문·검사코드 모두 부분일치**가 되고, 형태소 분석기 같은
**외부 의존성이 없다**(온프레미스·SSL프록시 환경에 적합). 3글자 미만 질의는 `LIKE`로 폴백한다.
`docx`/`xlsx`는 stdlib `zipfile`로, `pdf`는 선택 의존성(`pypdf`)으로 본문을 뽑는다.

테스트용 내용 인덱스(`.cache/content.fts.db`)에는 합성 fixture 본문만 적재한다. `.cache/`는 실행 중 생성되는
로컬 캐시이므로 커밋하지 않는다.

## API 계약 · 관측

`POST /find`와 `POST /search-content` 응답은 결과 목록과 함께 `result_count`, `elapsed_ms`,
`over_budget`을 반환한다. `POST /refresh`, `POST /refresh-content`, `GET /health`도 명시적인
Pydantic 응답 모델을 사용해 OpenAPI 도구 계약을 고정한다.

- `/health`: `ready`, `indexed_folders`, `indexed_files`, 인덱스 로드 여부를 반환한다.
- `/refresh`: 폴더 인덱스 갱신 후 폴더 수와 소요 시간을 반환한다.
- `/refresh-content`: 내용 인덱싱 통계와 소요 시간을 반환하되, SMB host/share 실제 값은 노출하지 않는다.
- `/admin/content-index-jobs`: 내용 인덱싱을 백그라운드 job으로 실행한다. `X-Admin-Token` 필요.

로그에는 요청 ID, 질의 길이, 결과 수, 소요 시간과 예산 초과 여부를 기록해 기능과 지연을 확인한다.

## MCP 로컬 MVP

기존 FastAPI 프로세스의 검색 runtime을 그대로 사용해 `http://127.0.0.1:8010/mcp`에
Streamable HTTP MCP를 선택적으로 제공한다. 기본값은 `MCP_ENABLED=false`라 endpoint가 마운트되지 않는다.
활성화된 MCP가 공개하는 도구는 읽기 전용 검색 두 개뿐이다.

- `find_folder`: 사전 구축된 인메모리 폴더 인덱스 검색
- `search_content`: 사전 구축된 로컬 FTS5 내용 인덱스 검색

관리자·인덱스 갱신·보고서·핵형요약 도구는 MCP catalog에 등록하지 않는다. 결과에서도 원문 query, 본문
`snippet`, 절대경로, SMB host/share, 내부 IP와 자격증명을 제외한다. MCP 요청이 SMB를 직접 순회하거나
인덱스를 갱신하지도 않는다.

로컬 테스트에서만 아래 값을 `.env`에 추가한다. `MCP_API_TOKEN`은 `ADMIN_API_TOKEN`과 다른 임의값을 쓰고,
서비스는 계속 `--host 127.0.0.1`로 바인딩한다.

```powershell
MCP_ENABLED=true
MCP_API_TOKEN=<별도로 생성한 충분히 긴 임의값>
MCP_ALLOW_REMOTE=false
MCP_ALLOWED_HOSTS=127.0.0.1,localhost,[::1]
MCP_ALLOWED_ORIGINS=
```

Origin 없는 로컬 서버형 client는 허용한다. Origin 헤더가 있는 client는 `MCP_ALLOWED_ORIGINS`에 정확한 값을
명시해야 하며 wildcard는 사용하지 않는다. 기본 CORS는 꺼져 있어 브라우저의 직접 교차 출처 연결은 이번 MVP에서
지원하지 않는다. 요청에는 `Authorization: Bearer <MCP_API_TOKEN>`이 필요하고, 본문은 64KiB로 제한된다.
사내 다중 사용자 공개와 OAuth/SSO는 이 로컬 MVP의 범위가 아니다.

## 구조

| 파일 | 역할 |
|---|---|
| `src/smb_finder/config.py` | `.env` 설정 로더 (SMB·인덱스·LLM·시간예산·내용검색) |
| `src/smb_finder/models.py` | 입출력 Pydantic 모델 (`Find*`/`ContentSearch*`) |
| `src/smb_finder/smb_client.py` | SMB 세션 + 트리 순회 (폴더 `walk_folders`, 파일 `walk_files`) |
| `src/smb_finder/index.py` | 폴더 인메모리 인덱스 + 빠른 검색 + JSON 캐시 |
| `src/smb_finder/indexer.py` | SMB 순회로 폴더 인덱스 빌드 (시작/백그라운드) |
| `src/smb_finder/intent.py` | 자연어 → 키워드 (규칙 우선, LLM 선택) |
| `src/smb_finder/finder.py` | 폴더 검색 오케스트레이터 (정규화→검색→응답, 시간 측정) |
| `src/smb_finder/extract.py` | 파일 본문 추출 (텍스트/`docx`/`xlsx`/`pdf`, cp949 폴백) |
| `src/smb_finder/content_index.py` | 내용 FTS5(trigram) 저장 + 검색 (bm25·snippet) |
| `src/smb_finder/content_indexer.py` | SMB 파일 순회→추출→FTS5 적재 (관리/백그라운드) |
| `src/smb_finder/content_search.py` | 내용 검색 오케스트레이터 (토큰화→검색, 시간 측정) |
| `src/smb_finder/rag_search.py` | 로컬 PostgreSQL 질의 임베딩→pgvector chunk 검색 (단계별 시간 측정) |
| `src/smb_finder/evaluation/` | validated/candidate dataset, retrieval/end-to-end scorer, MLflow Dataset·judge·trace adapter |
| `src/smb_finder/reports/` | 검사 보고서 업무 보조 tool — LLM 기반 ISCN 요약 + 로컬 후보 문서 검색/체크리스트 |
| `src/smb_finder/qc_audit/` | 첨부 추출·합성 SOP 감사·LLM 비수치 서술·QC Markdown 초안 조립과 재검증 |
| `src/smb_finder/tooling/` | 검색 도구 공통 Pydantic 계약·불변 catalog·timeout/동시 실행/감사 로그 executor |
| `src/smb_finder/mcp_server.py` | 읽기 전용 MCP 검색 도구 등록과 HTTP transport 설정 |
| `src/smb_finder/api.py` | FastAPI 앱 — `POST /find`·`/search-content`·`/refresh*` (OpenAPI 도구) |
| `src/smb_finder/playground/` | 자체 챗봇 Playground — 선택한 tool만 호출하는 local LLM 기반 채팅 API |
| `src/smb_finder/web/` | `/playground` 정적 UI — tool 선택, 채팅, Tool Lab 초안 화면 |
| `scripts/start_local_stack.ps1` | Playground 기본 실행과 MLflow·LangGraph Studio 선택 실행을 지원하는 Start/Stop/Status 실행기 |
| `scripts/start_remote_embedding_tunnel.ps1` | PuTTY 저장 세션으로 원격 온프레미스 embedding 서버를 `127.0.0.1:18080/v1`에 연결 |
| `scripts/run_mlflow_doc_eval.py` | 문서 RAG retrieval/end-to-end golden evaluation과 선택적 LLM judge CLI |
| `scripts/register_mlflow_eval_dataset.py` | golden/candidate JSONL을 로컬 MLflow Evaluation Dataset으로 등록 |
| `scripts/review_evaluation_candidate.py` | candidate 사람 검토 결정을 기록하고 golden 승격과 분리 |
| `scripts/evaluate_quality.py` | 저장소 전용 100점 rubric과 hard gate를 로컬/CI에서 동일하게 평가 |
| `scripts/check_development_lessons.py` | commit/push 전 짧은 개발 교훈 문서와 명시적 검토 상태를 검사 |
| `scripts/enable_quality_hook.ps1` | 추적된 pre-commit hook을 현재 clone에 활성화 |
| `config/project_quality_rubric.json` | 100점 배점·hard gate·합성 실측 evidence의 machine-readable 기준 |
| `.githooks/pre-commit`, `.github/workflows/project-quality.yml` | 커밋과 push/PR에서 품질 hard gate 실행 |
| `data/evaluation/` | 검증 완료 golden 15건과 검토 전 corpus 기반 candidate 25건을 분리한 합성 JSONL fixture |
| `docs/DEVELOPMENT_SETUP.md` | Frontend/Backend 역할별 개발 환경 설치·실행·검증 가이드 |
| `docs/PLAYGROUND_TOOLS.md` | `/playground` 등록 tool별 입력·동작·테스트 계약 |
| `docs/QC_REPORT_AUDIT_PLAN.md` | QC Report 감사 우선순위, 입출력 계약, 단계별 구현 계획과 현재 제한 |
| `docs/LLM_REPORT_PROCESS_FOR_AUTOMATION_SMB.md` | 세포유전 LLM 보고서 프로세스 이관 가이드 |
| `docs/TOOL_MCP_LANGCHAIN_ARCHITECTURE.md` | 공통 ToolSpec을 중심으로 REST·Playground·MCP·LangChain을 연결하는 목표 아키텍처와 흐름도 |
| `docs/MCP_IMPLEMENTATION_PLAN.md` | 검색 도구 2개부터 시작하는 MCP MVP의 작업 순서·테스트·롤백 계획 |
| `docs/playground_agentic_plan.md` | Playground 제한형 agent loop와 debug 토글 구현 계획 |
| `docs/MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md` | MLflow 기반 문서 RAG 검색·답변·지연 평가 최종 개발 계획 |
| `docs/PROJECT_QUALITY_RUBRIC.md` | 코드 변경마다 적용하는 100점 품질 기준, hard gate, 로컬/CI 실행 계약 |
| `docs/PRODUCTION_ARCHITECTURE.md` | 실제 온프레미스 운영 토폴로지, MinIO 범위, 장애·전환 계획 |
| `docs/DEVELOPMENT_LESSONS.md` | 작업 중 반복 가능한 문제와 다음 적용 원칙을 최신 12개 이내로 유지 |
| `integrations/langgraph/` | LangGraph 외피 — 같은 HTTP 호출을 LangGraph Studio(로컬)로 관리·디버깅 ([README](integrations/langgraph/README.md)) |

## 코드 우선 tool·MCP 연결

업무 tool은 외부 workflow 제품에서 만들거나 동기화하지 않는다. Python 도메인 서비스와 Pydantic 모델을 먼저
구현하고, 공통 catalog에 handler·timeout·surface·권한을 등록한다. Playground는 이 계약을 in-process로 사용하고
MCP adapter는 `allowed_surfaces`에 `mcp`가 선언된 읽기·멱등 tool만 공개한다. 현재 공개 목록은
`find_folder`, `search_content` 두 개다.

## 자체 챗봇 Playground

`smb_finder` 안에서 바로 쓰는 챗봇/tool UI를 제공한다.

- 화면: `GET /playground`
- tool 목록: `GET /api/playground/tools`
- PDF/Markdown 첨부/삭제: `POST /api/playground/attachments`, `DELETE /api/playground/attachments/{attachment_id}`
- skill 목록/생성: `GET|POST /api/playground/skills`
- skill 수정/삭제: `PUT|DELETE /api/playground/skills/{skill_id}`
- ISCN 핵형 요약: `POST /api/playground/karyotype-summary`
- 채팅 실행: `POST /api/playground/chat`
- Tool Lab 초안: `POST /api/playground/tool-draft`
- local LLM 확인: `POST /api/playground/llm-status`

기본 tool에는 `draft_qc_report`, `audit_qc_report`, `extract_uploaded_document`, `search_sop_knowledge`, `find_folder`, `search_content`,
`search_rag_chunks`, `cytogenetics_karyotype_summary`, `cytogenetics_report`, `ngs_report`, `refresh_content`,
`create_playground_skill`이 있다. UI는 `SMB 직접 접근`, `DB 접근`,
`보고서 관련`, `Skill 관리` 분류만
먼저 표시하고, 분류를 클릭하면 내부 tool 선택지가 열린다.

`PDF/MD 첨부`로 한 파일을 올린 뒤 전송하면 기본 선택된 `audit_qc_report`가 결정 LLM을 생략하고 로컬에서 즉시
실행된다. 감사 tool 내부에서 본문 추출과 SOP 검색을 조합하므로 agent 호출 제한을 소모하는 세 단계 호출이 필요 없다.
현재 SOP는 실제 검사실 자료와 무관한 `synthetic-qc-sop-v1`이며 결과에도 합성 기준임을 표시한다. PDF는 텍스트형만
지원하고 스캔 이미지 OCR은 후속 단계다.

`draft_qc_report`는 `temperature_c`, `recovery_rate_pct`, `self_check_status`와 선택적 `operator_notes`를 받는다.
코드가 먼저 합성 SOP 판정과 관찰값 표를 고정하고, 선택한 provider/model은 숫자 없는 요약·해석·후속 검토 문장만
생성한다. 조립된 Markdown은 기존 감사 엔진으로 다시 검사하며 값이나 판정이 달라지면 반환하지 않는다. 성공 결과도
항상 `DRAFT - HUMAN REVIEW REQUIRED`이며 최종 보고서로 자동 확정하거나 파일로 보존하지 않는다. 출력 token은
`PLAYGROUND_QC_DRAFT_MAX_TOKENS`로 제한하고 LLM·재검증·전체 지연을 trace에 남긴다.

Tool Lab은 내장 도구의 manifest 초안을 만들고 현재 catalog 계약을 확인하는 용도로만 사용한다. 외부 MCP 연결은
Playground가 아니라 승인된 MCP Host가 `/mcp`를 통해 수행한다. 자세한 계약은
[`docs/PLAYGROUND_TOOLS.md`](docs/PLAYGROUND_TOOLS.md)를 참고한다.

채팅 입력창 아래 `Skills` 버튼에서는 실제 agent와 같은 `<skill-id>/SKILL.md` 형식의 스킬을 선택·조회·추가·수정·삭제한다.
기본 스킬은 코드와 함께 제공되는 읽기 전용 문서이며, UI에서 만든 사용자 스킬은
`PLAYGROUND_SKILLS_DIR`(기본 `.cache/playground-skills`) 아래에 같은 디렉터리 구조로 저장된다. 선택한 SKILL.md의
frontmatter와 본문 지침은 채팅 요청의 agent system prompt에 실제로 주입되고, 응답의 `active_skill_ids`에서 적용 여부를
확인할 수 있다. `/tools`는 모든 tool 설명을, `/skills`는 설치된 모든 skill 설명을 LLM 호출 없이 즉시 반환한다.

기본 제공 skill은 다음과 같다.

- `tools`, `skills`: `/tools`, `/skills` slash command 사용법과 목록 출력
- `skill-creator`: 대화로 실제 `<skill-id>/SKILL.md`를 생성하고 같은 요청의 다음 agent 판단부터 즉시 적용
- `rag-grounded-answer`: PostgreSQL vector chunk 근거를 먼저 찾고 문서 위치와 함께 답변
- `smb-navigation`: 폴더명 검색과 파일 본문 검색 중 가장 작은 tool을 선택
- `report-workflow`: 핵형·세포유전·NGS 요청을 알맞은 보고서 tool로 라우팅
- `latency-first`: 불필요한 재호출을 줄이고 시간 예산 안에서 짧게 응답

새 스킬을 대화로 만들려면 아래 `Skills` 버튼에서 `skill-creator`를 선택하고, 예를 들어
`합성 프로젝트 회의록을 세 줄로 요약하는 스킬을 만들어줘`라고 입력한다. agent가 로컬
`create_playground_skill` tool을 호출하면 검증된 SKILL.md가 `PLAYGROUND_SKILLS_DIR`에 저장되고, 응답 직후 UI 선택 목록과
현재 대화에 함께 활성화된다. 생성 tool은 `skill-creator`가 활성화되면 별도 tool 체크 없이 agent에 자동 연결된다.

`search_rag_chunks`는 `rag_db_local_20260713.document_chunks`를 cosine 유사도로 검색한다. DB에 저장된
`nomic-embed-text-v2-moe`와 동일한 768차원 모델 endpoint가 필요하며, 질의에는 모델 권장
`search_query: ` prefix를 자동으로 붙인다. 모델은 로컬 PC가 아니라 원격 온프레미스 Linux에서 실행하며,
원격 서비스는 `127.0.0.1:18080`에만 바인딩한다. Windows의 PuTTY/Plink SSH 터널이 이를
`http://127.0.0.1:18080/v1`로 전달한다. 연결은 `.env`의 `RAG_DB_*`, `RAG_EMBEDDING_*`로 바꿀 수 있다.
`RAG_SIMILARITY_CUTOFF` 기본값은 저장된 합성 baseline으로 보정한 `0.4`다. 기준 미달 chunk는 답변 근거에서
제외하며, 모두 미달이면 응답의 `rag_grounding.decision=insufficient_evidence`와 최고 similarity·기준값을 반환한다.
기준을 통과한 chunk만 agent observation으로 전달되고 선택한 LLM이 근거 기반 최종 답변을 작성한다.
`search_rag_chunks` tool과 `rag-grounded-answer` skill만 선택한 문서 챗봇 요청은 결정용 LLM 호출을 생략하는
RAG fast path로 실행된다. 검색 성공 시 근거 합성 LLM을 1회만 호출하고, 검색 오류·무결과·cutoff 미달이면
추가 LLM 호출 없이 즉시 상태를 반환한다. 일반 agent 경로도 RAG cutoff 미달 뒤 두 번째 판단 LLM을 호출하지 않는다.
합성 LLM에는 `RAG_SYNTHESIS_EVIDENCE_CHARS`(기본 1800) 안에서 상위 5개 근거 본문을 균등 배분해 전달하고,
출력은 `RAG_SYNTHESIS_MAX_TOKENS`(기본 256)로 제한한다. 서비스 프로세스에서는 LLM HTTP keep-alive 연결을 재사용한다.
Playground의 `문서 근거` 영역에서는 실제 검색 결과의 파일명, 섹션/위치, 유사도와
embedding/DB 지연을 접어서 확인할 수 있다.
보고서 tool은 문서를 자동 확정하지 않고, 로컬 내용 인덱스에서 관련 템플릿 후보를 찾은 뒤
작성 체크리스트와 다음 행동을 제시한다. 결과 trace에는 구조화 payload가 포함되어 UI가 후보 문서와
체크리스트를 별도 블록으로 표시한다.

Playground는 local/on-prem OpenAI 호환 LLM과 OpenAI API provider를 둘 다 지원한다.
local provider는 `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TIMEOUT_MS` 환경변수를 사용한다.
OpenAI provider는 UI의 Settings에 임시 API key를 넣거나 서버 `.env`의 `OPENAI_API_KEY`를 사용한다. UI key가 비어 있으면
서버 `.env` 값을 자동 사용하므로 재시작·새로고침마다 다시 입력할 필요가 없다. 실제 key는 `.env.example`이나 커밋에 넣지 않는다.
현재는 합성 데이터 전용 테스트이므로 활성화된 tool은 local/OpenAI provider에서 같은 방식으로 선택·실행된다.
별도 합성 데이터 토글이나 provider별 tool 차단은 없다. LLM 모델이 비어 있으면 UI는 tool을 실행하지 않고
설정 필요 메시지를 반환한다.
화면에서 Base URL과 모델명을 임시 입력해 `.env` 수정 없이 Ollama/LM Studio/llama.cpp 같은 로컬 서버를 확인할 수 있다.
예: `http://127.0.0.1:11434/v1`, `qwen2.5-coder:7b`.
Qwen3 계열은 기본 thinking이 JSON 응답 예산을 소진할 수 있어 Playground의 JSON 전용 호출에 `/no_think`를 자동 적용한다.
OpenAI-compatible 응답에 `usage`가 있으면 채팅 응답과 연결 확인 결과에 `token_usage`가 포함되어
모델별 input/output/total token과 호출 수를 볼 수 있다.

## LangGraph Studio 개발 디버깅

LangGraph Studio는 `smb_agent` graph를 수동 실행하고 상태·tool 호출을 확인하는 개발 도구다. Playground 실행을
복제하거나 운영 trace를 저장하지 않으며, 평가·run 비교·trace의 기준점은 MLflow다. 필요할 때만 별도로 실행한다.

```powershell
# 1. Studio 의존성 설치
uv sync --native-tls --extra dev --extra studio

# 2. LangGraph 로컬 Agent Server 실행
# 최초 실행 시 .env.example을 .env.studio로 복사한다.
cd integrations/langgraph
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_local_studio.ps1
```

## MLflow 문서 챗봇 평가

MLflow는 평가·실험 비교·trace의 단일 기준점이며 Playground 요청 경로와 분리된 선택적 오프라인 계층이다.
Phase 1은 검증 완료된 15개 합성 golden case로
`RagVectorSearcher`를 직접 실행하고, Phase 2는 실제 `PlaygroundAgent`를 재사용해 tool 선택·검색·답변·token·전체 지연을
평가한다. Phase 2 trace는 `workflow → agent → tool → retriever → embedding/DB`와 생성 LLM span을 한 run에서 보여준다.
평가 결과는 `automation-smb-doc-chatbot` experiment의 metric, parameter, JSON artifact로 기록하며 MLflow가 없어도
Playground 서비스 import와 채팅 동작에는 영향이 없다.

Phase 3에서는 `document_chatbot_golden.jsonl`의 validated 15건만 baseline에 사용한다. corpus 6개 문서·115개 chunk에서
확장한 25건은 `document_chatbot_candidates.jsonl`에 따로 두며 검토·승격 전에는 기본 평가에 들어가지 않는다. 두 파일은
MLflow Evaluation Dataset으로 별도 등록할 수 있고, 동일 질문·`top_k` 입력은 MLflow의 input-hash 기준으로 갱신된다.
2026-07-16 실측에서는 golden baseline(k=5)과 k=3 비교, 세 LLM judge가 완료됐다. candidate 사전 검토는 25건 모두
chunk/hash 근거 일치를 통과했고 retrieval Hit@5/Recall@5 88.0%였지만, 사람 승인 전이므로 모두 candidate로 유지한다.

```powershell
# 실행 중인 로컬 스택을 멈춘 상태에서 최초 1회
uv sync --native-tls --extra dev --extra evaluation

# MLflow UI 시작
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Start -Profile Mlflow

# retrieval-only baseline 기록
uv run --no-sync python .\scripts\run_mlflow_doc_eval.py `
  --mode retrieval `
  --dataset .\data\evaluation\document_chatbot_golden.jsonl `
  --top-k 5

# 실제 agent end-to-end 평가(먼저 1건 smoke 후 --max-cases를 제거해 전체 실행)
uv run --no-sync python .\scripts\run_mlflow_doc_eval.py `
  --mode end-to-end `
  --provider openai `
  --model gpt-4.1-mini `
  --top-k 5 `
  --max-cases 1

# MLflow 없이 로컬 지표만 확인
uv run --no-sync python .\scripts\run_mlflow_doc_eval.py --top-k 5 --no-mlflow

# validated golden과 candidate를 서로 다른 MLflow Dataset으로 등록
uv run --no-sync python .\scripts\register_mlflow_eval_dataset.py --tier golden
uv run --no-sync python .\scripts\register_mlflow_eval_dataset.py --tier candidate

# 외부 LLM judge opt-in: 아래 네 항목을 모두 명시해야 실행
# 전송 범위는 합성 질문·답변·정답과 검색된 합성 chunk 본문이다.
# 먼저 --max-cases 1로 token/cost를 확인하고 전체 실행 여부를 결정한다.
uv run --no-sync python .\scripts\run_mlflow_doc_eval.py `
  --mode end-to-end `
  --provider openai `
  --model gpt-4.1-mini `
  --top-k 5 `
  --max-cases 1 `
  --judge `
  --include-trace-content `
  --confirm-external-judge-data
```

결과 UI는 `http://127.0.0.1:5000`에서 확인한다. golden fixture의 문서 key는 전체 내부 경로가 아닌 파일명 기반이다.
trace에는 기본적으로 질문·답변·chunk 본문을 넣지 않으며, 합성 데이터 디버깅이 필요할 때만
`--include-trace-content`를 명시한다. `Correctness`, `RelevanceToQuery`, `RetrievalGroundedness` judge는 하나가 실패해도
결정적 코드 지표와 나머지 scorer를 보존하며 release gate로 쓰지 않는다. judge 요청은 scorer당 최대 256 output token,
30초 timeout, retry 0을 기본으로 해 비용과 장시간 재시도를 제한한다. 자세한 단계별 계획과 baseline은
[`docs/MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md`](docs/MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md)를 참고한다.

현재 MLflow의 SQLite metadata와 `.cache/mlflow/artifacts`는 단일 개발 머신 전용이다. 실제 운영 pilot에서는
metadata를 PostgreSQL로, 허용된 평가 artifact를 온프레미스 MinIO로 분리한다. MinIO는 검색 hot path나 실제
원본 SMB 파일의 복제 저장소로 사용하지 않는다. 운영 경계는
[`docs/PRODUCTION_ARCHITECTURE.md`](docs/PRODUCTION_ARCHITECTURE.md)를 참고한다.

## 설치 · 실행

### 통합 로컬 스택

Playground, MLflow, LangGraph Studio는 별도 프로세스이지만 루트에서 한 스크립트로 필요한 조합을 실행할 수 있다.
인자 없이 실행하면 Playground만 시작한다. `Mlflow`는 평가할 때, `Studio`는 graph를 디버깅할 때 사용하며
`Studio` 프로필은 HTTP tool 호출에 필요한 Playground도 함께 시작한다. `All`은 세 도구가 모두 필요할 때만 명시한다.

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb

# 기본: Playground(:8010)만 실행
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Start

# 전체 실행이 필요할 때 먼저 명령 확인
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Plan -Profile All

# Playground(:8010) + MLflow(:5000) + LangGraph Studio(:2024)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Start -Profile All
```

지원 프로필은 `Playground`, `Mlflow`, `Studio`, `All`이다. 기본 프로필은 `Playground`이며 숨김 프로세스로 실행하고
`.cache/local-stack/logs`에 stdout/stderr를 저장한다. 서비스별 콘솔 창이 필요하면 `-Visible`, Playground의
reload를 끄려면 `-NoReload`를 추가한다. MLflow는 현재 선택 의존성이므로 첫 실행 시 `uv run --with`가
`mlflow[genai]>=3.9,<4`를 로컬 uv cache에 준비한다.

이 스크립트는 사용 환경마다 실행 명령이 다른 local LLM, embedding server, PostgreSQL은 시작하지 않는다.
문서 RAG를 쓰려면 해당 endpoint와 DB를 별도로 준비해야 한다. 기존
`integrations/langgraph/start_local_studio.ps1`는 그대로 유지하며 통합 스크립트가 내부에서 재사용한다.

```powershell
# uv.lock 기준으로 Python 3.11 가상환경과 개발 의존성을 한 번에 맞춘다.
uv sync --python 3.11 --native-tls --extra dev
Copy-Item .env.example .env                  # 합성 테스트에서는 SMB 자격증명을 비워 둔다.

# 서버 실행
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010 --reload

# PuTTY에서 원격 Linux 접속을 저장하고 먼저 대화형 로그인을 1회 확인
# 원격 서버에는 DB 적재 때 사용한 정확한 모델 변형으로 embedding 서비스를
# 127.0.0.1:18080에만 기동한다. 설치 전 CPU/RAM/디스크와 기존 포트 사용 여부를 확인한다.

# 별도 터미널: 비밀번호 기반 PuTTY 세션은 대화형 foreground 터널로 시작
powershell -ExecutionPolicy Bypass -File scripts\start_remote_embedding_tunnel.ps1 `
  -SessionName "<PUTTY_SAVED_SESSION>" -Foreground

# Pageant 또는 키 기반 비대화형 인증이 준비된 경우 -Foreground를 빼면 숨김 백그라운드로 실행

# 폴더 찾기 요청 (이름·경로)
curl -s -X POST http://localhost:8010/find \
  -H 'Content-Type: application/json' \
  -d '{"query": "demo project alpha 폴더 찾아줘"}'

# 내용 찾기 요청 (파일 본문)
curl -s -X POST http://localhost:8010/search-content \
  -H 'Content-Type: application/json' \
  -d '{"query": "orange widget revision"}'

# 폴더 인덱스 갱신 (이름 검색용)
curl -s -X POST http://localhost:8010/refresh

# 내용 DB화 — 원하는 폴더만 인덱싱 (path 지정, 관리자 토큰 필요)
curl -s -X POST http://localhost:8010/refresh-content \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>' \
  -d '{"path": "demo/projects/alpha"}'

# 운영 권장: 관리자용 백그라운드 job으로 내용 DB화
curl -s -X POST http://localhost:8010/admin/content-index-jobs \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>' \
  -d '{"path": "demo/projects/alpha"}'

curl -s http://localhost:8010/admin/content-index-jobs/<job_id> \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>'
```

> 내용 인덱스는 추출이 무거워 **시작 시 자동 빌드하지 않는다.** 공유 전체를 한 번에 돌지 않고,
> `/refresh-content`에 **폴더 경로(path)**를 줘서 원하는 폴더만 DB화한다(여러 폴더는 누적, 같은 폴더는
> 갱신, `path` 생략 시 전체). 같은 파일은 `size/mtime`이 같으면 본문 읽기를 건너뛰고,
> 이번 범위에서 사라진 기존 인덱스만 정리한다. 이후 `/search-content`는 떠 있는 인덱스에서 즉시 검색한다.
> (`PDF` 본문까지 쓰려면 `uv pip install -e ".[pdf]"`).
> 운영 자동화와 관리자 UI는 동기 `/refresh-content` 대신 `/admin/content-index-jobs`를 사용한다.
> `ADMIN_API_TOKEN`이 비어 있으면 `/refresh`, `/refresh-content`, `/admin/*` 인덱싱 엔드포인트는 닫힌다.
> host/share override는 `SMB_ALLOWED_HOSTS`, `SMB_ALLOWED_SHARES` 또는 기본 `SMB_HOST`/`SMB_SHARE_NAME`에
> 포함된 대상만 허용한다.

## 테스트 · 린트

```bash
.venv/Scripts/python -m pytest tests/ -m "not integration"   # 라이브 SMB 불필요
.venv/Scripts/python -m ruff check src tests integrations/langgraph scripts

# 테스트·린트·문서 링크·저장소 경계를 한 번에 평가
uv run --no-sync python scripts/evaluate_quality.py

# 개발 교훈 문서 형식과 개수 확인
uv run --no-sync python scripts/check_development_lessons.py
```

권장 품질 목표는 **hard gate 전부 통과 + 85점 이상**이다. JSON 결과는 `--json-output <path>`, 85점 미만도
실패 처리하려면 `--strict-score`, Ruff/pytest 없이 빠른 정적 사전 점검만 하려면 `--static-only`를 사용한다.
`--static-only`는 실행 hard gate를 건너뛰므로 완료 판정에는 사용할 수 없다.
현재 clone의 pre-commit hook은 다음 명령으로 한 번 활성화한다. push와 PR에서는 전체 평가와 hard gate를
강제하고 점수는 advisory로 표시한다. release-readiness 확인 때 `--strict-score`를 별도로 실행한다.
pre-commit은 staged 코드·설정 변경에 교훈 문서가 포함되지 않으면 검토 알림을 내며, push 절차에서는 outgoing
diff에 대한 문서 갱신 또는 `--reviewed-no-change` 판단을 명시적으로 요구한다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\enable_quality_hook.ps1
```

평가 항목, hard gate와 합성 실측 근거 갱신 방법은
[`docs/PROJECT_QUALITY_RUBRIC.md`](docs/PROJECT_QUALITY_RUBRIC.md)를 참고한다.

## 다음 단계

- **공통 catalog 단계적 확장**: 검색 tool의 Playground manifest를 Pydantic schema에서 파생하고,
  `search_rag_chunks`, QC·보고서 tool을 tool별 계약·회귀 테스트와 함께 이관하되 MCP 공개 여부는 별도로 승인한다.
- **QC 초안 평가·승인 계약**: local/OpenAI 합성 case에서 사실 보존·숫자 위반 차단·재감사 일치율과 p50/p95를
  측정하고 `DRAFT → REVIEWED → APPROVED/REJECTED` 상태 및 승인 템플릿을 정의한다.
- **Phase 4 재측정**: 원격 온프레미스 embedding endpoint의 SSH 터널과 pgvector를 준비해 구현된
  cutoff·압축 근거·256 token 상한·
  HTTP keep-alive를 같은 15건으로 재측정하고 no-answer 95% 이상, end-to-end p95 1초 목표를 확인한다.
- **검색 품질**: PostgreSQL/pgvector RAG와 FTS5를 기준으로 hybrid/RRF·reranking 필요성을 검증하고
  Hit@5 90% 이상을 추적한다.
- **candidate 검토**: 25건을 사람이 approve/revise/reject로 확정하고 승인된 항목만 golden v3로 승격한다.
- `hwp`/`hwpx` 본문 추출 추가(로컬 파서, 선택 의존성).
- 음성(STT) 입력 추가 — L1, 로컬 모델(`OS.md` 8장 미확정 항목).
