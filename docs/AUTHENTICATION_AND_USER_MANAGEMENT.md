# 인증과 사용자 관리

기준일: 2026-08-19

## 목적과 현재 범위

인증 데이터는 문서 저장소와 분리된 PostgreSQL `auth` schema에 저장한다. 현재 구현은 합성 데이터 기능 검증을 위한
로컬 계정, 선택적 SeeLIS 로그인, cookie session, 사용자·서비스 권한 관리 화면을 제공한다.

인증 정보가 있다고 해서 모든 Playground 업무 API가 보호되는 것은 아니다. principal과 서비스 권한을 검색·대화·기안
API에 강제하는 작업은 운영 전 후속 과제다.

## 사용자 흐름

```text
/register ─> 로컬 계정 신청 ─> 기본 비활성 사용자
/login    ─> 로컬 또는 SeeLIS 인증 ─> HttpOnly session cookie
/user     ─> 관리자 사용자 조회·역할·서비스 권한 변경
```

- 로컬 로그인은 저장된 password hash를 검증한다.
- SeeLIS 로그인은 서버가 token API와 Keycloak userinfo를 순서대로 호출하고 외부 subject를 로컬 사용자와 연결한다.
- Frontend는 외부 token을 받거나 저장하지 않는다.
- 로그아웃은 현재 session을 폐기하고 cookie를 만료한다.

## 역할과 권한

| 필드 | 의미 |
|---|---|
| `system_role` | `user` 또는 `admin` |
| `is_superuser` | 최고 관리자 보호와 전체 관리 권한 |
| `is_active` | 로그인 가능 여부 |
| `all_services_access` | 모든 등록 서비스 접근 |
| `service_keys` | 명시적으로 허용된 서비스 목록 |

관리 API는 관리자 session을 요구한다. 최고 관리자 보호 규칙과 알 수 없는 서비스 key 거부는 Backend에서 적용한다.

## 공개 API

| Method | Endpoint | 목적 |
|---|---|---|
| POST | `/api/auth/register` | 로컬 사용자 등록 |
| POST | `/api/auth/login` | 로컬 로그인 |
| POST | `/api/auth/seelis-login` | 선택적 SeeLIS 로그인 |
| GET | `/api/auth/me` | 현재 session 사용자 |
| POST | `/api/auth/logout` | session 종료 |
| GET | `/api/users` | 사용자·서비스 목록 |
| PATCH | `/api/users/{user_id}` | 역할·활성·서비스 권한 변경 |

오류 응답은 내부 DB·외부 인증 주소와 자격증명을 노출하지 않는다. SeeLIS 요청은 client별 시도 제한과 단계별 timeout을
사용한다.

## 저장 구조

| Table | 책임 |
|---|---|
| `auth.users` | 로컬·SeeLIS 사용자, 역할, 활성 상태 |
| `auth.sessions` | hash된 session token과 만료 시각 |
| `auth.services` | 접근 제어 대상 서비스 catalog |
| `auth.user_service_permissions` | 사용자별 서비스 허용 관계 |

서비스 시작 시 필요한 table과 기본 서비스 row를 idempotent하게 준비한다. 인증 DB 장애는 인증 화면만 degraded 처리하며
기존 문서 Playground process 기동을 막지 않는다.

## 설정 경계

- 인증 DB는 `AUTH_DB_*` 설정을 우선 사용하고 문서 DB와 계정·schema를 분리할 수 있다.
- session cookie는 HttpOnly이며 TTL과 secure 옵션을 환경으로 정한다.
- 초기 관리자 비밀번호와 SeeLIS endpoint·header 값은 `.env`에만 둔다.
- 설정값과 외부 token을 로그·문서·응답에 기록하지 않는다.

## 구현 위치

- `backend/src/smb_finder/auth/api.py` — HTTP router와 session cookie
- `backend/src/smb_finder/auth/service.py` — 인증·권한 use case
- `backend/src/smb_finder/auth/store.py` — PostgreSQL schema와 query
- `backend/src/smb_finder/auth/seelis.py` — 외부 인증 adapter
- `frontend/src/components/LoginScreen.vue` — 로컬·SeeLIS 로그인
- `frontend/src/components/UserManagementScreen.vue` — 사용자 관리

## 검증

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$PWD\backend\.venv"
uv run --no-sync pytest backend/tests/test_auth.py backend/tests/test_seelis_auth.py -q
npm.cmd --prefix frontend run test
```

실제 SeeLIS smoke는 외부로 전달되는 값과 대상 endpoint를 사용자가 승인한 환경에서만 수행한다.

## 운영 전 필수 과제

1. 모든 Playground 업무 API에 session principal과 서비스 권한을 강제한다.
2. 문서별 ACL과 감사 주체를 문서 저장소 계약에 연결한다.
3. session 폐기·계정 비활성화·관리자 변경을 운영 감사 로그에 남긴다.
