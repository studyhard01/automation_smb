# automation_smb — 공유 폴더 찾기 서비스

텍스트(이후 음성) 명령으로 **온프레미스 SMB 공유폴더를 즉시 찾아주는** 서비스.
노코딩 자동화 툴(→ [`OS.md`](./OS.md))의 첫 성공 케이스이자 L4 도구다.
작업 규칙·보안·지연 요구는 [`CLAUDE.md`](./CLAUDE.md) 참고.

## 핵심 설계 — 지연 최소화

매 요청마다 SMB를 도는 대신, **폴더 트리를 미리 인덱싱(메모리+JSON 캐시)** 하고 **인메모리로 검색**한다.
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

> ⚠️ **보안**: 내용 인덱스(`.cache/content.fts.db`)에는 환자/검사 **본문**이 들어간다. 원본 공유폴더와
> 동급의 민감 데이터다 — 로컬에만 두고(`.cache/` 는 `.gitignore`), 외부 전송·커밋 금지.

## API 계약 · 관측

`POST /find`와 `POST /search-content` 응답은 결과 목록과 함께 `result_count`, `elapsed_ms`,
`over_budget`을 반환한다. `POST /refresh`, `POST /refresh-content`, `GET /health`도 명시적인
Pydantic 응답 모델을 사용해 OpenAPI 도구 계약을 고정한다.

- `/health`: `ready`, `indexed_folders`, `indexed_files`, 인덱스 로드 여부를 반환한다.
- `/refresh`: 폴더 인덱스 갱신 후 폴더 수와 소요 시간을 반환한다.
- `/refresh-content`: 내용 인덱싱 통계와 소요 시간을 반환하되, SMB host/share 실제 값은 노출하지 않는다.
- `/admin/content-index-jobs`: 내용 인덱싱을 백그라운드 job으로 실행한다. `X-Admin-Token` 필요.

운영 로그에는 원문 질의, SMB 경로, 파일 본문 `snippet`, 내부 host/share 값을 남기지 않는다. 지연 초과 로그는
질의 길이, 결과 수, 소요 시간처럼 민감도가 낮은 값만 기록한다.

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
| `src/smb_finder/api.py` | FastAPI 앱 — `POST /find`·`/search-content`·`/refresh*` (OpenAPI 도구) |
| `src/smb_finder/playground/` | 자체 챗봇 Playground — 선택한 tool만 호출하는 local LLM 기반 채팅 API |
| `src/smb_finder/web/` | `/playground` 정적 UI — tool 선택, 채팅, Tool Lab 초안 화면 |
| `integrations/langflow/` | 노코드 외피 — Langflow 컴포넌트 + `folder_search` 워크플로우 자동 생성기 ([README](integrations/langflow/README.md)) |
| `integrations/langgraph/` | LangGraph 외피 — 같은 HTTP 호출을 LangGraph Studio(로컬)로 관리·디버깅 ([README](integrations/langgraph/README.md)) |

## 노코드 외피 (Langflow)

코딩 없이 워크플로를 짜는 외피로 [Langflow](https://github.com/langflow-ai/langflow)를 쓴다.
`smb_finder` 코드는 그대로 두고, `integrations/langflow/`의 커스텀 컴포넌트가 `POST /find`를
**사내 localhost로** 호출해 캔버스에 끌어다 쓸 수 있게 감싼다(외부 전송 없음). 자세한 실행은
[`integrations/langflow/README.md`](integrations/langflow/README.md).

첫 자동 생성 템플릿은 `folder_search`다. 자연어 요구사항을 입력하면 기존 `SMBFolderFinder`
컴포넌트를 재사용하는 Langflow flow를 생성하고, Langflow API에 바로 등록할 수 있다. 기본은
규칙/로컬 LLM이며, OpenAI는 명시 설정이 있을 때만 사용한다.

## 자체 챗봇 Playground

Langflow 없이 `smb_finder` 안에서 바로 쓰는 챗봇/tool UI를 제공한다.

- 화면: `GET /playground`
- tool 목록: `GET /api/playground/tools`
- 채팅 실행: `POST /api/playground/chat`
- Tool Lab 초안: `POST /api/playground/tool-draft`

첫 버전은 local/on-prem OpenAI 호환 LLM만 사용한다. `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`,
`LLM_TIMEOUT_MS` 환경변수를 사용하며, SMB tool 결과를 외부 OpenAI로 재전송하지 않는다. LLM 모델이 비어 있으면
UI는 tool을 실행하지 않고 설정 필요 메시지를 반환한다.

## 설치 · 실행

```bash
uv venv --python 3.11 --native-tls          # 사내망 SSL: --native-tls
uv pip install --native-tls -e ".[dev]"
cp .env.example .env                         # SMB 자격증명 입력 (실제 값은 ../automation/.env)

# 서버 실행
.venv/Scripts/uvicorn smb_finder.api:app --port 8010 --reload

# 폴더 찾기 요청 (이름·경로)
curl -s -X POST http://localhost:8010/find \
  -H 'Content-Type: application/json' \
  -d '{"query": "OO검사 결과 폴더 찾아줘"}'

# 내용 찾기 요청 (파일 본문)
curl -s -X POST http://localhost:8010/search-content \
  -H 'Content-Type: application/json' \
  -d '{"query": "BRCA1 변이 보고서"}'

# 폴더 인덱스 갱신 (이름 검색용)
curl -s -X POST http://localhost:8010/refresh

# 내용 DB화 — 원하는 폴더만 인덱싱 (path 지정, 관리자 토큰 필요)
curl -s -X POST http://localhost:8010/refresh-content \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>' \
  -d '{"path": "검사결과/2026/OO검사"}'

# 운영 권장: 관리자용 백그라운드 job으로 내용 DB화
curl -s -X POST http://localhost:8010/admin/content-index-jobs \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: <ADMIN_API_TOKEN>' \
  -d '{"path": "검사결과/2026/OO검사"}'

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
.venv/Scripts/python -m ruff check src tests integrations/langflow
```

## 다음 단계

- **의미 검색(벡터) — 같은 SQLite에 추가**: 현재 키워드(FTS5) 위에, 표현이 달라도 의미가 가까운
  검색을 위해 **로컬 임베딩 + 벡터**를 얹어 하이브리드(RRF)로 융합. 외부 전송 금지 원칙상 임베딩은
  온프레미스 로컬 모델로. 실제 필요성 확인 후 진행.
- `hwp`/`hwpx` 본문 추출 추가 (진단검사실에 흔함 — OLE/zip 파서, 선택 의존성)
- 음성(STT) 입력 추가 — L1, 로컬 모델 (`OS.md` 8장 미확정 항목)
- 실측으로 시간 예산(`FIND_BUDGET_MS`/`CONTENT_SEARCH_BUDGET_MS`) 조정
