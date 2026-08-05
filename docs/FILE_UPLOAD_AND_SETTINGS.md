# 파일 첨부와 설정 기능

## 사용자 흐름

왼쪽 패널은 `자연어 파일 검색 → 파일 첨부 → 검색 결과 → 대화 참고 파일 → 설정` 순서로 구성한다.

- 파일 선택·해제는 검색 결과와 대화 범위만 바꾸며 회색 성공 문구를 추가하지 않는다.
- `파일 첨부`는 Browser의 로컬 파일 선택창을 열어 한 파일을 설정된 SMB 하위 폴더에 저장한다.
- 왼쪽 하단 `설정`은 업로드 상대 경로와 PostgreSQL·MinIO·Neo4j·로컬 LLM 상태를 표시한다.
- 설정 화면은 SMB host·share·계정·비밀번호를 표시하거나 변경하지 않는다.

## 공개 API

| 목적 | Endpoint | 핵심 계약 |
|---|---|---|
| 설정 조회 | `GET /api/playground/settings` | 비밀·내부 주소 없이 업로드 정책과 로컬 LLM 구성 여부 반환 |
| 상대 경로 저장 | `PATCH /api/playground/settings/upload` | `relative_directory` 하나만 검증·저장 |
| 파일 첨부 | `POST /api/playground/files/upload` | multipart `file`, 성공 HTTP 201, `indexed=false` |

오류는 `detail.code`와 사용자용 `detail.message`로 반환한다. traversal·절대/UNC/drive 경로는 422, 금지 확장자는 415,
동일 이름은 409, 초과 크기는 413, 저장소 미설정·연결 실패는 503으로 처리한다.

## 설정과 저장 경계

SMB 접속 정보는 runtime env에만 둔다. canonical 키는 다음과 같다.

```text
SMB_UPLOAD_ENABLED
SMB_HOST
SMB_SHARE_NAME
SMB_USERNAME
SMB_PASSWORD
SMB_UPLOAD_DEFAULT_RELATIVE_DIRECTORY
SMB_UPLOAD_RUNTIME_SETTINGS_PATH
SMB_UPLOAD_MAX_SIZE_BYTES
SMB_UPLOAD_ALLOWED_EXTENSIONS
SMB_UPLOAD_TIMEOUT_MS
SMB_UPLOAD_MAX_CONCURRENCY
```

- 쓰기는 `SMB_UPLOAD_ENABLED=true`일 때만 허용한다.
- 설정 화면이 저장하는 값은 고정 share 아래의 상대 경로 하나다.
- 상대 설정은 `.runtime/playground_settings.json`에 원자적으로 저장하고 Docker writable volume으로 유지한다.
- root filesystem은 read-only이고 runtime user는 UID/GID 10001이다.
- 파일명, 제어문자, Windows 예약명, 확장자, 실제 스트림 byte 크기를 검증한다.
- 고유 partial을 `xb`로 만든 뒤 비덮어쓰기 rename으로 확정한다. 실패하면 서비스가 만든 partial만 정리한다.
- PostgreSQL·MinIO·Neo4지는 계속 read-only다.

## 검색 반영 경계

파일 첨부 성공은 공유폴더 저장 성공만 뜻한다. 응답의 `indexed=false`처럼 문서 변환, PostgreSQL/MinIO/Neo4j 적재와
버전 관계 생성은 이번 기능에 포함하지 않는다. 첨부 파일을 검색 후보로 만들려면 기존 ingestion 파이프라인과 연결하는
후속 작업이 필요하다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_upload_api.py backend/tests/test_container_packaging.py backend/tests/test_llmops_api_contracts.py
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
.\.venv\Scripts\python.exe scripts\evaluate_quality.py
```

현재 단위·계약 테스트는 traversal, 위험 파일명, 금지 확장자, 실제 byte 크기 제한, 명시적 gate, 비덮어쓰기 경합,
runtime 설정 migration과 비밀 비노출을 검증한다. 실제 SMB 쓰기 smoke는 공유폴더에 테스트 파일을 남기지 않기 위해
수행하지 않았으며, 승인된 합성 파일로 화면에서 확인한다.

## 운영 전 승인 항목

현재 LAN Playground에는 사용자 인증이 없다. 실제 업무 운영 전에 reverse proxy/SSO, 사용자별 쓰기 권한, 감사 로그,
악성 파일 검사, 파일 보존·삭제 정책과 자동 ingestion 책임을 승인해야 한다.
