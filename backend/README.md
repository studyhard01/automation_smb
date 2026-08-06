# Backend

FastAPI와 문서 검색·대화·저장소 adapter를 관리하는 Python Backend다.

```text
backend/
  src/smb_finder/  # application package와 Vue production bundle
  tests/           # Backend 단위·계약·회귀 테스트
```

Python project 설정과 lockfile은 저장소 전체 실행·품질 자동화를 위해 루트의 `pyproject.toml`과 `uv.lock`을 사용한다.
개발 서버는 `backend` 폴더에서, 테스트는 저장소 루트에서 실행한다.

```powershell
cd .\backend
.\.venv\Scripts\Activate.ps1
uvicorn main:app --reload --host 127.0.0.1 --port 8010

# 테스트는 저장소 루트에서 실행
cd ..
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -m "not integration" --basetemp .tmp/pytest
```

Frontend 원본은 `frontend/`에서 관리하고 `npm run build` 결과만 `backend/src/smb_finder/web/`에 생성한다.

`src/smb_finder/auth/`는 기존 LLMOps read-only adapter와 분리된 PostgreSQL 인증·사용자·서비스 권한 경계다.
