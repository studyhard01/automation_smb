# Docker 실행

루트 `.env`는 build context나 image에 복사하지 않고 Compose가 컨테이너 시작 시 process 환경으로만 주입한다.
기존 `.env`의 주소를 바꾸지 않는다. 컨테이너 안에서만 다른 endpoint가 필요한 경우 `docker.env.example`을
`.env.docker`로 복사하고 필요한 항목만 주석 해제한다. Compose는 `.env` 다음에 선택적인 `.env.docker`를 읽으므로
두 번째 파일에 적은 값만 컨테이너 process 환경에서 덮어쓴다.

## 기본 실행

저장소 루트에서 실행한다.

```powershell
& .\scripts\docker\prepare_env.ps1
docker compose build
docker compose up -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8011/health
```

준비 스크립트는 `.env`의 `LLMOPS_DB_HOST`, `DEV_SERVER`, `POSTGRES_HOST` 순서로 PostgreSQL host 후보를 고르고
컨테이너 전용 `.env.docker`에 `LLMOPS_DB_HOST`만 기록한다. 주소 값은 출력하지 않으며 `.env.docker`는 Git과 image
build context에서 제외된다. 이미 `.env.docker`에 명시적인 `LLMOPS_DB_HOST`가 있으면 그 값을 유지한다.

화면은 `http://127.0.0.1:8011/playground`, OpenAPI는 `http://127.0.0.1:8011/docs`다.

중지할 때는 다음 명령을 사용한다.

```powershell
docker compose down
```

## 사내 SSL 검사 환경에서 build

기본 build는 TLS 인증서를 검증한다. 사내 SSL 검사 때문에 `uv sync`가 `UnknownIssuer`로 실패하는 경우에만 공개
Python package host를 현재 PowerShell process에서 opt-in하고 다시 build한다.

```powershell
$env:AUTOMATION_SMB_BUILD_UV_INSECURE_HOST = "files.pythonhosted.org"
docker compose build
```

이 값은 package 다운로드에만 사용하고 최종 runtime 환경에는 남기지 않는다. 내부 주소나 자격증명을 build argument에
넣지 않는다. 조직 CA를 image에 복사하지 않는다.

## 컨테이너 전용 endpoint override

기존 `.env`가 이미 LAN endpoint를 사용한다면 `.env.docker`가 필요 없다. `.env`의 endpoint가
`127.0.0.1` 또는 `localhost`라서 컨테이너에서 host process를 가리켜야 할 때만 다음처럼 만든다.

```powershell
Copy-Item .\docker.env.example .\.env.docker
# .env.docker에서 필요한 endpoint만 주석 해제한다.
docker compose up -d --build
```

DB host override는 host 이름만 적으며 port·계정·DB 이름은 기존 `.env` 값을 그대로 사용한다. URL/URI endpoint는
scheme과 port를 포함한다. `.env.docker`는 기존 `.gitignore`의 `.env.*` 규칙으로 제외되며 image build context에도
포함되지 않는다. 실제 자격증명이나 내부 주소를 template 또는 image build argument에 넣지 않는다.

Healthcheck는 `/health`의 HTTP 응답으로 process liveness만 확인한다. Adapter 연결 상태와 `ready` 값은 health 응답과
별도 smoke test에서 확인한다.
