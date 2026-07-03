# Langflow 통합 — smb-finder를 노코드 캔버스로 감싸기

[Langflow](https://github.com/langflow-ai/langflow)(오픈소스 노코드 LLM 워크플로)를 **외피**로 두고,
지금까지 만든 `smb_finder` 서비스를 **커스텀 컴포넌트**로 끼워 코딩 없이 워크플로를 구성한다.

```
[Langflow 캔버스]  ──끌어다 연결──▶  [SMB 공유폴더 찾기 컴포넌트]  ──HTTP POST /find──▶  [smb-finder 서비스]  ──인메모리 인덱스──▶  결과
       (노코드)                         (이 폴더의 래퍼)                  (localhost, 사내망)            (이미 구현·테스트됨)
```

`smb_finder` 코드는 건드리지 않는다. 이 폴더가 **서비스를 밖에서 감싸는** 얇은 어댑터다.

## 구성

| 경로 | 역할 |
|---|---|
| `components/smb_finder/smb_folder_finder.py` | **SMB 공유폴더 찾기** — `POST /find`(폴더 이름·경로) |
| `components/smb_finder/smb_content_indexer.py` | **SMB 폴더 내용 DB화** — `POST /admin/content-index-jobs` 생성 + 상태 조회(`관리자 API 토큰` 필요) |
| `components/smb_finder/smb_content_search.py` | **SMB 파일 내용 검색** — `POST /search-content`(파일 본문 검색) |
| `components/smb_finder/smb_workflow_composer.py` | **SMB 워크플로우 자동 생성** — 자연어 요구사항 → Langflow import용 JSON |
| `flow_builder/` | 워크플로우 템플릿 판별·렌더링·Langflow 등록 공용 모듈 (`folder_search`부터 지원) |
| `scripts/generate_flow.py` | CLI에서 워크플로우 JSON 생성, 필요 시 Langflow API에 바로 등록 |
| `docker-compose.yml` | Langflow를 띄우고 `components/`, `flow_builder/`를 마운트 |

내용 검색은 **두 컴포넌트가 짝**이다: 먼저 **DB화**로 원하는 폴더를 인덱싱하고, 그 뒤 **내용 검색**으로 찾는다.

> **DB화는 폴더 단위**다. 공유폴더 전체를 한 번에 돌지 않고, 입력한 **경로 아래만** 인덱싱한다
> (`검사결과/2026/OO검사` 처럼). 여러 폴더를 차례로 DB화하면 누적되고, 같은 폴더를 다시 DB화하면
> 그 폴더만 갱신된다. 경로를 비우면 공유 전체(느림)가 대상이 된다.

## 보안 (CLAUDE.md — 항상 우선)

- 이 컴포넌트는 **사내 localhost의 smb-finder만** 호출한다(기본 `http://localhost:8010`). 폴더 경로·목록을 외부로 보내지 않는다.
- `smb-finder 주소`를 외부 호스트로 바꾸지 말 것. Langflow도 **사내망에서만** 띄운다.
- 같은 캔버스에 **외부 LLM 노드(OpenAI 등)를 두지 말 것** — 환자/검사 데이터가 외부로 나갈 수 있다. LLM이 필요하면 온프레미스 모델만.
- 워크플로우 자동 생성기는 실제 SMB 경로·파일 목록·본문·자격증명을 프롬프트나 flow JSON에 넣지 않는다.
  OpenAI는 `LANGFLOW_COMPOSER_ALLOW_OPENAI=true`와 `OPENAI_API_KEY`가 있을 때만 사용한다.

## 실행

### 1) smb-finder 서비스 먼저 (호스트)
```bash
cd ../..                                   # automation_smb 루트
.venv/Scripts/uvicorn smb_finder.api:app --port 8010
```

### 2) Langflow 띄우기

**Docker (권장):**
```bash
cd integrations/langflow
docker compose up -d
# http://localhost:7860 접속
```
- Compose는 보안상 `127.0.0.1`에만 포트를 연다. 다른 PC에서 접근해야 하면 SSH 터널이나 사내 접근통제 뒤에서 별도 설정한다.
- 컨테이너 안에서는 호스트의 smb-finder를 못 보므로, 캔버스에서 컴포넌트의
  **smb-finder 주소**를 `http://host.docker.internal:8010` 으로 바꾼다.

**또는 로컬 pip 설치 Langflow:**
```bash
# 사내망 SSL이면: pip install ... --trusted-host pypi.org --trusted-host files.pythonhosted.org
pip install langflow
PYTHONPATH="$(pwd)" LANGFLOW_COMPONENTS_PATH="$(pwd)/components" langflow run --port 7860
```
- 이 경우 컴포넌트 주소는 기본값 `http://localhost:8010` 그대로 두면 된다.

### 3) 캔버스에서 사용

**워크플로우 자동 생성 (첫 템플릿: 공유폴더 찾기):**
1. **SMB 워크플로우 자동 생성** 컴포넌트를 캔버스에 놓는다.
2. `만들 워크플로우`에 "검사자가 채팅으로 공유폴더를 찾을 수 있는 workflow를 만들어줘"처럼 입력한다.
3. **Langflow에 등록** 출력을 실행하면 현재 Langflow 서버에 flow를 생성하거나 같은 이름의 기존 flow를 갱신한다.
4. 반환된 `flow_url`을 열어 생성된 캔버스를 확인한다. 등록 API가 인증 문제로 실패하면 `워크플로우 JSON` 출력값을 Import Flow로 가져온다.
5. 생성된 flow의 **SMB 공유폴더 찾기** 컴포넌트에서 `smb-finder 주소`를 Docker 사용 시
   `http://host.docker.internal:8010`으로 맞춘 뒤 실행한다.

원클릭 등록은 Langflow API의 `POST /api/v1/flows/`와 `PATCH /api/v1/flows/{id}`를 사용한다. 인증이 켜진 Langflow라면
실제 token을 컴포넌트 입력에 쓰지 말고 `LANGFLOW_API_KEY` 같은 환경변수로만 주입한다.

CLI로도 같은 JSON을 만들 수 있다:

```bash
cd ../..  # automation_smb 루트
.venv/Scripts/python integrations/langflow/scripts/generate_flow.py \
  --instruction "검사자가 공유폴더를 찾는 workflow를 만들어줘" \
  --out integrations/langflow/flows/folder_search.json \
  --llm-provider rule
```

생성과 등록을 한 번에 하려면:

```bash
.venv/Scripts/python integrations/langflow/scripts/generate_flow.py \
  --instruction "검사자가 공유폴더를 찾는 workflow를 만들어줘" \
  --out integrations/langflow/flows/folder_search.json \
  --llm-provider rule \
  --install \
  --langflow-url http://127.0.0.1:7860
```

`--llm-provider auto`는 `LANGFLOW_COMPOSER_LOCAL_BASE_URL`과 `LANGFLOW_COMPOSER_LOCAL_MODEL`이 있으면
로컬/사내 URL일 때만 LLM을 쓰고, 없거나 외부 URL이면 규칙 기반으로 즉시 생성한다. 생성된 `smb-finder 주소`도
`localhost`, `host.docker.internal`, 사설 IP, 또는 `LANGFLOW_COMPOSER_ALLOWED_HOSTS`에 있는 사내 호스트만 허용한다.
OpenAI를 쓰려면 `--llm-provider openai`와 함께 `LANGFLOW_COMPOSER_ALLOW_OPENAI=true`, `OPENAI_API_KEY`가 필요하다.

Docker에서 LLM API key를 쓸 때도 `docker-compose.yml`에는 key를 넣지 않는다. `docker compose config`가
환경변수를 평문 출력할 수 있으므로, 커밋하지 않는 `docker-compose.override.yml`에서만 주입한다.

```yaml
services:
  langflow:
    environment:
      LANGFLOW_COMPOSER_ALLOW_OPENAI: "true"
      OPENAI_API_KEY: ${OPENAI_API_KEY}
      # 인증이 켜진 Langflow API에 flow를 등록해야 하는 경우:
      # LANGFLOW_API_KEY: ${LANGFLOW_API_KEY}
      # 로컬 LLM에 key가 필요한 경우에만:
      # LANGFLOW_COMPOSER_LOCAL_API_KEY: ${LANGFLOW_COMPOSER_LOCAL_API_KEY}
```

**내용 검색 (DB화 → 검색, 2단계):**
1. **SMB 폴더 내용 DB화** 끌어다 놓기 → `DB화할 폴더 경로`에 `검사결과/2026/OO검사` 입력 → 실행.
   - `관리자 API 토큰`을 Langflow secret/env로 넣어 `/admin/content-index-jobs`를 사용한다. 토큰이 없으면 DB화는 실행하지 않는다.
   - 결과 메시지에 `'…' DB화 완료: N개 파일 적재`가 뜨면 성공이고, 아직 실행 중이면 job id와 현재 상태가 반환된다.
2. **SMB 파일 내용 검색** 끌어다 놓기 → `질의`에 "BRCA1 변이 보고서" → **요약 메시지**를 Chat Output에 연결 → 실행.

**폴더 찾기 (이름·경로):**
- **SMB 공유폴더 찾기** → `질의`에 "OO검사 결과 폴더 찾아줘" → 실행.

> 각 컴포넌트의 입력은 `tool_mode`라 그대로 **에이전트 도구**가 된다. "폴더 찾기 + 내용 검색"을
> 한 에이전트에 물리면 질의 성격에 따라 알아서 고른다. DB화 컴포넌트도 도구로 쓰면 에이전트가
> "이 폴더 인덱싱해줘"에 반응한다.

## 버전

- 검증 기준: Langflow **1.10.1** (`lfx.custom.custom_component.component.Component`, `lfx.io`, `lfx.schema`).
- 더 낮은 버전은 import 경로가 `langflow.custom...`/`langflow.io`일 수 있다 — 그땐 컴포넌트 상단 import만 바꾸면 된다.
