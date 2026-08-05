# Docker 배포와 LAN 접속

Vue production bundle과 FastAPI를 하나의 `automation-smb:local` 이미지로 실행한다. PostgreSQL·MinIO·Neo4j와
Ollama는 이미지에 포함하지 않으며, 저장소 루트의 `.env`를 컨테이너 시작 시에만 주입한다. `.env`와 `.env.docker`는
Docker build context와 Git에서 제외된다.

## 사전 조건

- Docker Desktop 또는 Docker Engine + Compose
- 합성 데이터 저장소의 read-only 접속 정보가 있는 `.env`
- 파일 첨부를 사용할 때 SMB 접속 정보와 share 기준 상대 업로드 폴더가 있는 `.env`
- PostgreSQL·MinIO·Neo4j·Ollama가 Docker 호스트에서 접근 가능한 상태
- 기본 공개 포트 `8011`이 비어 있음

현재 서비스는 인증이 없으므로 합성 데이터 기능 검증용 신뢰된 사내 `Private` 네트워크에서만 공개한다. 실제 업무 데이터나
운영 환경에 노출하기 전에는 reverse proxy 인증, 사용자별 ACL, TLS와 감사 정책 승인이 필요하다.

## 빌드와 실행

저장소 루트의 PowerShell에서 실행한다.

```powershell
cd C:\Users\AI_team\Desktop\project\automation_smb
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\docker\prepare_env.ps1
docker compose build
docker compose up -d
docker compose ps
```

`prepare_env.ps1`은 기존 `.env`를 수정하지 않는다. 컨테이너가 사용할 PostgreSQL host 후보만 `.env.docker`의
`LLMOPS_DB_HOST`로 복사하고 실제 값은 출력하지 않는다. 이미 `.env.docker`에 해당 값이 있으면 유지한다. 비밀이 아닌
업로드 상대 경로 설정은 host의 `.runtime/`에 저장되어 container 재시작 뒤에도 유지된다.

기본 주소는 다음과 같다.

- Playground: `http://127.0.0.1:8011/playground`
- OpenAPI: `http://127.0.0.1:8011/docs`
- Liveness: `http://127.0.0.1:8011/health`

8011이 이미 사용 중이면 현재 PowerShell 프로세스에서 host port만 바꾼다.

```powershell
$env:AUTOMATION_SMB_PORT = "8013"
docker compose up -d
```

이 경우 주소의 포트도 `8013`으로 바뀐다. 컨테이너 내부 포트는 계속 `8011`이다.

## 사내 SSL 검사 환경

기본 build는 TLS 인증서를 검증한다. `uv sync`가 `UnknownIssuer`로 실패할 때만 공개 Python wheel host를 해당
PowerShell 프로세스에서 opt-in한다.

```powershell
$env:AUTOMATION_SMB_BUILD_UV_INSECURE_HOST = "files.pythonhosted.org"
docker compose build
```

이 값에는 내부 주소나 자격증명을 넣지 않는다. 최종 runtime image에는 build argument가 환경변수로 남지 않는다.
장기적으로는 Docker daemon이 조직 CA를 신뢰하게 하는 구성이 우선이다.

## 컨테이너 endpoint 규칙

- 이미 LAN 주소인 endpoint는 기존 `.env` 값을 사용한다.
- host process가 `127.0.0.1` 또는 `localhost`에만 떠 있으면 컨테이너에서는 `host.docker.internal`을 사용한다.
- 필요한 예시는 `docker.env.example`에 주석으로만 제공한다.
- 자격증명은 `.env.docker`에 복제하지 않고 기존 `.env`에서 런타임 주입한다.

## 상태와 기능 smoke

`/health`는 process liveness다. 모든 저장소가 실제 검색에 참여하는지는 별도로 확인한다.

```powershell
Invoke-RestMethod http://127.0.0.1:8011/health
Invoke-RestMethod http://127.0.0.1:8011/api/playground/stores/status

Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8011/api/playground/files/search `
  -ContentType "application/json" `
  -Body '{"query":"합성 WBS 찾아줘","limit":5}'
```

성공 기준은 PostgreSQL·MinIO·Neo4j가 모두 `connected=true`이고, 검색 응답의 `queried_stores`에 세 저장소가 모두
포함되며 `llm_expanded=true`인 것이다. Neo4j graph-space 모드에서 논리 DB 지정이 무시되면 연결은 성공하더라도
예상된 degraded warning이 표시될 수 있다.

## 파일 첨부 설정

파일 첨부 쓰기는 기본적으로 꺼져 있다. 승인된 합성 테스트 share에 한해 `.env` 또는 무시되는 `.env.docker`에서
`SMB_UPLOAD_ENABLED=true`로 명시적으로 활성화한다. host·share·계정·비밀번호는 runtime env로만 주입하고, 화면에서는
share 내부의 상대 업로드 경로만 변경할 수 있다. 전체 UNC, drive 경로와 `..`는 허용하지 않는다.

```powershell
Invoke-RestMethod http://127.0.0.1:8011/api/playground/settings
```

응답에는 내부 주소와 자격증명이 없으며 `upload.enabled`와 `upload.configured`로 사용 가능 여부만 확인한다. 실제 합성 파일
첨부 smoke는 설정 화면의 `파일 첨부` 버튼으로 수행한다. 성공해도 `indexed=false`이므로 별도 ingestion 전에는 검색 결과에
표시되지 않는다. 동일 이름 파일은 덮어쓰지 않고 거부한다.

## 다른 로컬 컴퓨터에서 접속

Docker 호스트의 LAN IPv4를 확인한 뒤 같은 네트워크의 다른 PC에서 다음 주소를 연다.

```powershell
Get-NetIPConfiguration | Where-Object IPv4DefaultGateway | Select-Object -ExpandProperty IPv4Address
```

`http://<Docker-host-LAN-IPv4>:8011/playground`

Windows 방화벽이 차단하면 관리자 PowerShell에서 아래처럼 `Private` profile과 `LocalSubnet`으로만 제한해 허용한다.
실제 운영 포트가 8013이면 두 명령의 `8011`을 `8013`으로 바꾼다.

```powershell
New-NetFirewallRule `
  -DisplayName "automation_smb Docker 8011" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8011 `
  -Profile Private -RemoteAddress LocalSubnet
```

규칙 제거:

```powershell
Remove-NetFirewallRule -DisplayName "automation_smb Docker 8011"
```

방화벽 변경은 호스트 관리자 승인 대상이다. 공용 네트워크 profile이나 전체 원격 주소로 범위를 넓히지 않는다.

## 로그·중지·재시작

```powershell
docker compose logs --tail 100 automation-smb
docker compose restart automation-smb
docker compose down
```

실제 endpoint, 자격증명, 문서 경로·본문을 로그나 이슈에 복사하지 않는다.
