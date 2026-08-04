# Backend

FastAPI와 문서 검색·대화·저장소 adapter를 관리하는 Python Backend다.

```text
backend/
  src/smb_finder/  # application package와 Vue production bundle
  tests/           # Backend 단위·계약·회귀 테스트
```

Python project 설정과 lockfile은 저장소 전체 실행·품질 자동화를 위해 루트의 `pyproject.toml`과 `uv.lock`을 사용한다.
명령은 저장소 루트에서 실행한다.

```powershell
uv run --no-sync uvicorn smb_finder.api:app --host 127.0.0.1 --port 8010 --reload --reload-dir backend/src
uv run --no-sync pytest backend/tests -m "not integration" --basetemp .tmp/pytest
```

Frontend 원본은 `frontend/`에서 관리하고 `npm run build` 결과만 `backend/src/smb_finder/web/`에 생성한다.
