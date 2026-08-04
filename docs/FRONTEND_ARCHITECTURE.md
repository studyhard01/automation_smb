# Vue Frontend 아키텍처

- 적용일: 2026-08-04
- 상태: Vue 3 전환, 최신 Figma 3열 UI, 실제 LLMOps 저장소 흐름 적용 완료
- 범위: 합성 데이터 전용 Playground UI

## 결정

Frontend는 Vue 3, Vite, TypeScript로 구현하고 FastAPI Backend와 dependency·개발 프로세스를 분리한다. Backend의
Pydantic/OpenAPI 계약은 유지하며 Frontend는 같은 origin의 `/api/playground/*` endpoint만 호출한다.

Nuxt, Vue Router와 Pinia는 현재 사용하지 않는다. 단일 Playground 화면이므로 Vue Composition API의 상위 상태와
명시적 props/emits로 충분하다. 화면 route나 전역 domain store가 실제로 둘 이상 필요해질 때만 추가한다.

## 디렉터리 경계

```text
frontend/
  package.json             # Vue/Vite/TypeScript dependency와 script
  package-lock.json        # 재현 가능한 npm dependency
  vite.config.ts           # /playground base, /api proxy, production outDir
  src/
    App.vue                # 화면 상태와 API orchestration
    api/client.ts          # typed fetch와 안전한 오류 변환
    components/            # 파일, 대화, 문서 기능, 저장소 상태 UI
    types.ts               # Backend 공개 응답에 대응하는 TypeScript type
    app.css                # 공통·반응형 스타일

backend/src/smb_finder/
  api.py                   # FastAPI와 정적 bundle 제공
  playground/document_*    # 선택 문서 채팅 API·서비스·계약
  web/                     # Vite production build 결과. 직접 수정 금지
```

Python package는 `backend/src/`, Backend 회귀 테스트는 `backend/tests/`에 둔다. 루트 `pyproject.toml`과 `uv.lock`은
package discovery와 전체 저장소 품질 자동화를 소유하므로 명령은 저장소 루트에서 실행한다.

## 실행 경계

### 개발

```text
브라우저 :5173
  → Vite HMR
  → /api proxy
  → FastAPI :8011
```

두 프로세스를 별도 터미널에서 실행한다.

```powershell
uv run --no-sync uvicorn smb_finder.api:app --app-dir backend/src --host 127.0.0.1 --port 8011 --reload --reload-dir backend/src
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_frontend.ps1 -Action Dev
```

Backend가 기본 `8011`이 아닌 포트에서 실행되면 Frontend 실행 전에 process 환경변수
`VITE_BACKEND_URL`을 해당 로컬 주소로 지정한다. 이 값은 DB 설정이나 자격증명이 아니며 Vite proxy target으로만 쓴다.

### 통합·운영

```text
npm run build
  → backend/src/smb_finder/web/index.html + hashed assets
  → FastAPI /playground에서 같은 origin으로 제공
```

별도 Frontend CDN이나 외부 hosting은 사용하지 않는다. 온프레미스 배포 artifact를 만들기 전에 Frontend build를 먼저
실행해야 한다.

## 컴포넌트 책임

| 컴포넌트 | 책임 |
|---|---|
| `App.vue` | 공통 상태, API 호출 orchestration, 선택 파일·대화 계약 연결 |
| `AppHeader.vue` | 서비스와 저장소 준비 상태 |
| `FileSidebar.vue` | 자연어 파일 검색, DB 후보 선택·해제, 결과별 버전 확인 |
| `ChatWorkspace.vue` | 중앙 선택 문서 Q&A, 검색 후보 카드, 메시지 전송, Citation 표시 |
| `FeatureSidebar.vue` | 문서 요약·보고서 초안과 저장소 연결 상태 |
| `FileInspectorDialog.vue` | MinIO Preview/Canonical과 Neo4j 버전 관계 조회 |

검색 결과는 안정적인 `doc_id`·`revision_id`와 함께 `selectedFiles` 상태에 들어가며, 중앙 채팅 요청의
`selected_files`에 그대로 포함된다. 선택 파일이 하나 이상이면 모든 중앙 대화는 `document_qa` mode로 보내며 Backend가
활성 Revision과 범위를 다시 검증한다. UI는 검색 DB `503`, stale revision `409`, Preview `404/413`, Graph degraded를
서로 다른 상태로 표시하고 임시 파일·가짜 다운로드를 만들지 않는다.

## Figma 화면 계약

기준 노드는 Figma Playground 시작 화면(`79:721`)이다. 1440×960 desktop에서 헤더는 71px이고, 본문은
`288px / minmax(0, 816px) / 336px` 3열이다. 중앙 패널은 헤더·스크롤 메시지·하단 Composer의 세 구획으로 나누며,
페이지 높이가 늘어나도 Composer는 중앙 패널 하단에 유지한다.

- 왼쪽: 자연어 파일 검색, DB 검색 후보, 후보별 버전 확인, 대화 참고 파일, 연결 상태
- 중앙: 선택 범위, 근거 기반 대화, Citation, Composer
- 오른쪽: 문서 요약·보고서 초안, 실행 안내와 실제 결과 상태, 접을 수 있는 저장소 상태
- 1180px 이하: 오른쪽 패널을 다음 행으로 이동
- 1024px 이하: 왼쪽 248px·중앙 가변 폭으로 축소
- 760px 이하: 단일 열로 전환하고 각 패널을 문서 흐름에 배치

Figma에 표현된 파일 직접 첨부, 대화 이력, 관리자 메뉴는 현재 제품 경계에서 제외한다. 화면 유사도를 위해 가짜 버튼이나
rule-based 결과를 만들지 않고, 실제 API가 제공하는 검색·선택·대화·저장소 상태만 배치한다.

## 보안·데이터 경계

- Browser에는 DB 자격증명, 내부 Object URI, 실제 파일 경로를 전달하지 않는다.
- 외부 LLM provider와 Browser API key 입력은 제공하지 않는다.
- 파일 첨부·Tool·Skill 편집 상태는 현재 제품 경계에서 제외한다.
- 실제 공유폴더·의료자료의 외부 LLM 전송은 이 Frontend 전환에 포함되지 않는다.
- Vite production build에는 `.env`나 DB 설정을 포함하지 않는다.

## 검증 계약

```powershell
npm.cmd --prefix .\frontend run typecheck
npm.cmd --prefix .\frontend run test
npm.cmd --prefix .\frontend run build
uv run --no-sync python scripts/evaluate_quality.py
```

Frontend 단위 테스트는 자연어 파일 검색 event, 결과 선택, 결과별 버전 확인, 기능 2개 구성, 중앙 선택 범위,
`document_qa` 요청, Citation/검색 요약, Preview/Graph dialog를 고정한다. Backend 회귀 테스트는 Vite bundle 존재와
`selected_files` API 계약을 함께 확인한다.

## 다음 단계

1. Pydantic OpenAPI에서 TypeScript type을 생성해 수동 계약 중복을 줄인다.
2. 컴포넌트 상태가 여러 route에서 공유될 때만 Pinia와 Vue Router 도입을 재평가한다.
3. 좁은 화면의 좌·우 패널 전환과 키보드 focus 회귀 테스트를 자동화한다.
4. 긴 Citation 본문의 접기·펼치기와 문서 표 미리보기 렌더링을 고도화한다.
