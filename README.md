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

## 구조

| 파일 | 역할 |
|---|---|
| `src/smb_finder/config.py` | `.env` 설정 로더 (SMB·인덱스·LLM·시간예산) |
| `src/smb_finder/models.py` | 입출력 Pydantic 모델 (`FindRequest`/`FindResponse`) |
| `src/smb_finder/smb_client.py` | SMB 세션 + 폴더 트리 순회 (디렉터리만) |
| `src/smb_finder/index.py` | 인메모리 인덱스 + 빠른 검색 + JSON 캐시 |
| `src/smb_finder/indexer.py` | SMB 순회로 인덱스 빌드 (시작/백그라운드) |
| `src/smb_finder/intent.py` | 자연어 → 키워드 (규칙 우선, LLM 선택) |
| `src/smb_finder/finder.py` | 오케스트레이터 (정규화→검색→응답, 시간 측정) |
| `src/smb_finder/api.py` | FastAPI 앱 — `POST /find` (OpenAPI 도구) |
| `integrations/langflow/` | 노코드 외피 — 이 서비스를 Langflow 커스텀 컴포넌트로 감싼다 ([README](integrations/langflow/README.md)) |

## 노코드 외피 (Langflow)

코딩 없이 워크플로를 짜는 외피로 [Langflow](https://github.com/langflow-ai/langflow)를 쓴다.
`smb_finder` 코드는 그대로 두고, `integrations/langflow/`의 커스텀 컴포넌트가 `POST /find`를
**사내 localhost로** 호출해 캔버스에 끌어다 쓸 수 있게 감싼다(외부 전송 없음). 자세한 실행은
[`integrations/langflow/README.md`](integrations/langflow/README.md).

## 설치 · 실행

```bash
uv venv --python 3.11 --native-tls          # 사내망 SSL: --native-tls
uv pip install --native-tls -e ".[dev]"
cp .env.example .env                         # SMB 자격증명 입력 (실제 값은 ../automation/.env)

# 서버 실행
.venv/Scripts/uvicorn smb_finder.api:app --port 8010 --reload

# 폴더 찾기 요청
curl -s -X POST http://localhost:8010/find \
  -H 'Content-Type: application/json' \
  -d '{"query": "OO검사 결과 폴더 찾아줘"}'

# 인덱스 강제 갱신 (관리용)
curl -s -X POST http://localhost:8010/refresh
```

## 테스트 · 린트

```bash
.venv/Scripts/python -m pytest tests/ -m "not integration"   # 라이브 SMB 불필요
.venv/Scripts/python -m ruff check src tests
```

## 다음 단계

- 음성(STT) 입력 추가 — L1, 로컬 모델 (`OS.md` 8장 미확정 항목)
- 탐색 전략 튜닝 — 인덱싱 깊이/갱신 주기, 점수 규칙 개선
- 실측으로 시간 예산(`FIND_BUDGET_MS`) 조정
