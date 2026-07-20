# LangGraph Studio 합성 챗봇 테스트

LangGraph Studio는 `smb_finder`의 agent와 tool 호출을 수동으로 실행·디버깅하는 로컬 개발 도구다. 운영 관측이나
평가 저장소가 아니며 필요할 때만 실행한다. 반복 평가, run 비교, trace의 기준점은 MLflow다.

```text
[Studio / Agent Server]
          │
          ▼
 [smb_agent graph] ── [local OpenAI-compatible LLM]
          │ HTTP
          ▼
 [smb-finder :8010] ── /find · /search-content · /admin/content-index-jobs
```

## 구성

| 경로 | 역할 |
|---|---|
| `smb_agent/graph.py` | `create_react_agent` 기반 챗봇 graph |
| `smb_agent/tools.py` | `find_folder`·`search_content`·`refresh_content` HTTP tool |
| `smb_agent/config.py` | `.env.studio` 기반 LLM·smb-finder·시간 예산 설정 |
| `langgraph.json` | `smb_agent` graph와 `.env.studio`를 등록하는 CLI 진입점 |
| `.env.example` | 합성 테스트 설정 예시 |
| `start_local_studio.ps1` | `.env.studio` 생성 후 로컬 Agent Server 실행 |

## 실행

먼저 루트 API와 사용할 local LLM을 실행한다.

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010
```

다른 PowerShell에서 Studio Agent Server를 실행한다.

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb\integrations\langgraph
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_local_studio.ps1
```

최초 실행 때 `.env.example`을 `.env.studio`로 복사한다. 로컬 환경에 맞게 다음 값만 바꾼다.

```dotenv
SMB_FINDER_URL=http://localhost:8010
LLM_BASE_URL=http://localhost:8080/v1
LLM_MODEL=local-model
LLM_API_KEY=local-no-key
TOOL_TIMEOUT_MS=1500
```

Agent Server 주소는 `http://127.0.0.1:2024`다. Studio에서 `smb_agent`를 선택하고 일반 합성 문장으로
`find_folder`와 `search_content` 호출 흐름을 확인한다.

## 지연과 실행 경계

- `TOOL_TIMEOUT_MS` 기본값은 1500ms다.
- 검색 결과는 `FIND_LIMIT`, `CONTENT_LIMIT` 상위 N건만 반환한다.
- 검색 요청마다 SMB 전체를 순회하지 않고 이미 구축한 인덱스를 사용한다.
- `refresh_content`는 관리자 토큰이 있을 때만 API job을 만들며 SMB 원본 파일을 수정하지 않는다.
- 단순 검색 성능은 agent LLM 왕복과 분리해 `/find`와 `/search-content`에서 측정한다.
- Studio의 in-memory 실행 기록은 개발 서버 재시작 시 사라진다. 보존·비교가 필요한 trace는 MLflow 평가 runner로 남긴다.
