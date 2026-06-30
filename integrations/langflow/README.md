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
| `components/smb_finder/smb_folder_finder.py` | Langflow 커스텀 컴포넌트 — `POST /find`를 호출해 표/메시지로 반환 |
| `docker-compose.yml` | Langflow를 띄우고 `components/`를 마운트(`LANGFLOW_COMPONENTS_PATH`) |

컴포넌트 출력 2개:
- **폴더 목록(DataFrame)** — `path/name/score/depth` 표. 시각 워크플로·표시용.
- **요약 메시지(Message)** — 사람이 읽는 텍스트. 챗봇/에이전트 도구용. (입력 `query`는 `tool_mode`라 에이전트 도구로도 노출됨)

## 보안 (CLAUDE.md — 항상 우선)

- 이 컴포넌트는 **사내 localhost의 smb-finder만** 호출한다(기본 `http://localhost:8010`). 폴더 경로·목록을 외부로 보내지 않는다.
- `smb-finder 주소`를 외부 호스트로 바꾸지 말 것. Langflow도 **사내망에서만** 띄운다.
- 같은 캔버스에 **외부 LLM 노드(OpenAI 등)를 두지 말 것** — 환자/검사 데이터가 외부로 나갈 수 있다. LLM이 필요하면 온프레미스 모델만.

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
- 컨테이너 안에서는 호스트의 smb-finder를 못 보므로, 캔버스에서 컴포넌트의
  **smb-finder 주소**를 `http://host.docker.internal:8010` 으로 바꾼다.

**또는 로컬 pip 설치 Langflow:**
```bash
# 사내망 SSL이면: pip install ... --trusted-host pypi.org --trusted-host files.pythonhosted.org
pip install langflow
LANGFLOW_COMPONENTS_PATH="$(pwd)/components" langflow run --port 7860
```
- 이 경우 컴포넌트 주소는 기본값 `http://localhost:8010` 그대로 두면 된다.

### 3) 캔버스에서 사용
1. 좌측 컴포넌트 목록 **smb_finder** 카테고리 → **SMB 공유폴더 찾기** 끌어다 놓기.
2. `질의`에 "OO검사 결과 폴더 찾아줘" 입력(또는 Chat Input 연결).
3. **요약 메시지** 출력을 Chat Output에, 또는 **폴더 목록**을 다음 노드에 연결.
4. 실행 → smb-finder가 인메모리 인덱스로 즉시 응답.

## 버전

- 검증 기준: Langflow **1.10.1** (`lfx.custom.custom_component.component.Component`, `lfx.io`, `lfx.schema`).
- 더 낮은 버전은 import 경로가 `langflow.custom...`/`langflow.io`일 수 있다 — 그땐 컴포넌트 상단 import만 바꾸면 된다.
