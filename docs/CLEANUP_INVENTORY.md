# 정리 인벤토리

기준일: 2026-08-04

이 문서는 현재 제품 목표에 필요한 경로와 실행 경로에서 분리됐지만 물리적으로 남은 레거시를 구분한다. 영구 삭제는
복구 비용이 있으므로 사용자의 명시 승인을 받은 뒤 한 번에 수행하고 전체 회귀 검증한다.

## 유지

- `frontend/`: Vue 3 원본, 컴포넌트 테스트, Vite 설정
- `backend/src/smb_finder/api.py`: FastAPI app과 Vue 정적 배포
- `backend/src/smb_finder/config.py`, `models.py`, `intent.py`: 현재 설정·계약·질의 정규화
- `backend/src/smb_finder/llmops_*.py`: PostgreSQL·MinIO·Neo4j read-only adapter
- `backend/src/smb_finder/playground/document_*.py`: 검색·선택·근거 대화 API
- `backend/src/smb_finder/web/`: Vite production build 산출물
- `backend/tests/test_api_contracts.py`, `test_llmops_*.py`, `test_frontend_build.py`, 품질·교훈 테스트
- `docs/PRODUCT_DEVELOPMENT_PLAN.md`, `IMPLEMENTATION_STATUS.md`, `DATASET_DB_INTEGRATION_PLAN.md`,
  `FRONTEND_ARCHITECTURE.md`, `PRODUCTION_ARCHITECTURE.md`, `DEVELOPMENT_SETUP.md`
- `scripts/start_local_stack.ps1`, `start_frontend.ps1`, `evaluate_quality.py`, `check_development_lessons.py`

## 실행 경로에서 분리 완료, 영구 삭제 승인 대기

### Backend 실험·이전 구현

- 직접 SMB/SQLite: `content_*.py`, `extract.py`, `finder.py`, `index*.py`, `jobs.py`, `smb_client.py`
- 이전 RAG·관측: `rag_search.py`, `telemetry.py`, `evaluation/`
- 범용 확장: `mcp_server.py`, `tooling/`
- 별도 업무 데모: `qc_audit/`, `reports/`
- 이전 Playground: `playground/agent.py`, `api.py`, `models.py`, `skills.py`, `tool_ids.py`, `tools.py`,
  `playground/builtin_skills/`

### 이전 테스트·통합·스크립트

- 위 기능만 검증하는 `backend/tests/test_content*.py`, `test_finder.py`, `test_mcp_server.py`, `test_phase*.py`,
  `test_playground.py`, `test_qc*.py`, `test_rag_search.py`, `test_skills.py`, `test_tooling.py`
- `integrations/langgraph/`
- MLflow 평가·등록·review script와 remote embedding tunnel script
- 이전 MLflow 합성 평가 데이터

### 이전 문서·로컬 잔여물

- MCP, MLflow 평가, Tool/Skill, QC·보고서 전용 계획 문서
- `.mcp.json`
- 루트 `dist/`, `.pytest_cache/`, `.ruff_cache/`, `.tmp/`, `.cache/`의 재생성 가능한 로컬 산출물

`.env`는 삭제 대상이 아니며 커밋하지 않는다. 캐시에 외부 package가 남아 있어도 현재 `pyproject.toml`과 `uv.lock`의
dependency에는 포함되지 않는다.

## 삭제 후 확인할 항목

1. `backend/src/smb_finder`에 현재 모듈만 남았는지 확인
2. `pyproject.toml` package discovery와 wheel 내용 확인
3. Backend Ruff·pytest, Frontend typecheck·test·build 재실행
4. 자연어 검색 → 후보 선택 → 선택 문서 채팅 실제 연결 smoke 재실행
5. `scripts/evaluate_quality.py` hard gate와 점수 확인
