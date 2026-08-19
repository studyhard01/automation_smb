# 문서 안내

이 디렉터리는 현재 구현을 인수인계하고 운영 준비를 이어가기 위한 문서만 유지한다. 프로젝트를 처음 보는 사람은
아래 순서대로 읽으면 된다.

## 15분 인수인계 순서

1. [루트 README](../README.md) — 서비스 목적, 사용자 기능, 실행 방법
2. [구현 현황](IMPLEMENTATION_STATUS.md) — 완료 기능, 공개 API, 미완료 과제
3. [개발 환경](DEVELOPMENT_SETUP.md) — Python·Node 설치와 로컬 실행
4. [운영 아키텍처](PRODUCTION_ARCHITECTURE.md) — 온프레미스 구성과 안전 경계

## 기능 계약

| 문서 | 다루는 내용 |
|---|---|
| [데이터 저장소 연동](DATASET_DB_INTEGRATION_PLAN.md) | PostgreSQL·MinIO·Neo4j의 역할과 ID·조회 계약 |
| [Frontend 구조](FRONTEND_ARCHITECTURE.md) | Vue 화면, 컴포넌트, Backend 연결 |
| [파일 첨부와 설정](FILE_UPLOAD_AND_SETTINGS.md) | SMB 신규 저장과 runtime 설정 |
| [기안 초안](PROPOSAL_DRAFT.md) | 근거 선별, 생성·검증, 질문, XLSX, 수정 |
| [인증과 사용자 관리](AUTHENTICATION_AND_USER_MANAGEMENT.md) | 로컬·SeeLIS 로그인, 세션, 역할과 서비스 권한 |

## 검증과 운영

| 문서 | 다루는 내용 |
|---|---|
| [기안 평가](PROPOSAL_EVALUATION.md) | 합성 dataset과 offline/live 평가 실행 |
| [프로젝트 품질 기준](PROJECT_QUALITY_RUBRIC.md) | 정적 검사, hard gate, 품질 점수 |
| [Docker 배포](DOCKER_DEPLOYMENT.md) | 이미지 빌드, LAN 실행, smoke |
| [개발 교훈](DEVELOPMENT_LESSONS.md) | 반복해서 적용할 문제 해결 원칙 |
| [제품 개발 계획](PRODUCT_DEVELOPMENT_PLAN.md) | 현재 우선순위와 승인 Gate |

## 문서 유지 원칙

- 현재 동작은 `IMPLEMENTATION_STATUS.md`, 앞으로의 일은 `PRODUCT_DEVELOPMENT_PLAN.md`에만 기록한다.
- 상세 코드 계약은 기능 문서 한 곳에서만 설명하고 다른 문서는 링크한다.
- 완료된 과거 실험·설문·삭제된 기능의 계획 문서는 유지하지 않는다.
- 실제 주소·자격증명·SMB 경로·의료 식별자는 문서에 기록하지 않는다.
- 코드와 API가 바뀌면 같은 변경에서 README, 구현 현황, 관련 기능 문서를 함께 갱신한다.
