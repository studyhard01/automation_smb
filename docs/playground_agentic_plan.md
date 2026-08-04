# Playground 챗봇 기능 테스트 설계

## 현재 단계

Playground는 실제 의료 환경과 무관한 합성 데이터로만 기능을 검증한다. 2026-08-03부터 최우선 목표는 자연어로
문서를 찾고 선택한 뒤 `문서 요약`, `문서 기반 Q&A`, `파일 버전 확인`, `보고서 작성`을 실행하는 흐름이다. 검색은
선택 tool이 아니라 RAG fast path를 포함한 기본 동작으로 둔다. 기존 PDF/Markdown QC 감사와 LLM 초안은 삭제하지
않고 선택 업무 기능과 회귀 fixture로 유지한다.

화면·데이터·단계별 기준은 [문서 검색·활용 서비스 개발 계획](PRODUCT_DEVELOPMENT_PLAN.md)을 우선한다. 외부
`llmops` 데이터셋의 상세 읽기 계약은 [데이터셋 DB 연동 계획](DATASET_DB_INTEGRATION_PLAN.md)을 따른다.

운영용 의료데이터 탐지, provider별 합성 데이터 허용 gate, 별도 안전 프로필은 기본 기능 테스트 이후에 다시
설계한다. 현재 기능 경로에는 이를 추가하지 않는다.

## 사용자 흐름

### 기본 문서 흐름

1. 사용자가 자연어로 파일이나 업무 내용을 찾는다.
2. 인덱스 검색이 파일명·위치·최신본 후보와 소요 시간을 반환한다.
3. 사용자가 문서를 선택하고 요약·Q&A·버전 확인·보고서 작성 중 하나를 실행한다.
4. 답변은 핵심 결과, Citation, 최신본 판단 근거, 지연 순서로 표시한다.
5. 보고서 같은 고정 출력은 우측 산출물 영역에서 상태와 다운로드를 표시한다.

### 기존 QC 회귀 흐름

1. `/playground`에서 합성 PDF 또는 Markdown QC Report 한 건을 첨부한다.
2. `audit_qc_report`가 선택된 상태에서 감사 요청을 전송한다.
3. 서버가 외부 LLM 없이 문서를 추출하고 합성 SOP 세 항목과 대조한다.
4. PASS/WARNING/FAIL/확인 불가 판정, 관찰값, 기준, 근거와 소요 시간을 확인한다.
5. 새 보고서를 만들 때는 합성 온도·회수율·상태값을 입력하고 `draft_qc_report`를 선택한다.
6. LLM 서술과 결정론적 표·판정을 조립한 `DRAFT` 및 재감사 상태를 확인한다.
7. 일반 채팅은 기존처럼 선택한 tool 범위의 제한형 agent loop를 사용한다.

## 계약

- `GET /api/playground/tools`
  - `provider_availability` 같은 provider별 정책 정보는 반환하지 않는다.
  - `enabled=true`이고 `execution_type`이 `code` 또는 `llm`인 tool만 UI에서 선택할 수 있다.
- `POST /api/playground/chat`
  - local/OpenAI provider가 같은 tool 계약을 사용한다.
  - 요청에서 선택하지 않은 tool은 실행하지 않는다. 단, `skill-creator`가 활성화된 요청은
    `create_playground_skill`을 자동으로 tool 범위에 추가한다.
  - 알 수 없는 tool ID는 `400 unknown_tool`로 거절한다.
  - 응답은 `assistant_message`, `tool_calls`, `agent_steps`, `active_skill_ids`, `elapsed_ms`, `over_budget`,
    optional `rag_grounding`, `token_usage`를 제공한다.
- `POST /api/playground/attachments`
  - PDF/Markdown 한 건만 허용하고 임의 ID 경로에 로컬 저장한다.
  - 감사 완료 후 UI가 임시 첨부를 삭제한다.
- `POST /api/playground/karyotype-summary`
  - 별도 합성 데이터 토글이나 provider 정책 `403` 없이 선택한 provider로 실행한다.
  - 입력·LLM 설정 오류는 `400`, 길이 오류는 `422`, provider 호출/응답 오류는 `502`로 반환한다.

## 실행 경계

- agent step, tool call 수, timeout, 전체 시간 예산은 항상 제한한다.
- 첨부와 `audit_qc_report`가 함께 선택된 요청은 provider/model 확인보다 먼저 로컬 QC fast path로 실행한다.
- QC fast path는 high-level tool 한 번 안에서 추출과 SOP 대조를 조합하며 1초 시간 예산을 측정·로그한다.
- QC 초안 tool은 측정값·판정·근거를 코드가 소유하고 LLM에는 비수치 서술만 맡긴다.
- 초안은 기존 감사 엔진 재검증을 통과해야 반환하며 추가 최종 합성 LLM을 호출하지 않는다.
- 텍스트형 PDF와 Markdown만 지원하고 이미지형 PDF OCR은 다음 단계로 분리한다.
- 선택 tool이 정확히 `search_rag_chunks`, 선택 skill이 정확히 `rag-grounded-answer`인 요청은 결정용 LLM을 생략하고
  RAG fast path로 실행한다.
- RAG fast path는 cutoff를 통과한 검색 성공 시 근거 합성 LLM 1회만 호출한다. 검색 오류·무결과·cutoff
  미달에서는 추가 LLM을 호출하지 않으며 tool 오류 코드는 최상위 응답의 `error_code`에도 반영한다.
- 일반 agent 경로도 RAG cutoff 미달 뒤 두 번째 판단 LLM을 호출하지 않고 `insufficient_evidence`로 종료한다.
- RAG 합성 입력은 상위 hit마다 본문 예산을 균등 배분한 1800자 이하의 전용 근거로 압축하고 출력은 256 token으로
  제한한다. API router는 thread-safe LLM HTTP client를 재사용하고 종료 시 연결을 닫는다.
- 질의 embedding 기본 endpoint는 `http://127.0.0.1:18080/v1`로 고정한다. 모델은 원격 온프레미스
  Linux의 loopback에만 기동하고 PuTTY/Plink SSH 터널로 연결한다. 로컬 PC에는 모델·서버 바이너리를 설치하지 않는다.
- 근거 문서 표시는 LLM이 작성한 임의 출처가 아니라 실제 검색 hit의 파일명과 섹션/위치를 서버에서 조립한다.
- 같은 tool/인자의 반복 호출은 막는다.
- 내용 인덱싱처럼 무거운 관리자 동작은 Playground 자동 실행 대상이 아니다.
- SMB 원본은 읽기 전용이며, 검색 요청마다 전체 공유폴더를 순회하지 않는다.
- API key와 SMB 자격증명은 환경변수나 현재 브라우저 탭 메모리에서만 받는다.

## LangGraph Studio

Studio는 단일 `.env.studio`와 `langgraph.json` 경로로 `smb_agent`를 수동 실행하는 개발 도구다. Playground 요청을
별도 observer graph로 복제하지 않으며, 평가·run 비교·보존 trace는 MLflow를 단일 기준점으로 사용한다.

## MLflow 문서 챗봇 평가

문서 RAG의 검색·tool 선택·답변 품질과 지연을 반복 비교하기 위한 MLflow 도입은
[MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md](MLFLOW_DOCUMENT_CHATBOT_EVALUATION_PLAN.md)를 최종 개발 계획으로 따른다.
MLflow는 선택 설치하는 오프라인 평가 계층이며, 정상 Playground 요청과 기존 `ChatResponse` 계약에는 의존성을 추가하지 않는다.
1단계 결과 화면은 별도 프론트엔드가 아니라 loopback MLflow UI를 사용한다.

## 완료 기준

- 두 provider에서 동일한 활성 tool 목록을 선택할 수 있다.
- 문서 챗봇 fast path의 정상 요청은 tool 1회와 LLM 1회로 끝나고, 오류·무결과 요청은 tool 1회와 LLM 0회로 끝난다.
- Playground에서 RAG hit를 `문서 근거` 카드로 확인하고, 긴 chunk 본문은 접어서 볼 수 있다.
- 별도 테스트 토글 없이 합성 입력으로 LLM-backed tool을 실행할 수 있다.
- Studio가 단일 실행 스크립트로 시작된다.
- disabled/admin tool, timeout, step/tool-call 상한, 지연 측정은 유지된다.
- `pytest -m "not integration"`와 `ruff check`가 통과한다.

## 2026-07-16 P0 실측

- 변경 전 합성 문서 질의: 11,542.5ms, LLM 2회.
- RAG fast path 적용 후 3회 중앙값: 4,183.8ms, 매 요청 LLM 1회, 모두 10초 예산 이내.
- 검색 단계 실측: embedding 약 88~123ms, DB 약 90~109ms.
- 합성 1-case smoke에서 MLflow trace 1개와 workflow·agent·tool·retriever·LLM span 7개 기록을 확인했다.
