# Frontend/Backend 개발 환경 설정

이 문서는 새 개발자가 `automation_smb`를 합성 데이터 전용 개발 모드로 준비하는 절차를 역할별로 정리한다.
개발 머신은 Windows 11과 PowerShell을 기준으로 하며, Linux/macOS에서는 아래에 표시한 명령만 바꿔 실행한다.

> 실제 SMB 주소·자격증명·의료데이터는 이 절차에 필요하지 않다. `.env`와 `.cache/`는 Git에 포함하지 않으며,
> 실제 공유폴더 연결은 별도로 승인된 Backend 작업에서만 읽기 전용으로 사용한다.

## 역할별로 무엇을 준비하나

| 역할 | 주 작업 위치 | 필수 런타임 | 기본 실행 대상 |
|---|---|---|---|
| Frontend | `src/smb_finder/web/` | Python 3.11, `uv` | FastAPI가 제공하는 `/playground` |
| Backend | `src/smb_finder/`, `tests/`, `scripts/` | Python 3.11, `uv` | FastAPI API와 Python 테스트 |
| 공통 | `README.md`, `docs/`, API 계약 | Git, Python 3.11, `uv` | Playground 프로필 |

Frontend는 별도 Node 프로젝트가 아니다. 현재 UI는 `index.html`, CSS, 순수 JavaScript를 FastAPI가 정적으로 제공하므로
`npm install`이나 프론트엔드 빌드 과정이 없다. Node.js는 JavaScript 문법을 빠르게 검사할 때만 선택적으로 사용한다.

## 공통 준비

### 1. 도구 확인

저장소를 내려받은 뒤 루트에서 실행한다.

```powershell
git --version
uv --version
uv python find 3.11
```

Python 3.11이 아직 없다면 다음 `uv sync`가 호환 Python을 준비할 수 있다. 사내 SSL 검사 프록시 환경을 고려해
의존성 설치에는 `--native-tls`를 사용한다.

```powershell
uv sync --python 3.11 --native-tls --extra dev
```

이 명령은 저장소의 `uv.lock`을 기준으로 `.venv`를 만들고 Backend 런타임, `pytest`, `ruff`를 함께 설치한다.

### 2. 합성 테스트용 환경변수 준비

```powershell
Copy-Item .env.example .env
```

복사한 `.env`에서 다음 원칙을 지킨다.

- `SMB_HOST`, `SMB_SHARE_NAME`, `SMB_USERNAME`, `SMB_PASSWORD`는 비워 둔다.
- 일반 UI/API 개발에서는 `RAG_DB_ENABLED=false`로 바꿔 PostgreSQL과 임베딩 서버 의존성을 끈다.
- `LLM_INTENT_ENABLED=false`, `MCP_ENABLED=false`를 유지한다.
- 실제 외부 LLM을 시험하지 않으면 `OPENAI_API_KEY`도 비워 둔다.
- 합성 개발 중 자동 SMB 재탐색을 피하려면 `SMB_INDEX_REFRESH_SEC=0`으로 바꾼다.

SMB 설정이 비어 있으면 서버는 공유폴더 순회를 건너뛰고 빈 폴더 인덱스로 시작한다. 이 상태에서도 Playground UI,
OpenAPI, 합성 QC 도구와 단위 테스트를 개발할 수 있다.

### 3. Windows 캐시·임시 폴더 문제를 피하는 선택 설정

회사 PC의 사용자 공용 캐시 권한 때문에 `uv` 또는 `pytest`가 실패하면 현재 저장소 아래의 Git 제외 경로를 사용한다.
같은 PowerShell 창에서 다음 명령을 먼저 실행한다.

```powershell
$env:UV_CACHE_DIR = Join-Path $PWD ".cache\uv"
New-Item -ItemType Directory -Force $env:UV_CACHE_DIR, ".tmp\pytest" | Out-Null
```

이후 테스트에 `--basetemp .tmp/pytest`를 붙인다. 정상 환경에서는 이 선택 설정을 생략해도 된다.

## Frontend 환경

### 설치와 실행

Frontend도 정적 파일을 제공하는 최소 Backend가 필요하므로 공통 `uv sync`를 한 번 실행해야 한다. 별도
`npm install`은 실행하지 않는다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Start -Profile Playground
```

준비 상태를 확인한다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Status -Profile Playground
Invoke-RestMethod http://127.0.0.1:8010/health
```

브라우저에서 다음 주소를 연다.

- Playground UI: `http://127.0.0.1:8010/playground`
- API 문서: `http://127.0.0.1:8010/docs`
- 정적 자산: `http://127.0.0.1:8010/playground/assets/`

실행 로그는 `.cache/local-stack/logs/`에 남는다. 작업을 마치면 이 스크립트가 시작한 프로세스만 중지한다.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_stack.ps1 `
  -Action Stop -Profile Playground
```

### 주요 파일과 검증

| 파일 | 역할 |
|---|---|
| `src/smb_finder/web/index.html` | Playground 화면 구조 |
| `src/smb_finder/web/assets/playground.css` | 스타일과 반응형 레이아웃 |
| `src/smb_finder/web/assets/playground.js` | API 호출, 화면 상태와 이벤트 처리 |
| `tests/test_playground.py` | Playground API와 정적 화면 회귀 테스트 |
| `tests/test_api_contracts.py` | Backend 응답 계약 회귀 테스트 |

Node.js가 설치된 경우 JavaScript 문법을 먼저 검사할 수 있다.

```powershell
node --check .\src\smb_finder\web\assets\playground.js
uv run --no-sync pytest .\tests\test_playground.py .\tests\test_api_contracts.py `
  --basetemp .tmp/pytest
```

Node.js가 없다면 `node --check`만 생략한다. UI를 변경했으면 최소한 다음을 직접 확인한다.

- `/playground`가 오류 없이 열리고 브라우저 개발자 도구에 JavaScript 오류가 없는가
- tool 분류와 선택 상태, 첨부·채팅·설정 컨트롤이 의도대로 표시되는가
- 좁은 화면에서 주요 컨트롤이 잘리거나 겹치지 않는가
- 실패한 API 요청의 오류 메시지가 화면에 표시되는가

## Backend 환경

### 기본 설치와 실행

```powershell
uv sync --python 3.11 --native-tls --extra dev
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010 --reload
```

다른 터미널에서 외부 서비스나 실제 SMB가 필요 없는 안전한 smoke test를 실행한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8010/health

$body = @{ query = "demo project alpha 폴더 찾아줘" } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8010/find `
  -ContentType "application/json" `
  -Body $body
```

SMB 설정과 캐시가 모두 비어 있으면 `/find` 결과가 0건인 것은 정상이다. 중요한 확인점은 서버가 준비 상태가 되고
요청이 timeout 없이 구조화된 응답과 `elapsed_ms`를 반환하는 것이다.

### 테스트와 린트

```powershell
uv run --no-sync pytest .\tests -m "not integration" --basetemp .tmp/pytest
uv run --no-sync ruff check src tests integrations/langgraph scripts
uv run --no-sync python scripts/check_development_lessons.py
uv run --no-sync python scripts/evaluate_quality.py
```

`integration` 마커 테스트는 실제 SMB 또는 별도 LLM 서비스가 필요한 경우가 있으므로 기본 검증에서 제외한다.
기능을 바꾼 뒤에는 마지막 품질 평가까지 실행하고 총점, hard gate, 합성 evidence freshness와 미달 항목을 확인한다.

### 선택 기능

필요한 작업에 해당하는 의존성만 추가한다.

```powershell
# MLflow 합성 평가
uv sync --python 3.11 --native-tls --extra dev --extra evaluation

# LangGraph Studio
uv sync --python 3.11 --native-tls --extra dev --extra studio
```

| 기능 | 추가 준비 | 로컬 주소 |
|---|---|---|
| 기본 API/Playground | 없음 | `http://127.0.0.1:8010` |
| MLflow 평가 UI | `evaluation` extra 또는 로컬 스택 `Mlflow` 프로필 | `http://127.0.0.1:5000` |
| LangGraph Studio | `studio` extra와 `Studio` 프로필 | `http://127.0.0.1:2024` |
| PostgreSQL RAG | 승인된 로컬 DB와 embedding endpoint | DB `5432`, 기본 tunnel `18080` |
| MCP | `.env`에서 token과 allowlist를 명시하고 `MCP_ENABLED=true` | `http://127.0.0.1:8010/mcp` |

선택 기능의 외부 endpoint, 키 또는 실제 데이터 전송이 필요하면 무엇을 전송하는지 먼저 확인한 뒤 사용한다.

### 실제 SMB 연결이 승인된 경우

합성 기능 개발에는 이 단계가 필요 없다. 승인된 Backend 통합 작업에서만 `.env`의 `SMB_*` 값을 로컬로 채운다.

- `.env`를 메신저, 문서, 이슈, 로그, 커밋에 넣지 않는다.
- 공유폴더는 읽기 전용으로 탐색하고 파일을 쓰거나 이동·삭제하지 않는다.
- 먼저 host, share name, 자격증명, TCP 445 방화벽과 도메인 인증을 확인한다.
- 전체 실시간 순회 대신 캐시 인덱스를 사용하고 제한된 범위에서만 갱신한다.
- 실제 파일 내용·경로·목록을 외부 LLM이나 외부 저장소로 보내지 않는다.

## Frontend와 Backend가 함께 지킬 계약

- API 필드나 오류 형식을 바꾸면 Pydantic 모델, OpenAPI, `tests/test_api_contracts.py`와 UI 처리를 함께 갱신한다.
- UI는 등록된 tool 목록 API를 기준으로 표시하며, Backend에 없는 tool 계약을 프론트에서 임의로 확정하지 않는다.
- API 요청에는 지연 측정값을 유지하고, 검색 경로에 요청당 SMB 전체 순회나 동기 LLM 호출을 추가하지 않는다.
- 합성 fixture는 실제 환자·검사·보고서 형식과 닮지 않은 일반 문구로 작성한다.
- 신규 모듈이나 주요 문서를 추가하면 메인 `README.md`의 구조 표 또는 관련 섹션을 갱신한다.

## Linux/macOS 명령 차이

개발 절차는 같고 환경 파일 복사와 가상환경 경로만 다르다.

```bash
cp .env.example .env
export UV_CACHE_DIR="$PWD/.cache/uv"
mkdir -p "$UV_CACHE_DIR" .tmp/pytest
uv sync --python 3.11 --native-tls --extra dev
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010 --reload
uv run --no-sync pytest tests -m "not integration" --basetemp .tmp/pytest
```

PowerShell 전용 `scripts/start_local_stack.ps1` 대신 위 `uvicorn` 명령으로 기본 API/Playground를 실행한다.

## 자주 발생하는 문제

| 증상 | 확인할 내용 |
|---|---|
| `invalid peer certificate: UnknownIssuer` | `uv` 명령에 `--native-tls`가 있는지 확인 |
| `uv` cache permission 오류 | `UV_CACHE_DIR`를 저장소의 `.cache/uv`로 지정 |
| `pytest` temp permission 오류 | `--basetemp .tmp/pytest` 사용 |
| `8010` 포트 충돌 | 로컬 스택 `Status`와 `Stop`을 실행한 뒤 다시 시작 |
| RAG tool 연결 실패 | RAG 작업이 아니면 `.env`에서 `RAG_DB_ENABLED=false` |
| Playground가 열리지 않음 | `/health`, `.cache/local-stack/logs/`, 브라우저 콘솔 순서로 확인 |
| SMB 연결 실패 | 승인된 설정인지 확인한 뒤 host/share/자격증명/445 포트/도메인 인증 점검 |

프로젝트의 현재 기능 범위와 저장소 경계는 [`AGENTS.md`](../AGENTS.md), 전체 구조와 실행 프로필은
[`README.md`](../README.md), 반복 가능한 개발 교훈은 [`DEVELOPMENT_LESSONS.md`](DEVELOPMENT_LESSONS.md)를 따른다.
